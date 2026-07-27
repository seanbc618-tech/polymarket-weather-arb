# Antigravity Task: Calibrated LLM Weather Vote

## Objective

Turn the existing advisory LLM review into a measurable weather-model vote that
can earn a small, bounded pricing weight only after resolved evidence shows that
it is useful. The LLM must never place orders, override reconciliation, change
prices, relax exposure limits, or bypass `TradingService` / `PositionExitService`.

This task is one behavioral objective and must be one commit. Do not combine it
with runtime concurrency or discovery changes.

## Mandatory Reading

Read before editing:

- `AGENTS.md`
- `docs/agent-worker-standards.md`
- `services/llm_advisor_service.py`
- `domain/llm_decision.py`
- `services/autopilot_service.py`
- `services/calibration_service.py`
- `services/settlement_service.py`
- `storage/repositories.py`
- `storage/db.py`
- `domain/global_bucket_pricing.py`

## Reuse Map

- Extend `LlmAdvisorService`; do not create another LLM service.
- Reuse `model_signals` for LLM probability history; do not create a new table.
- Reuse `CalibrationService` and settlement backfill to score resolved signals.
- Reuse `MarketWorkflowService` / `global_bucket_pricing` for pricing.
- Keep `TradingService` as the only BUY owner and `PositionExitService` as the
  only SELL owner.
- Extend the existing `/calibration` or `/app` calibration display; do not create
  a competing dashboard page.

## Current Problem

The current LLM is called only after quantitative selection has chosen one
market. Its output contains `action`, `confidence`, and `reason`, but no
calibratable YES probability. It cannot compare sibling temperature buckets,
cannot affect ranking, and cannot earn a data-backed weight.

## Required Design

### 1. Produce one group-level probability distribution

Add a group evaluation method to `LlmAdvisorService`. One call covers one
city/target-date event and all currently tradable sibling buckets.

The prompt must not reveal the quant engine's final action, fair probability, or
selected bucket. It may include:

- exact settlement wording and parsed rules;
- target city/date, station/source and timezone;
- GFS/ECMWF/ICON/GEM member summaries;
- NOAA and Google deterministic forecasts;
- D0 observed maximum when available;
- evidence freshness and missing-source warnings.

Require strict JSON:

```json
{
  "bucket_probabilities": [
    {"market_id": "123", "yes_probability": 0.42}
  ],
  "other_probability": 0.08,
  "confidence": 0.71,
  "reason": "concise explanation"
}
```

Validation invariants:

- Returned market IDs must exactly match the supplied sibling set.
- Every probability and confidence must be finite and within `[0, 1]`.
- Bucket probabilities plus `other_probability` must be within `0.98..1.02`.
- Invalid, missing, duplicate, or extra IDs reject the whole LLM distribution.
- A failed/invalid/timeout response records an unavailable review and never
  blocks quantitative analysis or live execution.

Do not convert `action/confidence` into a probability using an arbitrary formula.

### 2. Persist through the existing `model_signals` table

Add a narrow public Repository method for external model signals. Insert rows
with:

- `analysis_id=NULL`;
- `model_version=llm-weather-vote-v1`;
- `forecast_provider=llm:<provider>:<model>`;
- `source_grade=research_forecast`;
- one `yes_probability` row per sibling market;
- `decision=advisory` and no executable side;
- raw payload containing provider, model, event/group identity, confidence,
  reason, source forecast IDs/timestamps, and distribution total.

SQLite permits multiple NULL values in the existing UNIQUE `analysis_id` field.
Do not migrate or replace the table.

Prevent duplicate samples for the same provider/model + event + source forecast
revision. Retries/restarts must not inflate calibration counts.

### 3. Settle and score without a second calibration engine

Ensure existing settlement backfill resolves the LLM `model_signals` rows for the
same markets. Extend `CalibrationService` only as needed to report:

- resolved distinct events;
- resolved bucket signals;
- Brier score;
- hit rate;
- provider/model;
- current effective LLM weight;
- why the weight is still zero or was reduced.

Sibling buckets from one city/date are correlated. Weight activation must use
**distinct resolved events**, not only raw row count.

### 4. Bounded automatic weight schedule

The LLM starts at weight `0`. It may earn only a fraction of one model-level vote:

| Distinct resolved events | Quality requirement | Weight |
|---:|---|---:|
| `< 20` | any | `0` |
| `20-49` | Brier `<= 0.24`, hit rate `>= 0.52` | `0.10` |
| `50-99` | Brier `<= 0.22`, hit rate `>= 0.55` | `0.25` |
| `>= 100` | Brier `<= 0.20`, hit rate `>= 0.58` | `0.50` |

Additional rules:

- Maximum LLM weight is `0.50`; it never receives a full model vote in v1.
- Brier `> 0.27`, malformed rate `> 10%`, or stale signal resets weight to `0`.
- Weight is computed, not manually claimed from `confidence`.
- Existing numerical models and NOAA/Google weights remain unchanged.
- Do not add an environment switch for manually forcing a nonzero weight.

### 5. Apply the vote only at the existing pricing boundary

Extend `analyze_global_bucket_price` with an optional external probability and
weight input. Preserve current behavior exactly when weight is zero or absent.

Use a weighted mean across model-level probabilities. Do not fake LLM ensemble
members and do not multiply one LLM answer into many synthetic votes.

The pricing reasons and forecast raw payload must show:

- LLM probability;
- effective weight;
- resolved distinct-event count;
- Brier/hit rate;
- whether it affected pricing or remained calibration-only.

### 6. Control cost and latency

- At most one new group-level LLM call per tick.
- Prefer a group that has no signal for the current forecast revision.
- Cache/dedupe through persisted signal identity, not process memory only.
- LLM timeout/failure cannot consume the whole Autopilot research budget.
- Do not send private keys, wallet addresses, credentials, balances, positions,
  order IDs, or account PnL to the LLM.

## Tests Required

At minimum:

1. valid sibling distribution parses and persists;
2. unknown/duplicate/missing market ID rejects the whole response;
3. sum outside tolerance rejects;
4. timeout leaves quant analysis unchanged;
5. restart/retry does not duplicate the same event/forecast sample;
6. settlement resolves LLM model signals;
7. fewer than 20 distinct events gives weight zero;
8. each weight tier and weak reset is tested;
9. 100 sibling rows from one event do not count as 100 events;
10. weight zero is byte-for-byte/field-for-field equivalent to old pricing;
11. weight 0.25 changes weighted fair probability by the correct amount;
12. LLM never changes reconciliation, risk, idempotency, BUY or SELL ownership;
13. `/calibration` or `/app` shows score, sample count and effective weight;
14. prompt payload contains no account or credential fields.

## Stop Conditions

Stop and report instead of proceeding if:

- a new table or second strategy/LLM/calibration service appears necessary;
- the LLM would need to call BUY/SELL code;
- an invalid or unavailable LLM response could block quant execution;
- the implementation needs a manually forced positive weight;
- real-money testing appears necessary.

## Verification

```bash
uv run pytest tests/test_llm_advisor.py tests/test_autopilot_service.py \
  tests/test_strategy_v2.py tests/test_settlement_service.py -q
uv run ruff check src/ tests/
MAX_ORDER_USDC=1 MAX_DAILY_USDC=5 MAX_MARKET_USDC=2 uv run pytest -q
git diff --check
git status --short
```

No real BUY/SELL, no daemon restart, no force-push. Commit and normal-push only
after all gates pass. The report must follow `docs/agent-worker-standards.md`.

