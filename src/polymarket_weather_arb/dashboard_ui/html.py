from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from polymarket_weather_arb.dashboard_ui.i18n import _t
from polymarket_weather_arb.desktop.csrf import inject_csrf_into_post_forms

DISPLAY_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")


def render_page(title: str, body: str, lang: str, current_path: str) -> str:
    body = inject_csrf_into_post_forms(body)
    html_lang = "zh-CN" if lang == "zh" else "en"
    # Simplified navigation - grouped by workflow
    nav_links = "".join(
        [
            f'<a href="{_href("/beginner", lang)}">{_t(lang, "nav.beginner")}</a>',
            f'<a href="{_href("/", lang)}">{_t(lang, "nav.overview")}</a>',
            f'<a href="{_href("/candidates", lang)}">{_t(lang, "nav.candidates")}</a>',
            f'<a href="{_href("/markets", lang)}">{_t(lang, "nav.markets")}</a>',
            f'<a href="{_href("/actions", lang)}">{_t(lang, "nav.actions")}</a>',
            f'<a href="{_href("/orders", lang)}">{_t(lang, "nav.orders")}</a>',
            f'<a href="{_href("/live", lang)}">{_t(lang, "nav.live")}</a>',
            f'<details class="nav-dropdown"><summary>{_t(lang, "nav.more")}</summary>',
            f'<a href="{_href("/discovery", lang)}">{_t(lang, "nav.discovery")}</a>',
            f'<a href="{_href("/risk", lang)}">{_t(lang, "nav.risk")}</a>',
            f'<a href="{_href("/reconciliation", lang)}">{_t(lang, "nav.reconciliation")}</a>',
            f'<a href="{_href("/open-orders", lang)}">{_t(lang, "nav.open_orders")}</a>',
            f'<a href="{_href("/positions", lang)}">{_t(lang, "nav.positions")}</a>',
            f'<a href="{_href("/fills", lang)}">{_t(lang, "nav.fills")}</a>',
            f'<a href="{_href("/calibration", lang)}">{_t(lang, "nav.calibration")}</a>',
            f'<a href="{_href("/overrides", lang)}">{_t(lang, "nav.overrides")}</a>',
            f'<a href="{_href("/profiles", lang)}">{_t(lang, "nav.profiles")}</a>',
            f'<a href="{_href("/doctor", lang)}">{_t(lang, "nav.doctor")}</a>',
            f'<a href="{_href("/fixtures", lang)}">{_t(lang, "nav.fixtures")}</a>',
            f'<a href="{_href("/operator", lang)}">{_t(lang, "nav.operator")}</a>',
            f'<a href="{_href("/modules", lang)}">{_t(lang, "nav.modules")}</a>',
            f'<a href="{_href("/setup", lang)}">{_t(lang, "nav.setup")}</a>',
            f'<a href="{_href("/runs", lang)}">{_t(lang, "nav.runs")}</a>',
            "</details>",
        ]
    )
    language_switcher = "".join(
        [
            '<span class="language-switcher">',
            f'<a href="{_href(current_path, "zh")}">{_t(lang, "language.zh")}</a>',
            " / ",
            f'<a href="{_href(current_path, "en")}">{_t(lang, "language.en")}</a>',
            "</span>",
        ]
    )
    time_note = f'<p class="muted">{_t(lang, "time.zone")}</p>'
    busy_text = _e(_t(lang, "ui.busy"))
    return f'''<!doctype html>
<html lang="{html_lang}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_e(title)}</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0f172a; color: #e2e8f0; }}
    header {{ padding: 18px 24px; background: #111827; border-bottom: 1px solid #334155; }}
    main {{ padding: 24px; max-width: 1200px; margin: 0 auto; }}
    a {{ color: #93c5fd; }}
    nav a {{ margin-right: 16px; }}
    .nav-dropdown {{ display: inline-block; position: relative; }}
    .nav-dropdown summary {{ cursor: pointer; color: #93c5fd; margin-right: 16px; }}
    .nav-dropdown[open] summary {{ margin-bottom: 8px; }}
    .nav-dropdown a {{ display: block; padding: 4px 0; }}
    table {{ width: 100%; min-width: 680px; table-layout: auto; border-collapse: collapse; margin-top: 12px; background: #111827; }}
    th, td {{ padding: 9px 10px; border-bottom: 1px solid #334155; text-align: left; vertical-align: top; overflow-wrap: anywhere; word-break: break-word; }}
    th {{ color: #cbd5e1; background: #1e293b; }}
    pre {{ white-space: pre-wrap; overflow-wrap: anywhere; background: #020617; padding: 12px; border-radius: 8px; border: 1px solid #334155; }}
    input, select {{ background: #020617; border: 1px solid #475569; color: #e2e8f0; padding: 6px 8px; border-radius: 6px; }}
    button {{ padding: 7px 12px; border: 0; border-radius: 6px; background: #2563eb; color: white; cursor: pointer; }}
    button:disabled {{ opacity: 0.65; cursor: wait; }}
    .danger {{ background: #b91c1c; }}
    .secondary {{ background: #475569; }}
    .card {{ overflow-x: auto; background: #111827; border: 1px solid #334155; border-radius: 10px; padding: 16px; margin-bottom: 18px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; }}
    .filters, .inline-actions {{ display: flex; gap: 8px; align-items: end; flex-wrap: wrap; margin-bottom: 12px; }}
    .stacked-form {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; align-items: end; }}
    .muted {{ color: #94a3b8; }}
    .flash {{ border-radius: 8px; padding: 10px 12px; margin-bottom: 16px; background: #064e3b; border: 1px solid #10b981; }}
    .flash.error {{ background: #7f1d1d; border-color: #ef4444; }}
    .language-switcher {{ float: right; color: #94a3b8; }}
    .ok {{ color: #86efac; }}
    .warning {{ color: #fecaca; }}
    .note-cell {{ display: block; max-width: 36rem; white-space: normal; overflow-wrap: anywhere; word-break: break-word; }}
    .busy-toast {{ display: none; position: fixed; right: 18px; bottom: 18px; z-index: 60; max-width: min(360px, calc(100vw - 36px)); padding: 12px 14px; border: 1px solid #38bdf8; border-radius: 10px; background: #082f49; color: #e0f2fe; box-shadow: 0 16px 48px rgba(0, 0, 0, 0.35); }}
    .busy-toast.visible {{ display: block; }}
    .scan-modal {{ display: none; position: fixed; inset: 0; z-index: 50; place-items: center; background: rgba(2, 6, 23, 0.82); backdrop-filter: blur(3px); }}
    .scan-modal.visible {{ display: grid; }}
    .scan-modal-card {{ width: min(520px, calc(100vw - 40px)); border: 1px solid #2563eb; background: #0b1220; border-radius: 14px; padding: 24px; box-shadow: 0 24px 80px rgba(0, 0, 0, 0.5); }}
    .scan-modal-card strong {{ display: block; font-size: 20px; margin-bottom: 8px; }}
    .workflow-guide {{ background: #0f172a; border: 1px solid #334155; border-radius: 10px; padding: 16px; margin-bottom: 16px; }}
    .workflow-step {{ display: flex; align-items: center; gap: 12px; padding: 8px 0; }}
    .step-num {{ display: inline-flex; align-items: center; justify-content: center; width: 28px; height: 28px; background: #2563eb; border-radius: 50%; font-size: 14px; font-weight: bold; }}
    .workflow-actions {{ display: flex; gap: 12px; margin-top: 12px; }}
    .live-candidates {{ display: grid; gap: 14px; }}
    .live-candidate {{ padding: 14px 0; border-top: 1px solid #334155; }}
    .live-candidate:first-child {{ border-top: 0; }}
    .live-candidate-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px 14px; margin-top: 10px; }}
    .live-field-label {{ display: block; color: #94a3b8; font-size: 12px; margin-bottom: 4px; }}
    .live-field-value {{ overflow-wrap: anywhere; }}
    .btn {{ display: inline-block; padding: 10px 20px; background: #2563eb; color: white; border: none; border-radius: 8px; cursor: pointer; text-decoration: none; font-size: 14px; }}
    .btn:hover {{ background: #1d4ed8; }}
    .btn-secondary {{ background: #475569; }}
    .btn-secondary:hover {{ background: #334155; }}
    .stat-card {{ text-align: center; }}
    .stat-value {{ font-size: 32px; font-weight: bold; color: #38bdf8; margin-bottom: 4px; }}
    .stat-label {{ font-size: 14px; color: #94a3b8; }}
    .progress-bar {{ height: 8px; overflow: hidden; background: #1e293b; border-radius: 999px; margin: 18px 0; }}
    .progress-bar span {{ display: block; width: 42%; height: 100%; border-radius: inherit; background: linear-gradient(90deg, #38bdf8, #2563eb); animation: scan-progress 1.2s ease-in-out infinite; }}
    @keyframes scan-progress {{ 0% {{ transform: translateX(-110%); }} 100% {{ transform: translateX(260%); }} }}
  </style>
</head>
<body>
  <header>
    {language_switcher}
    <h1>{_e(title)}</h1>
    <nav>{nav_links}</nav>
  </header>
  <main>{time_note}{body}</main>
  <div class="busy-toast" id="busy-toast" role="status" aria-live="polite">{busy_text}</div>
  <script>
    const busyToast = document.getElementById('busy-toast');
    for (const form of document.querySelectorAll('form[method="post"]')) {{
      form.addEventListener('submit', () => {{
        const button = form.querySelector('button[type="submit"]');
        if (busyToast) busyToast.classList.add('visible');
        if (button) {{
          button.disabled = true;
          button.textContent = button.dataset.loadingLabel || '{busy_text}';
        }}
      }});
    }}
    for (const form of document.querySelectorAll('.discovery-form')) {{
      form.addEventListener('submit', () => {{
        const panel = form.parentElement.querySelector('.scan-modal');
        if (panel) panel.classList.add('visible');
      }});
    }}
  </script>
</body>
</html>'''


def _section(title: str, body: str) -> str:
    return f'<section class="card"><h2>{_e(title)}</h2>{body}</section>'


def _table(
    headers: list[str], rows: list[list[Any]], lang: str, *, raw_columns: set[int] | None = None
) -> str:
    raw_columns = raw_columns or set()
    head = "".join(f"<th>{_e(header)}</th>" for header in headers)
    if not rows:
        return f'<p class="muted">{_t(lang, "table.no_rows")}</p>'
    body = "".join(
        "<tr>"
        + "".join(
            f"<td>{str(cell) if index in raw_columns else _e(cell)}</td>"
            for index, cell in enumerate(row)
        )
        + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _definition_table(
    fields: list[tuple[str, object | None]], *, raw_values: set[int] | None = None
) -> str:
    raw_values = raw_values or set()
    rows = "".join(
        f"<tr><th>{_e(label)}</th><td>{str(value) if index in raw_values else _e('-' if value is None or value == '' else value)}</td></tr>"
        for index, (label, value) in enumerate(fields)
    )
    return f"<table><tbody>{rows}</tbody></table>"


def _render_flash(query: dict[str, list[str]], lang: str) -> str:
    flash = _single(query, "flash")
    if not flash:
        return ""
    level = _single(query, "level") or "ok"
    detail = _single(query, "detail")
    label = _t(lang, flash)
    message = label if not detail else f"{label}: {detail}"
    css = "flash error" if level == "error" else "flash"
    return f'<div class="{css}">{_e(message)}</div>'


def _href(path: str, lang: str, **params: object) -> str:
    parsed = urlparse(path)
    query = {key: values[-1] for key, values in parse_qs(parsed.query).items() if values}
    query["lang"] = lang
    for key, value in params.items():
        if value is None:
            query.pop(key, None)
        else:
            query[key] = str(value)
    encoded = urlencode(query)
    base = parsed.path or "/"
    return f"{base}?{encoded}" if encoded else base


def _hidden_lang(lang: str) -> str:
    return f'<input type="hidden" name="lang" value="{_e(lang)}">'


def _status_label(status: str, lang: str) -> str:
    return _t(lang, f"status.{status}")


def _kind_label(kind: str, lang: str) -> str:
    return _t(lang, f"kind.{kind}")


def _bool_label(value: object | None, lang: str) -> str:
    if value is None:
        return "-"
    return _t(lang, "bool.yes") if bool(value) else _t(lang, "bool.no")


def _display_time(value: object | None) -> str:
    if value is None or value == "":
        return "-"
    text = str(value)
    try:
        normalized = text.replace("Z", "+00:00")
        if " " in normalized and "T" not in normalized:
            normalized = normalized.replace(" ", "T")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S UTC+8")


def _tags_label(value: object | None) -> str:
    if value is None:
        return "-"
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value or "-"
        if isinstance(parsed, list):
            return ", ".join(str(item) for item in parsed) or "-"
    return str(value)


def _json_list_label(value: object | None) -> str:
    if value is None:
        return "-"
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return str(value)
    if isinstance(parsed, list):
        return "; ".join(str(item) for item in parsed) or "-"
    return str(parsed)


def _duration_label(value: object | None) -> str:
    if value is None:
        return "-"
    return f"{value} ms"


def _dash(value: object | None) -> str:
    return "-" if value is None else str(value)


def _short_note(value: str | None, max_chars: int = 180) -> str:
    if not value:
        return "-"
    normalized = " ".join(str(value).split())
    return normalized if len(normalized) <= max_chars else f"{normalized[: max_chars - 3]}..."


def _note_cell(value: str | None, max_chars: int = 180) -> str:
    return f'<span class="note-cell">{_e(_short_note(value, max_chars))}</span>'


def _single(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key) or []
    value = values[0].strip() if values else ""
    return value or None


def _e(value: object) -> str:
    return escape(str(value), quote=True)
