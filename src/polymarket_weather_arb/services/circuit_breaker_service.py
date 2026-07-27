from dataclasses import dataclass

from polymarket_weather_arb.storage.repositories import Repository


@dataclass(frozen=True)
class CircuitBreakerStatus:
    tripped: bool
    reason: str | None
    tripped_at: str | None


class CircuitBreakerService:
    def __init__(self, repository: Repository):
        self.repository = repository

    def status(self) -> CircuitBreakerStatus:
        row = self.repository.get_circuit_breaker_state()
        return CircuitBreakerStatus(
            tripped=bool(row["circuit_breaker_tripped"]),
            reason=row["circuit_breaker_reason"],
            tripped_at=row["tripped_at"],
        )

    def trip(self, reason: str, by: str = "system", audit_id: int | None = None) -> None:
        self.repository.trip_circuit_breaker(reason, by, audit_id)

    def clear(self, by: str, note: str) -> None:
        if not note or not note.strip():
            raise ValueError("Circuit breaker clear note cannot be empty")
        self.repository.clear_circuit_breaker(by, note)


def live_execution_blocked(repository: Repository) -> str | None:
    row = repository.get_circuit_breaker_state()
    if row and row["circuit_breaker_tripped"]:
        reason = row["circuit_breaker_reason"] or "unknown reason"
        return f"Circuit breaker tripped: {reason}"
    return None
