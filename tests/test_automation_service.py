import json
from types import SimpleNamespace

import pytest

from polymarket_weather_arb.services.automation_service import (
    AutomationService,
    action_argv,
    normalize_kind,
)
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


def open_repo(tmp_path):
    database = Database(tmp_path / "test.db")
    database.init_schema()
    connection = database.connect()
    return database, connection, Repository(connection)


def seed_market(repo):
    repo.upsert_market(
        SimpleNamespace(
            id="m1",
            slug="m1",
            title="Test market",
            description="NOAA station KNYC",
            event_slug=None,
            event_title=None,
            category=None,
            tags=(),
            yes_token_id="yes-token",
            no_token_id="no-token",
            close_time=None,
            status="active",
            is_weather=True,
        ),
        {"id": "m1"},
    )


class RecordingRunner:
    def __init__(self, returncode=0, stdout="ok", stderr=""):
        self.calls = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, argv, *, cwd, timeout, capture_output, text):
        self.calls.append(
            {
                "argv": argv,
                "cwd": cwd,
                "timeout": timeout,
                "capture_output": capture_output,
                "text": text,
            }
        )
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout, stderr=self.stderr)


class RecordingWorkflow:
    def __init__(self, repo, *, observe_transaction=None):
        self.repo = repo
        self.observe_transaction = observe_transaction
        self.calls = []

    def _record(self, name, market_id):
        if self.observe_transaction is not None:
            self.observe_transaction.append(self.repo.connection.in_transaction)
        self.calls.append((name, market_id))

    def refresh_weather(self, market_id):
        self._record("refresh_weather", market_id)
        return SimpleNamespace(market_id=market_id, summary="weather refreshed", details=[])

    def analyze(self, market_id):
        self._record("analyze", market_id)
        return SimpleNamespace(market_id=market_id, summary="analyzed", details=[])

    def research_market(self, market_id):
        self._record("research_market", market_id)
        return SimpleNamespace(market_id=market_id, summary="researched", details=[])

    def dry_run_trade(self, market_id):
        self._record("dry_run_trade", market_id)
        return SimpleNamespace(market_id=market_id, summary="dry run recorded", details=["ok"])


class RecordingWorkflowFactory:
    def __init__(self, *, observe_transaction=None):
        self.observe_transaction = observe_transaction
        self.workflows = []

    def __call__(self, repo):
        workflow = RecordingWorkflow(repo, observe_transaction=self.observe_transaction)
        self.workflows.append(workflow)
        return workflow


def test_claim_commits_before_running_workflow_action(tmp_path):
    _, connection, repo = open_repo(tmp_path)
    try:
        seed_market(repo)
        observed = []
        workflow_factory = RecordingWorkflowFactory(observe_transaction=observed)

        service = AutomationService(repo, workflow_factory=workflow_factory)
        action = service.propose(kind="analyze", market_id="m1")
        service.approve(action["id"], "user-1")

        executed = service.execute(action["id"])

        assert executed["status"] == "executed"
        assert observed == [False]
        assert workflow_factory.workflows[0].calls == [("analyze", "m1")]
    finally:
        connection.close()


def test_propose_and_approve_are_durable_and_idempotent(tmp_path):
    _, connection, repo = open_repo(tmp_path)
    try:
        seed_market(repo)
        service = AutomationService(repo)

        action = service.propose(
            kind="dry-run", market_id="m1", reason="good edge", idempotency_key="same-action"
        )
        duplicate = service.propose(
            kind="dry_run", market_id="m1", reason="good edge", idempotency_key="same-action"
        )
        approved = service.approve(action["id"], "user-1")
        approved_again = service.approve(action["id"], "user-1")
        connection.commit()

        assert action["id"] == duplicate["id"]
        assert approved["status"] == "approved"
        assert approved_again["status"] == "approved"
        events = [row["event"] for row in repo.list_automation_audit_events(action["id"])]
        assert events == ["created", "approved"]
    finally:
        connection.close()


def test_reject_only_pending_action(tmp_path):
    _, connection, repo = open_repo(tmp_path)
    try:
        seed_market(repo)
        service = AutomationService(repo)
        action = service.propose(kind="analyze", market_id="m1")

        rejected = service.reject(action["id"], "user-1", "not now")
        approved_after_reject = service.approve(action["id"], "user-1")
        connection.commit()

        assert rejected["status"] == "rejected"
        assert approved_after_reject["status"] == "rejected"
    finally:
        connection.close()


def test_expired_action_cannot_be_approved_or_executed(tmp_path):
    _, connection, repo = open_repo(tmp_path)
    try:
        seed_market(repo)
        service = AutomationService(repo)
        action = service.propose(kind="dry_run", market_id="m1", ttl_minutes=1)
        repo.connection.execute(
            "UPDATE automation_actions SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (action["id"],),
        )

        approved = service.approve(action["id"], "user-1")
        executed = service.execute(action["id"])
        connection.commit()

        assert approved["status"] == "expired"
        assert executed["status"] == "expired"
    finally:
        connection.close()


def test_approved_action_expires_before_execution(tmp_path):
    _, connection, repo = open_repo(tmp_path)
    try:
        seed_market(repo)
        runner = RecordingRunner()
        service = AutomationService(repo, runner=runner)
        action = service.propose(kind="dry_run", market_id="m1", ttl_minutes=1)
        service.approve(action["id"], "user-1")
        repo.connection.execute(
            "UPDATE automation_actions SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (action["id"],),
        )

        executed = service.execute(action["id"])
        connection.commit()

        assert executed["status"] == "expired"
        assert runner.calls == []
    finally:
        connection.close()


def test_execute_uses_internal_workflow_for_non_live_action_and_is_idempotent(tmp_path):
    _, connection, repo = open_repo(tmp_path)
    try:
        seed_market(repo)
        runner = RecordingRunner(returncode=0, stdout="dry-run order recorded")
        workflow_factory = RecordingWorkflowFactory()
        service = AutomationService(repo, runner=runner, workflow_factory=workflow_factory)
        action = service.propose(kind="dry_run", market_id="m1")
        service.approve(action["id"], "user-1")

        executed = service.execute(action["id"])
        executed_again = service.execute(action["id"])
        connection.commit()

        assert executed["status"] == "executed"
        assert executed_again["status"] == "executed"
        assert runner.calls == []
        assert len(workflow_factory.workflows) == 1
        assert workflow_factory.workflows[0].calls == [
            ("research_market", "m1"),
            ("dry_run_trade", "m1"),
        ]
        assert executed["result_summary"] == "dry run recorded\n- ok"
        assert json.loads(executed["execution_argv"]) == [
            "service",
            "trade",
            "--market",
            "m1",
            "--dry-run",
        ]
    finally:
        connection.close()


def test_live_trade_failure_is_recorded_without_bypassing_cli(tmp_path):
    _, connection, repo = open_repo(tmp_path)
    try:
        seed_market(repo)
        runner = RecordingRunner(returncode=1, stdout="", stderr="reconciliation state is stale")
        service = AutomationService(repo, runner=runner)
        action = service.propose(kind="trade_live", market_id="m1")
        service.approve(action["id"], "user-1")

        failed = service.execute_approved()[0]
        connection.commit()

        assert failed["status"] == "failed"
        assert failed["return_code"] == 1
        assert "reconciliation state is stale" in failed["failure_reason"]
        assert runner.calls[0]["argv"] == [
            "uv",
            "run",
            "polymarket-weather",
            "trade",
            "--market",
            "m1",
        ]
    finally:
        connection.close()


def test_unsupported_actions_and_malformed_ids_are_rejected(tmp_path):
    _, connection, repo = open_repo(tmp_path)
    try:
        seed_market(repo)
        service = AutomationService(repo)

        with pytest.raises(ValueError, match="unsupported automation action kind"):
            service.propose(kind="shell", market_id="m1")
        with pytest.raises(ValueError, match="invalid action_id"):
            service.approve("bad;rm -rf", "user-1")
    finally:
        connection.close()


def test_suggest_next_action_prefers_approved_action(tmp_path):
    _, connection, repo = open_repo(tmp_path)
    try:
        seed_market(repo)
        service = AutomationService(repo)
        action = service.propose(kind="dry_run", market_id="m1")
        service.approve(action["id"], "user-1")

        suggestion = service.suggest_next_action()

        assert suggestion.label == "run-approved"
        assert suggestion.action_id == action["id"]
        assert "<" not in suggestion.command
        assert "operator run-approved" in suggestion.command
    finally:
        connection.close()


def test_propose_next_uses_real_candidate_market_id(tmp_path):
    _, connection, repo = open_repo(tmp_path)
    try:
        seed_market(repo)
        repo.connection.execute(
            """
            INSERT INTO market_candidates (
                market_id, status, tradable, rejection_reason, best_bid, best_ask, spread,
                snapshot_fetched_at, rule_updated_at, notes, updated_at
            ) VALUES ('m1', 'dry_run_ready', 1, NULL, 0.1, 0.2, 0.1, NULL, CURRENT_TIMESTAMP, NULL, CURRENT_TIMESTAMP)
            """
        )
        service = AutomationService(repo)

        suggestion = service.suggest_next_action()
        action = service.propose_next(kind="analyze")

        assert suggestion.label == "propose-next"
        assert suggestion.market_id == "m1"
        assert "<" not in suggestion.command
        assert action["market_id"] == "m1"
        assert action["status"] == "pending"
    finally:
        connection.close()

    assert normalize_kind("dry-run") == "dry_run"
    assert normalize_kind("trade-review") == "trade_live"
    assert action_argv("refresh-weather", "m1") == ["refresh-weather", "--market", "m1"]


def test_execute_approved_trade_live_blocked_by_breaker(tmp_path):
    _, connection, repo = open_repo(tmp_path)
    try:
        seed_market(repo)
        repo.trip_circuit_breaker(reason="test breaker")

        service = AutomationService(repo)

        action = service.propose(kind="trade_live", market_id="m1")
        from polymarket_weather_arb.services.automation_service import _now_iso

        repo.approve_automation_action(action["id"], actor="tester", now=_now_iso())

        results = service.execute_approved()

        assert len(results) == 1
        result = results[0]
        assert result["status"] == "failed"
        assert "Circuit breaker is tripped: test breaker" in result["failure_reason"]
    finally:
        connection.close()
