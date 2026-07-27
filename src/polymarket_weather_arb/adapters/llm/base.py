from __future__ import annotations

from typing import Protocol


class LlmClient(Protocol):
    provider: str
    model: str

    def complete_json(self, *, system: str, user: str) -> dict[str, object]: ...
