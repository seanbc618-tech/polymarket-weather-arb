from __future__ import annotations

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
from polymarket_weather_arb.services.live_launchpad_service import (
    LiveLaunchpadCandidate,
    LiveLaunchpadPreview,
    LiveLaunchpadSnapshot,
)


def render_live_launchpad(
    snapshot: LiveLaunchpadSnapshot,
    lang: str,
    current_path: str,
    query: dict[str, list[str]] | None = None,
) -> str:
    body = "".join(
        [
            _render_flash(query or {}, lang),
            f"<h2>{_t(lang, 'live.title')}</h2>",
            f'<p class="muted">{_t(lang, "live.subtitle")}</p>',
            _section(_t(lang, "live.readiness"), _readiness_table(snapshot, lang)),
            _section(_t(lang, "live.exchange_state"), _exchange_state(snapshot, lang)),
            _section(_t(lang, "live.candidates"), _candidates_table(snapshot, lang)),
            _section(_t(lang, "live.preview"), _preview_panel(snapshot, lang)),
            _section(_t(lang, "live.execution_locked"), _execution_locked(lang)),
        ]
    )
    return render_page(_t(lang, "live.title"), body, lang, current_path)


def _readiness_table(snapshot: LiveLaunchpadSnapshot, lang: str) -> str:
    return _table(
        ["Gate", "OK", "Status", "Detail"],
        [
            [gate.name, "yes" if gate.ok else "no", gate.status, gate.detail]
            for gate in snapshot.readiness_gates
        ],
        lang,
    )


def _exchange_state(snapshot: LiveLaunchpadSnapshot, lang: str) -> str:
    exchange_rows = [
        [
            "LIVE_MARKET_IDS",
            ", ".join(snapshot.live_market_ids) if snapshot.live_market_ids else "-",
        ],
        ["reconciliation", snapshot.reconciliation_status],
        ["max_order_usdc", snapshot.max_order_usdc],
        ["max_daily_usdc", snapshot.max_daily_usdc],
        ["max_market_usdc", snapshot.max_market_usdc],
    ]
    order_rows = [
        [_t(lang, "live.open_orders"), str(snapshot.open_orders_count)],
        [_t(lang, "live.stale_open_orders"), str(snapshot.stale_open_orders_count)],
        [_t(lang, "live.open_order_notional"), f"{snapshot.open_orders_notional} USDC"],
    ]
    position_rows = [
        [_t(lang, "live.positions"), str(snapshot.positions_count)],
        [_t(lang, "live.nonzero_positions"), str(snapshot.nonzero_positions_count)],
        [_t(lang, "live.position_exposure"), f"{snapshot.position_total_exposure} USDC"],
        [_t(lang, "live.max_market_exposure"), f"{snapshot.position_max_market_exposure} USDC"],
        [
            _t(lang, "live.position_concentration"),
            _format_risk_ratio(snapshot.position_concentration_risk),
        ],
    ]
    refresh = (
        f'<form method="post" action="{_href("/live/refresh", lang)}">'
        f"{_hidden_lang(lang)}"
        f'<button type="submit">{_t(lang, "live.refresh")}</button>'
        "</form>"
    )
    controls = _order_safety_controls(snapshot, lang)
    market_exposures = _market_exposure_table(snapshot, lang)
    return "".join(
        [
            refresh,
            _table(["Field", "Value"], exchange_rows, lang),
            f"<h3>{_t(lang, 'live.order_risk')}</h3>",
            _table(["Field", "Value"], order_rows, lang),
            controls,
            f"<h3>{_t(lang, 'live.position_risk')}</h3>",
            _table(["Field", "Value"], position_rows, lang),
            market_exposures,
        ]
    )


def _order_safety_controls(snapshot: LiveLaunchpadSnapshot, lang: str) -> str:
    stale_disabled = " disabled" if snapshot.stale_open_orders_count == 0 else ""
    all_disabled = " disabled" if snapshot.open_orders_count == 0 else ""
    return "".join(
        [
            '<div class="inline-actions">',
            f'<form method="post" action="{_href("/live/cancel-stale-orders", lang)}">',
            _hidden_lang(lang),
            f'<button type="submit" class="danger"{stale_disabled}>',
            _t(lang, "live.cancel_stale_orders"),
            "</button>",
            "</form>",
            f'<form method="post" action="{_href("/live/cancel-all-orders", lang)}">',
            _hidden_lang(lang),
            f'<input name="confirmation" placeholder="{_e(_t(lang, "live.cancel_all_phrase"))}">',
            f'<button type="submit" class="danger"{all_disabled} ',
            f"onclick=\"return confirm('{_e(_t(lang, 'live.cancel_all_confirm'))}')\">",
            _t(lang, "live.cancel_all_orders"),
            "</button>",
            "</form>",
            "</div>",
        ]
    )


def _market_exposure_table(snapshot: LiveLaunchpadSnapshot, lang: str) -> str:
    if not snapshot.position_market_exposures:
        return f'<p class="muted">{_t(lang, "live.no_market_exposure")}</p>'
    rows = [
        [market_id, f"{exposure} USDC"]
        for market_id, exposure in sorted(snapshot.position_market_exposures.items())
    ]
    return _table([_t(lang, "live.market"), _t(lang, "live.exposure")], rows, lang)


def _format_risk_ratio(value: str) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except ValueError:
        return value


def _candidates_table(snapshot: LiveLaunchpadSnapshot, lang: str) -> str:
    if not snapshot.candidates:
        return f'<p class="muted">{_t(lang, "live.no_candidates")}</p>'
    return (
        '<div class="live-candidates">'
        + "".join(_candidate_panel(candidate, lang) for candidate in snapshot.candidates)
        + "</div>"
    )


def _candidate_panel(candidate: LiveLaunchpadCandidate, lang: str) -> str:
    fields = [
        (
            _t(lang, "live.bid_ask"),
            _e(f"{candidate.best_bid or '-'} / {candidate.best_ask or '-'}"),
        ),
        (
            _t(lang, "live.whitelist"),
            _gate_cell(candidate.whitelisted, "LIVE_MARKET_IDS", _t(lang, "live.add_to_whitelist")),
        ),
        (_t(lang, "live.override"), _override_cell(candidate, lang)),
        (
            _t(lang, "live.reconciliation"),
            _gate_cell(
                candidate.reconciliation_fresh,
                _t(lang, "live.fresh"),
                _t(lang, "live.refresh_needed"),
            ),
        ),
        (_t(lang, "live.max_order"), _e(f"{candidate.max_order_usdc} USDC")),
        (_t(lang, "live.credibility"), _credibility_cell(candidate)),
        (_t(lang, "live.model_trust"), _model_trust_cell(candidate, lang)),
        (_t(lang, "live.next_step"), _next_step(candidate, lang)),
        (_t(lang, "live.action"), _preview_button(candidate, lang)),
        (_t(lang, "live.blockers"), _blockers_cell(candidate, lang)),
        (_t(lang, "live.cli_command"), _cli_command_cell(candidate, lang)),
    ]
    grid = "".join(
        f'<div><span class="live-field-label">{_e(label)}</span><div class="live-field-value">{value}</div></div>'
        for label, value in fields
    )
    return (
        '<div class="live-candidate">'
        f"{_candidate_title(candidate)}"
        f'<div class="live-candidate-grid">{grid}</div>'
        "</div>"
    )


def _candidate_title(candidate: LiveLaunchpadCandidate) -> str:
    return (
        f"<strong>{_e(candidate.title)}</strong>"
        f'<br><span class="muted">{_e(candidate.market_id)} · {_e(candidate.module_id)}</span>'
    )


def _gate_cell(ok: bool, ok_text: str, blocked_text: str) -> str:
    label = ok_text if ok else blocked_text
    klass = "ok" if ok else "warning"
    return f'<span class="{klass}">{_e("yes" if ok else "no")}</span><br><span class="muted">{_e(label)}</span>'


def _credibility_cell(candidate: LiveLaunchpadCandidate) -> str:
    return (
        f"{_e(candidate.credibility_live_eligibility)}"
        f'<br><span class="muted">{_e(candidate.credibility_rule_status)} · '
        f"{_e(candidate.credibility_source_grade)}</span>"
    )


def _model_trust_cell(candidate: LiveLaunchpadCandidate, lang: str) -> str:
    detail = _t(lang, "live.calibration_samples").format(
        resolved=candidate.calibration_resolved_signals,
        total=candidate.calibration_total_signals,
    )
    scores = []
    if candidate.calibration_brier_score is not None:
        scores.append(f"Brier {candidate.calibration_brier_score}")
    if candidate.calibration_hit_rate is not None:
        scores.append(f"hit {_format_risk_ratio(candidate.calibration_hit_rate)}")
    score_text = " · ".join(scores) if scores else _t(lang, "live.calibration_no_scores")
    return (
        f"{_e(candidate.calibration_status)}"
        f'<br><span class="muted">{_e(detail)} · {_e(score_text)}</span>'
    )


def _override_cell(candidate: LiveLaunchpadCandidate, lang: str) -> str:
    if candidate.override_enabled:
        return _gate_cell(True, _t(lang, "live.override_on"), "")
    return "".join(
        [
            _gate_cell(False, "", _t(lang, "live.override_missing")),
            f'<form method="post" action="{_href("/live/override", lang)}" class="inline-actions">',
            _hidden_lang(lang),
            f'<input type="hidden" name="market_id" value="{_e(candidate.market_id)}">',
            '<input name="max_order_usdc" value="2" size="4">',
            f'<button type="submit">{_t(lang, "live.enable_override")}</button>',
            "</form>",
        ]
    )


def _next_step(candidate: LiveLaunchpadCandidate, lang: str) -> str:
    if candidate.can_preview:
        return f'<span class="ok">{_t(lang, "live.ready_to_preview")}</span>'
    if not candidate.whitelisted:
        return _e(_t(lang, "live.next_whitelist").format(market_id=candidate.market_id))
    if not candidate.override_enabled:
        return _e(_t(lang, "live.next_override"))
    if not candidate.reconciliation_fresh:
        return _e(_t(lang, "live.next_refresh"))
    return _e("; ".join(candidate.blockers))


def _preview_button(candidate: LiveLaunchpadCandidate, lang: str) -> str:
    disabled = "" if candidate.can_preview else " disabled"
    return (
        f'<form method="post" action="{_href("/live/preview", lang)}">'
        f"{_hidden_lang(lang)}"
        f'<input type="hidden" name="market_id" value="{_e(candidate.market_id)}">'
        f'<button type="submit"{disabled}>{_t(lang, "live.preview_button")}</button>'
        "</form>"
    )


def _blockers_cell(candidate: LiveLaunchpadCandidate, lang: str) -> str:
    """显示阻断因素"""
    if not candidate.blockers:
        return f'<span class="ok">{_t(lang, "live.no_blockers")}</span>'
    items = "".join(f"<li>{_e(blocker)}</li>" for blocker in candidate.blockers)
    return f'<ul class="blockers-list">{items}</ul>'


def _cli_command_cell(candidate: LiveLaunchpadCandidate, lang: str) -> str:
    """显示下一步 CLI 命令"""
    if candidate.can_preview:
        return (
            f"<code>uv run polymarket-weather trade --market {candidate.market_id} --dry-run</code>"
        )
    if not candidate.whitelisted:
        return f"<code>uv run polymarket-weather operator override-set --market {candidate.market_id} --profile micro-live --live-auto</code>"
    if not candidate.reconciliation_fresh:
        return "<code>uv run polymarket-weather reconcile</code>"
    return "<code>uv run polymarket-weather live-readiness</code>"


def _preview_panel(snapshot: LiveLaunchpadSnapshot, lang: str) -> str:
    if snapshot.preview is None:
        return f'<p class="muted">{_t(lang, "live.no_preview")}</p>'
    preview = snapshot.preview
    return "".join(
        [
            _table(
                ["Field", "Value"],
                [
                    ["market", preview.market_id],
                    ["side", preview.side],
                    ["token_id", preview.token_id or "-"],
                    ["limit_price", preview.limit_price],
                    ["size", preview.size],
                    ["notional", preview.notional],
                    ["max_loss", preview.max_loss],
                    ["risk", "accepted" if preview.accepted else "blocked"],
                    ["risk_reasons", "; ".join(preview.risk_reasons)],
                    ["rationale", preview.rationale],
                ],
                lang,
            ),
            _proposal_form(preview, lang)
            if preview.accepted
            else f'<p class="warning">{_t(lang, "live.preview_blocked")}</p>',
        ]
    )


def _proposal_form(preview: LiveLaunchpadPreview, lang: str) -> str:
    return (
        f'<form method="post" action="{_href("/live/propose", lang)}" class="stacked-form">'
        f"{_hidden_lang(lang)}"
        f'<input type="hidden" name="market_id" value="{_e(preview.market_id)}">'
        f'<label><input type="checkbox" name="ack" value="true"> {_t(lang, "live.real_money_ack")}</label>'
        f"<label>{_t(lang, 'live.confirm_phrase')} "
        '<input name="confirmation" value=""></label>'
        f'<button type="submit" class="danger">{_t(lang, "live.propose")}</button>'
        "</form>"
    )


def _execution_locked(lang: str) -> str:
    return (
        f"<p>{_t(lang, 'live.execution_locked_body')}</p>"
        '<button class="danger" disabled>'
        f"{_t(lang, 'live.execution_locked')}"
        "</button>"
    )
