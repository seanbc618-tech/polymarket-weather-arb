from __future__ import annotations

from polymarket_weather_arb.adapters.llm.anthropic_messages import AnthropicMessagesClient
from polymarket_weather_arb.adapters.llm.base import LlmClient
from polymarket_weather_arb.adapters.llm.openai_compatible import OpenAICompatibleClient
from polymarket_weather_arb.adapters.llm.presets import get_provider_preset
from polymarket_weather_arb.config import Settings


def build_llm_client(settings: Settings) -> LlmClient | None:
    if not settings.llm_enabled:
        return None
    if not settings.llm_api_key:
        return None
    preset = get_provider_preset(settings.llm_provider)
    api_base = settings.llm_api_base or preset.api_base
    if not api_base:
        raise ValueError("LLM_API_BASE is required for custom provider")
    model = settings.llm_model or preset.default_model
    if not model:
        raise ValueError("LLM_MODEL is required")
    if preset.adapter == "anthropic_messages":
        return AnthropicMessagesClient(
            provider=preset.id,
            api_base=api_base,
            api_key=settings.llm_api_key,
            model=model,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    return OpenAICompatibleClient(
        provider=preset.id,
        api_base=api_base,
        api_key=settings.llm_api_key,
        model=model,
        timeout_seconds=settings.llm_timeout_seconds,
    )


def llm_runtime_label(settings: Settings) -> str:
    if not settings.llm_enabled:
        return "disabled"
    if not settings.llm_api_key:
        return "missing_api_key"
    preset = get_provider_preset(settings.llm_provider)
    model = settings.llm_model or preset.default_model or "unset"
    return f"{preset.id}/{model}"
