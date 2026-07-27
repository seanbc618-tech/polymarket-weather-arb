from __future__ import annotations

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.dashboard_ui.html import (
    _e,
    _hidden_lang,
    _href,
    _render_flash,
    _section,
    render_page,
)
from polymarket_weather_arb.storage.repositories import Repository


def render_beginner(
    repository: Repository, settings: Settings, lang: str, current_path: str
) -> str:
    labels = _labels(lang)
    body = "".join(
        [
            _render_flash(_query(current_path), lang),
            f"<h2>{labels['title']}</h2>",
            f'<p class="muted">{labels["intro"]}</p>',
            '<section class="grid">',
            _safety_card(repository, settings, labels),
            _rehearsal_card(repository, lang, labels),
            "</section>",
            _section(labels["safe_tick_title"], _safe_tick_card(lang, labels)),
            _section(
                labels["setup_checklist_title"],
                _setup_checklist_card(repository, settings, lang, labels),
            ),
            _section(labels["next_steps"], _next_steps(labels, lang)),
            _section(labels["live_locked_title"], f"<p>{labels['live_locked_body']}</p>"),
        ]
    )
    return render_page(labels["page_title"], body, lang, current_path)


def _safety_card(repository: Repository, settings: Settings, labels: dict[str, str]) -> str:
    credentials_ready = bool(settings.polymarket_private_key and settings.polymarket_funder)
    latest_reconciliation = repository.latest_reconciliation()
    dry_runs = repository.list_recent_order_intents(limit=5)
    dry_run_count = sum(1 for row in dry_runs if row["dry_run"])
    auto_exit_label = (
        labels.get("auto_exit_on", "ON (env only — not armed without daemon flags)")
        if settings.auto_exit_enabled
        else labels.get("auto_exit_off", "OFF (default)")
    )
    rows = [
        (
            labels["kill_switch"],
            labels["locked"] if settings.trading_disabled else labels["unlocked"],
        ),
        (labels["credentials"], labels["configured"] if credentials_ready else labels["missing"]),
        (
            labels["reconciliation"],
            latest_reconciliation["status"]
            if latest_reconciliation is not None
            else labels["missing"],
        ),
        (labels["recent_dry_runs"], str(dry_run_count)),
        # Status-only: no one-click enable control for automatic exits.
        (labels.get("auto_exit", "AUTO EXIT"), auto_exit_label),
    ]
    items = "".join(f"<li><strong>{_e(name)}:</strong> {_e(value)}</li>" for name, value in rows)
    return f'<section class="card"><h2>{labels["safety_title"]}</h2><ul>{items}</ul><p class="muted">{labels["browser_safe"]}</p></section>'


def _rehearsal_card(repository: Repository, lang: str, labels: dict[str, str]) -> str:
    last_rehearsal = repository.latest_dry_run_rehearsal()
    result_html = _render_last_rehearsal(last_rehearsal, lang, labels) if last_rehearsal else ""
    return "".join(
        [
            '<section class="card">',
            f"<h2>{labels['rehearsal_title']}</h2>",
            f"<p>{labels['rehearsal_body']}</p>",
            result_html,
            f'<form method="post" action="{_href("/beginner/rehearse", lang)}">',
            _hidden_lang(lang),
            f'<button type="submit" data-loading-label="{_e(labels["running"])}">{labels["run_rehearsal"]}</button>',
            "</form>",
            "</section>",
        ]
    )


def _render_last_rehearsal(rehearsal: dict, lang: str, labels: dict[str, str]) -> str:
    """渲染最近一次演练结果"""
    from datetime import datetime

    # 解析时间
    created_at = rehearsal.get("intent_created_at", "")
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if lang == "zh":
            time_str = dt.strftime("%m月%d日 %H:%M")
        else:
            time_str = dt.strftime("%b %d, %H:%M")
    except (ValueError, AttributeError):
        time_str = created_at

    # 风险决策
    accepted = rehearsal.get("accepted")
    if accepted == 1:
        risk_status = labels["risk_accepted"]
        risk_class = "color: #10b981;"
    elif accepted == 0:
        risk_status = labels["risk_rejected"]
        risk_class = "color: #ef4444;"
    else:
        risk_status = labels["risk_unknown"]
        risk_class = "color: #94a3b8;"

    # 市场名称（截断显示）
    market_title = rehearsal.get("market_title") or rehearsal.get("market_id", "")
    if len(market_title) > 40:
        market_title = market_title[:37] + "..."

    # 订单方向
    side = rehearsal.get("side", "").upper()
    side_label = labels.get(f"side_{side.lower()}", side)

    return f"""
    <div style="background: #0f172a; border: 1px solid #334155; border-radius: 8px; padding: 14px; margin: 12px 0;">
        <h3 style="margin: 0 0 10px 0; font-size: 15px;">{labels["last_rehearsal"]} <span style="color: #94a3b8; font-weight: normal;">{_e(time_str)}</span></h3>
        <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
            <tr><td style="padding: 4px 8px 4px 0; color: #94a3b8; white-space: nowrap;">{labels["market"]}:</td><td style="padding: 4px 0;">{_e(market_title)}</td></tr>
            <tr><td style="padding: 4px 8px 4px 0; color: #94a3b8; white-space: nowrap;">{labels["order_side"]}:</td><td style="padding: 4px 0;">{_e(side_label)}</td></tr>
            <tr><td style="padding: 4px 8px 4px 0; color: #94a3b8; white-space: nowrap;">{labels["order_price"]}:</td><td style="padding: 4px 0;">{rehearsal.get("limit_price", "-")}</td></tr>
            <tr><td style="padding: 4px 8px 4px 0; color: #94a3b8; white-space: nowrap;">{labels["order_size"]}:</td><td style="padding: 4px 0;">{rehearsal.get("size", "-")} USDC</td></tr>
            <tr><td style="padding: 4px 8px 4px 0; color: #94a3b8; white-space: nowrap;">{labels["risk_status"]}:</td><td style="padding: 4px 0; {risk_class}">{risk_status}</td></tr>
        </table>
        <p style="margin: 10px 0 8px 0; font-size: 12px; color: #94a3b8;">{labels["rehearsal_explanation"]}</p>
        <a href="{_href("/orders", lang)}" style="display: inline-block; padding: 6px 12px; background: #475569; color: #e2e8f0; text-decoration: none; border-radius: 6px; font-size: 13px;">{labels["view_details"]}</a>
    </div>
    """


def _safe_tick_card(lang: str, labels: dict[str, str]) -> str:
    """渲染安全一键执行卡片"""
    return f"""
    <div style="background: #111827; border: 1px solid #334155; border-radius: 10px; padding: 16px;">
        <p>{labels["safe_tick_body"]}</p>
        <ul style="margin: 10px 0; padding-left: 20px; font-size: 13px; color: #94a3b8;">
            <li>{labels["safe_tick_feature_1"]}</li>
            <li>{labels["safe_tick_feature_2"]}</li>
            <li>{labels["safe_tick_feature_3"]}</li>
        </ul>
        <form method="post" action="{_href("/beginner/safe-tick", lang)}">
            {_hidden_lang(lang)}
            <button type="submit" data-loading-label="{_e(labels["running_tick"])}">{labels["run_safe_tick"]}</button>
        </form>
        <p style="margin: 10px 0 0 0; font-size: 12px; color: #94a3b8;">{labels["safe_tick_safety"]}</p>
    </div>
    """


def _setup_checklist_card(
    repository: Repository, settings: Settings, lang: str, labels: dict[str, str]
) -> str:
    """渲染设置检查清单卡片"""
    import importlib.util
    from pathlib import Path

    # 1. Database initialized - 检查数据库是否存在
    db_path = settings.database_path
    db_exists = db_path.exists() and db_path.stat().st_size > 0

    # 2. SDK installed - 检查 polymarket-client 是否可导入
    sdk_installed = importlib.util.find_spec("polymarket") is not None

    # 3. credentials missing/configured
    credentials_ready = bool(
        settings.polymarket_private_key
        and settings.polymarket_funder
    )

    # 4. Compliance status. Do not call the geoblock endpoint while rendering this local page.
    compliance_ok, compliance_detail = _local_compliance_status(settings, labels)

    # 5. reconciliation missing/fresh/stale
    from polymarket_weather_arb.services.live_monitor_service import is_fresh_reconciliation

    latest_reconciliation = repository.latest_reconciliation()
    if latest_reconciliation is None:
        reconciliation_status = "missing"
    elif is_fresh_reconciliation(latest_reconciliation):
        reconciliation_status = "fresh"
    else:
        reconciliation_status = "stale"

    # 6. backups exist - 检查备份目录
    backup_dir = Path("backups")
    backups_exist = backup_dir.exists() and (
        any(backup_dir.glob("*.sqlite3")) or any(backup_dir.glob("*.db"))
    )

    # 7. latest daemon tick status - use the runs table instead of reconciliation as a proxy.
    latest_run = repository.list_recent_runs(limit=1)
    tick_status = latest_run[0]["status"] if latest_run else "never"

    # 构建检查清单
    checks = [
        (
            labels["check_database"],
            db_exists,
            labels["check_database_ok"] if db_exists else labels["check_database_missing"],
        ),
        (
            labels["check_sdk"],
            sdk_installed,
            labels["check_sdk_ok"] if sdk_installed else labels["check_sdk_missing"],
        ),
        (
            labels["check_credentials"],
            credentials_ready,
            labels["check_credentials_ok"]
            if credentials_ready
            else labels["check_credentials_missing"],
        ),
        (labels["check_compliance"], compliance_ok, compliance_detail),
        (
            labels["check_reconciliation"],
            reconciliation_status == "fresh",
            labels[f"check_reconciliation_{reconciliation_status}"],
        ),
        (
            labels["check_backups"],
            backups_exist,
            labels["check_backups_ok"] if backups_exist else labels["check_backups_missing"],
        ),
        (
            labels["check_tick"],
            tick_status != "never",
            labels["check_tick_ok"] if tick_status != "never" else labels["check_tick_never"],
        ),
    ]

    # 渲染检查清单
    items = []
    for name, ok, detail in checks:
        status_icon = "✓" if ok else "✗"
        status_color = "#10b981" if ok else "#ef4444"
        items.append(f"""
        <tr>
            <td style="padding: 8px 12px; border-bottom: 1px solid #334155;">
                <span style="color: {status_color}; margin-right: 8px;">{status_icon}</span>
                {_e(name)}
            </td>
            <td style="padding: 8px 12px; border-bottom: 1px solid #334155; color: #94a3b8; font-size: 13px;">
                {_e(detail)}
            </td>
        </tr>
        """)

    return f"""
    <div style="background: #111827; border: 1px solid #334155; border-radius: 10px; padding: 16px;">
        <p style="margin: 0 0 12px 0; font-size: 13px; color: #94a3b8;">{labels["setup_checklist_body"]}</p>
        <table style="width: 100%; border-collapse: collapse;">
            {"".join(items)}
        </table>
        <div style="margin-top: 16px; padding: 12px; background: #0f172a; border-radius: 8px; font-size: 12px; color: #94a3b8;">
            <strong style="color: #e2e8f0;">{labels["cli_commands_title"]}</strong>
            <pre style="margin: 8px 0 0 0; padding: 8px; background: #020617; border-radius: 4px; font-size: 11px; overflow-x: auto;">{_e(labels["cli_commands"])}</pre>
        </div>
    </div>
    """


def _local_compliance_status(settings: Settings, labels: dict[str, str]) -> tuple[bool, str]:
    if settings.trading_disabled:
        return False, labels["check_compliance_trading_disabled"]
    if not settings.compliance_check_enabled:
        return False, labels["check_compliance_disabled"]
    return False, labels["check_compliance_needs_live_readiness"]


def _next_steps(labels: dict[str, str], lang: str) -> str:
    steps = [
        (labels["step_load_demo"], "/beginner"),
        (labels["step_review_orders"], "/orders"),
        (labels["step_readiness"], "/doctor"),
        (labels["step_candidates"], "/candidates"),
        (labels["step_live_launchpad"], "/live"),
    ]
    return (
        "<ol>"
        + "".join(
            f'<li><a href="{_href(href, lang)}">{_e(label)}</a></li>' for label, href in steps
        )
        + "</ol>"
    )


def _query(current_path: str) -> dict[str, list[str]]:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(current_path).query)


def _labels(lang: str) -> dict[str, str]:
    if lang == "en":
        return {
            "page_title": "Beginner Cockpit",
            "title": "Beginner Mode",
            "intro": "A safe first screen for dry-run operation. Live trading remains locked here.",
            "safety_title": "Safety status",
            "kill_switch": "Kill switch",
            "locked": "Live trading locked",
            "unlocked": "Live trading switch is off",
            "credentials": "Live credentials",
            "configured": "configured",
            "missing": "missing",
            "reconciliation": "Latest reconciliation",
            "recent_dry_runs": "Recent dry-runs",
            "auto_exit": "AUTO EXIT",
            "auto_exit_off": "OFF (default)",
            "auto_exit_on": "ENV=true (still needs daemon --allow-auto-exit + micro-live)",
            "browser_safe": "Browser actions on this page only load demo data and record dry-run intents.",
            "rehearsal_title": "Safe rehearsal",
            "rehearsal_body": "Load the bundled demo market and record one dry-run order intent.",
            "run_rehearsal": "Run safe rehearsal",
            "running": "Running...",
            "next_steps": "What to do next",
            "step_load_demo": "Run the safe rehearsal",
            "step_review_orders": "Review order intents",
            "step_readiness": "Review setup and live readiness warnings",
            "step_candidates": "Browse candidate markets",
            "step_live_launchpad": "Open Live Launchpad",
            "live_locked_title": "Live trading is locked",
            "live_locked_body": "This beginner page never approves or executes live actions. Use the CLI checklist when you intentionally move toward live mode.",
            "last_rehearsal": "Last rehearsal",
            "market": "Market",
            "order_side": "Side",
            "order_price": "Price",
            "order_size": "Size",
            "risk_status": "Risk check",
            "risk_accepted": "✓ Accepted",
            "risk_rejected": "✗ Rejected",
            "risk_unknown": "— Unknown",
            "side_buy": "BUY",
            "side_sell": "SELL",
            "rehearsal_explanation": "This was a safe dry-run. No real order was placed.",
            "view_details": "View order details",
            "safe_tick_title": "Safe automation tick",
            "safe_tick_body": "Run one safe automation tick. This will:",
            "safe_tick_feature_1": "Propose a dry-run action from existing candidates",
            "safe_tick_feature_2": "Auto-execute dry-run actions only (no live)",
            "safe_tick_feature_3": "Run risk guard checks",
            "run_safe_tick": "Run safe tick",
            "running_tick": "Running tick...",
            "safe_tick_safety": "This uses dry-run-demo profile. No live actions will be executed.",
            "setup_checklist_title": "Setup checklist",
            "setup_checklist_body": "Review these items before attempting live trading:",
            "check_database": "Database initialized",
            "check_database_ok": "Database file exists",
            "check_database_missing": "Run init-db to create database",
            "check_sdk": "Polymarket SDK installed",
            "check_sdk_ok": "polymarket-client is importable",
            "check_sdk_missing": "Install polymarket-client package",
            "check_credentials": "Live credentials",
            "check_credentials_ok": "Private key and funder wallet configured",
            "check_credentials_missing": "Set POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER in .env",
            "check_compliance": "Compliance check",
            "check_compliance_trading_disabled": "TRADING_DISABLED=true keeps live trading locked",
            "check_compliance_disabled": "COMPLIANCE_CHECK_ENABLED=false; enable it before live mode",
            "check_compliance_needs_live_readiness": "Run live-readiness to perform the current geoblock check",
            "check_reconciliation": "Reconciliation",
            "check_reconciliation_fresh": "Latest reconciliation is fresh",
            "check_reconciliation_stale": "Latest reconciliation is stale, run reconcile",
            "check_reconciliation_missing": "No reconciliation found, run reconcile",
            "check_backups": "Backups",
            "check_backups_ok": "Backup files exist in backups/",
            "check_backups_missing": "No backups found, run backup-db",
            "check_tick": "Daemon tick",
            "check_tick_ok": "Daemon has run at least once",
            "check_tick_never": "Daemon has never run",
            "cli_commands_title": "CLI commands for live readiness:",
            "cli_commands": """# Check overall health
uv run polymarket-weather doctor --live

# Check live readiness
uv run polymarket-weather live-readiness

# Initialize database
uv run polymarket-weather init-db

# Create backup
uv run polymarket-weather backup-db

# Run reconciliation
uv run polymarket-weather reconcile""",
        }
    return {
        "page_title": "新手操作台",
        "title": "新手模式",
        "intro": "给第一次操作准备的安全首页。这里可以做 dry-run，live 交易保持锁定。",
        "safety_title": "安全状态",
        "kill_switch": "Kill switch",
        "locked": "Live 交易已锁定",
        "unlocked": "Live 开关当前未锁",
        "credentials": "Live 凭证",
        "configured": "已配置",
        "missing": "缺失",
        "reconciliation": "最近对账",
        "recent_dry_runs": "最近模拟次数",
        "auto_exit": "AUTO EXIT",
        "auto_exit_off": "关闭（默认）",
        "auto_exit_on": "ENV=true（仍需 daemon --allow-auto-exit + micro-live）",
        "browser_safe": "本页浏览器操作只会加载 demo 数据并记录 dry-run intent。",
        "rehearsal_title": "安全演练",
        "rehearsal_body": "加载内置 demo 市场，并记录一条 dry-run 订单意图。",
        "run_rehearsal": "运行安全演练",
        "running": "演练中...",
        "next_steps": "接下来做什么",
        "step_load_demo": "运行安全演练",
        "step_review_orders": "查看订单意图",
        "step_readiness": "查看设置和 live readiness 警告",
        "step_candidates": "浏览候选市场",
        "step_live_launchpad": "打开 Live Launchpad",
        "live_locked_title": "Live 交易已锁定",
        "live_locked_body": "新手页不会批准或执行 live 操作。真的要进入 live mode 时，请走 CLI checklist。",
        "last_rehearsal": "上次演练",
        "market": "市场",
        "order_side": "方向",
        "order_price": "价格",
        "order_size": "数量",
        "risk_status": "风险检查",
        "risk_accepted": "✓ 通过",
        "risk_rejected": "✗ 拒绝",
        "risk_unknown": "— 未知",
        "side_buy": "买入",
        "side_sell": "卖出",
        "rehearsal_explanation": "这是一次安全的 dry-run，没有真实下单。",
        "view_details": "查看详情",
        "safe_tick_title": "安全自动执行",
        "safe_tick_body": "运行一次安全的自动执行。这会：",
        "safe_tick_feature_1": "从现有候选中提出一个 dry-run 行动",
        "safe_tick_feature_2": "仅自动执行 dry-run 行动（不会 live）",
        "safe_tick_feature_3": "运行风险检查",
        "run_safe_tick": "运行安全 tick",
        "running_tick": "执行中...",
        "safe_tick_safety": "使用 dry-run-demo 配置，不会执行任何 live 操作。",
        "setup_checklist_title": "设置检查清单",
        "setup_checklist_body": "在尝试 live 交易之前，请检查以下项目：",
        "check_database": "数据库已初始化",
        "check_database_ok": "数据库文件存在",
        "check_database_missing": "运行 init-db 创建数据库",
        "check_sdk": "Polymarket SDK 已安装",
        "check_sdk_ok": "polymarket-client 可导入",
        "check_sdk_missing": "安装 polymarket-client 包",
        "check_credentials": "Live 凭证",
        "check_credentials_ok": "私钥和 funder wallet 已配置",
        "check_credentials_missing": "在 .env 中设置 POLYMARKET_PRIVATE_KEY 和 POLYMARKET_FUNDER",
        "check_compliance": "合规检查",
        "check_compliance_trading_disabled": "TRADING_DISABLED=true，live 交易保持锁定",
        "check_compliance_disabled": "COMPLIANCE_CHECK_ENABLED=false；live 前请启用",
        "check_compliance_needs_live_readiness": "运行 live-readiness 执行当前 geoblock 检查",
        "check_reconciliation": "对账",
        "check_reconciliation_fresh": "最新对账是新鲜的",
        "check_reconciliation_stale": "最新对账已过期，运行 reconcile",
        "check_reconciliation_missing": "未找到对账，运行 reconcile",
        "check_backups": "备份",
        "check_backups_ok": "backups/ 目录中存在备份文件",
        "check_backups_missing": "未找到备份，运行 backup-db",
        "check_tick": "Daemon 执行",
        "check_tick_ok": "Daemon 至少运行过一次",
        "check_tick_never": "Daemon 从未运行过",
        "cli_commands_title": "Live readiness 的 CLI 命令：",
        "cli_commands": """# 检查整体健康状态
uv run polymarket-weather doctor --live

# 检查 live readiness
uv run polymarket-weather live-readiness

# 初始化数据库
uv run polymarket-weather init-db

# 创建备份
uv run polymarket-weather backup-db

# 运行对账
uv run polymarket-weather reconcile""",
    }
