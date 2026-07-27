"""Expanded /setup first-run flow using existing dashboard rendering.

Resumable multi-step setup. Secrets render only as configured/not configured.
Completing setup never starts Autopilot or live trading.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from polymarket_weather_arb.adapters.keychain import SECRET_ENV_KEYS, KeychainStore
from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.dashboard_ui.html import (
    _e,
    _hidden_lang,
    _href,
    _render_flash,
    render_page,
)
from polymarket_weather_arb.desktop.paths import (
    DesktopPaths,
    is_setup_complete,
    resolve_desktop_paths,
)
from polymarket_weather_arb.desktop.setup_flow import (
    DEFAULT_SETUP_MODE,
    RISK_PRESETS,
    SETUP_STEPS,
    SetupReadinessItem,
    build_setup_readiness,
    saved_risk_preset,
    saved_setup_mode,
)
from polymarket_weather_arb.storage.db import Database


def desktop_runtime_active() -> bool:
    return bool(
        os.environ.get("POLYMARKET_DESKTOP") == "1"
        or os.environ.get("POLYMARKET_DESKTOP_DATA_ROOT")
    )


def resolve_setup_paths(settings: Settings) -> DesktopPaths | None:
    if not desktop_runtime_active():
        return None
    root = os.environ.get("POLYMARKET_DESKTOP_DATA_ROOT")
    return resolve_desktop_paths(root=Path(root) if root else None)


def render_setup(
    settings: Settings,
    lang: str,
    current_path: str,
    query: dict[str, list[str]] | None = None,
    *,
    repository: Any | None = None,
    keychain: KeychainStore | None = None,
    secret_status: Mapping[str, bool] | None = None,
    readiness: list[SetupReadinessItem] | None = None,
) -> str:
    query = query or {}
    step = _step_from_query(query)
    paths = resolve_setup_paths(settings)
    labels = _labels(lang)
    status = secret_status
    if status is None and keychain is not None:
        try:
            status = keychain.secret_status()
        except Exception:  # noqa: BLE001
            status = {key: False for key in SECRET_ENV_KEYS}
    if status is None:
        status = {
            "POLYMARKET_PRIVATE_KEY": bool(settings.polymarket_private_key),
            "GOOGLE_WEATHER_API_KEY": bool(settings.google_weather_api_key),
            "LLM_API_KEY": bool(settings.llm_api_key),
            "TELEGRAM_BOT_TOKEN": bool(settings.telegram_bot_token),
        }

    step_body = {
        "health": lambda: _step_health(settings, paths, labels, lang),
        "mode": lambda: _step_mode(settings, paths, repository, labels, lang),
        "wallet": lambda: _step_wallet(settings, status, labels, lang),
        "connectivity": lambda: _step_connectivity(settings, labels, lang),
        "risk": lambda: _step_risk(settings, paths, labels, lang),
        "weather": lambda: _step_weather(settings, status, labels, lang),
        "review": lambda: _step_review(
            settings, paths, repository, status, readiness, labels, lang
        ),
    }[step]()

    body = "".join(
        [
            _render_flash(query, lang),
            _setup_styles(),
            '<section class="setup-shell">',
            f'<p class="setup-eyebrow">{_e(labels["eyebrow"])}</p>',
            f"<h2 class=\"setup-title\">{_e(labels['title'])}</h2>",
            f'<p class="setup-lede muted">{_e(labels["subtitle"])}</p>',
            _step_nav(step, lang, labels),
            step_body,
            f'<p class="muted setup-foot"><a href="{_href("/app", lang)}">{_e(labels["open_app"])}</a></p>',
            "</section>",
        ]
    )
    return render_page(labels["title"], body, lang, current_path)


def _step_from_query(query: dict[str, list[str]]) -> str:
    raw = ""
    if query.get("step"):
        raw = query["step"][-1]
    step = (raw or "health").strip().lower()
    return step if step in SETUP_STEPS else "health"


def _step_nav(step: str, lang: str, labels: dict[str, str]) -> str:
    items = []
    for index, name in enumerate(SETUP_STEPS, start=1):
        cls = "setup-step is-active" if name == step else "setup-step"
        href = _href("/setup", lang, step=name)
        items.append(
            f'<a class="{cls}" href="{href}"><span class="setup-step-num">{index}</span>'
            f"{_e(labels[f'step_{name}'])}</a>"
        )
    return f'<nav class="setup-steps" aria-label="setup">{"".join(items)}</nav>'


def _step_health(
    settings: Settings,
    paths: DesktopPaths | None,
    labels: dict[str, str],
    lang: str,
) -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        app_version = version("polymarket-weather-arb")
    except PackageNotFoundError:
        app_version = "0.1.2"

    root = str(paths.root) if paths else str(Path(settings.database_path).parent)
    db_path = str(paths.database_path) if paths else str(settings.database_path)
    db_exists = Path(db_path).is_file()
    schema_ok = False
    if db_exists or True:
        try:
            Database(Path(db_path)).init_schema()
            schema_ok = True
        except Exception as exc:  # noqa: BLE001
            schema_note = str(exc)
        else:
            schema_note = labels["schema_ok"]
    first_run = not (paths and is_setup_complete(paths))
    writable = True
    write_note = labels["writable_ok"]
    try:
        probe_dir = Path(root)
        probe_dir.mkdir(parents=True, exist_ok=True)
        probe = probe_dir / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        writable = False
        write_note = str(exc)

    rows = [
        (labels["version"], app_version),
        (labels["app_support"], root),
        (labels["database"], db_path),
        (labels["schema"], schema_note if schema_ok else labels["schema_fail"]),
        (labels["writable"], write_note if writable else write_note),
        (labels["install_state"], labels["first_run"] if first_run else labels["existing_install"]),
    ]
    table = _kv_table(rows)
    form = _form(
        "/setup/health",
        lang,
        "".join(
            [
                table,
                '<input type="hidden" name="step" value="health">',
                f'<button type="submit" class="btn">{_e(labels["continue"])}</button>',
            ]
        ),
    )
    return _card(labels["step_health"], labels["health_help"], form)


def _step_mode(
    settings: Settings,
    paths: DesktopPaths | None,
    repository: Any | None,
    labels: dict[str, str],
    lang: str,
) -> str:
    current = saved_setup_mode(paths, repository) or DEFAULT_SETUP_MODE
    options = [
        ("observe", labels["mode_observe"], labels["mode_observe_help"]),
        ("paper", labels["mode_paper"], labels["mode_paper_help"]),
        ("micro_live", labels["mode_micro_live"], labels["mode_micro_live_help"]),
        ("full_live", labels["mode_full_live"], labels["mode_full_live_help"]),
    ]
    cards = []
    for value, title, help_text in options:
        checked = "checked" if value == current else ""
        cards.append(
            "".join(
                [
                    '<label class="setup-mode-card">',
                    f'<input type="radio" name="app_mode" value="{_e(value)}" {checked}>',
                    f"<strong>{_e(title)}</strong>",
                    f'<span class="muted">{_e(help_text)}</span>',
                    "</label>",
                ]
            )
        )
    form = _form(
        "/setup/mode",
        lang,
        "".join(
            [
                f'<p class="warning">{_e(labels["mode_no_autostart"])}</p>',
                f'<div class="setup-mode-grid">{"".join(cards)}</div>',
                f'<button type="submit" class="btn">{_e(labels["continue"])}</button>',
            ]
        ),
    )
    return _card(labels["step_mode"], labels["mode_help"], form)


def _step_wallet(
    settings: Settings,
    status: Mapping[str, bool],
    labels: dict[str, str],
    lang: str,
) -> str:
    pk_status = labels["configured"] if status.get("POLYMARKET_PRIVATE_KEY") else labels["not_configured"]
    funder = settings.polymarket_funder or ""
    form = _form(
        "/setup/wallet",
        lang,
        "".join(
            [
                _kv_table(
                    [
                        (labels["private_key_status"], pk_status),
                    ]
                ),
                f'<label class="setup-field">{_e(labels["private_key"])} '
                f'<span class="muted">({_e(labels["blank_keep"])})</span>'
                f'<input type="password" name="polymarket_private_key" autocomplete="off" '
                f'spellcheck="false" placeholder="••••••••"></label>',
                f'<label class="setup-field">{_e(labels["funder"])} '
                f'<input name="polymarket_funder" value="{_e(funder)}" '
                f'placeholder="0x…"></label>',
                f'<label class="setup-field"><input type="checkbox" name="derive_funder" value="1"> '
                f'{_e(labels["derive_funder"])}</label>',
                f'<label class="setup-field danger-text"><input type="checkbox" name="delete_private_key" value="1"> '
                f'{_e(labels["delete_private_key"])}</label>',
                f'<p class="muted">{_e(labels["wallet_note"])}</p>',
                f'<button type="submit" class="btn">{_e(labels["continue"])}</button>',
            ]
        ),
    )
    return _card(labels["step_wallet"], labels["wallet_help"], form)


def _step_connectivity(settings: Settings, labels: dict[str, str], lang: str) -> str:
    form = _form(
        "/setup/connectivity",
        lang,
        "".join(
            [
                f'<p class="muted">{_e(labels["connectivity_help"])}</p>',
                '<details class="setup-advanced"><summary>'
                + _e(labels["advanced_proxy"])
                + "</summary>",
                f'<label class="setup-field">PROXY_URL '
                f'<input name="proxy_url" value="{_e(settings.proxy_url or "")}" '
                f'placeholder="http://127.0.0.1:7890"></label>',
                "</details>",
                f'<label class="setup-field"><input type="checkbox" name="run_probes" value="1" checked> '
                f'{_e(labels["run_probes"])}</label>',
                f'<button type="submit" class="btn">{_e(labels["continue"])}</button>',
            ]
        ),
    )
    return _card(labels["step_connectivity"], labels["connectivity_title"], form)


def _step_risk(
    settings: Settings, paths: DesktopPaths | None, labels: dict[str, str], lang: str
) -> str:
    selected = saved_risk_preset(paths, settings)
    preset_cards = []
    for key, values in RISK_PRESETS.items():
        summary = (
            f"order={values['MAX_ORDER_USDC']} daily={values['MAX_DAILY_USDC']} "
            f"market={values['MAX_MARKET_USDC']} exit_cap={values['AUTO_EXIT_MAX_POSITION_USDC']}"
        )
        checked = "checked" if key == selected else ""
        preset_cards.append(
            f'<label class="setup-mode-card"><input type="radio" name="risk_preset" '
            f'value="{_e(key)}" {checked}><strong>{_e(labels[f"risk_{key}"])}</strong>'
            f'<span class="muted mono">{_e(summary)}</span></label>'
        )
    custom_checked = "checked" if selected == "custom" else ""
    preset_cards.append(
        f'<label class="setup-mode-card"><input type="radio" name="risk_preset" value="custom" {custom_checked}>'
        f'<strong>{_e(labels["risk_custom"])}</strong>'
        f'<span class="muted">{_e(labels["risk_custom_help"])}</span></label>'
    )
    form = _form(
        "/setup/risk",
        lang,
        "".join(
            [
                f'<p class="muted">{_e(labels["risk_help"])}</p>',
                f'<div class="setup-mode-grid">{"".join(preset_cards)}</div>',
                '<div class="setup-custom-risk">',
                f'<label class="setup-field">MAX_ORDER_USDC <input name="max_order_usdc" value="{_e(settings.max_order_usdc)}"></label>',
                f'<label class="setup-field">MAX_DAILY_USDC <input name="max_daily_usdc" value="{_e(settings.max_daily_usdc)}"></label>',
                f'<label class="setup-field">MAX_MARKET_USDC <input name="max_market_usdc" value="{_e(settings.max_market_usdc)}"></label>',
                f'<label class="setup-field">AUTO_EXIT_MAX_POSITION_USDC <input name="auto_exit_max_position_usdc" value="{_e(settings.auto_exit_max_position_usdc)}"></label>',
                "</div>",
                f'<button type="submit" class="btn">{_e(labels["continue"])}</button>',
            ]
        ),
    )
    return _card(labels["step_risk"], labels["risk_title"], form)


def _step_weather(
    settings: Settings,
    status: Mapping[str, bool],
    labels: dict[str, str],
    lang: str,
) -> str:
    providers = ["auto", "open-meteo", "noaa", "china-official"]
    option_parts: list[str] = []
    if settings.weather_provider not in providers:
        option_parts.append(
            f'<option value="{_e(settings.weather_provider)}" selected>'
            f"{_e(settings.weather_provider)}</option>"
        )
    for provider in providers:
        selected = " selected" if settings.weather_provider == provider else ""
        option_parts.append(
            f'<option value="{_e(provider)}"{selected}>{_e(provider)}</option>'
        )
    options = "".join(option_parts)
    tg_status = labels["configured"] if status.get("TELEGRAM_BOT_TOKEN") else labels["not_configured"]
    google_weather_key_status = (
        labels["configured"]
        if status.get("GOOGLE_WEATHER_API_KEY")
        else labels["not_configured"]
    )
    form = _form(
        "/setup/weather",
        lang,
        "".join(
            [
                f'<label class="setup-field">{_e(labels["weather_provider"])} '
                f"<select name=\"weather_provider\">{options}</select></label>",
                _kv_table(
                    [
                        (labels["google_weather_api_key_status"], google_weather_key_status),
                        (labels["telegram_token_status"], tg_status),
                    ]
                ),
                f'<label class="setup-field">{_e(labels["google_weather_api_key"])} '
                f'<span class="muted">({_e(labels["blank_keep"])})</span>'
                f'<input type="password" name="google_weather_api_key" autocomplete="off"></label>',
                f'<label class="setup-field"><input type="checkbox" name="telegram_notify_enabled" value="true" '
                f'{"checked" if settings.telegram_notify_enabled else ""}> '
                f'{_e(labels["telegram_enable"])}</label>',
                f'<label class="setup-field">{_e(labels["telegram_token"])} '
                f'<span class="muted">({_e(labels["blank_keep"])})</span>'
                f'<input type="password" name="telegram_bot_token" autocomplete="off"></label>',
                f'<label class="setup-field">{_e(labels["telegram_chat_id"])} '
                f'<input name="telegram_chat_id" value="{_e(settings.telegram_chat_id or "")}"></label>',
                f'<label class="setup-field"><input type="checkbox" name="send_test_telegram" value="1"> '
                f'{_e(labels["telegram_test"])}</label>',
                f'<button type="submit" class="btn">{_e(labels["continue"])}</button>',
            ]
        ),
    )
    return _card(labels["step_weather"], labels["weather_help"], form)


def _step_review(
    settings: Settings,
    paths: DesktopPaths | None,
    repository: Any | None,
    status: Mapping[str, bool],
    readiness: list[SetupReadinessItem] | None,
    labels: dict[str, str],
    lang: str,
) -> str:
    # When credentials exist, build_setup_readiness constructs the same
    # GammaPolymarketClient path as CLI live-readiness for read-only balance
    # and signing checks (no order placement).
    items = readiness or build_setup_readiness(
        settings,
        repository=repository,
        secret_status=status,
    )
    rows = []
    for item in items:
        tone = "ok" if item.ok else "warning"
        rows.append(
            f'<li class="setup-check {tone}"><strong>{_e(item.name)}</strong> '
            f'<span class="badge">{_e(item.status)}</span> '
            f'<span class="muted">[{_e(item.kind)}]</span> '
            f"<p>{_e(item.detail)}</p></li>"
        )
    form = _form(
        "/setup/complete",
        lang,
        "".join(
            [
                f'<ul class="setup-check-list">{"".join(rows)}</ul>',
                f'<p class="warning">{_e(labels["review_no_live"])}</p>',
                f'<button type="submit" class="btn btn-primary">{_e(labels["finish"])}</button>',
            ]
        ),
    )
    return _card(labels["step_review"], labels["review_help"], form)


def _card(title: str, help_text: str, body: str) -> str:
    return (
        f'<section class="setup-card card"><h3>{_e(title)}</h3>'
        f'<p class="muted">{_e(help_text)}</p>{body}</section>'
    )


def _form(action: str, lang: str, body: str) -> str:
    return "".join(
        [
            f'<form method="post" action="{_href(action, lang)}" class="setup-form stacked-form">',
            _hidden_lang(lang),
            body,
            "</form>",
        ]
    )


def _kv_table(rows: list[tuple[str, object]]) -> str:
    body = "".join(
        f"<tr><th>{_e(k)}</th><td>{_e(v)}</td></tr>" for k, v in rows
    )
    return f'<table class="setup-kv"><tbody>{body}</tbody></table>'


def _setup_styles() -> str:
    return """
<style>
  .setup-shell { max-width: 920px; margin: 0 auto; }
  .setup-eyebrow { color: #75e6d6; font-size: 12px; font-weight: 700; text-transform: uppercase; }
  .setup-title { margin: 0 0 8px; font-size: clamp(1.6rem, 3vw, 2.2rem); }
  .setup-lede { margin-top: 0; max-width: 60ch; }
  .setup-steps { display: flex; flex-wrap: wrap; gap: 8px; margin: 18px 0; }
  .setup-step {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 8px 10px; border-radius: 999px; border: 1px solid #334155;
    background: #111827; color: #cbd5e1; text-decoration: none; font-size: 13px;
  }
  .setup-step.is-active { border-color: #2dd4bf; color: #99f6e4; }
  .setup-step-num {
    width: 22px; height: 22px; border-radius: 50%; display: inline-grid; place-items: center;
    background: #2563eb; color: white; font-size: 12px; font-weight: 700;
  }
  .setup-card h3 { margin-top: 0; }
  .setup-mode-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px;
    margin: 12px 0 18px;
  }
  .setup-mode-card {
    display: flex; flex-direction: column; gap: 8px; padding: 14px;
    border: 1px solid #334155; border-radius: 10px; background: #0b1220; cursor: pointer;
  }
  .setup-mode-card:has(input:checked) { border-color: #2dd4bf; box-shadow: inset 0 0 0 1px rgba(45,212,191,.35); }
  .setup-field { display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px; }
  .setup-form { display: block; }
  .setup-form .btn, .setup-form button {
    margin-top: 8px; min-height: 42px; padding: 10px 16px; border-radius: 8px;
    border: 0; background: #2dd4bf; color: #04201d; font-weight: 800; cursor: pointer;
  }
  .setup-advanced { margin: 12px 0; padding: 10px; border: 1px dashed #475569; border-radius: 8px; }
  .setup-kv { width: 100%; margin: 12px 0; }
  .setup-check-list { list-style: none; padding: 0; margin: 0 0 16px; display: grid; gap: 10px; }
  .setup-check { padding: 12px; border: 1px solid #334155; border-radius: 10px; background: #0b1220; }
  .setup-check.ok { border-color: rgba(34,197,94,.4); }
  .setup-check.warning { border-color: rgba(245,158,11,.45); }
  .setup-check .badge { color: #99f6e4; font-size: 12px; font-weight: 700; }
  .setup-check p { margin: 6px 0 0; color: #94a3b8; font-size: 0.9rem; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
  .danger-text { color: #fecaca; }
  .setup-foot { margin-top: 18px; }
  @media (max-width: 720px) {
    .setup-steps { flex-direction: column; }
    .setup-mode-grid { grid-template-columns: 1fr; }
  }
</style>
"""


def _labels(lang: str) -> dict[str, str]:
    if lang == "zh":
        return {
            "eyebrow": "Polymarket Weather",
            "title": "首次设置",
            "subtitle": "完成以下步骤后即可使用 /app。设置不会自动启动自动交易或真实下单。",
            "open_app": "打开 /app",
            "continue": "继续",
            "finish": "完成设置并进入 /app",
            "configured": "已配置",
            "not_configured": "未配置",
            "blank_keep": "留空表示保留现有密钥",
            "advanced": "高级设置",
            "advanced_proxy": "代理（高级）",
            "version": "应用版本",
            "app_support": "Application Support 路径",
            "database": "数据库路径",
            "schema": "数据库结构",
            "schema_ok": "已初始化",
            "schema_fail": "初始化失败",
            "writable": "数据目录可写",
            "writable_ok": "可写",
            "install_state": "安装状态",
            "first_run": "首次运行",
            "existing_install": "已有安装",
            "step_health": "本地健康",
            "step_mode": "运行模式",
            "step_wallet": "钱包与 Polymarket",
            "step_connectivity": "连通性与合规",
            "step_risk": "风险预设",
            "step_weather": "天气与通知",
            "step_review": "就绪复查",
            "health_help": "确认本地数据目录、数据库和版本。",
            "mode_help": "选择默认操作模式。真实交易不会在设置完成时自动启动。",
            "mode_no_autostart": "即使选择 Micro Live / Full Live，系统仍会保持停止，直到你在 /app 明确启动。",
            "mode_observe": "观察",
            "mode_observe_help": "仅扫描与分析，不创建订单意图。",
            "mode_paper": "纸面自动交易（推荐）",
            "mode_paper_help": "扫描、分析并记录 dry-run 订单。推荐默认。",
            "mode_micro_live": "Micro Live",
            "mode_micro_live_help": "小额真实交易路径，仍需凭证、合规与对账门槛。",
            "mode_full_live": "Full Live",
            "mode_full_live_help": "无人值守真实模式，仍受硬风控约束，且不会在设置时启动。",
            "wallet_help": "私钥写入 Keychain，页面只显示配置状态与地址。",
            "wallet_note": "设置只保存官方客户端需要的私钥与 funder；不会创建授权或真实订单。余额授权与签名授权是不同路径。",
            "private_key": "私钥",
            "private_key_status": "私钥状态",
            "funder": "Funder 地址",
            "derive_funder": "用私钥推导 signer，并在 funder 为空时填入",
            "delete_private_key": "删除已保存的私钥（明确操作）",
            "connectivity_title": "连通性",
            "connectivity_help": "只读探测。失败会给出恢复提示，不会擦除配置。",
            "run_probes": "运行只读连通性探测",
            "risk_title": "风险预设",
            "risk_help": "仅配置现有 MAX_* / AUTO_EXIT 控件，不会发明新门槛。自动退出上限不会低于下单上限。",
            "risk_paper": "纸面默认",
            "risk_starter_live": "Starter Live（1 USDC）",
            "risk_cautious_live": "Cautious Live（2 USDC）",
            "risk_custom": "自定义",
            "risk_custom_help": "高级用户；仍受硬顶限制。",
            "weather_help": "天气源与可选 Telegram。测试通知不会启动 Autopilot。",
            "weather_provider": "天气数据源",
            "google_weather_api_key": "Google Weather API Key（定价参考源）",
            "google_weather_api_key_status": "Google Weather 参考源状态",
            "telegram_enable": "启用 Telegram 通知",
            "telegram_token": "Telegram Bot Token",
            "telegram_token_status": "Telegram Token 状态",
            "telegram_chat_id": "Telegram Chat ID",
            "telegram_test": "发送只读测试通知（不启动 Autopilot）",
            "review_help": "最终只读清单。签名未通过真实下单验证。",
            "review_no_live": "完成设置后 Live 仍保持停止。请在 /app 明确启动。",
        }
    return {
        "eyebrow": "Polymarket Weather",
        "title": "Setup",
        "subtitle": "Complete these steps to use /app. Setup never starts Autopilot or live orders.",
        "open_app": "Open /app",
        "continue": "Continue",
        "finish": "Finish setup and open /app",
        "configured": "configured",
        "not_configured": "not configured",
        "blank_keep": "blank keeps the existing secret",
        "advanced": "Advanced settings",
        "advanced_proxy": "Proxy (advanced)",
        "version": "Application version",
        "app_support": "Application Support path",
        "database": "Database path",
        "schema": "Database schema",
        "schema_ok": "initialized",
        "schema_fail": "initialization failed",
        "writable": "Writable data directories",
        "writable_ok": "writable",
        "install_state": "Install state",
        "first_run": "first run",
        "existing_install": "existing installation",
        "step_health": "Local health",
        "step_mode": "Operating mode",
        "step_wallet": "Wallet & Polymarket",
        "step_connectivity": "Connectivity",
        "step_risk": "Risk presets",
        "step_weather": "Weather & notifications",
        "step_review": "Readiness review",
        "health_help": "Validate local data directories, database, and version.",
        "mode_help": "Choose the default operating mode. Live trading never auto-starts from setup.",
        "mode_no_autostart": "Even if you pick Micro Live or Full Live, the system stays stopped until you start it from /app.",
        "mode_observe": "Observe",
        "mode_observe_help": "Scan and analyze only; no order intents.",
        "mode_paper": "Paper auto trading (recommended)",
        "mode_paper_help": "Scan, analyze, and record dry-run orders. Recommended default.",
        "mode_micro_live": "Micro Live",
        "mode_micro_live_help": "Small live path; still requires credentials, compliance, and reconciliation gates.",
        "mode_full_live": "Full Live",
        "mode_full_live_help": "Unattended live mode under hard caps; still not started by setup.",
        "wallet_help": "Private keys go to Keychain. The UI only shows status and addresses.",
        "wallet_note": "Setup stores only the private key and funder required by the official client. It does not create approvals or live orders.",
        "private_key": "Private key",
        "private_key_status": "Private key status",
        "funder": "Funder address",
        "derive_funder": "Derive signer from private key and fill funder when empty",
        "delete_private_key": "Delete saved private key (explicit action)",
        "connectivity_title": "Connectivity",
        "connectivity_help": "Read-only probes. Failures show recovery text and do not erase configuration.",
        "run_probes": "Run read-only connectivity probes",
        "risk_title": "Risk presets",
        "risk_help": "Configures existing MAX_* / AUTO_EXIT controls only. Auto-exit cap is never below the order cap.",
        "risk_paper": "Paper default",
        "risk_starter_live": "Starter Live (1 USDC)",
        "risk_cautious_live": "Cautious Live (2 USDC)",
        "risk_custom": "Custom",
        "risk_custom_help": "Advanced users only; still hard-capped.",
        "weather_help": "Weather provider and optional Telegram. Test notify does not start Autopilot.",
        "weather_provider": "Weather provider",
        "google_weather_api_key": "Google Weather API key (pricing reference)",
        "google_weather_api_key_status": "Google Weather reference status",
        "telegram_enable": "Enable Telegram notifications",
        "telegram_token": "Telegram bot token",
        "telegram_token_status": "Telegram token status",
        "telegram_chat_id": "Telegram chat ID",
        "telegram_test": "Send a read-only test notification (does not start Autopilot)",
        "review_help": "Final read-only checklist. Signing is not proven by a real trade.",
        "review_no_live": "After setup, live remains stopped. Start explicitly from /app.",
    }
