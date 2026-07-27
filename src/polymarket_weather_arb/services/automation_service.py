from __future__ import annotations

import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from sqlite3 import Row
from typing import Callable, Protocol

from polymarket_weather_arb.adapters.polymarket.client import GammaPolymarketClient
from polymarket_weather_arb.adapters.weather.open_meteo import OpenMeteoProvider
from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.services.market_workflow_service import MarketWorkflowService
from polymarket_weather_arb.storage.repositories import Repository

ALLOWED_ACTION_KINDS = {"dry_run", "refresh_weather", "analyze", "trade_live"}
ACTION_ID_PREFIX = "act_"


def summarize_workflow_result(result, *, max_chars: int = 4000) -> str:
    lines = [result.summary, *[f"- {detail}" for detail in result.details]]
    summary = "\n".join(line for line in lines if line).strip() or "workflow completed"
    if len(summary) > max_chars:
        return summary[: max_chars - 3] + "..."
    return summary


class CommandRunner(Protocol):
    def __call__(
        self, argv: list[str], *, cwd: str, timeout: int
    ) -> subprocess.CompletedProcess[str]: ...


WorkflowFactory = Callable[[Repository], MarketWorkflowService]


@dataclass(frozen=True)
class AutomationAction:
    id: str
    kind: str
    market_id: str
    reason: str | None
    command_preview: str
    idempotency_key: str | None
    requested_by: str | None
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class OperatorSuggestion:
    label: str
    reason: str
    command: str
    action_id: str | None = None
    market_id: str | None = None


class AutomationService:
    def __init__(
        self,
        repository: Repository,
        *,
        project_root: str = str(Path(__file__).resolve().parents[3]),
        runner: CommandRunner | None = None,
        workflow_factory: WorkflowFactory | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.repository = repository
        self.project_root = project_root
        self.runner = runner or subprocess.run
        self.workflow_factory = workflow_factory
        self.settings = settings or Settings()

    def propose(
        self,
        *,
        kind: str,
        market_id: str,
        reason: str | None = None,
        ttl_minutes: int | None = None,
        requested_by: str | None = None,
        idempotency_key: str | None = None,
    ) -> Row:
        normalized_kind = normalize_kind(kind)
        if self.repository.get_market(market_id) is None:
            raise ValueError(f"unknown market: {market_id}")
        now = datetime.now(timezone.utc)
        ttl = ttl_minutes if ttl_minutes is not None else default_ttl_minutes(normalized_kind)
        if ttl <= 0:
            raise ValueError("ttl_minutes must be positive")
        action = AutomationAction(
            id=f"{ACTION_ID_PREFIX}{uuid.uuid4().hex[:16]}",
            kind=normalized_kind,
            market_id=market_id,
            reason=reason,
            command_preview=" ".join(action_argv(normalized_kind, market_id)),
            idempotency_key=idempotency_key,
            requested_by=requested_by,
            created_at=now,
            expires_at=now + timedelta(minutes=ttl),
        )
        return self.repository.create_automation_action(action)

    def approve(self, action_id: str, actor: str) -> Row:
        validate_action_id(action_id)
        now = _now_iso()
        self.repository.expire_automation_actions(now)
        action = self.repository.get_automation_action(action_id)
        if action is None:
            raise ValueError(f"unknown action: {action_id}")
        if action["status"] == "approved":
            return action
        if action["status"] != "pending":
            return action
        self.repository.approve_automation_action(action_id, actor, now)
        return self.repository.get_automation_action(action_id) or action

    def reject(self, action_id: str, actor: str, reason: str | None = None) -> Row:
        validate_action_id(action_id)
        now = _now_iso()
        self.repository.expire_automation_actions(now)
        action = self.repository.get_automation_action(action_id)
        if action is None:
            raise ValueError(f"unknown action: {action_id}")
        if action["status"] == "rejected":
            return action
        if action["status"] != "pending":
            return action
        self.repository.reject_automation_action(action_id, actor, reason, now)
        return self.repository.get_automation_action(action_id) or action

    def status(self, action_id: str) -> Row:
        validate_action_id(action_id)
        self.repository.expire_automation_actions(_now_iso())
        action = self.repository.get_automation_action(action_id)
        if action is None:
            raise ValueError(f"unknown action: {action_id}")
        return action

    def execute(self, action_id: str) -> Row:
        validate_action_id(action_id)
        now = _now_iso()
        action = self.repository.claim_approved_automation_action(action_id, now)
        if action is None:
            existing = self.repository.get_automation_action(action_id)
            if existing is None:
                raise ValueError(f"unknown action: {action_id}")
            return existing
        return self._run_claimed(action)

    def execute_approved(self, *, limit: int = 1) -> list[Row]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        results = []
        for _ in range(limit):
            action = self.repository.claim_next_approved_automation_action(_now_iso())
            if action is None:
                break
            results.append(self._run_claimed(action))
        return results

    def list_actions(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        market_id: str | None = None,
        limit: int = 20,
        expire: bool = True,
    ) -> list[Row]:
        if expire:
            self.repository.expire_automation_actions(_now_iso())
        normalized_kind = normalize_kind(kind) if kind else None
        return self.repository.list_automation_actions(
            limit=limit, status=status, kind=normalized_kind, market_id=market_id
        )

    def propose_next(
        self,
        *,
        kind: str,
        reason: str | None = None,
        ttl_minutes: int | None = None,
        requested_by: str | None = "operator",
    ) -> Row:
        candidate = self.repository.next_dry_run_ready_candidate()
        if candidate is None:
            raise ValueError(
                "no dry_run_ready candidate found; run discover-markets or load the demo fixture"
            )
        return self.propose(
            kind=kind,
            market_id=candidate["market_id"],
            reason=reason,
            ttl_minutes=ttl_minutes,
            requested_by=requested_by,
        )

    def suggest_next_action(self, *, expire: bool = True) -> OperatorSuggestion:
        if expire:
            self.repository.expire_automation_actions(_now_iso())
        approved = self.repository.latest_action_by_status("approved")
        if approved:
            return OperatorSuggestion(
                label="run-approved",
                reason=f"approved {approved['kind']} action is waiting",
                command="uv run polymarket-weather operator run-approved --limit 1",
                action_id=approved["id"],
                market_id=approved["market_id"],
            )
        pending = self.repository.latest_action_by_status("pending")
        if pending:
            return OperatorSuggestion(
                label="approve-or-reject",
                reason=f"pending {pending['kind']} action needs human review",
                command=f"/wufu action-approve action-id:{pending['id']}",
                action_id=pending["id"],
                market_id=pending["market_id"],
            )
        failed = self.repository.latest_failed_action()
        if failed:
            return OperatorSuggestion(
                label="inspect-failed",
                reason=f"latest action failed: {summarize_output(failed['failure_reason'] or '', '')}",
                command=f"uv run polymarket-weather automation status --action-id {failed['id']}",
                action_id=failed["id"],
                market_id=failed["market_id"],
            )
        candidate = self.repository.next_dry_run_ready_candidate()
        if candidate:
            market_id = candidate["market_id"]
            return OperatorSuggestion(
                label="propose-next",
                reason=f"candidate {market_id} is dry_run_ready",
                command="uv run polymarket-weather operator propose-next --kind analyze",
                market_id=market_id,
            )
        return OperatorSuggestion(
            label="discover",
            reason="no ready candidates or pending actions found",
            command="uv run polymarket-weather discover-markets --limit 50 --pages 1",
        )

    def _run_claimed(self, action: Row) -> Row:
        self.repository.connection.commit()
        if action["kind"] != "trade_live":
            return self._run_claimed_non_live(action)

        cb_state = self.repository.get_circuit_breaker_state()
        if cb_state and cb_state["circuit_breaker_tripped"]:
            self.repository.mark_automation_action_failed(
                action["id"],
                None,
                f"Circuit breaker is tripped: {cb_state['circuit_breaker_reason']}",
                _now_iso(),
                0,
            )
            return self.repository.get_automation_action(action["id"]) or action

        argv = [
            "uv",
            "run",
            "polymarket-weather",
            *action_argv(action["kind"], action["market_id"]),
        ]
        started_at = _now_iso()
        run_id = self.repository.start_run(" ".join(argv))
        self.repository.mark_automation_action_executing(action["id"], argv, started_at)
        self.repository.connection.commit()
        started = time.monotonic()
        try:
            completed = self.runner(
                argv, cwd=self.project_root, timeout=600, capture_output=True, text=True
            )
        except subprocess.TimeoutExpired as exc:
            duration_ms = _duration_ms(started)
            self.repository.mark_automation_action_failed(
                action["id"], None, f"timed out after {exc.timeout}s", _now_iso(), duration_ms
            )
            self.repository.finish_run(run_id, "failed", f"timed out after {exc.timeout}s")
            return self.repository.get_automation_action(action["id"]) or action
        duration_ms = _duration_ms(started)
        output = summarize_output(completed.stdout, completed.stderr)
        if completed.returncode == 0:
            self.repository.mark_automation_action_executed(
                action["id"], completed.returncode, output, _now_iso(), duration_ms
            )
            self.repository.finish_run(run_id, "ok")
        else:
            self.repository.mark_automation_action_failed(
                action["id"], completed.returncode, output, _now_iso(), duration_ms
            )
            self.repository.finish_run(run_id, "failed", output)
        return self.repository.get_automation_action(action["id"]) or action

    def _run_claimed_non_live(self, action: Row) -> Row:
        argv = action_argv(action["kind"], action["market_id"])
        started_at = _now_iso()
        run_id = self.repository.start_run("service:" + " ".join(argv))
        self.repository.mark_automation_action_executing(
            action["id"], ["service", *argv], started_at
        )
        self.repository.connection.commit()
        started = time.monotonic()
        try:
            result = self._execute_workflow_action(action["kind"], action["market_id"])
        except Exception as exc:
            duration_ms = _duration_ms(started)
            self.repository.mark_automation_action_failed(
                action["id"], None, str(exc), _now_iso(), duration_ms
            )
            self.repository.finish_run(run_id, "failed", str(exc))
            return self.repository.get_automation_action(action["id"]) or action
        duration_ms = _duration_ms(started)
        output = summarize_workflow_result(result)
        self.repository.mark_automation_action_executed(
            action["id"], 0, output, _now_iso(), duration_ms
        )
        self.repository.finish_run(run_id, "ok")
        return self.repository.get_automation_action(action["id"]) or action

    def _execute_workflow_action(self, kind: str, market_id: str):
        workflow = self._workflow()
        if kind == "refresh_weather":
            return workflow.refresh_weather(market_id)
        if kind == "analyze":
            return workflow.analyze(market_id)
        if kind == "dry_run":
            if self.repository.latest_analysis(market_id) is None:
                workflow.research_market(market_id)
            return workflow.dry_run_trade(market_id)
        raise ValueError(f"unsupported non-live automation action kind: {kind}")

    def _workflow(self) -> MarketWorkflowService:
        if self.workflow_factory is not None:
            return self.workflow_factory(self.repository)
        return MarketWorkflowService(
            self.settings,
            self.repository,
            weather_provider_factory=OpenMeteoProvider,
            polymarket_client_factory=GammaPolymarketClient,
        )


def normalize_kind(kind: str) -> str:
    normalized = kind.replace("-", "_")
    if normalized == "trade_review":
        normalized = "trade_live"
    if normalized not in ALLOWED_ACTION_KINDS:
        allowed = ", ".join(sorted(ALLOWED_ACTION_KINDS))
        raise ValueError(f"unsupported automation action kind: {kind}; allowed: {allowed}")
    return normalized


def action_argv(kind: str, market_id: str) -> list[str]:
    normalized_kind = normalize_kind(kind)
    if normalized_kind == "dry_run":
        return ["trade", "--market", market_id, "--dry-run"]
    if normalized_kind == "refresh_weather":
        return ["refresh-weather", "--market", market_id]
    if normalized_kind == "analyze":
        return ["analyze", "--market", market_id]
    if normalized_kind == "trade_live":
        return ["trade", "--market", market_id]
    raise ValueError(f"unsupported automation action kind: {kind}")


def default_ttl_minutes(kind: str) -> int:
    return 10 if normalize_kind(kind) == "trade_live" else 60


def validate_action_id(action_id: str) -> None:
    if not action_id.startswith(ACTION_ID_PREFIX):
        raise ValueError("invalid action_id")
    suffix = action_id[len(ACTION_ID_PREFIX) :]
    if (
        len(suffix) < 8
        or len(suffix) > 64
        or not all(char in "0123456789abcdef" for char in suffix)
    ):
        raise ValueError("invalid action_id")


def summarize_output(stdout: str | None, stderr: str | None, *, max_chars: int = 4000) -> str:
    parts = []
    if stdout and stdout.strip():
        parts.append(stdout.strip())
    if stderr and stderr.strip():
        parts.append(stderr.strip())
    summary = "\n".join(parts) or "no output"
    if len(summary) > max_chars:
        return summary[: max_chars - 3] + "..."
    return summary


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _duration_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
