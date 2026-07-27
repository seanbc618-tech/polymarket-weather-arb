from __future__ import annotations

from sqlite3 import Row
from urllib.parse import quote

from polymarket_weather_arb.dashboard_ui.html import (
    _bool_label,
    _dash,
    _definition_table,
    _display_time,
    _duration_label,
    _e,
    _hidden_lang,
    _href,
    _kind_label,
    _note_cell,
    _render_flash,
    _section,
    _short_note,
    _status_label,
    _table,
    render_page,
)
from polymarket_weather_arb.dashboard_ui.i18n import _t
from polymarket_weather_arb.profiles import get_profile, list_profiles
from polymarket_weather_arb.services.automation_service import AutomationService
from polymarket_weather_arb.services.live_monitor_service import build_live_monitor_snapshot
from polymarket_weather_arb.storage.repositories import Repository


def render_actions(
    repository: Repository,
    lang: str,
    current_path: str,
    *,
    status: str | None = None,
    kind: str | None = None,
    query: dict[str, list[str]] | None = None,
) -> str:
    actions = AutomationService(repository).list_actions(
        status=status, kind=kind, limit=100, expire=False
    )
    filters = "".join(
        [
            '<section class="card">',
            f"<h2>{_t(lang, 'actions.propose_next')}</h2>",
            _propose_next_form(lang),
            "</section>",
            '<form method="get" class="filters">',
            _hidden_lang(lang),
            f'<label>{_t(lang, "actions.filters.status")} <input name="status" value="{_e(status or "")}" placeholder="pending"></label>',
            f'<label>{_t(lang, "actions.filters.kind")} <input name="kind" value="{_e(kind or "")}" placeholder="dry_run"></label>',
            f'<button type="submit">{_t(lang, "actions.filters.submit")}</button>',
            f'<a href="{_href("/actions", lang)}">{_t(lang, "actions.filters.clear")}</a>',
            "</form>",
        ]
    )
    table = _table(
        [
            _t(lang, "actions.action"),
            _t(lang, "actions.status"),
            _t(lang, "actions.kind"),
            _t(lang, "actions.market"),
            _t(lang, "actions.updated"),
            _t(lang, "actions.expires"),
            _t(lang, "actions.note"),
            _t(lang, "actions.controls"),
        ],
        [
            [
                '<a href="'
                + _href("/actions/" + quote(action["id"]), lang)
                + '">'
                + _e(action["id"])
                + "</a>",
                _status_label(action["status"], lang),
                _kind_label(action["kind"], lang),
                _market_cell(action),
                _display_time(_display_time(action["updated_at"])),
                _display_time(_display_time(action["expires_at"])),
                _note_cell(
                    action["failure_reason"] or action["result_summary"] or action["reason"]
                ),
                _action_controls(action, lang),
            ]
            for action in actions
        ],
        lang,
        raw_columns={0, 3, 6, 7},
    )
    body = "".join([_render_flash(query or {}, lang), filters, table])
    return render_page(_t(lang, "actions.title"), body, lang, current_path)


def render_action_detail(
    action: Row,
    events: list[Row],
    lang: str,
    current_path: str,
    query: dict[str, list[str]] | None = None,
) -> str:
    fields = [
        (_t(lang, "actions.status"), _status_label(action["status"], lang)),
        (_t(lang, "actions.kind"), _kind_label(action["kind"], lang)),
        (_t(lang, "actions.market"), action["market_id"]),
        (_t(lang, "field.title"), _row_value(action, "market_title") or "-"),
        (_t(lang, "field.command"), action["command_preview"]),
        (_t(lang, "field.requested_by"), action["requested_by"] or "-"),
        (_t(lang, "field.approved_by"), action["approved_by"] or "-"),
        (_t(lang, "field.rejected_by"), action["rejected_by"] or "-"),
        (_t(lang, "field.created"), _display_time(action["created_at"])),
        (_t(lang, "actions.updated"), _display_time(action["updated_at"])),
        (_t(lang, "actions.expires"), _display_time(action["expires_at"])),
        (_t(lang, "field.started"), _display_time(_row_value(action, "execution_started_at"))),
        (
            _t(lang, "field.finished"),
            _display_time(
                _row_value(action, "execution_finished_at")
                or action["executed_at"]
                or action["failed_at"]
            ),
        ),
        (_t(lang, "field.duration"), _duration_label(_row_value(action, "execution_duration_ms"))),
        (
            _t(lang, "field.return_code"),
            str(action["return_code"]) if action["return_code"] is not None else "-",
        ),
        (_t(lang, "actions.reason"), action["reason"] or "-"),
        (
            _t(lang, "field.result"),
            _note_cell(action["result_summary"] or action["failure_reason"] or "-", 500),
        ),
    ]
    details = _definition_table(fields, raw_values={len(fields) - 1})
    argv = _row_value(action, "execution_argv") or "-"
    timeline = _table(
        [
            _t(lang, "field.time"),
            _t(lang, "field.event"),
            _t(lang, "field.actor"),
            _t(lang, "field.details"),
        ],
        [
            [
                _display_time(event["created_at"]),
                event["event"],
                event["actor"] or "-",
                event["details"],
            ]
            for event in events
        ],
        lang,
    )
    body = "".join(
        [
            _render_flash(query or {}, lang),
            f'<p><a href="{_href("/actions", lang)}">{_t(lang, "actions.back")}</a></p><h2>{_e(action["id"])}</h2>',
            '<section class="card">',
            _action_safety_panel(action, lang),
            _action_controls(action, lang, detail=True),
            "</section>",
            details,
            f"<h2>{_t(lang, 'actions.execution_argv')}</h2>",
            f"<pre>{_e(argv)}</pre>",
            f"<h2>{_t(lang, 'actions.timeline')}</h2>",
            timeline,
        ]
    )
    return render_page(f"{_t(lang, 'actions.action')} {action['id']}", body, lang, current_path)


def render_runs(runs: list[Row], lang: str, current_path: str) -> str:
    table = _table(
        [
            _t(lang, "field.id"),
            _t(lang, "field.command"),
            _t(lang, "actions.status"),
            _t(lang, "field.started"),
            _t(lang, "field.finished"),
            _t(lang, "field.error"),
        ],
        [
            [
                str(run["id"]),
                run["command"],
                run["status"],
                _display_time(run["started_at"]),
                _display_time(run["finished_at"]),
                _short_note(run["error"]),
            ]
            for run in runs
        ],
        lang,
    )
    return render_page(_t(lang, "runs.title"), table, lang, current_path)


def render_operator(
    repository: Repository, lang: str, current_path: str, query: dict[str, list[str]] | None = None
) -> str:
    suggestion = AutomationService(repository).suggest_next_action(expire=False)
    live_monitor = build_live_monitor_snapshot(
        repository,
        profile=get_profile("micro-live"),
        allow_live_auto=False,
        live_market_ids=set(),
    )
    body = "".join(
        [
            _render_flash(query or {}, lang),
            '<section class="card">',
            f'<p class="warning">{_t(lang, "operator.warning")}</p>',
            f"<p><strong>{_e(suggestion.label)}</strong>: {_e(suggestion.reason)}</p>",
            f"<pre>{_e(suggestion.command)}</pre>",
            '<form method="post" action="'
            + _href("/operator/tick", lang)
            + '" class="inline-actions">',
            _hidden_lang(lang),
            f"<label>{_t(lang, 'overrides.profile')} {_operator_profile_select(lang)}</label>",
            '<label><input type="checkbox" name="include_reconciliation" value="true"> '
            + _t(lang, "nav.reconciliation")
            + "</label>",
            f'<button type="submit">{_t(lang, "operator.tick")}</button>',
            "</form>",
            "</section>",
            _live_monitor_card(live_monitor, lang),
            _section(
                _t(lang, "overview.queue"),
                _counts_card(
                    _t(lang, "overview.queue"),
                    repository.automation_status_counts(),
                    _href("/actions", lang),
                    lang,
                ),
            ),
        ]
    )
    return render_page(_t(lang, "operator.title"), body, lang, current_path)


def _live_monitor_card(snapshot, lang: str) -> str:
    title = "Live 监控" if lang == "zh" else "Live Monitor"
    blockers_title = "阻塞原因" if lang == "zh" else "Blockers"
    no_blockers = "当前没有 live gate 阻塞。" if lang == "zh" else "No live gate blockers."
    actions_title = "Live Action Gate 明细" if lang == "zh" else "Live Action Gate Details"
    failed_gates_label = "失败 Gates" if lang == "zh" else "Failed Gates"
    blockers = (
        f'<p class="muted">{no_blockers}</p>'
        if not snapshot.blockers
        else "<ul>" + "".join(f"<li>{_e(blocker)}</li>" for blocker in snapshot.blockers) + "</ul>"
    )
    details = _definition_table(
        [
            ("profile", snapshot.profile),
            ("allow_live_auto", str(snapshot.allow_live_auto).lower()),
            ("risk_status", snapshot.risk_status),
            ("reconciliation_fresh", str(snapshot.reconciliation_fresh).lower()),
            ("open_orders", snapshot.open_orders_count),
            ("positions", snapshot.positions_count),
            ("nonzero_positions", snapshot.nonzero_positions_count),
            ("pending_live_actions", f"pending_live_actions={len(snapshot.pending_live_actions)}"),
        ]
    )
    actions_table = _live_monitor_actions_table(
        snapshot.pending_live_actions, lang, failed_gates_label
    )
    return "".join(
        [
            '<section class="card">',
            f"<h2>{title}</h2>",
            f'<p class="muted">allow_live_auto={str(snapshot.allow_live_auto).lower()} '
            f"pending_live_actions={len(snapshot.pending_live_actions)}</p>",
            details,
            f"<h3>{blockers_title}</h3>",
            blockers,
            f"<h3>{actions_title}</h3>",
            actions_table,
            "</section>",
        ]
    )


def _live_monitor_actions_table(actions, lang: str, failed_gates_label: str) -> str:
    if not actions:
        empty = "当前没有 pending live action。" if lang == "zh" else "No pending live actions."
        return f'<p class="muted">{empty}</p>'
    return _table(
        [
            _t(lang, "actions.action"),
            _t(lang, "actions.market"),
            "can_auto_execute",
            failed_gates_label,
        ],
        [
            [
                '<a href="'
                + _href("/actions/" + quote(action.action_id), lang)
                + '">'
                + _e(action.action_id)
                + "</a>",
                action.market_id or "-",
                _bool_label(action.can_auto_execute, lang),
                ", ".join(gate.name for gate in action.gates if not gate.ok) or "-",
            ]
            for action in actions
        ],
        lang,
        raw_columns={0},
    )


def render_overrides(
    repository: Repository,
    lang: str,
    current_path: str,
    query: dict[str, list[str]] | None = None,
) -> str:
    form = "".join(
        [
            '<section class="card">',
            f"<h2>{_t(lang, 'overrides.form_title')}</h2>",
            f'<p class="muted">{_t(lang, "overrides.safety_note")}</p>',
            f'<form method="post" action="{_href("/overrides/set", lang)}" class="stacked-form">',
            _hidden_lang(lang),
            f'<label>{_t(lang, "overrides.market")} <input name="market" value="*" required></label>',
            f"<label>{_t(lang, 'overrides.profile')} {_profile_select(lang)}</label>",
            f'<label>{_t(lang, "overrides.min_edge")} <input name="min_edge" placeholder="0.12"></label>',
            f'<label>{_t(lang, "overrides.max_order")} <input name="max_order_usdc" placeholder="3"></label>',
            f'<label>{_t(lang, "overrides.max_daily")} <input name="max_daily_usdc" placeholder="10"></label>',
            f'<label>{_t(lang, "overrides.max_market")} <input name="max_market_usdc" placeholder="5"></label>',
            f"<label>{_t(lang, 'overrides.live_auto')} {_live_auto_select(lang)}</label>",
            f'<label>{_t(lang, "overrides.notes")} <input name="notes" placeholder="tiny live test"></label>',
            f'<button type="submit">{_t(lang, "overrides.save")}</button>',
            "</form>",
            "</section>",
        ]
    )
    table = _table(
        [
            _t(lang, "overrides.market"),
            _t(lang, "overrides.profile"),
            _t(lang, "overrides.min_edge"),
            _t(lang, "overrides.max_order"),
            _t(lang, "overrides.max_daily"),
            _t(lang, "overrides.max_market"),
            _t(lang, "overrides.live_auto"),
            _t(lang, "overrides.notes"),
            _t(lang, "overrides.updated"),
            _t(lang, "actions.controls"),
        ],
        [
            [
                row["market_id"],
                row["profile"],
                _dash(row["min_edge"]),
                _dash(row["max_order_usdc"]),
                _dash(row["max_daily_usdc"]),
                _dash(row["max_market_usdc"]),
                _bool_label(row["live_auto_enabled"], lang),
                row["notes"] or "-",
                _display_time(row["updated_at"]),
                _override_delete_form(row, lang),
            ]
            for row in repository.list_strategy_overrides(limit=100)
        ],
        lang,
        raw_columns={9},
    )
    body = "".join([_render_flash(query or {}, lang), form, table])
    return render_page(_t(lang, "overrides.title"), body, lang, current_path)


def _overview_suggestion_controls(label: str, action: Row | None, lang: str) -> str:
    if label == "propose-next":
        return _propose_next_form(lang, compact=True)
    if action is not None:
        return _action_controls(action, lang, detail=True)
    return ""


def _propose_next_form(lang: str, *, compact: bool = False) -> str:
    kind_options = "".join(
        f'<option value="{kind}">{_kind_label(kind, lang)}</option>'
        for kind in ["analyze", "dry_run", "refresh_weather"]
    )
    ttl = (
        ""
        if compact
        else f'<label>{_t(lang, "actions.ttl")} <input name="ttl_minutes" placeholder="60"></label>'
    )
    reason = (
        ""
        if compact
        else f'<label>{_t(lang, "actions.reason")} <input name="reason" placeholder="operator review"></label>'
    )
    return "".join(
        [
            f'<form method="post" action="{_href("/actions/propose-next", lang)}" class="inline-actions">',
            _hidden_lang(lang),
            f'<label>{_t(lang, "actions.kind")} <select name="kind">{kind_options}</select></label>',
            reason,
            ttl,
            f'<button type="submit">{_t(lang, "actions.propose")}</button>',
            "</form>",
        ]
    )


def _action_controls(action: Row, lang: str, *, detail: bool = False) -> str:
    action_id = action["id"]
    status = action["status"]
    kind = action["kind"]
    pieces = ['<div class="inline-actions">']
    if status == "pending":
        if kind == "trade_live":
            pieces.append(f'<span class="warning">{_t(lang, "actions.live_blocked")}</span>')
        else:
            pieces.append(
                f'<form method="post" action="{_href(f"/actions/{quote(action_id)}/approve", lang)}">'
                f'{_hidden_lang(lang)}<button type="submit">{_t(lang, "actions.approve")}</button></form>'
            )
        pieces.append(
            f'<form method="post" action="{_href(f"/actions/{quote(action_id)}/reject", lang)}">'
            f"{_hidden_lang(lang)}"
            f'<input name="reason" placeholder="{_t(lang, "actions.reject_reason")}">'
            f'<button type="submit" class="danger">{_t(lang, "actions.reject")}</button></form>'
        )
    elif status == "approved":
        if kind == "trade_live":
            pieces.append(f'<span class="warning">{_t(lang, "actions.live_blocked")}</span>')
        else:
            pieces.append(
                f'<form method="post" action="{_href(f"/actions/{quote(action_id)}/run", lang)}">'
                f'{_hidden_lang(lang)}<button type="submit">{_t(lang, "actions.run")}</button></form>'
            )
    else:
        pieces.append(f'<span class="muted">{_t(lang, "actions.no_browser_action")}</span>')
    if not detail:
        pieces.append(
            f'<a href="{_href(f"/actions/{quote(action_id)}", lang)}">{_t(lang, "actions.view")}</a>'
        )
    pieces.append("</div>")
    return "".join(pieces)


def _action_safety_panel(action: Row, lang: str) -> str:
    if action["kind"] != "trade_live":
        return ""
    return "".join(
        [
            f"<h3>{_t(lang, 'actions.safety_title')}</h3>",
            f'<p class="warning">{_t(lang, "actions.live_blocked")}</p>',
            f'<p class="muted">{_t(lang, "actions.safety_text")}</p>',
        ]
    )


def _profile_select(lang: str) -> str:
    options = ['<option value="*">*</option>']
    options.extend(
        f'<option value="{_e(profile.name)}">{_e(profile.name)}</option>'
        for profile in list_profiles()
    )
    return f'<select name="profile">{"".join(options)}</select>'


def _live_auto_select(lang: str) -> str:
    return "".join(
        [
            '<select name="live_auto">',
            f'<option value="">{_t(lang, "overrides.no_override")}</option>',
            f'<option value="true">{_t(lang, "overrides.enabled")}</option>',
            f'<option value="false">{_t(lang, "overrides.disabled")}</option>',
            "</select>",
        ]
    )


def _override_delete_form(row: Row, lang: str) -> str:
    return "".join(
        [
            f'<form method="post" action="{_href("/overrides/delete", lang)}">',
            _hidden_lang(lang),
            f'<input type="hidden" name="market" value="{_e(row["market_id"])}">',
            f'<input type="hidden" name="profile" value="{_e(row["profile"])}">',
            f'<button type="submit" class="danger">{_t(lang, "overrides.delete")}</button>',
            "</form>",
        ]
    )


def _operator_profile_select(lang: str) -> str:
    options = []
    for profile in list_profiles():
        selected = " selected" if profile.name == "dry-run-demo" else ""
        options.append(f'<option value="{_e(profile.name)}"{selected}>{_e(profile.name)}</option>')
    return f'<select name="profile">{"".join(options)}</select>'


def _counts_card(title: str, rows: list[Row], href: str | None, lang: str) -> str:
    items = (
        "".join(f"<li>{_e(row['status'])}: {_e(str(row['count']))}</li>" for row in rows)
        or f"<li>{_t(lang, 'table.no_rows')}</li>"
    )
    heading = f'<a href="{href}">{_e(title)}</a>' if href else _e(title)
    return f'<section class="card"><h2>{heading}</h2><ul>{items}</ul></section>'


def _latest_failed_card(row: Row | None, lang: str) -> str:
    if row is None:
        return f'<section class="card"><h2>{_t(lang, "overview.latest_failure")}</h2><p class="muted">{_t(lang, "overview.no_failed_actions")}</p></section>'
    return "".join(
        [
            f'<section class="card"><h2>{_t(lang, "overview.latest_failure")}</h2>',
            '<p><a href="'
            + _href("/actions/" + quote(row["id"]), lang)
            + '">'
            + _e(row["id"])
            + "</a></p>",
            f"<p>{_e(_short_note(row['failure_reason']))}</p>",
            "</section>",
        ]
    )


def _market_cell(action: Row) -> str:
    title = _row_value(action, "market_title") or action["market_id"]
    return f'{_e(action["market_id"])}<br><span class="muted">{_e(title)}</span>'


def _row_value(row: Row, key: str) -> object | None:
    return row[key] if key in row.keys() else None
