"""Telegram Bot API notifier for daemon events.

Default off. Enable with TELEGRAM_NOTIFY_ENABLED=true plus bot token and chat id.
Uses stdlib urllib so no extra dependency is required.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from polymarket_weather_arb.config import Settings

logger = logging.getLogger(__name__)

NotifyFn = Callable[[dict[str, object]], None]

# Higher number = more important (stricter min level filters more).
_LEVEL_RANK = {"info": 10, "trade": 20, "risk": 30}


class TelegramNotifier:
    def __init__(
        self,
        settings: Settings,
        *,
        sender: Callable[[str, str, str], dict[str, Any]] | None = None,
    ) -> None:
        self.settings = settings
        self._sender = sender or _send_telegram_message
        self.payloads: list[dict[str, object]] = []
        self.sent: list[str] = []
        self.errors: list[str] = []

    def __call__(self, payload: dict[str, object]) -> None:
        self.payloads.append(dict(payload))

    def flush(self) -> None:
        pending, self.payloads = self.payloads, []
        if not self.settings.telegram_notify_ready():
            return
        token = self.settings.telegram_bot_token or ""
        chat_id = self.settings.telegram_chat_id or ""
        min_level = self.settings.telegram_notify_min_level
        for payload in pending:
            level = classify_payload_level(payload)
            if _LEVEL_RANK[level] < _LEVEL_RANK[min_level]:
                continue
            text = format_telegram_message(payload)
            try:
                self._sender(token, chat_id, text)
                self.sent.append(str(payload.get("daemon_event") or payload.get("kind") or "event"))
            except Exception as exc:  # noqa: BLE001 - never break trading loop
                logger.warning("telegram notify failed: %s", exc)
                self.errors.append(str(exc))


class FanoutNotifier:
    """Call multiple notifiers from a single daemon NotifyFn."""

    def __init__(self, *notifiers: NotifyFn) -> None:
        self.notifiers = [n for n in notifiers if n is not None]
        self.payloads: list[dict[str, object]] = []

    def __call__(self, payload: dict[str, object]) -> None:
        self.payloads.append(dict(payload))
        for notifier in self.notifiers:
            notifier(payload)

    def flush(self) -> None:
        for notifier in self.notifiers:
            flush = getattr(notifier, "flush", None)
            if callable(flush):
                flush()


def classify_payload_level(payload: dict[str, object]) -> str:
    event = str(payload.get("daemon_event") or "")
    kind = str(payload.get("kind") or "")
    status = str(payload.get("status") or "").lower()
    summary = str(payload.get("summary") or "").lower()

    # /app material trading events (Autopilot).
    if event in {
        "app_buy_submitted",
        "app_sell_submitted",
        "app_redeem_confirmed",
        "app_fill",
        "app_portfolio_digest",
    }:
        return "trade"
    if event in {
        "app_order_unverified",
        "app_redeem_unverified",
        "app_execution_risk",
        "app_stale_order_cancel_failed",
    } or status in {
        "submitted_unverified",
        "reconcile_failed",
    }:
        if event.startswith("app_") or kind == "trade_event":
            return "risk"

    if event == "daemon_risk" or kind == "risk_report" or status in {"warn", "error", "failed"}:
        return "risk"
    if "auto_exit" in event or "auto_live" in event:
        return "trade"
    if kind in {"review"} and (
        status == "executed"
        or "executed" in summary
        or "auto_live" in summary
        or "auto_exit" in summary
        or "已执行" in summary
        or "自动卖出" in summary
        or "自动平仓" in summary
    ):
        return "trade"
    if kind in {"proposal"} and ("trade_live" in summary or "trade_live" in str(payload)):
        return "trade"
    if event == "daemon_live":
        return "trade"
    if kind == "trade_event":
        return "trade"
    return "info"


# Internal event codes → Chinese labels for Telegram display only.
_EVENT_ZH: dict[str, str] = {
    "app_buy_submitted": "买入已提交",
    "app_sell_submitted": "卖出已提交",
    "app_redeem_confirmed": "结算赎回已确认",
    "app_fill": "成交确认",
    "app_portfolio_digest": "持仓收益摘要",
    "app_order_unverified": "订单待核实",
    "app_redeem_unverified": "结算赎回待核实",
    "app_execution_risk": "执行风险",
    "daemon_live": "实盘执行",
    "daemon_auto_exit": "自动平仓",
    "daemon_risk": "风险告警",
    "daemon_discovery": "市场发现",
    "daemon_tick": "周期摘要",
    "daemon_proposal": "待审批提案",
    "trade_event": "交易事件",
    "risk_report": "风险报告",
    "discovery": "市场发现",
    "proposal": "待审批提案",
    "review": "执行摘要",
    "dry_run": "模拟执行",
}

_STATUS_ZH: dict[str, str] = {
    "submitted": "已提交",
    "filled": "已成交",
    "open": "挂单中",
    "live": "挂单中",
    "executed": "已执行",
    "ok": "正常",
    "warn": "警告",
    "error": "错误",
    "failed": "失败",
    "submitted_unverified": "已提交未核实",
    "reconcile_failed": "对账失败",
    "needs_human_approval": "待人工审批",
    "auto_dry_run_pending": "自动模拟待执行",
}

_FIELD_ZH: dict[str, str] = {
    "side": "方向",
    "outcome": "结果侧",
    "price": "价格",
    "size": "数量",
    "order_id": "订单ID",
    "fill_id": "成交ID",
    "exchange_fill_id": "成交ID",
    "intent_id": "意图ID",
}

_SIDE_ZH: dict[str, str] = {
    "buy": "买入",
    "sell": "卖出",
    "buy_yes": "买入 YES",
    "buy_no": "买入 NO",
    "sell_yes": "卖出 YES",
    "sell_no": "卖出 NO",
    "yes": "YES",
    "no": "NO",
}


def _zh_event(event: str) -> str:
    return _EVENT_ZH.get(event, event)


def _zh_status(status: str) -> str:
    if not status:
        return status
    return _STATUS_ZH.get(status.lower(), status)


def _zh_side(value: object) -> str:
    text = str(value).strip()
    if not text:
        return text
    return _SIDE_ZH.get(text.lower(), text)


def format_telegram_message(payload: dict[str, object]) -> str:
    """Render a human-facing Telegram body in Chinese (field labels + event titles)."""
    event = str(payload.get("daemon_event") or payload.get("kind") or "event")
    if event == "app_portfolio_digest":
        return _format_portfolio_digest(payload)
    status = str(payload.get("status") or "")
    summary = str(payload.get("summary") or "")
    project = str(payload.get("project") or "polymarket-weather-arb")
    lines = [
        f"[{project}]",
        f"事件: {_zh_event(event)}",
    ]
    if status:
        lines.append(f"状态: {_zh_status(status)}")
    if summary:
        lines.append(f"说明: {summary}")
    rendered_fields: set[tuple[str, str]] = set()
    for key in (
        "side",
        "outcome",
        "price",
        "size",
        "order_id",
        "fill_id",
        "exchange_fill_id",
        "intent_id",
    ):
        value = payload.get(key)
        if value is None or value == "":
            continue
        label = _FIELD_ZH.get(key, key)
        display = _zh_side(value) if key in {"side", "outcome"} else value
        field_key = (label, str(display))
        if field_key in rendered_fields:
            continue
        rendered_fields.add(field_key)
        lines.append(f"{label}: {display}")
    items = payload.get("items")
    if isinstance(items, list) and items:
        lines.append("详情:")
        for item in items[:20]:
            lines.append(f"  - {item}")
    market = payload.get("market") or payload.get("market_id")
    if market:
        lines.append(f"市场: {market}")
    title = payload.get("market_title") or payload.get("title")
    if title:
        lines.append(f"标题: {title}")
    action_id = payload.get("action_id")
    if action_id:
        lines.append(f"动作ID: {action_id}")
    return "\n".join(lines)


def _format_portfolio_digest(payload: dict[str, object]) -> str:
    project = str(payload.get("project") or "polymarket-weather-arb")
    age = payload.get("reconciliation_age_minutes")
    recon_text = "新鲜" if payload.get("reconciliation_fresh") else "陈旧"
    if age is not None:
        recon_text += f"（{age}分钟前）"
    lines = [
        f"[{project}]",
        "天气持仓 · 4小时摘要",
        (
            f"对账: {recon_text} · 持仓 {payload.get('open_position_count', 0)} · "
            f"挂单 {payload.get('open_order_count', 0)}"
        ),
        "",
        f"组合建仓成本: {_money(payload.get('total_buy_cost'))}",
        f"已回收卖出: {_money(payload.get('total_sell_proceeds'))}",
        f"当前持仓估值: {_money(payload.get('total_current_value'))}",
        (
            "持仓周期估算: "
            f"{_signed_money(payload.get('total_estimated_pnl'))} "
            f"({_signed_percent(payload.get('total_estimated_return_pct'))})"
        ),
        f"累计已实现: {_signed_money(payload.get('total_realized_pnl'))}",
    ]
    positions = payload.get("positions")
    if isinstance(positions, list):
        for index, raw_position in enumerate(positions[:10], start=1):
            if not isinstance(raw_position, dict):
                continue
            city = str(raw_position.get("city") or "").strip()
            bucket = str(raw_position.get("bucket") or "").strip()
            outcome = str(raw_position.get("outcome") or "").strip()
            title = str(raw_position.get("market_title") or "").strip()
            heading = " · ".join(item for item in (city, bucket, outcome) if item)
            if not heading:
                heading = title[:72] or str(raw_position.get("market_id") or "未知市场")
            lines.extend(
                [
                    "",
                    f"{index}. {heading}",
                    (
                        f"数量 {raw_position.get('position_size')} · "
                        f"成本 {_money(raw_position.get('buy_cost'))} · "
                        f"现值 {_money(raw_position.get('current_value'))}"
                    ),
                    (
                        "估算 "
                        f"{_signed_money(raw_position.get('estimated_pnl'))} "
                        f"({_signed_percent(raw_position.get('estimated_return_pct'))})"
                    ),
                    _portfolio_time_text(raw_position),
                ]
            )
    remaining = max(0, len(positions) - 10) if isinstance(positions, list) else 0
    if remaining > 0:
        lines.extend(["", f"其余 {remaining} 笔持仓已合并到组合汇总。"])
    unverified = int(payload.get("unverified_open_positions") or 0)
    if unverified:
        lines.extend(["", f"⚠ {unverified} 笔持仓账本尚未完整关联，未计入盈亏数字。"])
    lines.extend(["", "估值来自最近一次交易所对账，不等于最终可成交收益。"])
    return "\n".join(lines)


def _portfolio_time_text(position: dict[str, object]) -> str:
    target_date = str(position.get("target_date") or "").strip()
    day_offset = position.get("local_day_offset")
    seconds = position.get("seconds_to_target_end")
    if isinstance(seconds, int):
        if seconds <= 0:
            return f"时间: {target_date or '目标日'} 已结束 · 等待结算"
        if day_offset == 0:
            hours, remainder = divmod(seconds, 3600)
            minutes = remainder // 60
            return f"时间: D0 · 距当地目标日结束约 {hours}小时{minutes}分"
        if isinstance(day_offset, int) and day_offset > 0:
            return f"时间: D{day_offset} · 目标日 {target_date}"
    if target_date:
        return f"时间: 目标日 {target_date} · 当地倒计时不可确认"
    return "时间: 目标日未知"


def _money(value: object) -> str:
    return f"${_decimal(value):.2f}"


def _signed_money(value: object) -> str:
    number = _decimal(value)
    sign = "+" if number >= 0 else "-"
    return f"{sign}${abs(number):.2f}"


def _signed_percent(value: object) -> str:
    if value is None or value == "":
        return "—"
    number = _decimal(value)
    sign = "+" if number >= 0 else "-"
    return f"{sign}{abs(number):.1f}%"


def _decimal(value: object) -> Decimal:
    return Decimal(str(value or 0))


def _send_telegram_message(token: str, chat_id: str, text: str) -> dict[str, Any]:
    from polymarket_weather_arb.adapters.http_client import (
        apply_proxy_environment,
        effective_proxy_url,
    )

    apply_proxy_environment()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps(
        {
            "chat_id": chat_id,
            "text": text[:4000],
            "disable_web_page_preview": True,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener()
    proxy = effective_proxy_url()
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    try:
        with opener.open(request, timeout=15) as response:  # noqa: S310 - fixed API host
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"telegram HTTP {exc.code}: {detail}") from exc
    payload = json.loads(raw)
    if not payload.get("ok"):
        raise RuntimeError(f"telegram API not ok: {payload}")
    return payload
