#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO_MARKET_ID = "demo-weather-nyc-high-2026-05-08"
DEMO_FIXTURE = "demo-weather-nyc-high-2026-05-08.json"


@dataclass(frozen=True)
class RehearsalStep:
    name: str
    command: list[str]


@dataclass(frozen=True)
class RehearsalPlan:
    work_dir: Path
    database_path: Path
    environment: dict[str, str]
    steps: list[RehearsalStep]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a safe dry-run rehearsal ending with live-readiness checks."
    )
    parser.add_argument("--work-dir", type=Path, default=ROOT / ".run" / "live-readiness")
    parser.add_argument("--database-path", type=Path, default=None)
    parser.add_argument(
        "--check-exchange",
        action="store_true",
        help="Let live-readiness perform read-only exchange checks. Default is offline.",
    )
    parser.add_argument(
        "--command-prefix",
        nargs="+",
        default=["uv", "run", "polymarket-weather"],
        help="Command prefix used to invoke the CLI.",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="Print the command plan without executing it.",
    )
    args = parser.parse_args()

    database_path = args.database_path or args.work_dir / "rehearsal.db"
    plan = build_plan(
        work_dir=args.work_dir,
        database_path=database_path,
        check_exchange=args.check_exchange,
        command_prefix=args.command_prefix,
    )
    print_plan(plan)
    sys.stdout.flush()
    if args.print_only:
        return 0
    return run_plan(plan)


def build_plan(
    *,
    work_dir: Path,
    database_path: Path,
    check_exchange: bool,
    command_prefix: list[str] | None = None,
) -> RehearsalPlan:
    prefix = command_prefix or ["uv", "run", "polymarket-weather"]
    bundled_fixture = ROOT / "fixtures" / "markets" / DEMO_FIXTURE
    env = {
        "DATABASE_PATH": str(database_path),
        "TRADING_DISABLED": "true",
        "COMPLIANCE_CHECK_ENABLED": "false",
        "UV_CACHE_DIR": str(work_dir / ".uv-cache"),
    }
    readiness = [*prefix, "live-readiness"]
    if not check_exchange:
        readiness.append("--no-check-exchange")
    steps = [
        RehearsalStep("init database", [*prefix, "init-db"]),
        RehearsalStep(
            "load bundled demo analysis",
            [
                *prefix,
                "fixtures",
                "load-market-fixture",
                str(bundled_fixture),
                "--demo-analysis",
            ],
        ),
        RehearsalStep(
            "record dry-run order intent",
            [*prefix, "trade", "--market", DEMO_MARKET_ID, "--dry-run"],
        ),
        RehearsalStep("review risk report", [*prefix, "risk-report"]),
        RehearsalStep("check live readiness", readiness),
    ]
    return RehearsalPlan(
        work_dir=work_dir,
        database_path=database_path,
        environment=env,
        steps=steps,
    )


def run_plan(plan: RehearsalPlan) -> int:
    plan.work_dir.mkdir(parents=True, exist_ok=True)
    plan.database_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(plan.environment)
    for step in plan.steps:
        print()
        print(f"==> {step.name}")
        print("+ " + " ".join(step.command))
        sys.stdout.flush()
        completed = subprocess.run(step.command, cwd=ROOT, env=env, text=True)
        if completed.returncode != 0:
            print(f"step failed: {step.name}", file=sys.stderr)
            return completed.returncode
    print()
    print("Rehearsal complete. TRADING_DISABLED remained true for every step.")
    return 0


def print_plan(plan: RehearsalPlan) -> None:
    print(f"Work dir: {plan.work_dir}")
    print(f"Database: {plan.database_path}")
    print("Environment overrides:")
    for key, value in plan.environment.items():
        print(f"  {key}={value}")
    print("Steps:")
    for index, step in enumerate(plan.steps, start=1):
        print(f"  {index}. {step.name}: {' '.join(step.command)}")


if __name__ == "__main__":
    raise SystemExit(main())
