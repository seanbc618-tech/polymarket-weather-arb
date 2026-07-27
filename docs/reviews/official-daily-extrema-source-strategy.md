# Official Daily Extrema Source Strategy

## Executive Summary

当前实现已通过本地日窗口（`_local_day_bounds` + ZoneInfo）解决了 UTC 日偏移问题（noaa.py:217-241 及相关 helper），并在 raw_payload 中保留了完整的样本列表（'observations'）、query/local 边界和 warnings，为审计提供了良好基础。但核心取值逻辑仍为：

```python
# noaa.py:250-257
if variable == 'temperature_high':
    ... = max(observations, key=lambda item: item[0])
else:
    ... = min(...)
```

这与 NWS 官方 daily high/low（通常基于 5-min moving average 的 local midnight-to-midnight max，或 published daily summary）存在系统性差异（详见 settlement-observation-audit.md High Risk #2）。

**明确建议（MVP 下一步）**：**继续使用 sample max/min + 强化 warning / provenance / preview 人工确认（Phase 1）**，暂不切换到完整 daily summary provider。理由：本地日对齐已就位，sample 路径延迟低、实现简单、无额外认证；官方 daily source（NCEI daily-summaries / GHCNd / LCD）更权威但引入延迟、不同 endpoint、站点 ID 映射、以及认证/访问方式验证成本，且当前 calibration 场景对“精确官方值”的边际收益有限。双轨（sample 主 + optional cross-check）可作为 Phase 2。

这与 nws-local-day-query-window-audit.md 和 parser-local-day-implementation-audit.md 中“避免过度工程化”和“保留 forensic trail”的精神一致。

## Current Implementation

**数据获取与选择（noaa.py:209-312）**：
- station = _resolve_station(...)（来自 mapping）。
- target_date 来自 rule.window_start。
- tz_name = _resolve_timezone(station)（mapping 中已为主要站点提供 IANA tz，如 KNYC: America/New_York）。
- 调用 `_local_day_bounds(target_date, tz_name)` 计算本地日 00:00–23:59:59（使用 datetime.combine + time.min/time.max + ZoneInfo），转换为 UTC ISO Z 字符串作为 query_start/query_end。
- 调 `https://api.weather.gov/stations/{station}/observations?start=...&end=...`。
- 遍历 features，提取每个 observation 的 temperature.value（非空）、unit、timestamp、qualityControl，收集为 observations 列表。
- 对 temperature_high 取 max(value)，temperature_low 取 min(value)。
- 归一化单位后构造 WeatherObservation。
- **raw_payload 关键可审计字段**（当前状态，已比早期版本大幅改进）：
  - `target_date`, `timezone`, `local_start`, `local_end`, `query_start`, `query_end`
  - `observation_count`, `observations`（完整 compact 列表：每个 sample 的 timestamp/value/unit/quality_status）
  - `selected_observation`（被选中的那个）
  - `warnings`（来自 local-day 的 "station timezone unknown..." + 覆盖率 <12 + QC 非 V/unknown）
  - 其他：source, station, source_grade 等。

**调用方（settlement_service.py:52-98）**：
- preview_market / backfill_market 均捕获 `warnings = tuple(raw_payload.get('warnings') or [])`，并放入 SettlementPreviewResult / SettlementBackfillResult。
- CLI（settlement-backfill --preview / 正常 backfill）会打印 warnings。
- Dashboard calibration preview 也会展示 warnings。
- 仍使用 `_matches_rule(observation.value, rule)` 决定 yes/no 并 settle signals。
- 保留完整 raw_payload 落库（repositories.save_observation）。

**domain/weather.py**：仅 WeatherObservation dataclass，无 extrema_method 等字段。

**mapping**：主要 US 站点均有 "timezone"（例如 KPHX 用 America/Phoenix）。

当前实现已满足本地日对齐，但 extrema 仍为“样本中的极值”，未利用任何 API 可能提供的 24h/daily 字段，也未与官方 daily summary 交叉验证。

## Official Source Options

基于 NWS API 文档（weather.gov/documentation/services-web-api）和 NCEI 官方资料（ncei.noaa.gov）：

1. **NWS /stations/{stationId}/observations**（当前使用）
   - 每个 observation 有 temperature.value（瞬时或短周期样本，通常 5-60 分钟频次）。
   - 文档明确指出：“Station observations endpoints always show missing (null) 24h max/min temperatures for stations outside the central time zone due to MADIS ingest bug。”
   - 偶尔有 24h max/min，但不可靠、常为 null。不是官方 daily high/low 的稳定来源。
   - 优点：实时、低延迟、按 station+时间范围查询简单、无 key。
   - 缺点：取样方法（max of returned values）≠ 官方 5-min moving average max（local 日历日）；覆盖可能不完整（夜间样本少会影响 high）。

2. **NCEI Daily Summaries / GHCNd (Global Historical Climatology Network - Daily)**
   - 官方集成 daily climate summaries，提供 TMAX（daily maximum temperature）、TMIN。
   - 来源包括 US 站点自动/人工观测，经过质量控制。
   - 通过 NCEI data access（daily-summaries endpoint、CDO Web Services 或 bulk）可按 station + date 查询，但访问方式需要实测：CDO Web Services 官方文档要求 token；Access Services / bulk 路径是否满足本项目的 station/date 查询和频率需求要单独验证。
   - 官方性高：这是气候学中标准的 daily max/min。
   - 延迟：通常有一定滞后（非实时）；历史数据丰富。
   - 数据公开免费；访问复杂度取决于入口，CDO Web Services 需要 token，Access Services / bulk 路径需要实测。
   - 适合自动回填：是，但需新 adapter、处理不同 schema、确认延迟是否可接受 settlement timeline。

3. **Local Climatological Data (LCD / LCDv2)**
   - NCEI 产品，包含 hourly + daily summaries。
   - Daily Summary 明确有 Temperature - Max / Min（whole °F），有 * 标记极端。
   - 衍生自 GHCN 等，针对主要机场/城市站点。
   - 文档（LCD_documentation.pdf）确认每日有明确的 max/min。
   - 优点：更接近“published daily value”；有 monthly 上下文。
   - 缺点：覆盖不如 GHCNd 广；可能仍是月度/总结产品，实时性差于 observations。

4. **其他（Global Hourly / ISD / GSOD）**：
   - Global Hourly (GHCNh)：小时数据，可自己聚合 daily max/min，但复杂度高，且 UTC vs local 问题重现。
   - GSOD：24h summaries（常 UTC midnight），与 local 日历日不完全一致。
   - 不推荐作为首要来源（与 ASOS 5-min local day 定义不符）。

**比较总结（MVP 维度）**：
- **官方性**：GHCNd/LCD daily summaries > NWS observations samples。
- **免费/认证复杂度**：NWS observations 无 key；NCEI 数据公开，但 CDO Web Services 需要 token，Access Services / bulk 路径可能更轻，需要下一步实测确认。
- **按 station + date 查询**：observations 最简单；daily summaries 支持但 endpoint/schema 不同。
- **数据延迟**：observations 最佳（最近几天）；daily summaries 有滞后。
- **实现复杂度**：当前 sample 路径最低；引入 daily 需要新 provider 类、映射、fallback 逻辑、payload 变更。
- **适合自动回填**：daily summaries 更权威，但对 calibration（而非实时交易）场景，sample + 人工 preview 已够用，尤其有完整 observations list 可复核。

## Recommended MVP Path

**Phase 1（立即，最小变更，保持只读审计精神）**：强化 sample max/min 路径的 warning + provenance + 人工确认。
- 在 noaa.py fetch_observation 的 raw_payload 和 warnings 中明确记录：
  - `extrema_method`: "max_of_observations_samples_in_local_window" (或 "min_of...")
  - `extrema_source`: "nws_observations_api"
  - `note`: "This is the maximum/minimum among returned observation samples for the local station day. It may differ from the official NWS daily high/low (typically based on 5-minute moving averages or published LCD/GHCNd daily summary). Full observations list is provided for review."
- 确保 CLI preview/backfill 和 dashboard calibration 始终高亮这些 warnings。
- 在 SettlementPreviewResult / BackfillResult 中已有的 warnings 机制上扩展（无需结构变更）。
- 更新 mapping usage_notes 提及当前 extrema 策略。
- 这直接回应了之前所有 audit 中“sample ≠ official”的 High Risk，同时保留 forensic trail。

**Phase 2（可选，后续小切片）**：引入 daily summary 作为 cross-check 或备选 provider。
- 新建轻量 DailySummaryProvider（或扩展 ObservationProvider 协议支持 optional daily_extrema）。
- 首选 NCEI daily-summaries / GHCNd（按 station+date 查询 TMAX/TMIN）。
- 先做 API probe：比较 NCEI Access Services、CDO Web Services、bulk daily summaries 对 KNYC/KLAX/KORD 的 station/date 查询可用性、认证要求、延迟和单位。
- 在 fetch_observation 后（或独立方法）可选获取 official 值，与 sample 值对比，生成 discrepancy_warning 放入 warnings/raw_payload。
- 仅在 preview 中默认展示对比；backfill 仍默认用 sample（或加策略开关）。
- 保持 preview 永不落库，backfill 仍需 tradable rule。

**Phase 3（远期，风险高时再考虑）**：自动化 confidence 规则（仅当 discrepancy 小 + coverage 高 + QC=V 时自动接受，否则强制 preview）。

优先 Phase 1。避免现在就重写整个 obs 路径或依赖新数据源的延迟/覆盖问题。

## Proposed Data Model / Payload Changes

在当前 raw_payload 基础上（最小增量）建议增加（向后兼容）：

```json
{
  "extrema_method": "max_of_observations_samples_in_local_window",
  "extrema_source": "nws_observations_api",
  "sample_count": 42,
  "sample_extrema_value": "86.0",
  "official_daily_value": null,  // Phase 2 填充
  "discrepancy_warning": "Sample max 86.0F may differ from official NWS daily high (5-min avg or LCD summary). Review 'observations' list and local window.",
  "warnings": [ ..., "low observation coverage: 8 usable records", "extrema_method is sample-based, not official daily summary" ]
}
```

在 WeatherObservation 或结果对象中可考虑（未来）增加可选 `extrema_method` 字段，但当前 warnings + raw_payload 字段已足够 forensic。

## Tests To Add

（仅建议，不修改任何测试文件）

- `test_noaa_fetch_observation_extrema_method_and_warning_in_raw_payload`：验证 raw_payload 包含 extrema_method / note 说明 sample vs official。
- `test_noaa_fetch_observation_discrepancy_warning_when_sample_vs_potential_official`（用 mock 模拟返回有 24h max 的 payload，对比）。
- `test_settlement_preview_and_backfill_surface_extrema_warnings`：CLI preview/backfill 和结果对象中 warnings 包含 extrema 相关条目。
- `test_noaa_fetch_observation_full_observations_list_allows_reproduction_of_sample_max`：用真实样本数据验证用列表中的 max 可重现 selected_value。
- 边界场景：低覆盖率 + 夜间样本少时 high 的 warning；未知 tz fallback 仍带 extrema_method warning。
- 与之前 local-day 测试结合：确保即使 query window 是正确的 local 日，extrema 警告仍出现。

## Do Not Change / Safety Invariants

- preview_market 永远不写库（仅返回结果 + warnings）。
- backfill_market 必须经过 tradable rule 验证 + _validate_rule（threshold/operator/unit 存在）。
- 不放松任何 live trading gate（source_grade、reconciliation 等）。
- 必须保留完整的 raw_payload forensic trail（'observations' 列表、local/query 边界、所有 warnings、target_date、timezone 等），以便后续人工或工具复核 sample vs official。
- 不自动重写历史已 settled 的 model_signals（任何 source 变更都只能影响新 backfill）。
- 继续支持无 tz 的 fallback + 明确 warning。
- 保持 observations provider 独立于 forecast 路径。

## Open Questions

- NWS observations payload 中 24h max/min 字段在 central time zone 外的实际可用性和稳定性如何？（需针对具体 settlement 市场用真实 KNYC/KLAX 等站点的近期调用验证；文档已知有 MADIS bug）。
- Polymarket 具体 weather 市场的 resolution source text 是否明确要求 “official published daily high from NWS LCD/CLI” 还是接受 “max from station observations”？
- NCEI daily-summaries / GHCNd 对于主要 Polymarket 相关 US 站点的更新延迟典型是多少？是否适合 calibration 回填的时间窗口？
- 对本项目最合适的 NCEI 入口到底是 Access Services、CDO Web Services（token）、还是 bulk daily summaries？
- 是否需要在 mapping 中增加 “preferred_extrema_source” 字段以支持未来双轨？
- 官方 daily summary 的单位和 rounding（whole °F）与当前 sample + normalize 逻辑的交互是否会引入新 rounding 差异？

## Official References To Verify Before Phase 2

- NWS API documentation: https://www.weather.gov/documentation/services-web-api
- NCEI CDO Web Services documentation: https://www.ncei.noaa.gov/cdo-web/webservices/v2
- NCEI Access Services / data endpoint examples should be tested directly for `daily-summaries` before implementation.

This document is a strategy note. It should be paired with a real API probe before any Phase 2 adapter is implemented.
