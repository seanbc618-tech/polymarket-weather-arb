# 竞品代码审计：suislanchez/polymarket-kalshi-weather-bot

审计日期：2026-06-03

竞品仓库：[suislanchez/polymarket-kalshi-weather-bot](https://github.com/suislanchez/polymarket-kalshi-weather-bot)

审计 commit：`e406394d59c208cc035c4fdf37ebb26636e15a47` (`2026-03-01 Add dashboard screenshot to README, update project structure`)

本文档用于决定本项目 `polymarket-weather-arb` 是否继续维护、fork 竞品、还是吸收竞品局部能力。

## 1. 结论先行

竞品项目在“天气信号研究层”明显比我们当前项目更有启发价值，尤其是：

- Open-Meteo 31-member GFS ensemble。
- Ensemble member count 直接映射市场概率。
- NWS observed temperature 用于 weather settlement。
- Kalshi KXHIGH ticker parser。
- React dashboard 对信号、forecast、edge、calibration 的展示。
- 研究文档中已经识别 NBM、ECMWF、HRRR、NWS 的真实角色。

但它不是一个可以直接替换我们的生产级 Polymarket live trading 系统，原因是：

- 它的“执行交易”主要是往本地 `Trade` 表写 simulation/paper trade，不是真实 CLOB 下单。
- 没有我们已经建立的 compliance/geoblock readiness。
- 没有 fresh reconciliation 作为 live gate。
- 没有 live market whitelist + strategy override 组合门。
- 没有真实 open orders / fills / positions 对账链。
- 没有把非 settlement-grade source 阻止在 live 外。
- 没有看到测试目录或自动化测试体系。

推荐策略：

**不要直接废弃本项目，也不要直接迁移到竞品。应该把竞品当成天气研究层样板，把它的 ensemble weather model、NWS settlement 思路、Kalshi/Polymarket market parser 和 dashboard 可视化思想移植进我们项目；同时保留我们的 CLI、SQLite 审计链、live gates、micro-live 风控和 HK readiness。**

一句话：

**竞品更像“聪明的信号实验室”，我们更像“笨但安全的交易控制系统”。下一步应该让我们的安全系统接上更聪明的天气信号。**

## 2. 审计范围

本次 clone 到临时目录：

```text
/private/tmp/polymarket-kalshi-weather-bot
```

重点查看文件：

- `README.md`
- `ARCHITECTURE.md`
- `RESEARCH.md`
- `VALIDATED_RESEARCH.md`
- `backend/data/weather.py`
- `backend/core/weather_signals.py`
- `backend/data/weather_markets.py`
- `backend/data/kalshi_markets.py`
- `backend/core/settlement.py`
- `backend/core/scheduler.py`
- `backend/models/database.py`
- `backend/api/main.py`
- `frontend/src/components/WeatherPanel.tsx`
- `frontend/src/components/SignalsTable.tsx`
- `frontend/src/types.ts`

本项目对照文件：

- `src/polymarket_weather_arb/domain/probability.py`
- `src/polymarket_weather_arb/domain/pricing.py`
- `src/polymarket_weather_arb/domain/risk.py`
- `src/polymarket_weather_arb/adapters/weather/open_meteo.py`
- `src/polymarket_weather_arb/adapters/weather/noaa.py`
- `src/polymarket_weather_arb/services/trading_service.py`
- `src/polymarket_weather_arb/services/live_readiness_service.py`
- `src/polymarket_weather_arb/services/live_launchpad_service.py`
- `src/polymarket_weather_arb/services/reconciliation_service.py`
- `src/polymarket_weather_arb/modules/registry.py`

## 3. 竞品项目概览

竞品是一个 Python FastAPI + React TypeScript 的 prediction market bot。

它包含两条策略：

1. BTC 5-minute Up/Down microstructure。
2. Weather temperature markets on Kalshi + Polymarket。

后端：

- FastAPI。
- SQLAlchemy。
- SQLite。
- APScheduler。
- Async HTTP clients。

前端：

- React 18。
- TypeScript。
- Vite。
- TanStack Query。
- Tailwind。
- Framer Motion。

它的 dashboard 更像专业交易台，展示：

- BTC microstructure。
- Weather forecasts。
- Signals table。
- Trades table。
- Equity curve。
- Calibration。
- Terminal/event log。

和我们的 dashboard 不同：

- 竞品 dashboard 是现代前端应用，视觉和交互更强。
- 我们 dashboard 是 stdlib HTTP server，轻、可部署、依赖少，但 UI 能力有限。

## 4. 竞品天气模型

### 4.1 Open-Meteo Ensemble

文件：`backend/data/weather.py`

竞品定义了 `CITY_CONFIG`：

- `nyc` -> KNYC / OKX gridpoint
- `chicago` -> KORD / LOT gridpoint
- `miami` -> KMIA / MFL gridpoint
- `los_angeles` -> KLAX / LOX gridpoint
- `denver` -> KDEN / BOU gridpoint

它通过 Open-Meteo Ensemble API 请求：

- `temperature_2m_max`
- `temperature_2m_min`
- `temperature_unit=fahrenheit`
- `models=gfs_seamless`

然后收集：

- control member。
- `temperature_2m_max_member01` 到 member30。
- 对 low 同样处理。

核心概率：

- `probability_high_above(threshold_f)`：高温超过阈值的 ensemble member 占比。
- `probability_high_below(threshold_f)`：1 - above。
- `probability_low_above(threshold_f)`。
- `probability_low_below(threshold_f)`。

它还计算：

- mean high。
- std high。
- mean low。
- std low。
- ensemble member count。
- ensemble agreement。

和我们当前模型相比：

- 我们 `domain/probability.py` 是一个基于单点 forecast 和默认 uncertainty 的保守区间模型。
- 竞品是直接用 ensemble members 做经验分布。
- 对天气温度市场，竞品模型更自然、更可解释。

### 4.2 Weather Signal

文件：`backend/core/weather_signals.py`

竞品针对每个 `WeatherMarket`：

1. 拉 ensemble forecast。
2. 根据市场 metric 和 direction 计算 YES probability。
3. 把极端概率 clipped 到 0.05 到 0.95。
4. `edge = model_probability - market_probability`。
5. 若 entry price 超过 `WEATHER_MAX_ENTRY_PRICE`，edge 置 0。
6. confidence = ensemble agreement。
7. suggested size = fractional Kelly。
8. 保存 signal，用于 calibration。

强项：

- 概率来自 ensemble，简单直接。
- reasoning 文案对人类很友好。
- 保存 signal 可以做校准。

弱项：

- edge 是单点值，不像我们现在用 fair lower/upper 做保守区间。
- Kelly sizing 对真实 live 可能过激。
- 对 source grade/live eligibility 没有严格分层。

### 4.3 NWS Observations

文件：`backend/data/weather.py`

函数：`fetch_nws_observed_temperature`

它使用：

```text
https://api.weather.gov/stations/{station}/observations
```

按目标日期拉取 station observations，然后取：

- high = max observed temps。
- low = min observed temps。

这对我们很重要，因为：

- Weather market 的结算不是看 forecast，而是看 official observation。
- 我们目前的 NOAA provider 更偏 forecast，而且直接把 NWS forecast 标成 `settlement_grade`，这个命名可能会让人误会。

建议：

- 我们应区分 `forecast_source_grade` 和 `settlement_observation_source_grade`。
- 对 live gate 来说，forecast 可以是 official forecast，但 settlement grade 最好保留给 observation/settlement source。

## 5. 竞品 Market Parser

### 5.1 Polymarket Weather Parser

文件：`backend/data/weather_markets.py`

它通过 Gamma events 查 weather/temperature markets，然后解析标题。

可解析：

- city aliases。
- threshold in Fahrenheit。
- high/low。
- above/below。
- date。
- yes/no price。
- volume。
- platform。

优点：

- 对 US city temp market 简洁有效。
- city alias mapping 清晰。
- 适合 dashboard 扫描。

不足：

- 城市范围固定。
- regex 偏英文和 Fahrenheit。
- Polymarket API 查询里 `search_term` 循环没有真正使用 search term 参数，实际主要依赖 `tag=Weather` 和 `slug_contains`。
- 不处理复杂结算描述。
- 不接 CLOB token translation。

### 5.2 Kalshi Weather Parser

文件：`backend/data/kalshi_markets.py`

它支持 Kalshi KXHIGH series：

- `KXHIGHNY`
- `KXHIGHCHI`
- `KXHIGHMIA`
- `KXHIGHLAX`
- `KXHIGHDEN`

Ticker parser 可处理：

```text
KXHIGHNY-26MAR01-B45.5
```

解析：

- date = 2026-03-01。
- boundary type `B` -> above。
- boundary type `T` -> below。
- threshold_f。
- metric = high。

这块对我们价值很高：

- Kalshi 的 ticker format 比 Polymarket 文案更结构化。
- 可以用 Kalshi 市场作为天气 probability model 的训练/验证场。
- 也可以为未来跨平台 weather arbitrage 做准备。

## 6. 竞品 Settlement 和 Calibration

### 6.1 Polymarket Settlement

文件：`backend/core/settlement.py`

竞品 settlement 逻辑：

- 通过 Gamma API event slug 或 market id 查 market。
- 如果 market closed，再看 `outcomePrices[0]`。
- `>0.99` 认为 first outcome 赢。
- `<0.01` 认为 second outcome 赢。

适合：

- Paper trade 事后结算。
- BTC Up/Down 或 Yes/No 市场。

不足：

- 它不是官方 CLOB fill/position 对账。
- 对 ambiguous/multi-outcome/市场特殊 resolution 没有强处理。
- 对 Polymarket resolved outcome 的解释依赖价格极端值。

### 6.2 Kalshi Settlement

同文件 `_fetch_kalshi_resolution`：

- 用 Kalshi client 查 market。
- status in `finalized`/`determined` 且 result yes/no。

价值：

- 对 Kalshi paper/live 后续方向有参考。

### 6.3 Calibration

竞品的 DB 有 `Signal` 表，settlement 后写：

- actual outcome。
- outcome_correct。
- settlement_value。
- settled_at。

API 里计算：

- accuracy。
- avg predicted edge。
- avg actual edge。
- Brier score。
- probability buckets。

这是我们当前项目明显缺的一块。

建议迁移：

- 我们应增加 `model_signals` 或扩展 `analyses`，记录 model probability、market probability、edge、source、module、forecast version。
- settlement/reconciliation 后回填 actual outcome。
- Live Launchpad 或 dashboard 增加 calibration summary。

## 7. 竞品 Dashboard

文件：

- `frontend/src/components/WeatherPanel.tsx`
- `frontend/src/components/SignalsTable.tsx`
- `frontend/src/components/CalibrationPanel.tsx`
- `frontend/src/components/EquityChart.tsx`
- `frontend/src/components/Terminal.tsx`

优点：

- 信号表统一展示 BTC + Weather。
- Weather panel 显示 mean high、std、agreement、best edge。
- signal row 可展开 reasoning。
- 可按 edge/model probability/suggested size 排序。
- 可视化比我们的 stdlib dashboard 强很多。

不足：

- 前端复杂度高，需要 Node/Vite 构建链。
- 对小白用户来说未必比我们的 beginner cockpit 更安全。
- `WeatherSignalResponse` 后端模型没有显式 `platform` 字段，但前端 type 里 `platform?: string`，这里有一点接口不一致。
- UI 上的 Trade button 当前只对 BTC simulation 生效，不是 weather live execution。

对我们启发：

- `/live` 页面应该增加 ensemble mean/std/agreement。
- Candidates table 应支持按 edge、module、source grade、blocker 排序。
- 每个信号要有一段人能读懂的 reasoning。
- Calibration 是判断模型是否值得 live 的关键。

## 8. 竞品执行层评估

文件：`backend/core/scheduler.py`

它的 `weather_scan_and_trade_job()`：

- 扫 weather signals。
- 找 actionable。
- 检查 bot state。
- 检查 pending exposure。
- 写入本地 `Trade` 表。
- 增加 total trades。
- 写 event log。

重点：

**它没有向 Polymarket CLOB 或 Kalshi order API 真实下单。**

所以不要被 README 里的 trading bot 字眼误导。它更像：

- Signal scanner。
- Paper trading simulator。
- Calibration dashboard。

而我们的项目虽然模型笨，但：

- 有 CLOB place limit order path。
- 有 order attempts。
- 有 open orders/fills/positions。
- 有 reconciliation gate。
- 有 compliance gate。
- 有 micro-live profile。
- 有 live launchpad blockers。

## 9. 测试情况

竞品仓库中没有看到明显的：

- `tests/`
- pytest tests。
- frontend test specs。
- playwright/vitest/jest specs。

这意味着：

- 它的研究代码值得读。
- 但不能直接照搬成生产逻辑。
- 迁移时必须用我们项目的测试纪律重写。

## 10. 我们项目 vs 竞品对比

| 维度 | 我们项目 | 竞品项目 | 判断 |
| --- | --- | --- | --- |
| 项目入口 | Typer CLI + stdlib dashboard | FastAPI + React dashboard | 竞品 UI 更现代，我们 CLI 更适合运维 |
| 天气概率模型 | 单点 forecast + default uncertainty interval | 31-member GFS ensemble member count | 竞品强 |
| NWS settlement | 有 NOAA provider，但偏 forecast | 有 NWS observations high/low | 竞品思路强 |
| Kalshi | 暂无 | KXHIGH parser/client | 竞品强 |
| Polymarket live order | 有 CLOB limit order path | 未见真实 CLOB live execution | 我们强 |
| Compliance | 有 geoblock/HK readiness | 未见 | 我们强 |
| Reconciliation | open orders/fills/positions | paper trades | 我们强 |
| Risk gates | hard caps/profile/override/whitelist | Kelly + simple exposure caps | 我们更适合 live |
| Dashboard | 安全、小白、readiness | 现代、信号密集、calibration | 各有优势 |
| Calibration | 目前弱 | Signal settlement + Brier score | 竞品强 |
| 测试 | 211 tests 级别 | 未见测试 | 我们强 |
| 依赖复杂度 | 轻量 Python | Python + Node frontend | 我们更容易部署 |

## 11. 不建议的路线

### 11.1 直接扔掉我们项目，迁移到竞品

不建议。

理由：

- 竞品没有我们的 live safety gates。
- 没有 HK/compliance 上下文。
- 没有真实 CLOB 对账闭环。
- 没有测试体系。
- 它包含 BTC microstructure，不是我们当前重点。

### 11.2 直接把竞品 clone 后改成我们的项目

不建议。

理由：

- 会引入 FastAPI/React/Node/SQLAlchemy/APScheduler 全套复杂度。
- 我们已有 CLI、SQLite repository、dashboard、tests，会被打散。
- 最终可能变成两个半成品拼在一起。

### 11.3 直接复制竞品 weather code

不建议。

理由：

- 竞品代码是 async/FastAPI 风格，我们是同步 provider + service 风格。
- 需要转换到我们的 `ForecastSnapshot`、`Analysis`、`Repository`、`ModuleCredibility`。
- 需要加 tests。

## 12. 推荐路线

### Phase A：移植 Ensemble Weather Research Layer

目标：

把竞品的 ensemble probability 思路移植成我们自己的 provider/model。

新增建议：

- `adapters/weather/open_meteo_ensemble.py`
- `domain/ensemble_weather.py`
- `domain/ensemble_pricing.py`
- `tests/test_open_meteo_ensemble_provider.py`
- `tests/test_ensemble_weather_pricing.py`

核心对象：

- `EnsembleForecastSnapshot`
  - city/location。
  - target_date。
  - variable。
  - unit。
  - member_values。
  - mean。
  - std。
  - member_count。
  - fetched_at。
  - source metadata。

核心函数：

- `probability_above(threshold)`
- `probability_below(threshold)`
- `agreement(threshold)`
- `to_probability_interval(conservative_widening=True)`

注意：

- 不要直接替换现有 `ForecastSnapshot`。
- 先作为新模型挂到 `weather` 或 `global_temp_bucket` dry-run。
- Live eligibility 初期仍 dry-run-only，直到 calibration 证明有效。

### Phase B：修正 NOAA/NWS Source Semantics

目标：

把 forecast source 和 settlement observation source 分开。

现状问题：

- 我们 `NoaaProvider` 当前用 NWS forecast，却 raw payload 标 `source_grade=settlement_grade`。
- 这对 live gate 可能太乐观。

建议改成：

- Forecast:
  - `source_grade=official_forecast`
  - `official_signal=True`
- Observation:
  - `source_grade=settlement_observation`
  - `settlement_source=True`

新增：

- `adapters/weather/nws_observations.py`
- `weather_observations` 回填流程。
- Dashboard 显示 forecast source 和 settlement source。

Live gate 更新：

- 对未结算市场，允许 official forecast 参与 micro-live，但必须显示“forecast 不是 settlement observation”。
- 对 settlement/calibration，必须用 observation 或 market resolved result。

### Phase C：引入 Signal Calibration

目标：

判断模型是否真的准，而不是只看 edge。

新增表或扩展：

- `model_signals`
  - market_id。
  - module_id。
  - model_version。
  - model_probability。
  - market_probability。
  - edge。
  - side。
  - confidence。
  - source list。
  - reasoning。
  - created_at。
  - actual_outcome。
  - brier_component。
  - settled_at。

或者在现有 `analyses` 上扩展：

- `model_probability`。
- `market_probability`。
- `confidence`。
- `sources`。
- `actual_outcome`。
- `settled_at`。

推荐：

**新表 `model_signals` 更清楚，避免把 trading analysis 和 calibration ledger 混在一起。**

Dashboard 增加：

- Brier score。
- Accuracy by module。
- Probability buckets。
- 最近 settled predictions。
- 模块 live 晋级状态。

### Phase D：Kalshi 作为研究/校准数据源

目标：

先不做 Kalshi live trading，只把 Kalshi weather markets 接入研究和 calibration。

新增：

- `adapters/kalshi/client.py`
- `adapters/kalshi/weather_markets.py`
- `domain/kalshi_weather.py`
- `services/kalshi_weather_discovery_service.py`

用途：

- 获取 KXHIGH 市场。
- 和 Polymarket weather 市场共享 ensemble model。
- 用 Kalshi settlement 更明确的数据来校准模型。

默认状态：

- `research_only`。
- 不接 live order。
- 不和 Polymarket 做自动 arbitrage。

### Phase E：升级 Live Launchpad

目标：

把“为什么敢/不敢 live”讲得更清楚。

从竞品借鉴：

- mean/std/agreement。
- reasoning 展开。
- calibration score。
- edge distribution。
- source tags。

保留我们自己的：

- readiness gates。
- whitelist。
- strategy override。
- reconciliation。
- module credibility。
- micro-live caps。
- source-grade blocker。

## 13. 具体下一步工作建议

下一步不要再大而全。建议做一个薄切片：

**Slice 1：Open-Meteo Ensemble Provider + Dry-run-only Weather Analysis**

范围：

1. 新增 ensemble provider。
2. 支持 5 个 US city mapping：NYC、Chicago、Miami、Los Angeles、Denver。
3. 解析 member values。
4. 生成 ensemble probability interval。
5. 在 analysis reasons 中展示 mean/std/member count/agreement。
6. Dashboard market detail 显示 ensemble context。
7. Live eligibility 不变，先 dry-run-only 或 source warning。
8. 测试覆盖 provider parsing、probability、workflow。

为什么先做这个：

- 它直接吸收竞品最强的部分。
- 不触碰真实下单。
- 可以马上提升我们项目“天气市场不傻”的程度。
- 为后续 calibration 和 module 晋级打基础。

不要先做：

- Kalshi live trading。
- Cross-platform arbitrage。
- 现代 React dashboard 重写。
- 自动撤单/补单。

## 14. 如果最终决定 fork 竞品

只有在以下条件满足时才考虑：

- 我们决定放弃 Polymarket live-first，转向 research dashboard-first。
- 用户愿意接受 FastAPI + React + SQLAlchemy + APScheduler。
- 用户愿意重新建立 compliance/live gates。
- 用户愿意重写测试体系。

否则，fork 成本大于收益。

## 15. 最终建议

保留我们项目作为主线。

短期主线：

1. 移植 ensemble weather model。
2. 修正 NOAA/NWS source semantics。
3. 加 calibration ledger。
4. 用 Live Launchpad 展示模型可信度。

中期主线：

1. 接入 NBM percentiles。
2. 接入 NWS observations。
3. 接入 Kalshi weather research。
4. 给模块定义 live 晋级标准。

长期主线：

1. Polymarket weather micro-live。
2. Kalshi research/cross-check。
3. 仅在单平台模型稳定 3 个月后，再考虑 cross-platform arbitrage。

项目方向调整为：

**Weather-specialized, safety-first prediction market operator.**

不是：

**Generic Polymarket bot from scratch.**

这能把“傻项目”变成“安全壳 + 聪明天气模型”的组合。
