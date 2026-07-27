# Polymarket Weather Arb 中文使用说明

[English](README.md) | 简体中文

> [!NOTE]
> 这是 MIT 协议的社区版，代码快照截止到 2026-07-25 的 Anchor 前 V5.1。
> 后续私有策略研发不包含在本仓库中。欢迎通过 Issue、Pull Request、
> 代码审查或研究复现提出意见。

> [!WARNING]
> 本项目是实验性研究软件，不构成投资建议，也不承诺盈利。启用任何实盘
> 模式前，请自行审查代码、结算规则、司法辖区限制与资金风险。

这是一个面向 Polymarket 天气市场的本地自动化交易程序。它可以发现天气市场、组合多个天气模型、估算区间概率、筛选价格优势，并在通过资金与执行检查后自动提交限价买单和卖单。

完全不使用命令行、通过 macOS `.dmg` 安装的用户，请先看 [小白上手指南](docs/小白上手指南.md)。

> [!WARNING]
> `--full-auto` 会执行真实资金交易。系统不会保证盈利。第一次运行前请确认账户、限额、自动退出和对账状态均正确。

## 最常用：启动正式全自动实盘

每次启动前，在终端依次执行：

```bash
cd /path/to/polymarket-weather-arb

# 如果你的网络需要代理，先执行你自己的代理命令，例如 proxy_on

git pull --ff-only
uv sync
uv run polymarket-weather reconcile
uv run polymarket-weather live-readiness
uv run polymarket-weather autopilot start --full-auto
```

浏览器打开：

```text
http://127.0.0.1:8765/app?lang=zh
```

启动成功时，终端应该明确出现：

```text
/app FULL-AUTO FULL_LIVE
Mode: live app_mode=full_live
```

必须保留启动命令所在的终端窗口。按 `Ctrl-C` 可以停止程序；也可以先在 `/app` 点击 **Pause**，再关闭进程。

## 第一次安装

需要安装 Git、Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)。然后执行：

```bash
git clone https://github.com/seanbc618-tech/polymarket-weather-arb.git
cd polymarket-weather-arb
uv sync
cp .env.example .env
uv run polymarket-weather init-db
```

如果你使用 macOS DMG 版本，可以直接打开应用并完成首次设置；终端版和 DMG 版使用的是同一套核心策略与安全检查。

## 全自动实盘需要的 `.env`

至少确认以下设置。密钥只保存在本机 `.env` 或 macOS Keychain，不要发给别人，也不要提交到 Git：

```env
# Polymarket 钱包与签名
POLYMARKET_PRIVATE_KEY=你的钱包私钥
POLYMARKET_FUNDER=你的Polymarket资金地址

# 允许真实交易
TRADING_DISABLED=false

# Full Live 自动包含退出；AUTO_EXIT_ENABLED 只用于 Micro Live
MAX_AUTO_EXITS_PER_TICK=1
AUTO_EXIT_MAX_SLIPPAGE=0.02

# 默认是保守的新手额度；验证稳定后再有意识地提高
MAX_ORDER_USDC=1
MAX_DAILY_USDC=5
MAX_MARKET_USDC=2
MIN_EDGE=0.05
SLIPPAGE_BUFFER=0.02

# 每轮间隔，默认 5 分钟
AUTOPILOT_TICK_SECONDS=300

```

`.env.example` 只保留普通用户需要维护的设置。API 地址、HTTP 重试、
Micro Live 白名单、LLM、交易所 WebSocket 和旧模块配置集中在
`.env.advanced.example`；只有确实需要时才把单个字段复制到 `.env`。

其他可选配置：

```env
# Telegram 即时推送交易动作，并每 4 小时汇总持仓收益
TELEGRAM_NOTIFY_ENABLED=true
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_NOTIFY_MIN_LEVEL=trade

# Google Weather 是可选的定价参考源
GOOGLE_WEATHER_API_KEY=

# LLM 属于高级可选项；量化模型仍是主要决策者
LLM_ENABLED=false
LLM_PROVIDER=openai
LLM_API_KEY=
LLM_MODEL=
```

修改 `.env` 后需要停止并重新启动程序，新的进程才会完整加载配置。

## 四种运行模式

| 模式 | 行为 | 是否使用真实资金 |
|---|---|---|
| 模拟观察 | 发现并分析市场，不创建订单 | 否 |
| 模拟自动交易 | 自动分析并记录模拟订单 | 否 |
| 微额实盘 | 使用更谨慎的微额路径，需要额外限制 | 是 |
| 正式实盘 | 自动买入、管理挂单、自动退出 | 是 |

命令区别：

```bash
# 默认模拟模式
uv run polymarket-weather autopilot start

# 仅启用普通 live，会进入 micro_live，不是完整全自动
uv run polymarket-weather autopilot start --live

# 正式全自动实盘：自动 BUY + 自动 SELL
uv run polymarket-weather autopilot start --full-auto

# 只执行一轮正式全自动检查，然后退出
uv run polymarket-weather autopilot start --full-auto --once
```

如果页面显示“微额实盘”，通常是因为启动时用了 `--live`，或者在页面里选择了 Micro live。正式全自动必须使用 `--full-auto`，成功后页面状态应为 `full_live`。

## 启动前检查

建议每次正式实盘前执行：

```bash
uv run polymarket-weather reconcile
uv run polymarket-weather live-readiness
uv run polymarket-weather operator circuit-breaker status
uv run polymarket-weather operator exit-guardian
```

需要确认：

- `reconcile` 返回 `status: ok`，余额、挂单、持仓和成交能够读取。
- `live-readiness` 每项均为 `yes`。
- 合规检查显示当前线路所在国家允许访问。
- Resolution circuit breaker 没有跳闸。
- `.env` 中的订单、单日和单市场额度符合你的承受能力。
- 自动退出已经开启，并且滑点限制合理。

`--full-auto` 不会绕过这些检查。对账失败、凭证失效、断路器跳闸、盘口过期或资金限额不通过时，本轮不会提交交易。

## 系统怎样选择买入

天气区间市场会组合可用的 GFS、ECMWF、ICON、GEM、NOAA/Open-Meteo 和已配置参考源：

- 所有日期使用至少三分之二模型赞同的投票规则。
- 使用模型概率的中位数计算净 Edge，减少单个异常模型的影响。
- D0（当天）会额外使用可靠的官方已观测最高温约束概率，但不使用单独的更高交易门槛。
- 同一城市、同一天默认只持有策略评分最好的一个区间，避免重复暴露。
- 城市范围由 Polymarket 当前天气页动态发现，不再受内置城市名单限制；新城市只有在结算规则、ICAO 站点和 IANA 时区验证通过后才进入策略链。已验证城市会写入 SQLite，页面临时不可用或程序重启后仍能继续发现未来三天市场。
- LLM 只提供独立复核与经过历史校准的有限投票权，不能直接下单、放宽执行检查或覆盖对账结果。

## 系统怎样自动卖出

自动退出采用分段保护策略：

1. 价格上涨且足以覆盖成本和费用时，卖出一部分仓位收回本金。
2. 本金已收回且模型优势仍在时，保留剩余仓位等待更高收益或结算。
3. 模型方向反转、净 Edge 消失或持仓风险上升时，全量退出剩余仓位。
4. 临近结算时结合官方观测、市场状态和盘口深度决定继续持有还是退出。

所有卖出均使用限价单，并通过 `PositionExitService` 校验真实持仓、最新盘口、可卖数量、幂等键和最大滑点。详细规则见 [Full-Live Autopilot Runbook](docs/runbooks/full-auto-micro-live.md)。

## 页面和日志

主页面：

```text
http://127.0.0.1:8765/app?lang=zh
```

常用高级页面：

- `/live`：实盘准备和订单预览。
- `/orders`：订单记录。
- `/positions`：当前持仓。
- `/fills`：已对账成交。
- `/calibration`：模型校准与结算回填。
- `/actions`：自动化动作审计。

持久日志位于数据库所在目录旁：

```text
<DATABASE_PATH 的父目录>/logs/autopilot.log
```

查看最近日志：

```bash
tail -100 data/logs/autopilot.log
```

持续跟踪日志：

```bash
tail -f data/logs/autopilot.log
```

如果你的 `DATABASE_PATH` 不在默认 `./data/`，请使用实际数据库父目录下的 `logs/autopilot.log`。

## 暂停、紧急停止与恢复

普通暂停：

1. 在 `/app` 点击 **Pause**。
2. 回到终端按 `Ctrl-C` 停止进程。

禁止所有新实盘执行：

```env
TRADING_DISABLED=true
```

设置后重启进程。它不会自动撤销交易所中已经存在的挂单，所以还要执行：

```bash
uv run polymarket-weather reconcile
uv run polymarket-weather operator open-orders
uv run polymarket-weather operator positions --nonzero-only
```

发生 Resolution Audit mismatch 时，全局断路器会阻止新的实盘动作。修复根因并核实后，才可以人工清除：

```bash
uv run polymarket-weather operator circuit-breaker status
uv run polymarket-weather operator circuit-breaker clear --note "已核实并修复具体原因"
```

不要在没有查明原因时直接清除断路器。

## 常用维护命令

```bash
# 更新程序
git pull --ff-only
uv sync

# 健康检查
uv run polymarket-weather doctor
uv run polymarket-weather live-readiness

# 同步交易所状态
uv run polymarket-weather reconcile

# 查看风险、挂单、持仓和成交
uv run polymarket-weather risk-report
uv run polymarket-weather operator open-orders
uv run polymarket-weather operator positions --nonzero-only
uv run polymarket-weather operator fills

# 查看命令帮助
uv run polymarket-weather --help
uv run polymarket-weather autopilot start --help
```

## 常见问题

### 为什么没有成交？

没有订单不一定是故障。可能没有市场同时满足模型投票、概率、净 Edge、盘口新鲜度、事件敞口和资金限额。先看 `/app` 的机会漏斗、拒绝原因和最近运行记录，再看日志中的 `tick complete`。

### 为什么 Telegram 没消息？

默认会即时推送买入/卖出提交、成交、自动退出和重要故障；持有仓位时，还会在成功且新鲜的对账后每 4 小时推送一次持仓收益摘要。摘要最多列出 10 笔已验证持仓，包括温度桶、建仓成本、当前估值、周期估算盈亏和距离当地目标日结束的时间。没有持仓、对账陈旧或普通 `idle`、`watch`、`reject` 时不推送是正常行为。

### 为什么启动后是微额实盘？

`--live` 对应 micro live；完整全自动命令是：

```bash
uv run polymarket-weather autopilot start --full-auto
```

### 为什么 full-auto 启动失败？

终端会显示具体阻断原因。最常见的是：

- `TRADING_DISABLED=true`。
- 私钥或 funder 配置不完整。
- 对账不新鲜或交易所读取失败。
- 当前网络线路未通过合规检查。
- Resolution circuit breaker 已跳闸。

### 可以直接关闭终端吗？

关闭终端会终止本地进程。先在 `/app` 暂停，然后按 `Ctrl-C`，确认没有遗留挂单后再关闭。

## 进一步文档

- [英文完整 README](README.md)
- [正式全自动实盘 Runbook](docs/runbooks/full-auto-micro-live.md)
- [人工实盘 Smoke Test](docs/runbooks/audited-live-smoke.md)
- [macOS 初学者应用说明](docs/runbooks/macos-beginner-app.md)
- [HK VPS 部署说明](docs/hk-vps-production-checklist.md)

本项目的目标是改善可重复、经过风险调整的天气市场交易表现，而不是保证利润。请先使用小额资金验证每一项策略和执行路径。
