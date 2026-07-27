# Settlement Backfill & Calibration Checklist

This runbook guides operators on how to preview and backfill official weather observations to evaluate our predictive model's performance (calibration).

## What is this feature?

The settlement backfill process fetches official observed weather data (e.g., from the NWS API), compares it against the market's parsed resolution rules, and records whether the model's past forecasts were correct.

This feeds directly into the Calibration Scoreboard, allowing us to track Brier scores, hit rates, and average edge.

## 安全口径 (Safety Boundaries)

- **Preview** 不会写库 (Does not write to DB).
- **Backfill** 只会写入 `weather_observations` 表，并更新 `model_signals` 的 settlement 字段.
- 它**不会**下单或执行交易 (Will NOT place orders).
- 它**不会**放宽 live trading gate (Will NOT relax live gate).
- 它**不会**创建新的 forecast (Will NOT create new forecasts).

## Preview vs. Backfill

- **Preview (`--preview` / 预览观测):** Fetches the observation and evaluates the YES/NO outcome in memory. **Nothing is saved to the database.**
- **Backfill (回填观测并结算):** Fetches the observation, saves it to the `weather_observations` table, and marks all associated `model_signals` for this market as resolved with the official outcome.

### When to ONLY Preview
- When you suspect the NWS station data might be delayed or incomplete.
- If there are known data quality warnings for the station.
- If the market rule was ambiguous and you want to verify the parsed operator/threshold against the fetched value.
- To debug a market that hasn't officially settled on Polymarket yet.

---

## Terminal Commands

If operating via CLI:

1. **Preview:**
   ```bash
   uv run polymarket-weather settlement-backfill --market <market_id> --preview
   ```
2. **Backfill:**
   ```bash
   uv run polymarket-weather settlement-backfill --market <market_id>
   ```
3. **Check Scoreboard:**
   ```bash
   uv run polymarket-weather calibration-report
   ```

---

## Browser / Dashboard Steps

If operating via the web UI:

1. Start the dashboard:
   ```bash
   uv run polymarket-weather dashboard --port 8766
   ```
2. Open the browser to: `http://localhost:8766/calibration?lang=zh`
3. Locate the **官方观测回填 (Official Observation Backfill)** section.
4. Enter the `market_id`.
5. Click **预览观测 (Preview observation)**.
6. Check the **预览结果 (Preview Result)** section to ensure the station, observed value, and expected outcome (YES/NO) are correct. Check for any warnings.
7. If everything looks correct, click **回填观测并结算 (Backfill)**.
8. Scroll to **最近观测值 (Recent Observations)** to confirm the data was saved.

---

## Common Errors & Explanations

- **`market not found`**: The market ID is not in your local database. You may need to run `discover-markets` first.
- **`rule is not tradable`**: The settlement service cannot backfill because the parsed rule was marked as non-tradable (e.g., ambiguous location, unsupported variable).
- **`no usable NWS observations`**: The NWS API doesn't have data for this time window/station yet. Observations often lag by a few hours. Try again later.

## 如何处理 Warning (Handling Warnings)

When running a Preview, you might encounter warnings. Here is how to handle them:

- **`low observation coverage`**: Only use Preview. Do not backfill yet. Wait for more complete data to arrive, or verify the official outcome manually.
- **`selected observation quality is <X>`**: Do not backfill directly. Cross-check the fetched value with the official Polymarket resolution or raw NWS data before proceeding.
- **`selected observation quality is unknown`**: Do not backfill directly. Proceed only if manually confirmed to be accurate.
- **`extrema_method is sample-based, not official daily summary`**:
  当前值来自本地日窗口内 NWS observation samples 的最大/最小值，不是 NWS/NCEI 事后发布的 official daily summary。Preview 时必须查看 `observations` 列表、样本数量和质量警告；如果市场已经有官方 resolution，优先服从官方 resolution。
- **`station timezone unknown; UTC calendar-day window used`** / **`station timezone invalid: <tz>; UTC calendar-day window used`**:
  Polymarket 的气温市场通常按**气象站本地时间 (Local Time)** 的自然日 (00:00 - 23:59) 进行结算。如果系统无法找到该站点的正确时区（或时区无效），会自动降级使用 UTC 时间的自然日窗口，这极有可能导致读取的观测数据段与当地自然日发生错位（例如把前一天晚上的温度算到今天）。
  *处理建议*：仅使用 Preview，**绝不要**直接 Backfill。请人工确认偏差是否会影响最终结果，或耐心等待官方 Polymarket 结算。

## 数据来源与极值策略说明 (Extrema Strategy)

我们目前的 NWS 结算数据基于**本地日窗口内的所有观测样本 (Observation Samples)** 计算最高 (Max) 和最低 (Min) 值。这与 NWS 最终发布的**官方每日气象总结 (Official Daily Summary)** 可能存在细微差异。

- **样本极值 (Sample Extrema)**：系统拉取当天每 5 到 60 分钟一次的实时观测值，并在其中找最大/最小值。
- **官方总结 (Official Summary)**：NWS 最终发布的值有时会基于 5 分钟滑动平均，或者经过事后人工质量控制（Quality Control）。

*操作指引*：
在绝大多数情况下，两者是一致的。但当您在 Preview 中看到 `low observation coverage` 或 `quality is unknown/X` 警告时，说明当天的实时样本可能不完整，样本极值有较大概率偏离官方最终总结。此时**务必暂缓 Backfill**，以官方解决 (Resolution) 为准。

---

## Operator Checklists

### Pre-operation Checklist
- [ ] Ensure the market has concluded its time window (you cannot fetch observations for the future).
- [ ] Verify you have the correct `market_id`.
- [ ] Run a **Preview** first.

### Post-operation Checklist
- [ ] Verify the observation appears in the `Recent Observations` (最近观测值) table.
- [ ] Check the Calibration Scoreboard (`calibration-report`) to ensure the `Resolved` count for the model increased.
- [ ] Confirm the calculated Brier score/Hit rate makes sense given the outcome.
