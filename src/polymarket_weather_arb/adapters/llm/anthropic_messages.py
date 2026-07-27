from __future__ import annotations

from typing import Any


from polymarket_weather_arb.adapters.http_client import build_httpx_client

from polymarket_weather_arb.adapters.llm.openai_compatible import _parse_json_object


class AnthropicMessagesClient:
    """Anthropic Messages API client."""

    def __init__(
        self,
        *,
        provider: str,
        api_base: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.provider = provider
        self.model = model
        self._api_base = api_base.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    def complete_json(self, *, system: str, user: str) -> dict[str, object]:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 1024,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "temperature": 0.1,
        }
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        with build_httpx_client(timeout=self._timeout_seconds) as client:
            response = client.post(
                f"{self._api_base}/v1/messages",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        parts = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        content = "\n".join(parts).strip()
        if not content:
            raise ValueError("anthropic response did not contain text content")
        return _parse_json_object(content)
