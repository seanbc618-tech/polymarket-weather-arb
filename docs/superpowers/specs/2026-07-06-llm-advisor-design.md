# LLM Advisory Review Design (Phase 2)

Date: 2026-07-06

## Goal

Add a pluggable, non-blocking LLM review on top of deterministic weather signals.
The review can express `buy_yes`, `buy_no`, or `skip`, but the quantitative model
owns execution decisions and hard invariants remain in code.

## Provider architecture

Two adapter families:

1. **OpenAI-compatible** (`/v1/chat/completions` + JSON response)
   - OpenAI, DeepSeek, Grok/xAI, OpenRouter, custom gateways

2. **Anthropic Messages API** (`/v1/messages`)
   - Native Claude endpoint

Selection via `.env`:

```env
LLM_ENABLED=true
LLM_PROVIDER=deepseek   # openai|anthropic|deepseek|grok|openrouter|custom
LLM_API_KEY=...
LLM_MODEL=deepseek-chat # optional; provider preset supplies default
LLM_API_BASE=           # optional override
LLM_MIN_CONFIDENCE=0.6
```

## Execution flow

1. Quant model finds edge >= MIN_EDGE
2. If LLM enabled, build context payload (rule, order book, forecast, calibration)
3. LLM returns JSON `{action, confidence, reason}` as an independent review
4. Persist and display the review in the existing Autopilot decision feed
5. Proceed through the existing quantitative dry-run/live path regardless of LLM opinion
6. If the LLM is unavailable, record that fact and continue the quantitative path

## Safety

- LLM cannot place, block, resize, or redirect an order
- LLM cannot bypass risk engine, compliance, reconciliation, or kill switch
- LLM disabled or missing API key => quant-only mode
