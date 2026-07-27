from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.dashboard_ui.html import (
    _bool_label,
    _definition_table,
    _e,
    _hidden_lang,
    _href,
    _render_flash,
    _section,
    _table,
    render_page,
)
from polymarket_weather_arb.dashboard_ui.i18n import _t
from polymarket_weather_arb.dashboard_ui.markets import _orders_table
from polymarket_weather_arb.modules.registry import list_modules
from polymarket_weather_arb.profiles import list_profiles, profile_summary
from polymarket_weather_arb.storage.repositories import Repository

FIXTURE_DIRS = (Path("fixtures/markets"), Path("data/fixtures/markets"))


def render_discovery(
    lang: str, current_path: str, query: dict[str, list[str]] | None = None
) -> str:
    form = "".join(
        [
            _render_flash(query or {}, lang),
            '<section class="card">',
            f"<h2>{_t(lang, 'discovery.title')}</h2>",
            f'<p class="warning">{_t(lang, "discovery.warning")}</p>',
            f'<form method="post" action="{_href("/discovery/run", lang)}" class="inline-actions discovery-form">',
            _hidden_lang(lang),
            f'<label>{_t(lang, "discovery.module")} <select name="module"><option value="weather">{_t(lang, "module.weather.label")}</option><option value="china_temp_bucket">{_t(lang, "module.china_temp_bucket.label")}</option></select></label>',
            f'<label>{_t(lang, "discovery.limit")} <input name="limit" value="100"></label>',
            f'<label>{_t(lang, "discovery.pages")} <input name="pages" value="30"></label>',
            f'<label>{_t(lang, "discovery.max_ask")} <input name="max_ask" value="0.10"></label>',
            f'<label>{_t(lang, "discovery.event_date")} <input name="event_date" type="date"></label>',
            f'<label><input type="checkbox" name="include_unsupported" value="true"> {_t(lang, "discovery.include_unsupported")}</label>',
            f'<button type="submit" data-loading-label="{_e(_t(lang, "discovery.loading"))}">{_t(lang, "discovery.run")}</button>',
            "</form>",
            '<div class="scan-modal" role="status" aria-live="polite" aria-modal="true">',
            '<div class="scan-modal-card">',
            f"<strong>{_t(lang, 'discovery.progress_title')}</strong>",
            f"<p>{_t(lang, 'discovery.progress_text')}</p>",
            '<div class="progress-bar"><span></span></div>',
            f'<p class="muted">{_t(lang, "discovery.progress_hint")}</p>',
            "</div>",
            "</div>",
            "</section>",
        ]
    )
    return render_page(_t(lang, "discovery.title"), form, lang, current_path)


def render_orders(repository: Repository, lang: str, current_path: str) -> str:
    return render_page(
        _t(lang, "orders.title"),
        _orders_table(repository.list_recent_order_intents(limit=100), lang),
        lang,
        current_path,
    )


def render_profiles(lang: str, current_path: str) -> str:
    rows = []
    for profile in list_profiles():
        summary = profile_summary(profile)
        rows.append(
            [
                summary["name"],
                summary["role"],
                summary["default_action_kind"],
                summary["action_ttl_minutes"],
                f"{summary['discovery_limit']} x {summary['discovery_pages']}",
                _bool_label(summary["dry_run"], lang),
                summary["max_order_usdc"] or "-",
                summary["max_daily_usdc"] or "-",
                summary["max_market_usdc"] or "-",
                summary["min_edge"] or "-",
                summary["description"],
            ]
        )
    table = _table(
        [
            _t(lang, "field.profile"),
            _t(lang, "field.actor"),
            _t(lang, "actions.kind"),
            _t(lang, "actions.ttl"),
            _t(lang, "nav.discovery"),
            _t(lang, "field.dry_run"),
            _t(lang, "overrides.max_order"),
            _t(lang, "overrides.max_daily"),
            _t(lang, "overrides.max_market"),
            _t(lang, "overrides.min_edge"),
            _t(lang, "field.description"),
        ],
        rows,
        lang,
    )
    return render_page(_t(lang, "profiles.title"), table, lang, current_path)


def render_doctor(settings: Settings, lang: str, current_path: str) -> str:
    rows = []
    problems = _doctor_problems(settings, live=True)
    rows.append((_t(lang, "setup.database_path"), settings.database_path))
    rows.append((_t(lang, "setup.weather_provider"), settings.weather_provider))
    rows.append(
        (
            _t(lang, "setup.risk_caps"),
            f"order={settings.max_order_usdc} daily={settings.max_daily_usdc} market={settings.max_market_usdc}",
        )
    )
    rows.append(
        (
            _t(lang, "setup.live_ready"),
            _t(lang, "setup.configured")
            if _live_credentials_configured(settings)
            else _t(lang, "setup.missing"),
        )
    )
    status = _t(lang, "doctor.ok") if not problems else _t(lang, "doctor.warning")
    body = "".join(
        [
            '<section class="card">',
            _definition_table([(_t(lang, "actions.status"), status), *rows]),
            "</section>",
            _section(
                _t(lang, "field.details"),
                _table([_t(lang, "field.error")], [[problem] for problem in problems], lang),
            ),
        ]
    )
    return render_page(_t(lang, "doctor.title"), body, lang, current_path)


def render_fixtures(lang: str, current_path: str, query: dict[str, list[str]] | None = None) -> str:
    files = _fixture_files()
    rows = []
    for fixture in files:
        rows.append([fixture, _fixture_load_form(fixture, lang)])
    table = _table(
        [_t(lang, "field.id"), _t(lang, "actions.controls")], rows, lang, raw_columns={1}
    )
    if not files:
        table = f'<p class="muted">{_t(lang, "fixtures.no_files")}</p>'
    body = "".join([_render_flash(query or {}, lang), table])
    return render_page(_t(lang, "fixtures.title"), body, lang, current_path)


def render_modules(lang: str, current_path: str) -> str:
    rows = []
    for module in list_modules():
        rows.append(
            [
                module.id,
                _t(lang, module.label_key),
                _t(lang, module.description_key),
                _bool_label(module.supports_discovery, lang),
                _bool_label(module.supports_analysis, lang),
                _bool_label(module.supports_dry_run, lang),
                '<a href="'
                + _href(f"/modules/{quote(module.id)}/markets", lang)
                + '">'
                + _t(lang, "nav.markets")
                + "</a>",
            ]
        )
    table = _table(
        [
            _t(lang, "field.id"),
            _t(lang, "field.title"),
            _t(lang, "field.description"),
            _t(lang, "nav.discovery"),
            _t(lang, "markets.analyze"),
            _t(lang, "markets.dry_run"),
            _t(lang, "actions.controls"),
        ],
        rows,
        lang,
        raw_columns={6},
    )
    return render_page(_t(lang, "modules.title"), table, lang, current_path)


def render_setup(
    settings: Settings,
    lang: str,
    current_path: str,
    query: dict[str, list[str]] | None = None,
    **kwargs: object,
) -> str:
    # Expanded multi-step first-run flow (dashboard_ui.setup). Keep this wrapper so
    # existing imports continue to resolve to the product setup page.
    from polymarket_weather_arb.dashboard_ui.setup import render_setup as render_setup_flow

    return render_setup_flow(settings, lang, current_path, query, **kwargs)  # type: ignore[arg-type]


def _doctor_problems(settings: Settings, *, live: bool) -> list[str]:
    problems = []
    if settings.max_order_usdc > Decimal("25"):
        problems.append("MAX_ORDER_USDC is above hard cap; runtime will clamp to 25")
    if settings.max_daily_usdc > Decimal("100"):
        problems.append("MAX_DAILY_USDC is above hard cap; runtime will clamp to 100")
    if settings.max_market_usdc > Decimal("50"):
        problems.append("MAX_MARKET_USDC is above hard cap; runtime will clamp to 50")
    if live:
        try:
            settings.ensure_live_trading_ready()
        except ValueError as exc:
            problems.append(str(exc))
    return problems


def _fixture_files() -> list[str]:
    files = []
    for directory in FIXTURE_DIRS:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.json")):
            files.append(str(path))
    return files


def _fixture_load_form(fixture: str, lang: str) -> str:
    return "".join(
        [
            '<form method="post" action="'
            + _href("/fixtures/load", lang)
            + '" class="inline-actions">',
            _hidden_lang(lang),
            f'<input type="hidden" name="fixture" value="{_e(fixture)}">',
            '<label><input type="checkbox" name="demo_analysis" value="true"> '
            + _t(lang, "fixtures.demo_analysis")
            + "</label>",
            f'<button type="submit">{_t(lang, "fixtures.load")}</button>',
            "</form>",
        ]
    )


def _resolve_fixture_path(value: str) -> Path:
    candidate = Path(value)
    allowed_files = {Path(item).resolve() for item in _fixture_files()}
    resolved = candidate.resolve()
    if resolved not in allowed_files:
        raise ValueError("fixture is not allowlisted")
    return resolved


def _live_credentials_configured(settings: Settings) -> bool:
    return bool(settings.polymarket_private_key and settings.polymarket_funder)
