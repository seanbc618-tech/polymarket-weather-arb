from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "notify_dashboard.py"


def load_notify_module():
    spec = importlib.util.spec_from_file_location("notify_dashboard_for_test", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_daemon_notification_key_includes_event_and_action():
    module = load_notify_module()

    key = module.daemon_notification_key(
        {
            "daemon_event": "daemon_proposal",
            "kind": "proposal",
            "action_id": "act_123",
            "market": "m1",
        }
    )

    assert key == "daemon:daemon_proposal:act_123:m1"


def test_notify_daemon_payload_suppresses_duplicates(tmp_path, monkeypatch):
    module = load_notify_module()
    monkeypatch.setattr(module, "STATE_PATH", tmp_path / "state.json")
    sent = []
    monkeypatch.setattr(module, "notify", sent.append)
    payload = {
        "daemon_event": "daemon_tick",
        "kind": "review",
        "role": "reviewer",
        "summary": "same",
    }

    module.notify_daemon_payload(payload)
    module.notify_daemon_payload(payload)

    assert sent == [payload]


def test_notify_daemon_payload_force_resends_duplicate(tmp_path, monkeypatch):
    module = load_notify_module()
    monkeypatch.setattr(module, "STATE_PATH", tmp_path / "state.json")
    sent = []
    monkeypatch.setattr(module, "notify", sent.append)
    payload = {
        "daemon_event": "daemon_tick",
        "kind": "review",
        "role": "reviewer",
        "summary": "same",
    }

    module.notify_daemon_payload(payload)
    module.notify_daemon_payload({**payload, "notify_force": True})

    assert len(sent) == 2
