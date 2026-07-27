from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

ALLOWED_LLM_ACTIONS = frozenset({"buy_yes", "buy_no", "skip"})
LLM_WEATHER_MODEL_VERSION = "llm-weather-vote-v1"


@dataclass(frozen=True)
class LlmTradeDecision:
    action: str
    confidence: Decimal
    reason: str
    provider: str
    model: str
    raw_response: str | None = None

    def __post_init__(self) -> None:
        if self.action not in ALLOWED_LLM_ACTIONS:
            raise ValueError(f"unsupported llm action: {self.action}")
        if self.confidence < 0 or self.confidence > 1:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True)
class LlmGroupDecision:
    bucket_probabilities: dict[str, Decimal]
    other_probability: Decimal
    confidence: Decimal
    reason: str
    provider: str
    model: str
    raw_response: str | None = None
    decision: str = "advisory"
    distribution_total: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if self.decision in ("error", "invalid"):
            return
        if self.confidence < 0 or self.confidence > 1:
            raise ValueError("confidence must be between 0 and 1")
        if self.other_probability < 0 or self.other_probability > 1:
            raise ValueError("other_probability must be between 0 and 1")
        total = self.other_probability
        for prob in self.bucket_probabilities.values():
            if prob < 0 or prob > 1:
                raise ValueError("bucket probability must be between 0 and 1")
            total += prob
        if not (Decimal("0.98") <= total <= Decimal("1.02")):
            raise ValueError(f"probabilities must sum to 1.0 (got {total})")
        object.__setattr__(self, "distribution_total", total)
