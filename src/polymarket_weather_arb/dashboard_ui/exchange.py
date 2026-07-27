from __future__ import annotations

from polymarket_weather_arb.dashboard_ui.html import (
    _bool_label,
    _dash,
    _definition_table,
    _display_time,
    _e,
    _hidden_lang,
    _href,
    _render_flash,
    _section,
    _short_note,
    _table,
    render_page,
)
from polymarket_weather_arb.dashboard_ui.i18n import _t
from polymarket_weather_arb.services.market_workflow_service import build_risk_report
from polymarket_weather_arb.storage.repositories import Repository


def render_open_orders(repository: Repository, lang: str, current_path: str) -> str:
    orders = repository.list_open_orders(limit=100)
    table = _table(
        [
            _t(lang, "field.order"),
            _t(lang, "actions.market"),
            _t(lang, "field.side"),
            _t(lang, "field.price"),
            _t(lang, "field.size"),
            _t(lang, "field.notional"),
            _t(lang, "actions.status"),
            _t(lang, "actions.updated"),
            _t(lang, "actions.cancel"),
        ],
        [
            [
                row["exchange_order_id"],
                row["market_id"] or "-",
                row["side"] or "-",
                _dash(row["price"]),
                _dash(row["size"]),
                _dash(row["notional"]),
                row["status"] or "-",
                _display_time(row["updated_at"]),
                f'<form method="post" action="{_href("/open-orders/cancel", lang)}" style="display:inline;">'
                f"{_hidden_lang(lang)}"
                f'<input type="hidden" name="order_id" value="{_e(row["exchange_order_id"])}">'
                f'<button type="submit" class="danger" onclick="return confirm(\'{_t(lang, "confirm.cancel_order")}\')">{_t(lang, "actions.cancel")}</button>'
                f"</form>",
            ]
            for row in orders
        ],
        lang,
    )

    # 添加刷新按钮
    refresh_form = f"""
    <div style="margin-bottom: 16px;">
        <form method="post" action="{_href("/open-orders/refresh", lang)}" style="display: inline;">
            {_hidden_lang(lang)}
            <button type="submit">{_t(lang, "actions.refresh")}</button>
        </form>
        <span style="margin-left: 12px; color: #94a3b8;">{_t(lang, "exchange.orders_count", count=len(orders))}</span>
    </div>
    """

    return render_page(_t(lang, "exchange.open_orders"), refresh_form + table, lang, current_path)


def render_positions(repository: Repository, lang: str, current_path: str) -> str:
    all_positions = repository.list_positions(limit=100)
    nonzero_positions = [p for p in all_positions if p["size"] and float(p["size"]) > 0]

    # 计算总 exposure
    total_exposure = sum(float(p["notional"] or 0) for p in nonzero_positions)

    table = _table(
        [
            _t(lang, "actions.market"),
            _t(lang, "field.outcome"),
            _t(lang, "field.size"),
            _t(lang, "field.notional"),
            _t(lang, "actions.updated"),
        ],
        [
            [
                row["market_id"],
                row["outcome"],
                _dash(row["size"]),
                _dash(row["notional"]),
                _display_time(row["updated_at"]),
            ]
            for row in all_positions
        ],
        lang,
    )

    # Position exposure summary
    exposure_summary = f"""
    <div style="background: #111827; border: 1px solid #334155; border-radius: 10px; padding: 16px; margin-bottom: 16px;">
        <h3 style="margin: 0 0 12px 0;">{_t(lang, "exchange.exposure_summary")}</h3>
        <table style="width: 100%; border-collapse: collapse;">
            <tr>
                <td style="padding: 8px 12px; border-bottom: 1px solid #334155; color: #94a3b8;">{_t(lang, "exchange.total_positions")}:</td>
                <td style="padding: 8px 12px; border-bottom: 1px solid #334155;">{len(all_positions)}</td>
            </tr>
            <tr>
                <td style="padding: 8px 12px; border-bottom: 1px solid #334155; color: #94a3b8;">{_t(lang, "exchange.nonzero_positions")}:</td>
                <td style="padding: 8px 12px; border-bottom: 1px solid #334155;">{len(nonzero_positions)}</td>
            </tr>
            <tr>
                <td style="padding: 8px 12px; border-bottom: 1px solid #334155; color: #94a3b8;">{_t(lang, "exchange.total_exposure")}:</td>
                <td style="padding: 8px 12px; border-bottom: 1px solid #334155;">{total_exposure:.2f} USDC</td>
            </tr>
        </table>
    </div>
    """

    # Nonzero positions block live auto warning
    block_warning = ""
    if nonzero_positions:
        block_warning = f"""
        <div style="background: #7f1d1d; border: 1px solid #ef4444; border-radius: 10px; padding: 16px; margin-bottom: 16px;">
            <h3 style="margin: 0 0 8px 0; color: #fecaca;">{_t(lang, "exchange.nonzero_block_title")}</h3>
            <p style="margin: 0; color: #fecaca;">{_t(lang, "exchange.nonzero_block_body")}</p>
            <p style="margin: 8px 0 0 0; font-size: 12px; color: #fca5a5;">{_t(lang, "exchange.nonzero_block_hint")}</p>
        </div>
        """

    return render_page(
        _t(lang, "exchange.positions"), exposure_summary + block_warning + table, lang, current_path
    )


def render_fills(repository: Repository, lang: str, current_path: str) -> str:
    table = _table(
        [
            _t(lang, "field.fill"),
            _t(lang, "field.order"),
            _t(lang, "actions.market"),
            _t(lang, "field.side"),
            _t(lang, "field.price"),
            _t(lang, "field.size"),
            _t(lang, "field.fee"),
            _t(lang, "field.filled"),
        ],
        [
            [
                row["exchange_fill_id"] or str(row["id"]),
                row["order_id"] or "-",
                row["market_id"],
                row["side"],
                _dash(row["price"]),
                _dash(row["size"]),
                _dash(row["fee"]),
                _display_time(row["filled_at"]),
            ]
            for row in repository.list_fills(limit=100)
        ],
        lang,
    )
    return render_page(_t(lang, "exchange.fills"), table, lang, current_path)


def render_risk(repository: Repository, lang: str, current_path: str) -> str:
    report = build_risk_report(repository)
    risks = repository.list_recent_risk_decisions(limit=50)
    exposure_rows = [[market_id, str(exposure)] for market_id, exposure in report.exposures]
    risk_table = _table(
        [
            _t(lang, "actions.market"),
            _t(lang, "field.accepted"),
            _t(lang, "field.side"),
            _t(lang, "field.price"),
            _t(lang, "field.size"),
            _t(lang, "field.notional"),
            _t(lang, "field.created"),
            _t(lang, "field.rationale"),
        ],
        [
            [
                row["market_id"],
                _bool_label(row["accepted"], lang),
                row["proposed_side"],
                _dash(row["proposed_price"]),
                _dash(row["proposed_size"]),
                _dash(row["proposed_notional"]),
                _display_time(row["created_at"]),
                _short_note(row["reasons"]),
            ]
            for row in risks
        ],
        lang,
    )
    body = "".join(
        [
            '<section class="card">',
            f"<h2>{_t(lang, 'risk.daily_notional')}</h2>",
            f"<p>{_e(report.daily_live_notional)}</p>",
            "</section>",
            _section(
                _t(lang, "risk.market_exposure"),
                _table(
                    [_t(lang, "actions.market"), _t(lang, "field.notional")], exposure_rows, lang
                ),
            ),
            _section(_t(lang, "risk.title"), risk_table),
        ]
    )
    return render_page(_t(lang, "risk.title"), body, lang, current_path)


def render_reconciliation(
    repository: Repository,
    lang: str,
    current_path: str,
    query: dict[str, list[str]] | None = None,
) -> str:
    latest = repository.latest_reconciliation()
    reconciliations = repository.list_reconciliations(limit=20)
    form = "".join(
        [
            _render_flash(query or {}, lang),
            '<section class="card">',
            f'<p class="warning">{_t(lang, "reconciliation.warning")}</p>',
            f'<form method="post" action="{_href("/reconciliation/run", lang)}">',
            _hidden_lang(lang),
            f'<button type="submit">{_t(lang, "reconciliation.run")}</button>',
            "</form>",
            "</section>",
        ]
    )
    latest_block = _section(
        _t(lang, "overview.reconciliation"),
        _definition_table(
            [
                (_t(lang, "actions.status"), latest["status"] if latest else "-"),
                (
                    _t(lang, "field.created"),
                    _display_time(latest["created_at"] if latest else None),
                ),
                (
                    _t(lang, "field.details_json"),
                    _short_note(latest["details"] if latest else None, 500),
                ),
            ]
        ),
    )
    history = _table(
        [
            _t(lang, "field.id"),
            _t(lang, "actions.status"),
            _t(lang, "field.created"),
            _t(lang, "field.details_json"),
        ],
        [
            [
                row["id"],
                row["status"],
                _display_time(row["created_at"]),
                _short_note(row["details"], 240),
            ]
            for row in reconciliations
        ],
        lang,
    )
    links = "".join(
        [
            "<p>",
            f'<a href="{_href("/open-orders", lang)}">{_t(lang, "nav.open_orders")}</a> ',
            f'<a href="{_href("/positions", lang)}">{_t(lang, "nav.positions")}</a> ',
            f'<a href="{_href("/fills", lang)}">{_t(lang, "nav.fills")}</a>',
            "</p>",
        ]
    )
    return render_page(
        _t(lang, "reconciliation.title"), form + latest_block + links + history, lang, current_path
    )
