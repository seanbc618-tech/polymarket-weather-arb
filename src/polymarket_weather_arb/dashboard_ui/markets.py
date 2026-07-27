from __future__ import annotations

from sqlite3 import Row
from urllib.parse import quote

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.dashboard_ui.html import (
    _bool_label,
    _dash,
    _definition_table,
    _display_time,
    _e,
    _hidden_lang,
    _href,
    _json_list_label,
    _kind_label,
    _render_flash,
    _section,
    _short_note,
    _status_label,
    _table,
    _tags_label,
    render_page,
)
from polymarket_weather_arb.dashboard_ui.i18n import _t
from polymarket_weather_arb.dashboard_ui.overview import (
    _render_market_readiness,
    _workbench_label,
)
from polymarket_weather_arb.domain.rules import parse_resolution_rule
from polymarket_weather_arb.modules.registry import get_module, list_modules
from polymarket_weather_arb.services.automation_service import AutomationService
from polymarket_weather_arb.services.module_workflows import resolve_module_workflow
from polymarket_weather_arb.storage.repositories import Repository

CANDIDATE_STATUSES = {"needs_review", "dry_run_ready", "rejected", "reviewed", "ignored"}


def render_markets(
    repository: Repository, lang: str, current_path: str, *, module_id: str | None = None
) -> str:
    markets = repository.list_weather_market_overview(limit=200, module_id=module_id)
    filters = _module_filter_form("/markets", lang, module_id)
    table = _table(
        [
            _t(lang, "field.id"),
            _t(lang, "field.module"),
            _t(lang, "field.title"),
            _t(lang, "actions.status"),
            _t(lang, "candidates.status"),
            _t(lang, "field.bucket"),
            _t(lang, "field.target_date"),
            _t(lang, "field.best_bid"),
            _t(lang, "field.best_ask"),
            _t(lang, "field.spread"),
            _t(lang, "field.fetched"),
            _t(lang, "actions.controls"),
        ],
        [
            [
                row["id"],
                _module_label(row["module_id"], lang),
                _short_note(row["title"], 120),
                row["status"] or "-",
                _candidate_status_label(row["candidate_status"], lang),
                _bucket_summary(row),
                _dash(row["target_date"]),
                _dash(row["best_bid"]),
                _dash(row["best_ask"]),
                _dash(row["spread"]),
                _display_time(row["snapshot_fetched_at"]),
                '<a href="'
                + _href("/markets/" + quote(row["id"]), lang)
                + '">'
                + _t(lang, "markets.open")
                + "</a>",
            ]
            for row in markets
        ],
        lang,
        raw_columns={11},
    )
    return render_page(_t(lang, "markets.title"), filters + table, lang, current_path)


def render_candidates(
    repository: Repository,
    lang: str,
    current_path: str,
    *,
    status: str | None = None,
    module_id: str | None = None,
    query: dict[str, list[str]] | None = None,
) -> str:
    candidates = repository.list_candidates(limit=200, status=status, module_id=module_id)
    filters = "".join(
        [
            _render_flash(query or {}, lang),
            '<form method="get" class="filters">',
            _hidden_lang(lang),
            f"<label>{_t(lang, 'field.module')} {_module_select(lang, module_id, include_blank=True)}</label>",
            f"<label>{_t(lang, 'candidates.filters.status')} {_candidate_status_select(lang, status, include_blank=True)}</label>",
            f'<button type="submit">{_t(lang, "candidates.filters.submit")}</button>',
            f'<a href="{_href("/candidates", lang)}">{_t(lang, "candidates.filters.clear")}</a>',
            "</form>",
        ]
    )
    table = _table(
        [
            _t(lang, "actions.market"),
            _t(lang, "field.module"),
            _t(lang, "field.title"),
            _t(lang, "candidates.status"),
            _t(lang, "field.bucket"),
            _t(lang, "field.target_date"),
            _t(lang, "field.best_bid"),
            _t(lang, "field.best_ask"),
            _t(lang, "field.spread"),
            _t(lang, "field.result"),
            _t(lang, "candidates.notes"),
            _t(lang, "actions.controls"),
        ],
        [
            [
                '<a href="'
                + _href("/markets/" + quote(row["market_id"]), lang)
                + '">'
                + _e(row["market_id"])
                + "</a>",
                _module_label(row["module_id"], lang),
                _short_note(row["title"], 100),
                _candidate_status_label(row["status"], lang),
                _bucket_summary(row),
                _dash(row["target_date"]),
                _dash(row["best_bid"]),
                _dash(row["best_ask"]),
                _dash(row["spread"]),
                _candidate_rejection_label(row),
                _short_note(row["notes"]),
                _candidate_research_form(row["market_id"], lang)
                + _candidate_mark_form(row["market_id"], lang),
            ]
            for row in candidates
        ],
        lang,
        raw_columns={0, 11},
    )
    return render_page(_t(lang, "candidates.title"), filters + table, lang, current_path)


def render_market_detail(
    repository: Repository,
    market: Row,
    lang: str,
    current_path: str,
    query: dict[str, list[str]] | None = None,
) -> str:
    market_id = market["id"]
    rule = repository.get_resolution_rule(market_id)
    bucket_rule = repository.get_temperature_bucket_rule(market_id)
    snapshot = repository.latest_market_snapshot(market_id)
    forecast = repository.latest_forecast(market_id)
    analysis = repository.latest_analysis(market_id)
    intents = repository.list_recent_order_intents(limit=20, market_id=market_id)
    readiness = resolve_module_workflow(
        Settings(), repository, module_id=market["module_id"]
    ).readiness(market_id)
    actions = AutomationService(repository).list_actions(limit=20, expire=False)
    related_actions = [action for action in actions if action["market_id"] == market_id]
    body = "".join(
        [
            _render_flash(query or {}, lang),
            f'<p><a href="{_href("/markets", lang)}">{_t(lang, "nav.markets")}</a></p>',
            f'<p class="muted">{_t(lang, "markets.workflow_hint")}</p>',
            '<section class="card">',
            f"<h2>{_e(market['title'])}</h2>",
            _definition_table(
                [
                    (_t(lang, "field.id"), market_id),
                    (_t(lang, "field.module"), _module_label(market["module_id"], lang)),
                    (_t(lang, "field.status"), market["status"]),
                    (_t(lang, "field.category"), market["category"]),
                    (_t(lang, "field.tags"), _tags_label(market["tags"])),
                    (_t(lang, "field.close_time"), market["close_time"]),
                    (_t(lang, "field.description"), market["description"]),
                ]
            ),
            _market_workflow_controls(market_id, lang),
            "</section>",
            _section(
                _workbench_label(lang, "readiness"), _render_market_readiness(readiness, lang)
            ),
            _section(
                _t(lang, "markets.bucket_rule"), _render_temperature_bucket_rule(bucket_rule, lang)
            )
            if bucket_rule
            else "",
            _section(_t(lang, "markets.rule"), _render_rule(rule, market, lang)),
            _section(_t(lang, "markets.snapshot"), _render_snapshot(snapshot, lang)),
            _section(_t(lang, "markets.forecast"), _render_forecast(forecast, lang)),
            _section(_t(lang, "markets.analysis"), _render_analysis(analysis, lang)),
            _section(_t(lang, "markets.order_intents"), _orders_table(intents, lang)),
            _section(_t(lang, "markets.related_actions"), _actions_table(related_actions, lang)),
        ]
    )
    return render_page(_t(lang, "markets.detail"), body, lang, current_path)


def _market_workflow_controls(market_id: str, lang: str) -> str:
    primary_forms = "".join(
        [
            _market_operation_form(market_id, "research", _t(lang, "markets.research"), lang),
            _market_operation_form(market_id, "trade-dry-run", _t(lang, "markets.dry_run"), lang),
        ]
    )
    debug_forms = "".join(
        [
            _market_operation_form(market_id, "inspect", _t(lang, "markets.inspect"), lang),
            _market_operation_form(
                market_id, "refresh-weather", _t(lang, "markets.refresh_weather"), lang
            ),
            _market_operation_form(market_id, "analyze", _t(lang, "markets.analyze"), lang),
        ]
    )
    return "".join(
        [
            f'<div class="inline-actions">{primary_forms}</div>',
            "<details><summary>" + _t(lang, "markets.debug_actions") + "</summary>",
            f'<div class="inline-actions">{debug_forms}</div>',
            "</details>",
        ]
    )


def _market_operation_form(market_id: str, operation: str, label: str, lang: str) -> str:
    return "".join(
        [
            f'<form method="post" action="{_href(f"/markets/{quote(market_id)}/{operation}", lang)}">',
            _hidden_lang(lang),
            f'<button type="submit">{label}</button>',
            "</form>",
        ]
    )


def _candidate_research_form(market_id: str, lang: str) -> str:
    return _market_operation_form(market_id, "research", _t(lang, "markets.research"), lang)


def _candidate_mark_form(market_id: str, lang: str) -> str:
    return "".join(
        [
            f'<form method="post" action="{_href(f"/candidates/{quote(market_id)}/mark", lang)}" class="inline-actions">',
            _hidden_lang(lang),
            _candidate_status_select(lang, None, include_blank=False),
            f'<input name="notes" placeholder="{_t(lang, "candidates.notes")}">',
            f'<button type="submit">{_t(lang, "candidates.mark")}</button>',
            "</form>",
        ]
    )


def _candidate_status_select(lang: str, selected: str | None, *, include_blank: bool) -> str:
    options = ['<option value=""></option>'] if include_blank else []
    for status in sorted(CANDIDATE_STATUSES):
        attr = " selected" if status == selected else ""
        options.append(
            f'<option value="{_e(status)}"{attr}>{_candidate_status_label(status, lang)}</option>'
        )
    return f'<select name="status">{"".join(options)}</select>'


def _module_select(lang: str, selected: str | None, *, include_blank: bool) -> str:
    options = ['<option value=""></option>'] if include_blank else []
    for module in list_modules():
        attr = " selected" if module.id == selected else ""
        options.append(
            f'<option value="{_e(module.id)}"{attr}>{_t(lang, module.label_key)}</option>'
        )
    return f'<select name="module">{"".join(options)}</select>'


def _module_filter_form(path: str, lang: str, module_id: str | None) -> str:
    return "".join(
        [
            '<form method="get" class="filters">',
            _hidden_lang(lang),
            f"<label>{_t(lang, 'field.module')} {_module_select(lang, module_id, include_blank=True)}</label>",
            f'<button type="submit">{_t(lang, "candidates.filters.submit")}</button>',
            f'<a href="{_href(path, lang)}">{_t(lang, "candidates.filters.clear")}</a>',
            "</form>",
        ]
    )


def _valid_module_filter(module_id: str | None) -> str | None:
    if not module_id:
        return None
    return get_module(module_id).id


def _module_label(module_id: str | None, lang: str) -> str:
    if not module_id:
        return "-"
    try:
        module = get_module(module_id)
    except KeyError:
        return module_id
    return _t(lang, module.label_key)


def _bucket_summary(row: Row) -> str:
    if row["bucket_lower_c"] is None or row["bucket_upper_c"] is None:
        return "-"
    city = row["city"] or row["city_cn"] or "-"
    return f"{city} {row['bucket_lower_c']}-{row['bucket_upper_c']}C"


def _candidate_status_label(status: object | None, lang: str) -> str:
    if status is None:
        return "-"
    return _t(lang, f"market_status.{status}")


def _candidate_rejection_label(row: Row) -> str:
    if row["tradable"]:
        return "-"
    return _short_note(row["rejection_reason"])


def _render_rule(rule: Row | None, market: Row, lang: str) -> str:
    if rule is None:
        parsed = parse_resolution_rule(market["title"], market["description"])
        return (
            '<p class="muted">'
            + _t(lang, "markets.no_rule")
            + "</p>"
            + _definition_table(
                [
                    (_t(lang, "field.variable"), parsed.variable),
                    (_t(lang, "field.value"), parsed.threshold),
                    (_t(lang, "field.unit"), parsed.unit),
                    (
                        _t(lang, "field.status"),
                        "tradable" if parsed.tradable else parsed.rejection_reason,
                    ),
                ]
            )
        )
    return _definition_table(
        [
            (_t(lang, "field.status"), _bool_label(rule["tradable"], lang)),
            (_t(lang, "field.variable"), rule["variable"]),
            (_t(lang, "field.side"), rule["operator"]),
            (_t(lang, "field.value"), rule["threshold"]),
            (_t(lang, "field.unit"), rule["unit"]),
            (_t(lang, "actions.market"), rule["location"] or rule["station"]),
            (_t(lang, "field.result"), rule["rejection_reason"]),
            (_t(lang, "actions.updated"), rule["updated_at"]),
        ]
    )


def _render_temperature_bucket_rule(row: Row | None, lang: str) -> str:
    if row is None:
        return f'<p class="muted">{_t(lang, "markets.no_rule")}</p>'
    return _definition_table(
        [
            (_t(lang, "field.status"), _bool_label(row["tradable"], lang)),
            (
                _t(lang, "field.city"),
                f"{row['city']} / {row['city_cn']}" if row["city_cn"] else row["city"],
            ),
            (_t(lang, "field.station"), row["settlement_station_id"] or row["station_id"]),
            (_t(lang, "field.source"), row["source"]),
            (_t(lang, "field.variable"), row["variable"]),
            (
                _t(lang, "field.bucket"),
                f"{row['bucket_lower_c']}-{row['bucket_upper_c']}C center={row['bucket_center_c']}C",
            ),
            (_t(lang, "field.target_date"), row["target_date"]),
            (_t(lang, "field.timezone"), row["settlement_timezone"]),
            (_t(lang, "field.confidence"), row["confidence"]),
            (_t(lang, "field.result"), row["rejection_reason"]),
            (_t(lang, "actions.updated"), row["updated_at"]),
        ]
    )


def _render_snapshot(row: Row | None, lang: str) -> str:
    if row is None:
        return f'<p class="muted">{_t(lang, "markets.no_snapshot")}</p>'
    return _definition_table(
        [
            (_t(lang, "field.best_bid"), row["best_bid"]),
            (_t(lang, "field.best_ask"), row["best_ask"]),
            (_t(lang, "field.price"), row["midpoint"]),
            (_t(lang, "field.spread"), row["spread"]),
            (_t(lang, "field.notional"), row["liquidity"]),
            (_t(lang, "field.fetched"), _display_time(row["fetched_at"])),
        ]
    )


def _render_forecast(row: Row | None, lang: str) -> str:
    if row is None:
        return f'<p class="muted">{_t(lang, "markets.no_forecast")}</p>'

    # Basic forecast info
    basic_info = [
        (_t(lang, "setup.weather_provider"), row["provider"]),
        (_t(lang, "field.variable"), row["variable"]),
        (_t(lang, "field.value"), row["value"]),
        (_t(lang, "field.unit"), row["unit"]),
        (_t(lang, "actions.market"), row["location"] or row["station"]),
        (_t(lang, "field.fetched"), _display_time(row["fetched_at"])),
    ]

    # Check if this is an ensemble forecast
    raw_payload = row["raw_payload"]
    if raw_payload:
        try:
            import json

            payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
            source_grade = payload.get("source_grade", "")

            # Always surface forecast provenance so operators do not confuse
            # official forecasts with settlement observations.
            if source_grade:
                grade_class = "ok" if source_grade == "official_forecast" else "warning"
                basic_info.append(
                    (
                        _t(lang, "markets.source_grade"),
                        f'<span class="{grade_class}">{source_grade}</span>',
                    )
                )
                hint = _t(lang, "markets.source_grade_hint")
                if hint and hint != "markets.source_grade_hint":
                    basic_info.append(("Note", hint))

            # Add ensemble-specific info
            if (
                source_grade in {"research_forecast", "research_grade"}
                or payload.get("provider") == "open-meteo-ensemble"
            ):
                ensemble_info = [
                    (_t(lang, "markets.ensemble_mean"), payload.get("mean", "-")),
                    (_t(lang, "markets.ensemble_std"), payload.get("std", "-")),
                    (_t(lang, "markets.ensemble_members"), payload.get("member_count", "-")),
                    (_t(lang, "markets.ensemble_agreement"), payload.get("agreement", "-")),
                ]
                basic_info.extend(ensemble_info)
            references = payload.get("pricing_references") or {}
            google = references.get("google_weather") if isinstance(references, dict) else None
            if isinstance(google, dict):
                basic_info.extend(
                    [
                        (
                            _t(lang, "markets.google_weather_reference"),
                            f"{google.get('value', '-')} {google.get('unit', '')}".strip(),
                        ),
                        (_t(lang, "markets.google_weather_target"), google.get("target_date")),
                        (_t(lang, "markets.google_weather_timezone"), google.get("timezone")),
                    ]
                )
        except (json.JSONDecodeError, TypeError):
            pass

    return _definition_table(basic_info)


def _render_analysis(row: Row | None, lang: str) -> str:
    if row is None:
        return f'<p class="muted">{_t(lang, "markets.no_analysis")}</p>'
    reasons = _json_list_label(row["reasons"])
    return _definition_table(
        [
            (_t(lang, "field.decision"), row["decision"]),
            (_t(lang, "field.side"), row["side"]),
            (_t(lang, "field.edge"), row["edge"]),
            (_t(lang, "field.price"), row["reference_price"]),
            (_t(lang, "field.value"), f"{row['fair_lower']} - {row['fair_upper']}"),
            (_t(lang, "field.rationale"), reasons),
            (_t(lang, "field.created"), _display_time(row["created_at"])),
        ]
    )


def _orders_table(rows: list[Row], lang: str) -> str:
    def fill_progress(row: Row) -> str:
        keys = set(row.keys())
        actual = row["filled_size"] if "filled_size" in keys else 0
        return f"{float(actual or 0):g} / {float(row['size'] or 0):g}"

    return _table(
        [
            _t(lang, "field.id"),
            _t(lang, "actions.market"),
            _t(lang, "field.side"),
            _t(lang, "field.price"),
            _t(lang, "field.size"),
            _t(lang, "field.fill_progress"),
            _t(lang, "field.notional"),
            _t(lang, "field.dry_run"),
            _t(lang, "actions.status"),
            _t(lang, "field.created"),
            _t(lang, "field.rationale"),
        ],
        [
            [
                row["id"],
                row["market_id"],
                row["side"],
                row["limit_price"],
                row["size"],
                fill_progress(row),
                row["notional"],
                _bool_label(row["dry_run"], lang),
                row["status"],
                _display_time(row["created_at"]),
                _short_note(row["rationale"]),
            ]
            for row in rows
        ],
        lang,
    )


def _actions_table(rows: list[Row], lang: str) -> str:
    return _table(
        [
            _t(lang, "actions.action"),
            _t(lang, "actions.kind"),
            _t(lang, "actions.status"),
            _t(lang, "actions.updated"),
            _t(lang, "actions.controls"),
        ],
        [
            [
                row["id"],
                _kind_label(row["kind"], lang),
                _status_label(row["status"], lang),
                _display_time(row["updated_at"]),
                '<a href="'
                + _href("/actions/" + quote(row["id"]), lang)
                + '">'
                + _t(lang, "actions.view")
                + "</a>",
            ]
            for row in rows
        ],
        lang,
        raw_columns={4},
    )
