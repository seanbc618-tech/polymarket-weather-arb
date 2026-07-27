from __future__ import annotations

from sqlite3 import Row
from urllib.parse import parse_qs, urlparse

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.dashboard_ui.html import (
    _bool_label,
    _dash,
    _definition_table,
    _e,
    _href,
    _render_flash,
    _section,
    _short_note,
    _table,
    render_page,
)
from polymarket_weather_arb.dashboard_ui.i18n import _t
from polymarket_weather_arb.services.cockpit_service import build_cockpit_snapshot
from polymarket_weather_arb.storage.repositories import Repository


def render_overview(
    repository: Repository, settings: Settings, lang: str, current_path: str
) -> str:
    cockpit = build_cockpit_snapshot(repository)
    body = "".join(
        [
            _render_flash(parse_qs(urlparse(current_path).query), lang),
            f"<h2>{_cockpit_label(lang, 'cockpit')}</h2>",
            '<section class="grid">',
            _cockpit_next_action_card(cockpit, lang),
            _cockpit_mode_card(cockpit, lang),
            "</section>",
            _cockpit_pipeline(cockpit, lang),
            '<section class="grid">',
            _cockpit_top_candidates(cockpit, lang),
            _cockpit_blockers(cockpit, lang),
            "</section>",
            '<section class="grid">',
            _cockpit_actions(cockpit, lang),
            _cockpit_runs(cockpit, lang),
            _reconciliation_card(repository.latest_reconciliation(), lang),
            "</section>",
        ]
    )
    return render_page(_t(lang, "app.title"), body, lang, current_path)


def _cockpit_next_action_card(cockpit, lang: str) -> str:
    action = cockpit.next_action
    return "".join(
        [
            '<section class="card">',
            f"<h2>{_cockpit_label(lang, 'next_action')}</h2>",
            f"<p><strong>{_e(_cockpit_action_label(action.label, lang))}</strong></p>",
            f"<p>{_e(action.reason)}</p>",
            f'<a class="btn" href="{_href(action.href, lang)}">{_cockpit_label(lang, "continue")}</a>',
            "</section>",
        ]
    )


def _cockpit_mode_card(cockpit, lang: str) -> str:
    return "".join(
        [
            '<section class="card">',
            f"<h2>{_cockpit_label(lang, 'mode')}</h2>",
            f"<p>{_e(cockpit.mode)}</p>",
            f"<p>{_cockpit_label(lang, 'profile')}: {_e(cockpit.profile)}</p>",
            f'<p class="muted">{_cockpit_label(lang, "browser_safety")}</p>',
            "</section>",
        ]
    )


def _cockpit_pipeline(cockpit, lang: str) -> str:
    stages = [
        ("found", cockpit.pipeline.found),
        ("parsed", cockpit.pipeline.parsed),
        ("quoted", cockpit.pipeline.quoted),
        ("signal", cockpit.pipeline.signal_ready),
        ("analyzed", cockpit.pipeline.analyzed),
        ("dry_run", cockpit.pipeline.dry_run),
    ]
    cards = "".join(
        f'<div class="card stat-card"><div class="stat-value">{count}</div><div class="stat-label">{_cockpit_label(lang, key)}</div></div>'
        for key, count in stages
    )
    return "".join(
        [
            '<section class="card">',
            f"<h2>{_cockpit_label(lang, 'pipeline')}</h2>",
            f'<div class="grid">{cards}</div>',
            "</section>",
        ]
    )


def _cockpit_top_candidates(cockpit, lang: str) -> str:
    if not cockpit.top_candidates:
        rows = f'<p class="muted">{_t(lang, "table.no_rows")}</p>'
    else:
        rows = _table(
            [
                _t(lang, "actions.market"),
                _t(lang, "field.module"),
                _t(lang, "field.status"),
                _t(lang, "field.best_bid"),
                _t(lang, "field.best_ask"),
                _cockpit_label(lang, "next_step"),
            ],
            [
                [
                    f'<a href="{_href(candidate.href, lang)}">{_e(candidate.market_id)}</a><br><span class="muted">{_e(candidate.title)}</span>',
                    candidate.module_id,
                    candidate.status,
                    _dash(candidate.best_bid),
                    _dash(candidate.best_ask),
                    _cockpit_step_label(candidate.next_step, lang),
                ]
                for candidate in cockpit.top_candidates
            ],
            lang,
            raw_columns={0},
        )
    return _section(_cockpit_label(lang, "top_candidates"), rows)


def _cockpit_blockers(cockpit, lang: str) -> str:
    if not cockpit.blockers:
        body = f'<p class="muted">{_cockpit_label(lang, "no_blockers")}</p>'
    else:
        body = (
            "<ul>"
            + "".join(
                f'<li><a href="{_href(blocker.href, lang)}">{_e(blocker.message)}</a></li>'
                for blocker in cockpit.blockers
            )
            + "</ul>"
        )
    return _section(_cockpit_label(lang, "blockers"), body)


def _cockpit_actions(cockpit, lang: str) -> str:
    if not cockpit.recent_actions:
        body = f'<p class="muted">{_t(lang, "table.no_rows")}</p>'
    else:
        body = (
            "<ul>"
            + "".join(
                f'<li><a href="{_href(action.href, lang)}">{_e(action.action_id)}</a> '
                f"{_e(action.kind)} / {_e(action.status)}</li>"
                for action in cockpit.recent_actions
            )
            + "</ul>"
        )
    return _section(
        _cockpit_label(lang, "recent_actions"),
        body + f'<p><a href="{_href("/actions", lang)}">{_t(lang, "nav.actions")}</a></p>',
    )


def _cockpit_runs(cockpit, lang: str) -> str:
    if not cockpit.recent_runs:
        body = f'<p class="muted">{_t(lang, "table.no_rows")}</p>'
    else:
        body = (
            "<ul>"
            + "".join(
                f'<li>#{run.run_id} {_e(run.status)} <span class="muted">{_e(_short_note(run.command, 60))}</span></li>'
                for run in cockpit.recent_runs
            )
            + "</ul>"
        )
    return _section(
        _cockpit_label(lang, "recent_runs"),
        body + f'<p><a href="{_href("/runs", lang)}">{_t(lang, "nav.runs")}</a></p>',
    )


def _cockpit_label(lang: str, key: str) -> str:
    labels = {
        "en": {
            "next_action": "Next action",
            "cockpit": "Operator Cockpit",
            "continue": "Continue",
            "mode": "Current mode",
            "profile": "Profile",
            "browser_safety": "Browser controls stay research/dry-run only in this slice.",
            "pipeline": "Candidate Pipeline",
            "found": "Found",
            "parsed": "Parsed",
            "quoted": "Quoted",
            "signal": "Signal",
            "analyzed": "Analyzed",
            "dry_run": "Dry-run",
            "top_candidates": "Top candidates",
            "blockers": "Blockers and failures",
            "no_blockers": "No current blockers.",
            "recent_actions": "Recent actions",
            "recent_runs": "Recent runs",
            "next_step": "Next step",
        },
        "zh": {
            "next_action": "下一步",
            "cockpit": "操作台",
            "continue": "继续",
            "mode": "当前模式",
            "profile": "Profile",
            "browser_safety": "本阶段浏览器只保留 research / dry-run 操作。",
            "pipeline": "候选漏斗",
            "found": "发现 Found",
            "parsed": "已解析 Parsed",
            "quoted": "有盘口 Quoted",
            "signal": "信号 Signal",
            "analyzed": "已分析 Analyzed",
            "dry_run": "模拟 Dry-run",
            "top_candidates": "重点候选",
            "blockers": "阻塞和失败",
            "no_blockers": "当前没有阻塞。",
            "recent_actions": "最近 action",
            "recent_runs": "最近运行",
            "next_step": "下一步",
        },
    }
    return labels.get(lang, labels["en"])[key]


def _cockpit_action_label(label: str, lang: str) -> str:
    if lang != "zh":
        return label
    return {
        "Run discovery": "运行市场扫描",
        "Run approved action": "执行已批准 action",
        "Review pending action": "审核待处理 action",
        "Refresh missing signals": "刷新缺失信号",
        "Analyze ready candidate": "分析就绪候选",
        "Dry-run latest analysis": "模拟最新分析",
        "Resolve blocker": "处理阻塞",
        "Review candidates": "查看候选市场",
    }.get(label, label)


def _cockpit_step_label(step: str, lang: str) -> str:
    if lang != "zh":
        return step
    return {
        "inspect": "解析规则",
        "refresh_quote": "刷新盘口",
        "refresh_signal": "刷新信号",
        "analyze": "分析",
        "dry_run": "模拟交易",
        "review": "复盘",
    }.get(step, step)


def _render_market_readiness(readiness, lang: str) -> str:
    blocker_text = (
        ", ".join(readiness.blockers)
        if readiness.blockers
        else _workbench_label(lang, "no_blockers")
    )
    return _definition_table(
        [
            (_workbench_label(lang, "next_step"), _cockpit_step_label(readiness.next_step, lang)),
            (_workbench_label(lang, "has_quote"), _bool_label(readiness.has_quote, lang)),
            (_workbench_label(lang, "has_signal"), _bool_label(readiness.has_signal, lang)),
            (_workbench_label(lang, "has_analysis"), _bool_label(readiness.has_analysis, lang)),
            (_workbench_label(lang, "has_dry_run"), _bool_label(readiness.has_dry_run, lang)),
            (_workbench_label(lang, "blockers"), blocker_text),
        ]
    )


def _workbench_label(lang: str, key: str) -> str:
    labels = {
        "en": {
            "readiness": "Decision State",
            "next_step": "Next step",
            "has_quote": "Quote",
            "has_signal": "Signal",
            "has_analysis": "Analysis",
            "has_dry_run": "Dry-run",
            "blockers": "Blockers",
            "no_blockers": "No blockers",
        },
        "zh": {
            "readiness": "决策状态",
            "next_step": "下一步",
            "has_quote": "盘口",
            "has_signal": "信号",
            "has_analysis": "分析",
            "has_dry_run": "模拟交易",
            "blockers": "阻塞",
            "no_blockers": "无阻塞",
        },
    }
    return labels.get(lang, labels["en"])[key]


def _reconciliation_card(row: Row | None, lang: str) -> str:
    if row is None:
        return f'<section class="card"><h2>{_t(lang, "overview.reconciliation")}</h2><p class="muted">{_t(lang, "overview.no_reconciliation")}</p></section>'
    return "".join(
        [
            f'<section class="card"><h2>{_t(lang, "overview.reconciliation")}</h2>',
            f"<p>{_t(lang, 'actions.status')}: {_e(row['status'])}</p>",
            f"<p>{_t(lang, 'field.created')}: {_e(row['created_at'])}</p>",
            "</section>",
        ]
    )
