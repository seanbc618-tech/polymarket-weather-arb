import importlib.util
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "rehearse_live_readiness.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("rehearse_live_readiness", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_default_rehearsal_steps_are_offline_and_safe(tmp_path):
    script = _load_script()

    plan = script.build_plan(
        work_dir=tmp_path / "run",
        database_path=tmp_path / "run" / "rehearsal.db",
        check_exchange=False,
    )

    rendered = [" ".join(step.command) for step in plan.steps]
    assert rendered[0].endswith("polymarket-weather init-db")
    assert any(
        "trade --market demo-weather-nyc-high-2026-05-08 --dry-run" in item for item in rendered
    )
    assert rendered[-1].endswith("polymarket-weather live-readiness --no-check-exchange")
    assert all(" trade --market " not in item or " --dry-run" in item for item in rendered)
    assert plan.environment["TRADING_DISABLED"] == "true"
    assert plan.environment["COMPLIANCE_CHECK_ENABLED"] == "false"
    assert plan.environment["DATABASE_PATH"] == str(tmp_path / "run" / "rehearsal.db")
    assert plan.environment["UV_CACHE_DIR"] == str(tmp_path / "run" / ".uv-cache")


def test_exchange_rehearsal_only_changes_readiness_check(tmp_path):
    script = _load_script()

    plan = script.build_plan(
        work_dir=tmp_path / "run",
        database_path=tmp_path / "run" / "rehearsal.db",
        check_exchange=True,
    )

    assert plan.steps[-1].command[-1] == "live-readiness"
    assert "--no-check-exchange" not in plan.steps[-1].command
    assert plan.environment["TRADING_DISABLED"] == "true"
