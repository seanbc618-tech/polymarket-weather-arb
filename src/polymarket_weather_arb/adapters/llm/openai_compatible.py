from __future__ import annotations

import json
import re
from typing import Any


from polymarket_weather_arb.adapters.http_client import build_httpx_client


class OpenAICompatibleClient:
    """OpenAI Chat Completions API client.

    Works with OpenAI, DeepSeek, Grok/xAI, OpenRouter, and other
    providers that expose the same /v1/chat/completions schema.
    """

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
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        with build_httpx_client(timeout=self._timeout_seconds) as client:
            response = client.post(
                f"{self._api_base}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        content = data["choices"][0]["message"]["content"]
        return _parse_json_object(content)


def _parse_json_object(content: str) -> dict[str, object]:
    text = content.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match is None:
            raise ValueError("llm response did not contain json object") from None
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("llm response json must be an object")
    return parsed
