from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COORDINATOR_DIR = Path(
    os.environ.get("AGENT_COORDINATOR_DIR", Path.home() / "agent-discussion-coordinator")
)
STATE_PATH = ROOT / "data" / "dashboard_notify_state.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Post read-only project notifications to Discord dashboards."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    tests = subcommands.add_parser("tests", help="Run pytest and notify the reviewer dashboard.")
    tests.add_argument("--extra-args", nargs=argparse.REMAINDER, default=[])

    discovery = subcommands.add_parser(
        "discovery", help="Run read-only discovery and notify the scanner dashboard."
    )
    discovery.add_argument("--limit", default="100")
    discovery.add_argument("--pages", default="3")

    subcommands.add_parser("risk", help="Run risk-report and notify the risk dashboard.")

    subcommands.add_parser(
        "queue", help="Post the current automation queue to the captain dashboard."
    )

    subcommands.add_parser("reconciliation", help="Run reconcile and notify the risk dashboard.")

    propose = subcommands.add_parser("propose", help="Send a read-only action proposal card.")
    propose.add_argument(
        "action", choices=["dry-run", "refresh-weather", "analyze", "trade-review"]
    )
    propose.add_argument("--market", required=True)
    propose.add_argument("--reason", default=None)
    propose.add_argument("--ttl-minutes", default=None)

    tick = subcommands.add_parser(
        "tick", help="Run one scheduled dashboard tick with duplicate suppression."
    )
    tick.add_argument("--limit", default="100")
    tick.add_argument("--pages", default="3")
    tick.add_argument("--include-tests", action="store_true")
    tick.add_argument("--include-reconciliation", action="store_true")
    tick.add_argument("--force", action="store_true", help="Send even if payload is unchanged.")

    daemon = subcommands.add_parser(
        "daemon", help="Post a daemon event JSON payload with duplicate suppression."
    )
    daemon.add_argument("--payload-file", default="-", help="JSON payload path, or stdin with -.")
    daemon.add_argument("--force", action="store_true", help="Send even if payload is unchanged.")

    args = parser.parse_args()
    if args.command == "tests":
        return notify_tests(args.extra_args)
    if args.command == "discovery":
        return notify_discovery(args.limit, args.pages)
    if args.command == "risk":
        return notify_risk()
    if args.command == "queue":
        return notify_queue()
    if args.command == "reconciliation":
        return notify_reconciliation()
    if args.command == "propose":
        notify(build_proposal_payload(args.action, args.market, args.reason, args.ttl_minutes))
        return 0
    if args.command == "tick":
        return notify_tick(
            limit=args.limit,
            pages=args.pages,
            include_tests=args.include_tests,
            include_reconciliation=args.include_reconciliation,
            force=args.force,
        )
    if args.command == "daemon":
        return notify_daemon(args.payload_file, force=args.force)
    return 1


def notify_tests(extra_args: list[str]) -> int:
    result = run(["uv", "run", "--extra", "dev", "pytest", "-q", "tests", *extra_args])
    notify(build_test_payload(result))
    return result.returncode


def notify_discovery(limit: str, pages: str) -> int:
    result = run(
        ["uv", "run", "polymarket-weather", "discover-markets", "--limit", limit, "--pages", pages]
    )
    notify(build_discovery_payload(result, limit, pages))
    return result.returncode


def notify_risk() -> int:
    result = run(["uv", "run", "polymarket-weather", "risk-report"])
    notify(build_risk_payload(result))
    return result.returncode


def notify_queue() -> int:
    result = run(["uv", "run", "polymarket-weather", "operator", "queue", "--limit", "20"])
    notify(build_queue_payload(result))
    return result.returncode


def notify_reconciliation() -> int:
    result = run(["uv", "run", "polymarket-weather", "reconcile"])
    notify(build_reconciliation_payload(result))
    return result.returncode


def notify_daemon(payload_file: str, *, force: bool) -> int:
    raw_payload = sys.stdin.read() if payload_file == "-" else Path(payload_file).read_text()
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"failed to parse daemon payload JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("daemon payload must be a JSON object")
    notify_daemon_payload(payload, force=force)
    return 0


def notify_tick(
    *, limit: str, pages: str, include_tests: bool, include_reconciliation: bool, force: bool
) -> int:
    state = load_state()
    exit_code = 0
    jobs = [
        (
            "discovery",
            run(
                [
                    "uv",
                    "run",
                    "polymarket-weather",
                    "discover-markets",
                    "--limit",
                    limit,
                    "--pages",
                    pages,
                ]
            ),
            lambda result: build_discovery_payload(result, limit, pages),
        ),
        (
            "candidates",
            run(["uv", "run", "polymarket-weather", "candidates", "--limit", "20"]),
            build_candidates_payload,
        ),
        ("risk", run(["uv", "run", "polymarket-weather", "risk-report"]), build_risk_payload),
    ]
    if include_reconciliation:
        jobs.append(
            (
                "reconciliation",
                run(["uv", "run", "polymarket-weather", "reconcile"]),
                build_reconciliation_payload,
            )
        )
    if include_tests:
        jobs.append(
            (
                "tests",
                run(["uv", "run", "--extra", "dev", "pytest", "-q", "tests"]),
                build_test_payload,
            )
        )

    for name, result, payload_builder in jobs:
        payload = payload_builder(result)
        if result.returncode != 0:
            exit_code = result.returncode
        if should_send(state, name, payload, force=force):
            notify(payload)
        else:
            print(f"suppressed unchanged notification: {name}")
    save_state(state)
    return exit_code


def build_test_payload(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return {
        "kind": "test_result",
        "project": "polymarket-weather-arb",
        "status": "passed" if result.returncode == 0 else "failed",
        "summary": first_nonempty_line(result.stdout) or "pytest completed",
        "body": result.stdout,
    }


def build_discovery_payload(
    result: subprocess.CompletedProcess[str], limit: str, pages: str
) -> dict[str, object]:
    return {
        "kind": "discovery",
        "project": "polymarket-weather-arb",
        "status": "ok" if result.returncode == 0 else "failed",
        "command": f"discover-markets --limit {limit} --pages {pages}",
        "summary": first_nonempty_line(result.stdout) or "discovery completed",
        "body": result.stdout,
    }


def build_candidates_payload(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return {
        "kind": "review",
        "role": "scanner",
        "project": "polymarket-weather-arb",
        "status": "ok" if result.returncode == 0 else "failed",
        "command": "candidates --limit 20",
        "summary": first_nonempty_line(result.stdout) or "candidate queue updated",
        "body": result.stdout,
    }


def build_risk_payload(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return {
        "kind": "risk_report",
        "project": "polymarket-weather-arb",
        "status": "ok" if result.returncode == 0 else "failed",
        "command": "risk-report",
        "summary": first_nonempty_line(result.stdout) or "risk report completed",
        "body": result.stdout,
    }


def build_queue_payload(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return {
        "kind": "review",
        "role": "captain",
        "project": "polymarket-weather-arb",
        "status": "ok" if result.returncode == 0 else "failed",
        "command": "operator queue --limit 20",
        "summary": first_nonempty_line(result.stdout) or "automation queue updated",
        "body": result.stdout,
        "items": [
            "local execute: uv run polymarket-weather operator run-approved --limit 1",
            "Discord approval only changes queue state",
        ],
    }


def build_reconciliation_payload(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return {
        "kind": "reconciliation",
        "role": "risk",
        "project": "polymarket-weather-arb",
        "status": _reconciliation_status(result.stdout, result.returncode),
        "command": "reconcile",
        "summary": first_nonempty_line(result.stdout) or "reconciliation completed",
        "body": result.stdout,
    }


def create_action(
    action: str, market: str, reason: str | None, ttl_minutes: str | None
) -> dict[str, object]:
    kind = action.replace("-", "_")
    if kind == "trade_review":
        kind = "trade_live"
    command = [
        "uv",
        "run",
        "polymarket-weather",
        "automation",
        "propose",
        "--kind",
        kind,
        "--market",
        market,
    ]
    if reason:
        command.extend(["--reason", reason])
    if ttl_minutes:
        command.extend(["--ttl-minutes", ttl_minutes])
    result = run(command)
    if result.returncode != 0:
        raise SystemExit(result.stdout or f"failed to create automation action for {market}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"failed to parse automation action response: {result.stdout}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("automation action response was not an object")
    return payload


def build_proposal_payload(
    action: str, market: str, reason: str | None, ttl_minutes: str | None
) -> dict[str, object]:
    proposal_action = f"propose_{action.replace('-', '_')}"
    created = create_action(action, market, reason, ttl_minutes)
    command = str(created["command_preview"])
    action_id = str(created["id"])
    expires_at = str(created["expires_at"])
    summary = reason or f"Review proposed local command for market {market}"
    return {
        "kind": "proposal",
        "role": "captain",
        "project": "polymarket-weather-arb",
        "status": "needs_human_approval",
        "action": proposal_action,
        "command": command,
        "summary": summary,
        "action_id": action_id,
        "expires_at": expires_at,
        "market": market,
        "items": [
            f"action_id={action_id}",
            f"expires_at={expires_at}",
            "mode=approval-only; Discord records approval only",
            f"Discord approve: /wufu action-approve action-id:{action_id}",
            "local execute after approval: uv run polymarket-weather operator run-approved --limit 1",
        ],
    }


def load_state() -> dict[str, str]:
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict[str, str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def should_send(
    state: dict[str, str], name: str, payload: dict[str, object], *, force: bool
) -> bool:
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    if not force and state.get(name) == digest:
        return False
    state[name] = digest
    return True


def notify_daemon_payload(payload: dict[str, object], *, force: bool = False) -> None:
    state = load_state()
    name = daemon_notification_key(payload)
    effective_force = force or bool(payload.get("notify_force"))
    if should_send(state, name, payload, force=effective_force):
        notify(payload)
    else:
        print(f"suppressed unchanged notification: {name}")
    save_state(state)


def daemon_notification_key(payload: dict[str, object]) -> str:
    parts = ["daemon", str(payload.get("daemon_event") or payload.get("kind") or "event")]
    action_id = payload.get("action_id")
    if action_id:
        parts.append(str(action_id))
    market = payload.get("market")
    if market:
        parts.append(str(market))
    return ":".join(part.replace(":", "_") for part in parts)


def notify(payload: dict[str, object]) -> None:
    process = subprocess.run(
        ["npm", "--prefix", str(COORDINATOR_DIR), "run", "notify", "--silent"],
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if process.stdout:
        print(process.stdout.strip())
    if process.stderr:
        print(process.stderr.strip(), file=sys.stderr)
    if process.returncode != 0:
        raise SystemExit(process.returncode)


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    output = "\n".join(part for part in [process.stdout.strip(), process.stderr.strip()] if part)
    return subprocess.CompletedProcess(command, process.returncode, output, "")


def first_nonempty_line(value: str) -> str | None:
    for line in value.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _reconciliation_status(output: str, returncode: int) -> str:
    if returncode != 0:
        return "failed"
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return "unknown"
    status = payload.get("status") if isinstance(payload, dict) else None
    return str(status or "unknown")


if __name__ == "__main__":
    raise SystemExit(main())
