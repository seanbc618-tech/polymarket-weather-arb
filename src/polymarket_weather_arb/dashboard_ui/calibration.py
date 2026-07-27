from __future__ import annotations

from decimal import Decimal

from polymarket_weather_arb.dashboard_ui.html import (
    _e,
    _hidden_lang,
    _href,
    _render_flash,
    _section,
    _table,
    render_page,
)
from polymarket_weather_arb.dashboard_ui.i18n import _t
from polymarket_weather_arb.services.calibration_service import CalibrationService
from polymarket_weather_arb.storage.repositories import Repository


def render_calibration(
    repository: Repository,
    lang: str,
    current_path: str,
    query: dict[str, list[str]] | None = None,
) -> str:
    report = CalibrationService(repository).report()
    q = query or {}
    preview_card = _preview_result_card(q, lang)
    body = "".join(
        [
            _render_flash(q, lang),
            f"<h2>{_t(lang, 'calibration.title')}</h2>",
            f'<p class="muted">{_t(lang, "calibration.subtitle")}</p>',
            _section(_t(lang, "calibration.scoreboard"), _scoreboard(report.groups, lang)),
            _section(
                _t(lang, "calibration.manual_settlement"),
                _settlement_forms(lang),
            ),
            preview_card,
            _section(
                _t(lang, "calibration.recent_signals"),
                _recent_signals(repository, lang),
            ),
            _section(
                _t(lang, "calibration.recent_observations"),
                _recent_observations(repository, lang),
            ),
        ]
    )
    return render_page(_t(lang, "calibration.title"), body, lang, current_path)


def _scoreboard(groups, lang: str) -> str:
    rows = [
        [
            group.model_version,
            group.forecast_provider,
            group.horizon,
            group.total_signals,
            group.resolved_signals,
            group.distinct_events,
            _decimal(group.brier_score),
            _percent(group.hit_rate),
            _percent(group.malformed_rate),
            _decimal(group.effective_weight),
            _decimal(group.average_edge),
            group.status,
            group.weight_reason,
        ]
        for group in groups
    ]
    return _table(
        [
            _t(lang, "calibration.model"),
            _t(lang, "calibration.provider"),
            _t(lang, "calibration.horizon"),
            _t(lang, "calibration.signals"),
            _t(lang, "calibration.resolved"),
            _t(lang, "calibration.distinct_events"),
            "Brier",
            _t(lang, "calibration.hit_rate"),
            _t(lang, "calibration.malformed_rate"),
            _t(lang, "calibration.effective_weight"),
            _t(lang, "calibration.avg_edge"),
            _t(lang, "calibration.status"),
            _t(lang, "calibration.weight_reason"),
        ],
        rows,
        lang,
    )


def _settlement_forms(lang: str) -> str:
    return "".join(
        [
            _official_observation_form(lang),
            _settlement_form(lang),
        ]
    )


def _official_observation_form(lang: str) -> str:
    return "".join(
        [
            f"<h3>{_t(lang, 'calibration.official_backfill')}</h3>",
            f'<p class="muted">{_t(lang, "calibration.official_backfill_hint")}</p>',
            f'<form method="post" action="{_href("/calibration/backfill-preview", lang)}" class="stacked-form">',
            _hidden_lang(lang),
            f"<label>{_t(lang, 'calibration.market_id')} "
            '<input name="market_id" placeholder="market id"></label>',
            f'<button type="submit">{_t(lang, "calibration.preview")}</button>',
            "</form>",
            f'<form method="post" action="{_href("/calibration/backfill", lang)}" class="stacked-form">',
            _hidden_lang(lang),
            f"<label>{_t(lang, 'calibration.market_id')} "
            '<input name="market_id" placeholder="market id"></label>',
            f'<button type="submit">{_t(lang, "calibration.backfill")}</button>',
            f'<p class="muted">{_t(lang, "calibration.backfill_hint")}</p>',
            "</form>",
        ]
    )


def _settlement_form(lang: str) -> str:
    return "".join(
        [
            f"<h3>{_t(lang, 'calibration.manual_entry')}</h3>",
            f'<form method="post" action="{_href("/calibration/settle", lang)}" class="stacked-form">',
            _hidden_lang(lang),
            f"<label>{_t(lang, 'calibration.market_id')} "
            '<input name="market_id" placeholder="market id"></label>',
            f"<label>{_t(lang, 'calibration.outcome')} "
            '<select name="outcome"><option value="yes">YES</option><option value="no">NO</option></select></label>',
            f"<label>{_t(lang, 'calibration.settlement_value')} "
            '<input name="settlement_value" placeholder="83"></label>',
            f"<label>{_t(lang, 'calibration.settlement_source')} "
            '<input name="settlement_source" placeholder="nws-observation"></label>',
            f'<button type="submit">{_t(lang, "calibration.settle")}</button>',
            "</form>",
        ]
    )


def _preview_result_card(query: dict[str, list[str]], lang: str) -> str:
    market_id = (query.get("preview_market_id") or [""])[0]
    if not market_id:
        return ""
    station = (query.get("preview_station") or ["-"])[0]
    variable = (query.get("preview_variable") or ["-"])[0]
    value = (query.get("preview_value") or ["-"])[0]
    unit = (query.get("preview_unit") or [""])[0]
    quality = (query.get("preview_quality") or ["-"])[0]
    outcome = (query.get("preview_outcome") or ["-"])[0]
    source = (query.get("preview_source") or ["-"])[0]
    raw_warnings = (query.get("preview_warnings") or [""])[0]
    warnings = [w for w in raw_warnings.split("|") if w]
    outcome_label = "YES" if outcome == "yes" else "NO"
    rows = [
        [_t(lang, "calibration.market_id"), _e(market_id)],
        [_t(lang, "calibration.station"), _e(station)],
        [_t(lang, "calibration.variable"), _e(variable)],
        [_t(lang, "calibration.observed_value"), f"{_e(value)} {_e(unit)}"],
        [_t(lang, "calibration.quality"), _e(quality)],
        [_t(lang, "calibration.would_resolve"), outcome_label],
        [_t(lang, "calibration.source"), _e(source)],
    ]
    table = _table(
        [_t(lang, "calibration.field"), _t(lang, "calibration.value_label")],
        rows,
        lang,
    )
    warnings_html = ""
    if warnings:
        items = "".join(f"<li>{_e(w)}</li>" for w in warnings)
        warnings_html = (
            f'<div class="warnings"><strong>{_t(lang, "calibration.warnings")}</strong>'
            f"<ul>{items}</ul></div>"
        )
    return _section(_t(lang, "calibration.preview_result"), table + warnings_html)


def _recent_signals(repository: Repository, lang: str) -> str:
    rows = [
        [
            row["market_id"],
            row["model_version"],
            row["forecast_provider"] or "-",
            _decimal(row["yes_probability"]),
            _decimal(row["market_price"]),
            _decimal(row["edge"]),
            row["decision"],
            row["outcome_status"],
            row["resolved_outcome"] or "-",
            row["created_at"],
        ]
        for row in repository.list_model_signals(limit=50)
    ]
    return _table(
        [
            _t(lang, "calibration.market_id"),
            _t(lang, "calibration.model"),
            _t(lang, "calibration.provider"),
            _t(lang, "calibration.yes_probability"),
            _t(lang, "calibration.market_price"),
            _t(lang, "calibration.edge"),
            _t(lang, "calibration.decision"),
            _t(lang, "calibration.outcome_status"),
            _t(lang, "calibration.resolved_outcome"),
            _t(lang, "calibration.created"),
        ],
        rows,
        lang,
    )


def _recent_observations(repository: Repository, lang: str) -> str:
    rows = [
        [
            row["market_id"] or "-",
            row["provider"],
            row["station"] or "-",
            row["variable"],
            f"{_decimal(row['value'])} {row['unit']}",
            row["quality_status"] or "-",
            row["observed_at"],
            row["fetched_at"],
        ]
        for row in repository.list_recent_observations(limit=50)
    ]
    return _table(
        [
            _t(lang, "calibration.market_id"),
            _t(lang, "calibration.provider"),
            _t(lang, "calibration.station"),
            _t(lang, "calibration.variable"),
            _t(lang, "calibration.observed_value"),
            _t(lang, "calibration.quality"),
            _t(lang, "calibration.observed_at"),
            _t(lang, "calibration.fetched_at"),
        ],
        rows,
        lang,
    )


def _decimal(value: object) -> str:
    if value is None:
        return "-"
    decimal = Decimal(str(value))
    text = format(decimal.normalize(), "f")
    return _e(text.rstrip("0").rstrip(".") if "." in text else text)


def _percent(value: object) -> str:
    if value is None:
        return "-"
    return f"{(Decimal(str(value)) * Decimal('100')).quantize(Decimal('0.1'))}%"
