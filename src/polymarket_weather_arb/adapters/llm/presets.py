from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LlmProviderPreset:
    id: str
    label: str
    adapter: str
    api_base: str | None
    default_model: str


PROVIDER_PRESETS: dict[str, LlmProviderPreset] = {
    "openai": LlmProviderPreset(
        id="openai",
        label="OpenAI",
        adapter="openai_compatible",
        api_base="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
    ),
    "anthropic": LlmProviderPreset(
        id="anthropic",
        label="Anthropic",
        adapter="anthropic_messages",
        api_base="https://api.anthropic.com",
        default_model="claude-sonnet-4-20250514",
    ),
    "deepseek": LlmProviderPreset(
        id="deepseek",
        label="DeepSeek",
        adapter="openai_compatible",
        api_base="https://api.deepseek.com/v1",
        default_model="deepseek-chat",
    ),
    "grok": LlmProviderPreset(
        id="grok",
        label="Grok (xAI)",
        adapter="openai_compatible",
        api_base="https://api.x.ai/v1",
        default_model="grok-2-latest",
    ),
    "openrouter": LlmProviderPreset(
        id="openrouter",
        label="OpenRouter",
        adapter="openai_compatible",
        api_base="https://openrouter.ai/api/v1",
        default_model="openai/gpt-4o-mini",
    ),
    "custom": LlmProviderPreset(
        id="custom",
        label="Custom OpenAI-compatible",
        adapter="openai_compatible",
        api_base=None,
        default_model="",
    ),
}


def get_provider_preset(provider_id: str) -> LlmProviderPreset:
    try:
        return PROVIDER_PRESETS[provider_id]
    except KeyError as exc:
        allowed = ", ".join(sorted(PROVIDER_PRESETS))
        raise ValueError(f"unknown llm provider: {provider_id}; allowed: {allowed}") from exc
