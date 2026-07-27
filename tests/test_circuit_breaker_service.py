import pytest

from polymarket_weather_arb.services.circuit_breaker_service import CircuitBreakerService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


from pathlib import Path


@pytest.fixture
def circuit_breaker_service(tmp_path: Path):
    db = Database(path=tmp_path / "test.db")
    db.init_schema()
    with db.connect() as conn:
        repo = Repository(conn)
        yield CircuitBreakerService(repo)


def test_circuit_breaker_initial_status(circuit_breaker_service):
    status = circuit_breaker_service.status()
    assert not status.tripped
    assert status.reason is None
    assert status.tripped_at is None


def test_circuit_breaker_trip(circuit_breaker_service):
    circuit_breaker_service.trip("Resolution mismatch")
    status = circuit_breaker_service.status()
    assert status.tripped
    assert status.reason == "Resolution mismatch"
    assert status.tripped_at is not None


def test_circuit_breaker_clear(circuit_breaker_service):
    circuit_breaker_service.trip("Testing")
    circuit_breaker_service.clear("test_user", "fixed bug")
    status = circuit_breaker_service.status()
    assert not status.tripped
    assert status.reason is None
    assert status.tripped_at is None

    row = circuit_breaker_service.repository.get_circuit_breaker_state()
    assert row["clear_note"] == "fixed bug"
    assert row["cleared_by"] == "test_user"
    assert row["cleared_at"] is not None


def test_circuit_breaker_clear_empty_note_raises(circuit_breaker_service):
    circuit_breaker_service.trip("Testing")
    with pytest.raises(ValueError, match="Circuit breaker clear note cannot be empty"):
        circuit_breaker_service.clear("test_user", "   ")
