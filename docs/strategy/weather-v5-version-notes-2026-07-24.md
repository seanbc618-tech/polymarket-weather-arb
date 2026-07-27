---
document: weather-v5-version-notes
version: V5.1
date: 2026-07-25
status: deployed_full_live
live_verdict: FULL_LIVE_2_USDC_100_USDC_DAY
v5_deployment_snapshot_head: 66bbc2d
runtime_code_commit: ca8ba75
real_trading_mutation: autonomous_buy_sell_redeem_authorized
intended_readers: Grok, Codex, Antigravity, Gemini, Claude
---

# Weather V5 版本说明与多 Agent 评审入口

## 1. 先读结论

V5 不是对 V8 天气概率模型的重写，而是一次入场和持仓兑现逻辑的
政策换代：

- 保留 `global-temp-bucket-multimodel-v8` 概率模型；
- 入场政策从 `weather-entry-v4` 升级为 `weather-entry-v5`；
- 退出政策升级为 `weather-exit-v3-settlement-only`；
- 目标从“高比例小额平仓获利”改为“手续费调整后的事件级结算净 EV”；
- 生产现已获批 `full_live`：单笔 `2 USDC`、每日 `100 USDC`。
- 所有合格天气桶均进入扫描、分析和排序，但同一城市/日期事件只允许
  概率收益最好的一个桶获得唯一 accepted BUY。

核心假设是：旧策略的主要问题并非完全押不中，而是频繁把最终正确桶在
低利润阶段提前卖掉，同时通过加仓、薄 edge 和低价强制退出积累左尾亏损。

本文件是其他 Agent 的单文件入口。评审时应以代码、生产 SQLite、Git 和
VPS 现场为事实源；不得把本文件中的反事实结果当成未来盈利保证。

## 2. 为什么从 V4 改为 V5

### V4 表面现象

- 严格 closed 的 V4 市场曾显示 `+$1.99`、`87.5%` cash-path win rate；
- 但 partial 市场合计 `-$5.84`；
- V4 全部市场现金流包含未结资本时约为 `-$16.91`；
- 薄 edge、scale-in、`0.15-0.20` 入场价和 D0 低价抛售是主要坏切片。

这里的 `87.5%` 是“提前平仓现金路径胜率”，不是结算桶命中率，也不是
策略已经盈利的证明。

### 正确桶的右尾被截断

Dallas 和 Manila 的正确桶在旧路径中只兑现了约 `$0.139` 和 `$0.255`
利润；静态持有到结算的对应收益约为 `$13.96` 和 `$25.93`。这说明：

```text
高胜率小盈利
    不等于
正确桶的完整结算价值被保留下来
```

### 独立生产回放

| 路径 | 样本 | BUY cash | 收入 | PnL |
| --- | ---: | ---: | ---: | ---: |
| A：实际 V4 现金路径 | 13 markets | `$35.804863` | SELL net `$35.394800` | `-$0.410063` |
| B：全部 V4 持有到结算 | 13 markets | `$35.804863` | `$47.740000` | `+$11.935137` |
| C-old：V5 选中仓位走旧退出 | 6 events | `$12.841480` | `$12.982207` | `+$0.140727` |
| C：V5 入场加结算核心 | 6 events | `$12.841480` | `$26.400000` | `+$13.558520` |

六个 V5 反事实事件是 Buenos Aires、Manila、Wuhan、Ankara、
Cape Town 和 Dallas，其中 Manila、Dallas 为正确桶。

该结果只支持“保留右尾值得继续验证”，不能证明未来盈利：

- 只有 6 个反事实事件；
- 历史最佳 bid 不是完整深度 VWAP；
- 历史可用 USDC 未完整持久化，回放使用明确的合成 `$100` bankroll；
- V5 回放中的模型价值退出和官方不可能退出均触发 `0` 次；模型价值退出
  已在 V5.1 明确删除，不再是待验证的真实 SELL 路径。

## 3. V4 到 V5 的政策差异

| 维度 | V4 主要行为 | V5 行为 |
| --- | --- | --- |
| 研究 edge | 生产曾使用 `0.05` | `MIN_EDGE=0.08` 保留影子可见性 |
| 最终 live edge | 随生产 `MIN_EDGE` | 代码独立硬边界 `>=0.10` |
| 最低 ask | 可进入极低价桶 | ask `<0.05` 禁止 live |
| D0 | 可 live | 暂停 live |
| 同事件加仓 | 曾多次 scale-in / sibling rotation | 首个 accepted BUY 后冻结 |
| 小额盈利 | 可触发回本或止盈 | 不是 SELL 理由 |
| dust | 可全平 | 不是 SELL 理由 |
| 模型反转 | 可推动退出 | 永远不能单独或连续确认后 SELL |
| 证据缺失/过期 | 可能进入退出链 | 明确 HOLD |
| 核心持仓 | 可被逐步卖空 | 当前小仓位 100% 为 settlement core |

V5 的“单事件一次 accepted BUY”按标准化的 `city + target_date` 判断，
而不是只按 market ID 判断，因此禁止在同一温度事件的兄弟桶之间继续轮换。

## 4. V5 精确入场规则

V8 仍负责产生定量概率和候选。最终 live BUY 必须同时满足：

```text
conservative net edge >= 0.10
executable ask >= 0.05
horizon != D0
same city/date event has no prior accepted live BUY
existing reconciliation/freshness/risk/idempotency/breaker gates all pass
```

`MIN_EDGE=0.08` 只让 `0.08-0.10` 的候选继续留下影子数据，不能绕过
`0.10` 的 live 边界。

## 5. V5 精确退出规则

当前小额天气仓位的 100% 都是结算核心，不拆 core/satellite。

### 明确不允许的自动 SELL 理由

- 小额浮盈；
- 回收本金；
- dust 或低于交易所最小规模；
- 入场 edge 消失；
- 单次模型方向反转；
- 单次 D0/TAF 矛盾；
- forecast 缺失、过期或不完整。

### 允许退出一：官方不可能

只有可靠的结算级官方观测已经不可逆地证明持有桶不可能命中，才可推荐
`exit_full`。如果官方观测已经锁定持有桶，则必须 `hold_for_resolution`。

### V5.1 删除模型型退出

`weather-exit-v3-settlement-only` 不再计算或执行连续 revision 的
value-exit。模型反转、负 hold edge、D0/TAF 矛盾、可执行高 bid、回本、
止盈和 dust 都只能进入研究遥测，不能形成 SELL。

市场规则、数据源、合约异常或系统级紧急风险只可通过明确、独立、
证据化的 guardian 风险路径退出；信息缺失或歧义本身默认 HOLD。

### 结算赢家自动 redeem

`full_live` 在成功对账后，可对“新鲜 Polymarket 官方响应已确认唯一赢家、
持仓 outcome 与赢家一致”的一个 condition 自动 redeem。Deposit Wallet
必须配置完整 Builder 凭据三元组。提交前先写 durable decision；任何
`submitted_unverified` 都禁止自动重试。

## 6. 实现边界

V5 没有创建第二套策略或执行引擎：

```text
BUY:  AutopilotService -> TradingService
SELL: ExitGuardianService -> AutoExitService -> PositionExitService
Truth: ReconciliationService -> Repository -> existing SQLite schema
```

主要代码：

- `src/polymarket_weather_arb/domain/strategy_versions.py`
- `src/polymarket_weather_arb/services/autopilot_service.py`
- `src/polymarket_weather_arb/services/trading_service.py`
- `src/polymarket_weather_arb/services/exit_guardian_service.py`
- `src/polymarket_weather_arb/services/auto_exit_service.py`
- `src/polymarket_weather_arb/services/position_exit_service.py`
- `src/polymarket_weather_arb/storage/repositories.py`

没有新增 Service、数据库表、BUY 路径、SELL 路径或 LLM 决策路径。
redeem 复用 `AutopilotService` 的 serial capital pulse、现有
`GammaPolymarketClient` 和 `autopilot_decisions` 审计表。

## 7. 验证与提交

- 主 V5 实现：`e9334c9`
- 影子调度修复：`ca8ba75`
- V5 部署证据基线 HEAD：`66bbc2d`
- 本地最终测试：`1051 passed, 1 skipped`
- VPS 主发布全量测试：`1050 passed, 1 skipped`
- VPS 调度修复定向测试：`33 passed`
- Ruff 和 `git diff --check`：通过

影子调度修复解决了一个现场发现的问题：`TRADING_DISABLED=true` 阻断
执行后，capital 时钟退避但 exit 时钟仍到期，导致重复对账并饿死 slow
refresh。现在两个时钟一起退避。

## 8. 生产现场快照

快照时间：`2026-07-24T14:01:28Z` 附近。

- VPS HEAD（本快照）：`66bbc2d`，worktree clean；
- 服务：active，PID `61889`，restart `0`；
- `TRADING_DISABLED=true`；
- `MIN_EDGE=0.08`；
- runtime：`live / micro_live`，但全局 execution-disabled；
- reconciliation：`6025 / ok`；
- open orders：`0`；
- reconciliation new fills：`0`；
- positions：`13`；
- circuit breaker：clear；
- order intent 最大 ID：`637`；
- fill 最大 ID：`306`。

最新 tick 的 `blocked` 和 `pulse_blocker|TRADING_DISABLED=true` 是当前
影子姿态的预期执行阻断，不代表 reconciliation 失败。未执行手工 BUY、
SELL、cancel、redeem 或 reconcile。

生产备份：

- `/opt/polymarket-weather-arb/data/backups/polymarket_weather-pre-v5-20260724T115240Z.db`
- `/etc/polymarket-weather-arb.env.pre-v5-20260724T115240Z.bak`

## 9. V5 已证明与未证明的内容

### 已证明

- 新入场和退出规则已落到唯一的现有执行路径；
- Dallas/Manila 风格正确桶不会再因旧止盈、回本和 dust 路径被卖掉；
- 薄 edge、D0、低价票和同事件 scale-in 已被 live gate 阻断；
- 交易禁用状态下服务可继续对账和收集影子分析；
- 历史 V5 反事实显著优于相同仓位的旧退出路径。

### 尚未证明

- 未来事件的正 EV；
- 结算核心在更大样本下的最大回撤；
- 官方不可能证据的现场覆盖率和触发延迟；
- 持有占用资本后，机会成本是否超过保留下来的右尾；
- maker-first 的真实成交率和 adverse selection。

## 10. 继续 live 的硬门槛

1. 当前运行获批的 `full_live`，`TRADING_DISABLED=false`。
2. 硬 cap 为单笔 `2 USDC`、每日 `100 USDC`；安全对账、breaker、
   compliance 和风险 cap 仍可 fail-stop。
3. 不按累计亏损自动暂停新 BUY；但这不允许绕过任何安全门禁。
4. 满 20 个新 V5 已结算真实事件且赢家均完成 redeem 后，以无外部资金
   流干扰的账户资金是否增加作为操作者通过口径。
5. 评审同时保留事件级、手续费调整后的净 EV、drawdown 和资金流审计；
   不得用高 win rate、服务 active 或测试全绿替代盈利证据。

## 11. V5b Maker-first：结论与复杂度

### 术语

- 当前直接买 best ask 是吃单，即 **taker**；
- 在不穿价的价格挂住、稍后被别人吃掉，才是 **maker**；
- 因此建议是从 taker 改为 maker-first，不是 maker 改为 taker。

Polymarket 当前官方规则说明：weather 属于 fee-enabled 类别，maker 的
平台费为 0，taker 承担按市场参数计算的费用；实际参数仍必须逐市场查询。
Maker 还可能获得日度 rebate，但不能在回放中提前当作确定收入。

本项目安装的 SDK 已支持：

```python
SecureClient.place_limit_order(
    ...,
    post_only=True,
    expiration=<unix timestamp>,
)
```

因此 V5b 的适配层改动不大，整体复杂度是 **中等**。难点不是签名或
post-only 参数，而是订单生命周期和测量。

### 建议的首版范围

只 maker-first 化自动 **BUY entry**：

- 不修改 V5 settlement-core 退出逻辑；
- 不把风险退出 SELL 改成等待成交的 maker 单；
- 不增加第二个 TradingService、scheduler、表或持久化 mode；
- 复用现有 `TradingService`、`OrderLifecycleService`、reconciliation、
  open orders、fills 和 idempotency。

### 必须解决的行为

1. **挂单价格**
   - 使用当前 tick size；
   - BUY 报价必须严格低于 best ask；
   - 可从 `best_bid + one_tick` 与 `best_ask - one_tick` 中选择；
   - 报价后仍需满足手续费调整后的 V5 edge。
2. **post-only 竞态**
   - 盘口变化导致订单穿价时，交易所会拒绝而不是成交；
   - 首版只允许刷新后有限次数重新报价，不得静默转 taker。
3. **到期和撤单**
   - 优先 GTD 自动过期，并保留现有 stale-order lifecycle；
   - 订单过期、取消或失败后才能释放重复订单和资金占用。
4. **部分成交**
   - reconciliation 是 filled size 的事实源；
   - 只对未成交余量执行到期/取消；
   - event exposure 和“同事件一次 accepted BUY”要明确以 intent、成交
     还是最终关闭状态为边界。
5. **资金预留**
   - open maker order 占用可用余额；
   - 新机会排序必须扣除未成交挂单的保留资金。
6. **可观测性**
   - `post_only_submitted`、`post_only_rejected_cross`、`live`、
     `partially_filled`、`expired`、`cancelled`、`filled` 必须可区分；
   - 交易所 accepted 之后的本地检查失败仍要保留
     `submitted_unverified` 语义。

### V5b 必须测量

- post-only 提交数和穿价拒绝率；
- time-to-first-fill、time-to-full-fill；
- 30 秒、1 分钟、5 分钟和 15 分钟 fill ratio；
- 部分成交比例；
- maker/taker 实际角色与真实 fee；
- 成交后 1/5/15 分钟 markout；
- maker 成交价格改善；
- 因未成交错失的正 EV；
- 资金被挂单占用的时间；
- rebate 单独记录，不提前计入保守 EV。

### 推荐发布顺序

```text
V5b-0 只读模拟
  -> 用历史/实时 BBO 推演 maker 报价和理论 fillability
  -> 不提交订单

V5b-1 代码与 mocked exchange tests
  -> adapter 暴露 post_only / expiration
  -> TradingService 仍为唯一 BUY owner
  -> TRADING_DISABLED=true

V5b-2 明确批准后的 micro-live
  -> 小额、BUY-only、无 taker fallback
  -> 先收集 fill/markout，再讨论 fallback
```

## 12. 是否适合交给 Grok

适合，但应先交给 Grok 一个边界明确的 **V5b-0 设计和只读研究任务**，
不要第一步就让它部署或开启真实挂单。

建议 Grok 交付：

`docs/reviews/weather-v5b-maker-first-design-2026-07-24-grok.md`

任务范围：

1. 只读核查当前 SDK、`TradingService`、`OrderLifecycleService`、
   reconciliation、open orders 和 fills；
2. 设计 maker BUY 报价公式、GTD 时间、有限重报价规则和资金预留；
3. 用生产 SQLite 和历史 BBO 做只读 fillability / markout 方法设计；
4. 列出需要修改的现有文件和测试，不创建第二套执行路径；
5. 给出 P0/P1/P2 风险和最小实现切片；
6. 明确说明真实 BUY、SELL、cancel、redeem 和服务变更均未执行。

禁止范围：

- 不真实提交 maker order；
- 不自动 cancel 或 taker fallback；
- 不修改 VPS 服务、环境或数据库；
- 不新建 Service、表、scheduler 或另一套 BUY engine；
- 不把理论 maker 节费当成已实现收益。

## 13. 给所有评审 Agent 的问题

请分别从以下角度挑战 V5，而不是只复述结论：

### 统计与策略

1. 六事件反事实是否有选择偏差或幸存者偏差？
2. 20 个新事件门槛是否足够，应该同时要求什么置信区间和 drawdown？
3. 100% settlement core 会不会把早平仓问题变成持有错误桶的问题？
4. `0.10` edge、`0.05` ask 和 D0 禁令应如何做 out-of-sample 复核？

### 退出数学

1. `net_sell >= hold_upper + 0.02` 是否口径一致？
2. NO 持仓的 `1 - fair_lower` 是否在所有分析来源下正确？
3. 两个 distinct revision 是否足够独立？
4. 官方不可能和官方锁定是否存在单位、时区或观测容差漏洞？

### 交易微观结构

1. maker-first 的价格改善能否覆盖未成交和 adverse selection？
2. GTD 多长最合适，是否应按 D2/D1/D0 分层？
3. 是否应永远禁止首版 taker fallback？
4. 如何避免频繁 cancel/repost 丢失队列优先级？

### 代码与安全

1. 是否存在绕过 V5 gate 的另一入口？
2. accepted intent freeze 是否可能永久阻塞事件？
3. partial fill、submitted_unverified 和 reconciliation 是否仍保持单一真相？
4. 影子 `live / micro_live + TRADING_DISABLED=true` 是否有调度或可观测性盲点？

### 资本效率

1. settlement hold 的资金占用是否应计入 event-level EV？
2. maker open order 的余额预留如何影响机会排序？
3. 应采用什么 bankroll/reinvestment 模型比较 taker 与 maker？

## 14. 评审交付格式

每个 Agent 的报告至少包含：

- verdict：`AGREE / AGREE_WITH_CHANGES / DISAGREE`；
- P0/P1/P2 findings；
- 复核过的 SQL、代码路径或官方文档；
- 对 A/B/C 回放方法的挑战；
- 对 V5 live gate 的建议；
- 对 V5b 的推荐范围；
- 明确声明是否执行任何真实交易或外部写操作。

讨论和只读分析不构成恢复 live 的批准。

## 15. 事实源

- `docs/strategy/weather-settlement-core-v5.md`
- `docs/reviews/weather-settlement-core-v5-replay-2026-07-24.md`
- `docs/reviews/weather-settlement-core-v5-implementation-2026-07-24.md`
- `docs/reviews/v8-v4-event-split-analysis-2026-07-24.md`
- `docs/reviews/strategy-loss-diagnosis-2026-07-24-grok.md`
- `docs/worker-tasks/2026-07-24-codex-weather-settlement-core-v5.md`
- `scripts/replay_weather_settlement_core_v5.py`
- Polymarket fees:
  `https://docs.polymarket.com/trading/fees`
- Polymarket post-only orders:
  `https://docs.polymarket.com/trading/orders/overview`
- Polymarket order lifecycle:
  `https://docs.polymarket.com/concepts/order-lifecycle`
