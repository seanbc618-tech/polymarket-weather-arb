"""Offline tests for Telegram notifier (no network)."""

from __future__ import annotations

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.cli_commands.common import _build_daemon_notifier
from polymarket_weather_arb.services.telegram_notifier import (
    FanoutNotifier,
    TelegramNotifier,
    classify_payload_level,
    format_telegram_message,
)


def test_classify_levels():
    assert classify_payload_level({"daemon_event": "daemon_risk", "kind": "risk_report"}) == "risk"
    assert classify_payload_level({"daemon_event": "daemon_live", "kind": "review"}) == "trade"
    assert classify_payload_level({"daemon_event": "daemon_auto_exit", "kind": "review"}) == "trade"
    assert classify_payload_level({"daemon_event": "daemon_discovery", "kind": "discovery"}) == "info"


def test_format_message_includes_summary_and_items():
    text = format_telegram_message(
        {
            "daemon_event": "daemon_tick",
            "kind": "review",
            "status": "ok",
            "summary": "周期摘要 策略=micro-live",
            "items": ["发现=1", "自动平仓成功=0"],
            "project": "polymarket-weather-arb",
        }
    )
    assert "事件: 周期摘要" in text
    assert "状态: 正常" in text
    assert "micro-live" in text
    assert "自动平仓成功=0" in text


def test_format_message_deduplicates_fill_id_aliases():
    text = format_telegram_message(
        {
            "daemon_event": "app_fill",
            "status": "filled",
            "fill_id": "fill-1",
            "exchange_fill_id": "fill-1",
        }
    )

    assert text.count("成交ID: fill-1") == 1


def test_telegram_notifier_filters_by_min_level_and_sends():
    sent: list[str] = []

    def fake_sender(token, chat_id, text):
        sent.append(text)
        return {"ok": True}

    settings = Settings(
        TELEGRAM_NOTIFY_ENABLED=True,
        TELEGRAM_BOT_TOKEN="token",
        TELEGRAM_CHAT_ID="123",
        TELEGRAM_NOTIFY_MIN_LEVEL="trade",
    )
    notifier = TelegramNotifier(settings, sender=fake_sender)
    notifier(
        {
            "daemon_event": "daemon_discovery",
            "kind": "discovery",
            "summary": "found 3",
            "status": "ok",
        }
    )
    notifier(
        {
            "daemon_event": "daemon_auto_exit",
            "kind": "review",
            "summary": "自动平仓已提交 1 笔卖出",
            "status": "executed",
            "items": ["意图ID=[9]"],
        }
    )
    notifier.flush()
    assert len(sent) == 1
    assert "自动平仓" in sent[0]


def test_telegram_disabled_sends_nothing():
    sent: list[str] = []

    def fake_sender(token, chat_id, text):
        sent.append(text)
        return {"ok": True}

    settings = Settings(TELEGRAM_NOTIFY_ENABLED=False, TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="1")
    notifier = TelegramNotifier(settings, sender=fake_sender)
    notifier({"daemon_event": "daemon_tick", "kind": "review", "summary": "x", "status": "ok"})
    notifier.flush()
    assert sent == []


def test_fanout_calls_both():
    a_calls: list[dict] = []
    b_calls: list[dict] = []

    def a(p):
        a_calls.append(p)

    def b(p):
        b_calls.append(p)

    fan = FanoutNotifier(a, b)
    fan({"k": 1})
    assert a_calls == [{"k": 1}]
    assert b_calls == [{"k": 1}]


def test_telegram_ready_helper():
    assert (
        Settings(
            TELEGRAM_NOTIFY_ENABLED=True,
            TELEGRAM_BOT_TOKEN="",
            TELEGRAM_CHAT_ID="",
        ).telegram_notify_ready()
        is False
    )
    assert (
        Settings(
            TELEGRAM_NOTIFY_ENABLED=True,
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_CHAT_ID="1",
        ).telegram_notify_ready()
        is True
    )


def test_legacy_daemon_does_not_auto_claim_app_telegram_configuration():
    settings = Settings(
        TELEGRAM_NOTIFY_ENABLED=True,
        TELEGRAM_BOT_TOKEN="token",
        TELEGRAM_CHAT_ID="123",
    )

    notifier = _build_daemon_notifier(
        settings=settings,
        notify_dashboard=False,
        notify_telegram=False,
        notify_force=False,
    )

    assert notifier is None


def test_legacy_daemon_can_still_explicitly_enable_telegram():
    settings = Settings(
        TELEGRAM_NOTIFY_ENABLED=True,
        TELEGRAM_BOT_TOKEN="token",
        TELEGRAM_CHAT_ID="123",
    )

    notifier = _build_daemon_notifier(
        settings=settings,
        notify_dashboard=False,
        notify_telegram=True,
        notify_force=False,
    )

    assert isinstance(notifier, TelegramNotifier)
