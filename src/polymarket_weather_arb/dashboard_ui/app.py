from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from polymarket_weather_arb.adapters.http_reader import open_meteo_usage_snapshot
from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.dashboard_ui.html import (
    _e,
    _hidden_lang,
    _href,
    _render_flash,
)
from polymarket_weather_arb.domain.risk import (
    HARDCODED_MAX_DAILY_USDC,
    HARDCODED_MAX_MARKET_USDC,
    HARDCODED_MAX_ORDER_USDC,
)
from polymarket_weather_arb.services.autopilot_service import AutopilotService
from polymarket_weather_arb.services.deploy_service import build_deploy_plan
from polymarket_weather_arb.services.cockpit_service import (
    build_cockpit_snapshot,
    OpportunityFunnel,
    VerifiedRealizedPnL,
)
from polymarket_weather_arb.dashboard_ui.stream_panel import (
    brand_mark_html,
    render_stream_monitor_panel,
)
from polymarket_weather_arb.storage.repositories import Repository


_STREAM_STYLE_PATH = Path(__file__).resolve().parent / "stream_panel.css"
_STREAM_STYLE = (
    _STREAM_STYLE_PATH.read_text(encoding="utf-8") if _STREAM_STYLE_PATH.is_file() else ""
)


_APP_STYLE = r"""
/* ===========================================================
       Weather Autopilot · console v7 — magazine stream console
       oklch tokens · glass surfaces · slate-blue accent · one
       accent per screen. V7: positions + funnel as mag-stream
       strips (same language as event feed); honest tick cadence.
       =========================================================== */
    :root {
      --bg:           oklch(96.8% 0.006 250);
      --bg-soft:      oklch(96.8% 0.006 250);
      --bg-elevated:  oklch(98.5% 0.004 250);
      --surface:      oklch(100% 0 0);
      --surface-2:   oklch(98% 0.005 250);
      --surface-3:   oklch(98% 0.005 250);
      --fg:           oklch(22% 0.02 250);
      --text:         oklch(22% 0.02 250);
      --muted:        oklch(48% 0.018 250);
      --muted-2:      oklch(62% 0.014 250);
      --border:       oklch(88% 0.01 250);
      --border-soft:  oklch(92% 0.008 250);
      --glass:        oklch(100% 0 0 / 0.72);
      --glass-border: oklch(100% 0 0 / 0.55);
      --accent:       oklch(52% 0.09 230);
      --accent-soft:  oklch(95% 0.02 230);
      --primary:      oklch(52% 0.09 230);
      --primary-strong: oklch(44% 0.09 230);
      --blue:         oklch(52% 0.08 240);
      --info:         oklch(52% 0.08 240);
      --info-soft:    oklch(95% 0.02 240);
      --success:      oklch(54% 0.13 150);
      --success-soft: oklch(95% 0.03 150);
      --warn:         oklch(70% 0.12 80);
      --warn-soft:   oklch(96% 0.035 90);
      --danger:       oklch(56% 0.17 25);
      --danger-soft:  oklch(96% 0.03 20);
      --stream:       oklch(58% 0.11 195);
      --stream-glow:  oklch(70% 0.1 195 / 0.35);
      --shadow-xs: 0 1px 0 oklch(30% 0.02 250 / 0.04);
      --shadow-sm: 0 1px 2px oklch(30% 0.02 250 / 0.05), 0 4px 12px oklch(30% 0.02 250 / 0.04);
      --shadow-md: 0 2px 4px oklch(30% 0.02 250 / 0.05), 0 10px 28px oklch(30% 0.02 250 / 0.07);
      --shadow: var(--shadow-md);
      --radius: 8px;
      --radius-sm: 6px;
      --radius-xs: 4px;
      --space-1: 4px; --space-2: 8px; --space-3: 12px;
      --space-4: 16px; --space-5: 20px; --space-6: 24px;
      --header-h: 52px;
      --font: -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC",
        "Hiragino Sans GB", "Noto Sans SC", "Segoe UI", system-ui, sans-serif;
      --font-mono: "SF Mono", "JetBrains Mono", ui-monospace, Menlo, Consolas, monospace;
      --text-xs: 11px; --text-sm: 12.5px; --text-md: 13.5px;
      --text-lg: 15px; --text-xl: 18px; --text-2xl: 22px;
      --dur: 160ms;
      --ease: cubic-bezier(0.22, 0.9, 0.28, 1);
      --focus-ring: 0 0 0 2px var(--surface), 0 0 0 4px oklch(58% 0.1 230);
    }

    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: var(--font);
      color: var(--fg);
      font-size: var(--text-md);
      line-height: 1.45;
      background:
        radial-gradient(1100px 420px at 8% -8%, oklch(97.2% 0.01 220 / 0.55), transparent 58%),
        linear-gradient(180deg, oklch(97.8% 0.004 250), var(--bg));
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
      text-rendering: optimizeLegibility;
    }
    button, input, select { font: inherit; }
    button { cursor: pointer; }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    :focus { outline: none; }
    :focus-visible { outline: none; box-shadow: var(--focus-ring); }

    .mono {
      font-family: var(--font-mono);
      font-variant-numeric: tabular-nums;
      letter-spacing: -0.01em;
      color: var(--fg);
    }
    .truncate { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .sr-only {
      position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
      overflow: hidden; clip: rect(0,0,0,0); border: 0;
    }
    .skip-link {
      position: absolute; left: 12px; top: -48px; z-index: 200;
      padding: 8px 12px; border-radius: var(--radius-sm);
      background: var(--fg); color: #fff; font-size: var(--text-sm); font-weight: 650;
      transition: top var(--dur) var(--ease);
    }
    .skip-link:focus { top: 10px; box-shadow: var(--focus-ring); }

    /* ===== Shell / header ===== */
    header.app-header {
      position: sticky; top: 0; z-index: 40;
      display: flex; align-items: center; gap: var(--space-3);
      min-height: var(--header-h);
      padding: 0 var(--space-4);
      background: oklch(100% 0 0 / 0.62);
      backdrop-filter: saturate(1.4) blur(18px);
      -webkit-backdrop-filter: saturate(1.4) blur(18px);
      border-bottom: 1px solid var(--border-soft);
      box-shadow: var(--shadow-xs);
    }
    .brand { display: flex; align-items: center; gap: 10px; min-width: 0; flex: 0 0 auto; }
    .brand-mark {
      width: 32px; height: 32px; border-radius: 9px;
      flex: 0 0 auto; overflow: hidden;
      border: 1px solid oklch(88% 0.02 230);
      background: oklch(18% 0.04 270);
      box-shadow:
        0 0 0 1px oklch(100% 0 0 / 0.6) inset,
        0 2px 8px oklch(40% 0.06 250 / 0.12),
        0 0 16px oklch(65% 0.1 220 / 0.18);
    }
    .brand-mark img {
      width: 100%; height: 100%; display: block;
      object-fit: cover; object-position: center 18%;
    }
    .brand-copy { display: flex; min-width: 0; flex-direction: column; }
    .brand-copy strong { font-size: var(--text-sm); font-weight: 650; letter-spacing: -0.01em; }
    .brand-copy small { color: var(--muted); font-size: 10.5px; font-weight: 500; }

    .top-metrics {
      display: flex; align-items: center; gap: 6px;
      flex: 1 1 auto; min-width: 0; overflow-x: auto; scrollbar-width: none;
    }
    .top-metrics::-webkit-scrollbar { display: none; }
    .metric-chip {
      display: inline-flex; align-items: center; gap: 6px;
      padding: 5px 9px; border-radius: 999px;
      border: 1px solid var(--border-soft);
      background: var(--glass);
      backdrop-filter: blur(10px);
      -webkit-backdrop-filter: blur(10px);
      white-space: nowrap; font-size: var(--text-xs);
      color: var(--muted); flex: 0 0 auto;
    }
    .metric-chip strong {
      color: var(--fg); font-weight: 600;
      font-family: var(--font-mono); font-variant-numeric: tabular-nums;
      font-size: 11.5px;
    }

    .header-cluster { display: flex; align-items: center; gap: 8px; flex: 0 0 auto; flex-wrap: wrap; justify-content: flex-end; }
    .header-mode-chip, .live-chip {
      display: inline-flex; align-items: center; gap: 6px;
      padding: 4px 9px; border-radius: 999px;
      font-size: var(--text-xs); font-weight: 650;
      border: 1px solid transparent; line-height: 1.2; white-space: nowrap;
    }
    .header-mode-chip.mode-safe { background: var(--info-soft); color: oklch(38% 0.06 240); border-color: oklch(88% 0.03 240); }
    .header-mode-chip.mode-live { background: var(--warn-soft); color: oklch(40% 0.08 70); border-color: oklch(88% 0.05 80); }
    .live-chip .pulse { width: 6px; height: 6px; border-radius: 50%; background: currentColor; flex: 0 0 auto; }
    .live-chip.is-running { background: var(--success-soft); color: oklch(34% 0.08 150); border-color: oklch(86% 0.05 150); }
    .live-chip.is-running .pulse { animation: pulse 1.8s ease-out infinite; }
    .live-chip.is-paused { background: var(--warn-soft); color: oklch(40% 0.08 70); border-color: oklch(88% 0.05 80); }
    .live-chip.is-stale { background: oklch(95% 0.01 250); color: var(--muted); border-color: var(--border); }
    .live-chip.is-blocked { background: var(--danger-soft); color: oklch(42% 0.12 20); border-color: oklch(88% 0.05 20); }
    .live-chip.is-paused .pulse, .live-chip.is-stale .pulse, .live-chip.is-blocked .pulse { animation: none; box-shadow: none; }
    @keyframes pulse {
      0% { box-shadow: 0 0 0 0 oklch(54% 0.13 150 / 0.45); }
      70% { box-shadow: 0 0 0 7px oklch(54% 0.13 150 / 0); }
      100% { box-shadow: 0 0 0 0 oklch(54% 0.13 150 / 0); }
    }

    .lang-nav { display: inline-flex; overflow: hidden; border: 1px solid var(--border); border-radius: 999px; background: var(--surface); }
    .lang-link {
      padding: 5px 9px; border: 0; border-radius: 0;
      color: var(--muted); font-size: var(--text-xs); font-weight: 600;
      background: transparent; text-decoration: none;
    }
    .lang-link.active { background: oklch(96% 0.01 250); color: var(--fg); }

    /* ===== Page / layout ===== */
    main {
      max-width: 1440px; margin: 0 auto;
      padding: var(--space-4) var(--space-4) 56px;
      display: flex; flex-direction: column; gap: var(--space-3);
    }

    .path-rail {
      display: flex; width: fit-content; max-width: 100%;
      align-items: center; gap: 5px; flex-wrap: wrap;
      padding: 7px 11px; border-radius: 999px;
      border: 1px solid var(--border-soft);
      background: var(--glass);
      backdrop-filter: blur(10px);
      -webkit-backdrop-filter: blur(10px);
      box-shadow: var(--shadow-xs);
    }
    .path-label { margin-right: 4px; color: var(--muted); font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; }
    .path-step {
      display: inline-flex; align-items: center; gap: 6px;
      padding: 4px 9px; border-radius: 999px;
      color: var(--muted); font-size: var(--text-sm); font-weight: 600;
      transition: color var(--dur) var(--ease), background var(--dur) var(--ease);
    }
    .path-step:hover, .path-step.is-active { color: var(--accent); background: var(--accent-soft); text-decoration: none; }
    .path-number {
      display: grid; width: 19px; height: 19px; place-items: center;
      border-radius: 50%; color: #fff; background: var(--accent);
      font-size: 10px; font-weight: 700;
    }

    /* ===== Windows / panels — flat glass, no nested cards ===== */
    .window {
      position: relative; margin-bottom: var(--space-3);
      padding: var(--space-4);
      border: 1px solid var(--glass-border);
      border-radius: var(--radius);
      background: var(--glass);
      backdrop-filter: saturate(1.2) blur(14px);
      -webkit-backdrop-filter: saturate(1.2) blur(14px);
      box-shadow: var(--shadow-sm);
      overflow: hidden; min-width: 0;
    }
    .window--aux { box-shadow: var(--shadow-xs); background: oklch(100% 0 0 / 0.48); }
    .window--path { border-color: var(--border); box-shadow: var(--shadow-sm); }
    .window--primary {
      border-color: oklch(86% 0.012 250);
      background: oklch(100% 0 0 / 0.78);
      box-shadow: var(--shadow-md);
    }
    .window--primary::before {
      content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
      background: linear-gradient(180deg, oklch(58% 0.09 230), oklch(54% 0.1 160));
      border-radius: var(--radius) 0 0 var(--radius);
      pointer-events: none;
    }
    .window--gate {
      border-color: oklch(86% 0.03 150);
      background: oklch(98.6% 0.012 150 / 0.6);
      box-shadow: var(--shadow-sm);
    }
    #command, #checks, #safety { scroll-margin-top: 84px; }

    .panel-head {
      display: flex; align-items: center; justify-content: space-between;
      gap: 10px; margin-bottom: 13px; min-height: 38px;
    }
    .panel-head h2, .window h2 { margin: 0; font-size: var(--text-md); font-weight: 650; letter-spacing: -0.01em; color: var(--fg); }
    .panel-sub { margin: 0; color: var(--muted); font-size: var(--text-xs); }
    .step-chip {
      display: inline-flex; width: fit-content; align-items: center;
      min-height: 24px; padding: 3px 9px; border-radius: 999px;
      color: var(--accent); background: var(--accent-soft);
      border: 1px solid oklch(88% 0.03 230);
      font-size: var(--text-xs); font-weight: 650; letter-spacing: 0.02em;
    }

    /* ===== Hero ===== */
    .hero {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(320px, 390px);
      gap: var(--space-3); align-items: stretch;
    }
    .hero-copy {
      min-height: 200px; padding: 26px;
      border: 1px solid var(--glass-border); border-radius: var(--radius);
      background: var(--glass);
      backdrop-filter: blur(14px);
      -webkit-backdrop-filter: blur(14px);
      box-shadow: var(--shadow-sm);
      display: flex; flex-direction: column; justify-content: center;
    }
    .eyebrow {
      margin: 0 0 8px; color: var(--accent);
      font-size: var(--text-xs); font-weight: 700;
      text-transform: uppercase; letter-spacing: 0.06em;
    }
    h1 {
      margin: 0; color: var(--fg);
      font-size: clamp(1.85rem, 3.4vw, 2.6rem);
      font-weight: 650; letter-spacing: -0.02em; line-height: 1.08;
    }
    .lede { margin: 12px 0 0; color: var(--muted); max-width: 58ch; font-size: var(--text-md); }

    /* ===== Command card ===== */
    .command-card { padding: var(--space-5); }
    .command-top { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
    .command-card h2 { margin: 0; font-size: var(--text-xl); color: var(--fg); }
    .command-card p { margin: 6px 0 0; color: var(--muted); font-size: var(--text-sm); }
    .command-actions {
      display: grid; grid-template-columns: 1fr 1fr; gap: 9px; margin-top: 16px;
    }
    .command-actions form:first-child { grid-column: 1 / -1; }
    .refresh-note { margin: 13px 0 0; color: var(--muted); font-size: var(--text-xs); }
    .status-chip {
      display: inline-flex; align-items: center; gap: 8px;
      padding: 5px 10px; border-radius: 999px;
      border: 1px solid var(--border-soft); background: oklch(100% 0 0 / 0.5);
      white-space: nowrap; font-size: var(--text-sm); font-weight: 650; color: var(--muted);
    }
    .status-chip .pulse { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
    .status-chip.status-ok { color: oklch(34% 0.08 150); background: var(--success-soft); border-color: oklch(86% 0.05 150); }
    .status-chip.status-warn { color: oklch(40% 0.08 70); background: var(--warn-soft); border-color: oklch(88% 0.05 80); }
    .status-chip.status-ok .pulse { animation: pulse 1.8s ease-out infinite; }

    .command-toolbar {
      display: flex; align-items: center; justify-content: space-between; gap: 12px;
      min-height: 54px; padding: 8px 10px 8px 12px;
      border: 1px solid var(--border); border-radius: var(--radius);
      background: oklch(100% 0 0 / 0.72); box-shadow: var(--shadow-xs);
    }
    .command-toolbar-state, .command-toolbar-actions {
      display: flex; align-items: center; gap: 8px; min-width: 0;
    }
    .toolbar-mode, .toolbar-tick {
      display: inline-flex; align-items: center; gap: 6px; min-width: 0;
      padding-left: 9px; border-left: 1px solid var(--border-soft);
      color: var(--muted); font-size: var(--text-xs);
    }
    .toolbar-mode strong, .toolbar-tick strong { color: var(--fg); font-size: var(--text-sm); }
    .command-toolbar-actions form { margin: 0; }
    .command-toolbar-actions .btn { width: auto; min-height: 34px; }
    .setup-disclosure { margin: 0; }
    .setup-disclosure > summary {
      display: flex; align-items: center; justify-content: space-between; gap: 12px;
      margin: 0; padding: 11px 14px; cursor: pointer; list-style: none;
    }
    .setup-disclosure > summary::-webkit-details-marker { display: none; }
    .setup-disclosure > summary span:first-child { display: flex; flex-direction: column; gap: 2px; }
    .setup-disclosure > summary strong { color: var(--fg); font-size: var(--text-sm); }
    .setup-disclosure > summary small { color: var(--muted); font-size: var(--text-xs); }
    .setup-expanded { padding-top: var(--space-3); }

    /* ===== Buttons ===== */
    .btn {
      display: inline-flex; align-items: center; justify-content: center; gap: 6px;
      width: 100%; min-height: 38px; padding: 0 12px;
      border: 1px solid var(--border); border-radius: var(--radius-sm);
      background: var(--surface); color: var(--fg);
      font-size: var(--text-sm); font-weight: 600;
      transition: background var(--dur) var(--ease), border-color var(--dur) var(--ease),
        opacity var(--dur) var(--ease), transform var(--dur) var(--ease);
      white-space: nowrap;
    }
    .btn:hover:not(:disabled) { background: oklch(97.5% 0.005 250); }
    .btn:active:not(:disabled) { transform: translateY(0.5px); }
    .btn:disabled { opacity: 0.42; cursor: not-allowed; }
    .btn-primary {
      background: oklch(32% 0.04 250); border-color: oklch(32% 0.04 250); color: #fff;
    }
    .btn-primary:hover:not(:disabled) { background: oklch(26% 0.04 250); }
    .btn-secondary {
      background: var(--info-soft); color: oklch(34% 0.06 240);
      border-color: oklch(88% 0.03 240);
    }
    .btn-secondary:hover:not(:disabled) { background: oklch(94% 0.02 240); }
    .btn-danger { background: var(--danger); border-color: oklch(50% 0.16 25); color: #fff; }
    .btn-danger:hover:not(:disabled) { background: oklch(50% 0.17 25); }
    .btn-ghost { background: transparent; border-color: transparent; color: var(--muted); }
    .btn-ghost:hover:not(:disabled) { background: oklch(96% 0.006 250); color: var(--fg); }

    .more-disclosure { margin-top: 8px; border-top: 1px solid var(--border-soft); }
    .more-disclosure > summary {
      padding: 9px 4px; cursor: pointer; list-style: none;
      color: var(--accent); font-size: var(--text-xs); font-weight: 650;
    }
    .more-disclosure > summary::-webkit-details-marker { display: none; }
    .more-disclosure > summary::before { content: "+"; margin-right: 7px; }
    .more-disclosure[open] > summary::before { content: "\2212"; }
    .panel-count { margin-left: auto; }

    /* ===== Stats grid ===== */
    .stats-grid {
      display: grid; grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 8px;
    }
    .stat-card {
      min-height: 78px; padding: 12px 13px;
      border: 1px solid var(--border-soft); border-radius: var(--radius);
      background: var(--glass);
      backdrop-filter: blur(10px);
      -webkit-backdrop-filter: blur(10px);
      box-shadow: var(--shadow-xs);
    }
    .stat-label { display: block; color: var(--muted); font-size: var(--text-xs); margin-bottom: 6px; }
    .stat-value { display: block; color: var(--fg); font-size: var(--text-sm); font-weight: 600; word-break: break-word; }
    .badge {
      display: inline-flex; width: fit-content; padding: 3px 8px;
      border-radius: 999px; font-size: var(--text-xs); font-weight: 650;
    }
    .badge-live { background: var(--warn-soft); color: oklch(40% 0.08 70); border: 1px solid oklch(88% 0.05 80); }
    .badge-dry { background: var(--success-soft); color: oklch(34% 0.08 150); border: 1px solid oklch(86% 0.05 150); }

    /* ===== Mode grid ===== */
    .mode-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
    .mode-card {
      min-height: 200px; display: flex; flex-direction: column; gap: 9px;
      padding: 0 14px 14px;
      border: 1px solid var(--border); border-radius: var(--radius);
      background: var(--surface); overflow: hidden;
      transition: transform var(--dur) var(--ease), border-color var(--dur) var(--ease),
        box-shadow var(--dur) var(--ease);
    }
    .mode-card:hover { transform: translateY(-2px); box-shadow: var(--shadow-sm); }
    .mode-card.selected { border-color: var(--mode-color, var(--accent)); box-shadow: inset 0 0 0 1px var(--mode-color, var(--accent)); }
    .mode-card.locked { opacity: 0.62; border-style: dashed; }
    .mode-media { height: 52px; margin: 0 -14px 3px; opacity: 0.7; background: var(--mode-wash); }
    .mode-observe { --mode-color: oklch(52% 0.08 240); --mode-wash: linear-gradient(120deg, oklch(92% 0.02 240), oklch(70% 0.06 240)); }
    .mode-paper { --mode-color: var(--accent); --mode-wash: linear-gradient(120deg, var(--accent-soft), oklch(80% 0.05 230)); }
    .mode-micro_live { --mode-color: oklch(70% 0.12 80); --mode-wash: linear-gradient(120deg, var(--warn-soft), oklch(78% 0.1 80)); }
    .mode-full_live { --mode-color: var(--danger); --mode-wash: linear-gradient(120deg, var(--danger-soft), oklch(72% 0.12 25)); }
    .mode-top { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
    .mode-top h3 { margin: 0; font-size: var(--text-md); color: var(--fg); }
    .mode-top span { color: var(--mode-color); font-size: var(--text-xs); font-weight: 650; }
    .mode-card p { margin: 0; color: var(--muted); font-size: var(--text-sm); flex: 1; }
    .mode-risk {
      display: inline-flex; width: fit-content; padding: 3px 7px; border-radius: 999px;
      color: var(--mode-color);
      background: color-mix(in srgb, var(--mode-color) 9%, white);
      font-size: var(--text-xs); font-weight: 650;
    }
    .mode-card form { margin-top: auto; }
    .mode-card .btn { min-height: 34px; }

    /* ===== V6 ops health rail (compact stream strip) ===== */
    .ops-health-rail {
      margin-bottom: var(--space-3);
      border: 1px solid var(--border-soft);
      border-radius: var(--radius);
      background: oklch(100% 0 0 / 0.72);
      box-shadow: var(--shadow-sm);
      overflow: hidden;
    }
    .ops-health-rail::before {
      content: ""; display: block; height: 2px;
      background: linear-gradient(90deg, oklch(58% 0.11 195), oklch(54% 0.1 150), oklch(58% 0.09 230));
    }
    .ops-health-main {
      display: flex; align-items: center; justify-content: space-between; gap: 12px;
      padding: 10px 14px; flex-wrap: wrap;
    }
    .ops-health-left, .ops-health-right {
      display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-width: 0;
    }
    .ops-health-label {
      font-size: 10.5px; font-weight: 700; letter-spacing: 0.06em;
      text-transform: uppercase; color: var(--muted);
    }
    .ops-cadence {
      display: inline-flex; align-items: center; gap: 6px;
      padding: 4px 9px; border-radius: 999px;
      border: 1px solid oklch(86% 0.04 195);
      background: oklch(96% 0.02 195 / 0.55);
      font-size: var(--text-xs); color: oklch(36% 0.07 195); font-weight: 650;
    }
    .ops-cadence .mono { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
    .ops-chip {
      display: inline-flex; align-items: center; gap: 5px;
      padding: 4px 8px; border-radius: 999px;
      border: 1px solid var(--border-soft);
      background: oklch(100% 0 0 / 0.7);
      font-size: 11px; color: var(--muted); white-space: nowrap;
    }
    .ops-chip strong {
      color: var(--fg); font-family: var(--font-mono);
      font-variant-numeric: tabular-nums; font-weight: 650; font-size: 11.5px;
    }
    .ops-health-details {
      border-top: 1px solid var(--border-soft);
      background: oklch(99% 0.004 250 / 0.55);
    }
    .ops-health-details > summary {
      list-style: none; cursor: pointer;
      padding: 8px 14px; font-size: var(--text-xs); font-weight: 650;
      color: var(--muted); user-select: none;
    }
    .ops-health-details > summary::-webkit-details-marker { display: none; }
    .ops-health-details > summary:hover { color: var(--fg); background: oklch(97% 0.006 250); }
    .ops-health-details[open] > summary { color: var(--fg); border-bottom: 1px solid var(--border-soft); }
    .ops-health-expand {
      display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: var(--space-3); padding: 12px 14px 14px;
    }
    .ops-health-expand > .window { margin: 0; box-shadow: none; }
    .more-ops-disclosure { margin-top: var(--space-2); }
    .more-ops-disclosure > summary {
      list-style: none; cursor: pointer;
      padding: 12px 14px; border-radius: var(--radius);
      border: 1px solid var(--border-soft);
      background: oklch(100% 0 0 / 0.65);
      font-size: var(--text-sm); font-weight: 650; color: var(--fg);
    }
    .more-ops-disclosure > summary::-webkit-details-marker { display: none; }
    .more-ops-disclosure > summary:hover { background: oklch(98% 0.005 250); }
    .more-ops-disclosure[open] > summary {
      border-bottom-left-radius: 0; border-bottom-right-radius: 0;
      border-bottom-color: transparent;
    }
    .more-ops-body {
      padding: 0 0 4px;
      border: 1px solid var(--border-soft); border-top: 0;
      border-radius: 0 0 var(--radius) var(--radius);
      background: oklch(100% 0 0 / 0.45);
    }
    .more-ops-body > * { margin-left: 10px; margin-right: 10px; }
    .more-ops-body > *:first-child { margin-top: 8px; }

    /* ===== Checks + safety dual grid ===== */
    .checks-safety-grid {
      display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: var(--space-3); align-items: stretch;
    }
    .checks-safety-grid > .window { margin-bottom: var(--space-3); }
    .checks-safety-grid .first-run-panel,
    .checks-safety-grid .safety-panel { padding: 15px; }
    .checks-safety-grid .panel-head { margin-bottom: 9px; }
    @media (max-width: 900px) {
      .ops-health-expand { grid-template-columns: 1fr; }
    }
    .check-summary { display: flex; gap: 7px; flex-wrap: wrap; margin-bottom: 10px; }
    .summary-pill { padding: 3px 8px; border-radius: 999px; font-size: var(--text-xs); font-weight: 650; }
    .summary-ok { color: oklch(34% 0.08 150); background: var(--success-soft); }
    .summary-warn { color: oklch(40% 0.08 70); background: var(--warn-soft); }
    .summary-bad { color: oklch(42% 0.12 20); background: var(--danger-soft); }
    .check-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 5px; }
    .check-row {
      position: relative; overflow: hidden;
      display: grid; grid-template-columns: 14px minmax(120px, 1fr) auto;
      gap: 8px 10px; align-items: center;
      padding: 8px 9px 8px 12px;
      border: 1px solid var(--border-soft); border-radius: var(--radius-sm);
      background: oklch(100% 0 0 / 0.5);
    }
    .check-row::before { content: ""; position: absolute; inset: 0 auto 0 0; width: 3px; background: var(--muted); }
    .check-row.check-ok::before { background: var(--success); }
    .check-row.check-warn::before { background: var(--warn); }
    .check-row.check-bad::before { background: var(--danger); }
    .check-dot { width: 9px; height: 9px; border-radius: 50%; background: var(--muted); }
    .check-ok .check-dot { background: var(--success); }
    .check-warn .check-dot { background: var(--warn); }
    .check-bad .check-dot { background: var(--danger); }
    .check-row strong { font-size: var(--text-sm); color: var(--fg); }
    .check-row p { grid-column: 2 / -1; margin: 0; color: var(--muted); font-size: var(--text-xs); }
    .first-run-panel.is-compact .check-list { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .first-run-panel.is-compact .check-row { grid-template-columns: 12px minmax(0, 1fr) auto; }
    .first-run-panel.is-compact .check-row p { display: none; }

    .safety-banner {
      display: flex; align-items: center; gap: 8px; margin-bottom: 11px;
      color: oklch(34% 0.08 150); font-size: var(--text-xs); font-weight: 650;
    }
    .safety-panel h2 { margin: 0 0 5px; font-size: var(--text-lg); }
    .safety-grid { display: grid; gap: 6px; margin-top: 12px; }
    .safety-panel.is-compact .safety-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .safety-grid > div {
      display: flex; align-items: center; justify-content: space-between; gap: 12px;
      padding: 8px 10px; border: 1px solid var(--border-soft); border-radius: var(--radius-sm);
      background: oklch(100% 0 0 / 0.6);
    }
    .safety-grid span { color: var(--muted); font-size: var(--text-sm); }
    .safety-grid strong { color: var(--fg); font-family: var(--font-mono); font-variant-numeric: tabular-nums; font-size: var(--text-sm); }
    .safety-limit { margin: 11px 0 0; color: oklch(40% 0.06 150); font-size: var(--text-xs); }

    /* ===== Finance center — primary visual weight ===== */
    .finance-panel {
      position: relative; margin-bottom: var(--space-3); padding: 0; overflow: hidden;
      border: 1px solid oklch(86% 0.012 250); border-radius: var(--radius);
      background: oklch(100% 0 0 / 0.78);
      box-shadow: var(--shadow-md);
    }
    .finance-panel::before {
      content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
      background: linear-gradient(180deg, oklch(58% 0.09 230), oklch(54% 0.1 160));
      border-radius: var(--radius) 0 0 var(--radius); pointer-events: none;
    }
    .finance-panel .pnl-heading {
      display: flex; align-items: center; justify-content: space-between; gap: 10px;
      padding: 13px 16px 9px 18px; text-align: left;
    }
    .finance-panel .pnl-heading h2 { margin: 0; font-size: var(--text-md); font-weight: 700; color: var(--fg); }
    .finance-panel > .muted, .finance-panel > .pnl-warning { max-width: none; margin: 0 16px 0 18px; text-align: left; color: var(--muted); font-size: var(--text-xs); }
    .finance-panel .pnl-warning { margin-top: 8px; margin-bottom: 0; }
    .pnl-warning {
      max-width: 760px; margin: 12px auto 18px; padding: 9px 10px;
      border: 1px solid oklch(88% 0.05 80); border-radius: var(--radius-sm);
      color: oklch(40% 0.08 70); background: var(--warn-soft);
      font-size: var(--text-xs); text-align: center;
    }
    .kpi-grid {
      display: grid; grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 0; margin: 11px 0 0; border-block: 1px solid var(--border-soft);
    }
    .kpi { padding: 14px 14px 12px; border-right: 1px solid var(--border-soft); min-width: 0; transition: background var(--dur) var(--ease); }
    .kpi:hover { background: oklch(99% 0.004 250 / 0.7); }
    .kpi:last-child { border-right: 0; }
    .kpi-label {
      display: flex; align-items: center; gap: 5px; flex-wrap: wrap;
      font-size: var(--text-xs); color: var(--muted); font-weight: 550; margin-bottom: 6px;
    }
    .kpi-label .tag {
      font-size: 9.5px; font-weight: 700; padding: 1px 5px; border-radius: 999px;
      border: 1px solid var(--border); color: var(--muted);
    }
    .kpi-label .tag.verified { background: var(--success-soft); border-color: oklch(86% 0.05 150); color: oklch(34% 0.08 150); }
    .kpi-label .tag.est { background: var(--info-soft); border-color: oklch(88% 0.03 240); color: oklch(38% 0.06 240); }
    .kpi-value {
      font-family: var(--font-mono); font-variant-numeric: tabular-nums;
      font-size: clamp(15px, 1.35vw, 20px); font-weight: 650; letter-spacing: -0.03em;
      line-height: 1.15; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
      color: var(--fg);
    }
    .kpi-value.pos, .kpi-value.pnl-positive { color: var(--success); }
    .kpi-value.neg, .kpi-value.pnl-negative { color: var(--danger); }
    .kpi-value.neutral, .kpi-value.pnl-neutral { color: var(--fg); }
    .kpi-foot { margin-top: 5px; font-size: 10.5px; color: var(--muted-2); }
    .recon-bar {
      display: flex; align-items: center; justify-content: space-between; gap: 10px;
      padding: 8px 14px 8px 18px; border-top: 1px solid var(--border-soft);
      background: oklch(98.5% 0.004 250 / 0.7); font-size: var(--text-xs); color: var(--muted);
      flex-wrap: wrap;
    }
    .recon-bar.is-stale { background: var(--warn-soft); color: oklch(40% 0.08 70); }
    .recon-bar.is-error { background: var(--danger-soft); color: oklch(42% 0.12 20); }
    .finance-panel .pnl-ledger-grid { padding: 12px 16px 16px 18px; gap: 18px; }
    .pnl-ledger-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; }
    .pnl-ledger { min-width: 0; }
    .pnl-ledger > header { min-height: 50px; margin-bottom: 8px; }
    .pnl-ledger h3 { margin: 0 0 3px; color: var(--fg); font-size: var(--text-sm); font-weight: 650; }
    .pnl-ledger header p { margin: 0; color: var(--muted); font-size: var(--text-xs); }
    .pnl-ledger-list { border-top: 1px solid var(--border-soft); }
    .pnl-ledger-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px 14px; padding: 10px 2px; border-bottom: 1px solid var(--border-soft); }
    .pnl-ledger-primary { min-width: 0; }
    .pnl-ledger-primary a { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--text-sm); font-weight: 650; color: var(--accent); }
    .pnl-ledger-primary span { display: block; margin-top: 2px; color: var(--muted); font-size: 10.5px; }
    .pnl-ledger-result { align-self: center; font-family: var(--font-mono); font-variant-numeric: tabular-nums; font-size: var(--text-sm); color: var(--fg); }
    .pnl-positive { color: var(--success) !important; }
    .pnl-negative { color: var(--danger) !important; }
    .pnl-neutral { color: var(--muted) !important; }
    .pnl-ledger-meta { grid-column: 1 / -1; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; }
    .pnl-ledger-meta span { color: var(--muted); font-size: 10.5px; }
    .pnl-ledger-meta b { color: var(--fg); font-family: var(--font-mono); font-variant-numeric: tabular-nums; font-weight: 600; }
    .pnl-empty { margin: 12px 0; color: var(--muted); font-size: var(--text-xs); }

/* ===== Positions + funnel mid grid (V7 magazine streams) ===== */
    .positions-funnel-grid {
      display: grid; grid-template-columns: 1.35fr 1fr;
      gap: var(--space-3); margin-bottom: var(--space-3); align-items: start;
    }
    .positions-funnel-grid > .window { margin-bottom: 0; height: 100%; }
    .row-detail { max-width: 360px; }
    .row-detail summary {
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
      cursor: pointer; color: var(--muted); font-size: var(--text-xs);
    }
    .row-detail[open] summary { white-space: normal; }
    .row-detail p { margin: 7px 0 0; color: var(--fg); font-size: var(--text-xs); }

    /* ===== Decisions / runs ===== */
    .decisions-panel { margin-top: var(--space-3); }
    .run-table td:first-child { min-width: 132px; }
    .run-table .run-result { min-width: 240px; max-width: 480px; }
    .run-detail summary { max-width: 420px; }
    .decision-feed { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .decision-card {
      overflow: hidden; min-width: 0; padding: 0;
      border: 1px solid var(--border-soft); border-radius: var(--radius);
      background: oklch(100% 0 0 / 0.6); position: relative;
    }
    .decision-card.status-ok::before, .decision-card.status-warn::before, .decision-card.status-bad::before {
      content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
    }
    .decision-card.status-ok::before { background: var(--success); }
    .decision-card.status-warn::before { background: var(--warn); }
    .decision-card.status-bad::before { background: var(--danger); }
    .decision-summary {
      position: relative; display: grid;
      grid-template-columns: auto minmax(110px, 1fr) minmax(90px, .7fr) 70px minmax(145px, auto);
      align-items: center; gap: 10px; padding: 11px 12px 11px 15px; cursor: pointer; list-style: none;
    }
    .decision-summary::marker { content: ""; }
    .decision-summary::-webkit-details-marker { display: none; }
    .decision-summary::after { position: absolute; right: 11px; content: "+"; color: var(--muted); font-weight: 700; }
    .decision-card[open] .decision-summary::after { content: "\2212"; }
    .decision-summary > span { min-width: 0; }
    .decision-summary small { display: block; color: var(--muted); font-size: 10.5px; }
    .decision-summary strong { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--text-sm); color: var(--fg); }
    .decision-summary time { color: var(--muted); font-size: 10.5px; text-align: right; }
    .decision-detail { padding: 0 12px 12px 15px; border-top: 1px solid var(--border-soft); }
    .decision-reason { margin: 0; color: var(--fg); font-size: var(--text-sm); }
    .decision-llm { margin-top: 11px; padding-top: 11px; border-top: 1px dashed var(--border); }
    .llm-tag { display: inline-block; margin-right: 8px; padding: 2px 8px; border-radius: var(--radius-xs); background: var(--accent-soft); color: oklch(38% 0.06 230); font-size: var(--text-xs); font-weight: 650; }
    .llm-conf { color: var(--muted); font-size: var(--text-xs); }
    .decision-llm p { margin: 8px 0 0; color: var(--fg); font-size: var(--text-sm); }
    .analysis-panel { margin: 11px 0; padding: 11px; border-radius: var(--radius-sm); background: oklch(98.5% 0.004 250 / 0.6); border: 1px solid var(--border-soft); }
    .analysis-panel h3 { margin: 0 0 9px; font-size: var(--text-xs); color: var(--muted); font-weight: 650; text-transform: uppercase; letter-spacing: 0.06em; }
    .analysis-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; }
    .analysis-reasons { grid-column: 1 / -1; }
    .analysis-reasons ul { margin: 6px 0 0; padding-left: 18px; color: var(--fg); font-size: var(--text-xs); }
    .meta-label { display: block; color: var(--muted); font-size: 10.5px; margin-bottom: 2px; }
    .action-tag { display: inline-block; padding: 2px 8px; border-radius: var(--radius-xs); background: var(--info-soft); color: oklch(34% 0.06 240); font-weight: 650; font-size: var(--text-sm); }

    /* ===== Status pills ===== */
    .status-pill {
      display: inline-block; padding: 3px 8px; border-radius: 999px;
      font-size: var(--text-xs); font-weight: 650; letter-spacing: 0.02em;
    }
    .status-pill.status-ok { background: var(--success-soft); color: oklch(34% 0.08 150); }
    .status-pill.status-warn { background: var(--warn-soft); color: oklch(40% 0.08 70); }
    .status-pill.status-bad { background: var(--danger-soft); color: oklch(42% 0.12 20); }
    .status-pill.status-neutral { background: oklch(96% 0.006 250); color: var(--muted); }
    .status-pill.status-info { background: var(--info-soft); color: oklch(38% 0.06 240); }

    /* ===== Advanced / remote ===== */
    .advanced-links { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 9px; }
    .advanced-link {
      display: flex; align-items: center; min-height: 36px; padding: 7px 10px;
      border-radius: var(--radius-sm); border: 1px solid var(--border-soft);
      background: oklch(100% 0 0 / 0.6); color: var(--accent);
      font-size: var(--text-sm); font-weight: 600;
    }
    .advanced-link:hover { background: var(--accent-soft); text-decoration: none; }
    .code-block {
      margin: 0; padding: 12px; border-radius: var(--radius-sm); overflow-x: auto;
      background: oklch(20% 0.02 250); border: 1px solid var(--border);
      color: oklch(88% 0.02 230); font-family: var(--font-mono); font-size: var(--text-xs);
    }

    /* ===== Ranked opportunities table ===== */
    .ranked-opportunities { margin-bottom: var(--space-3); }
    .table-scroll { overflow-x: auto; }
    .data-table { width: 100%; border-collapse: collapse; font-size: var(--text-xs); }
    .data-table th {
      padding: 7px 8px; border-bottom: 1px solid var(--border);
      color: var(--muted); font-size: 10px; font-weight: 650;
      text-align: left; text-transform: uppercase; white-space: nowrap;
    }
    .data-table td { padding: 8px; border-bottom: 1px solid var(--border-soft); vertical-align: middle; }
    .data-table tbody tr:last-child td { border-bottom: 0; }
    .data-table tbody tr:hover td { background: oklch(98.5% 0.004 250 / 0.6); }
    .data-table .num { text-align: right; font-variant-numeric: tabular-nums; }
    .ranked-table { min-width: 940px; table-layout: fixed; width: 100%; border-collapse: collapse; font-size: var(--text-sm); }
    .ranked-table th { text-align: left; font-size: 10.5px; font-weight: 650; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; padding: 8px 10px; border-bottom: 1px solid var(--border); white-space: nowrap; }
    .ranked-table td { padding: 9px 10px; border-bottom: 1px solid var(--border-soft); vertical-align: middle; max-width: 220px; }
    .ranked-table tr:hover td { background: oklch(98.5% 0.004 250 / 0.6); }
    .ranked-table th:first-child, .ranked-table td:first-child { width: 30%; }
    .ranked-table th:last-child, .ranked-table td:last-child { width: 28%; }
    .ranked-table small { display: block; margin-top: 4px; color: var(--muted); }
    .ranked-table .mono { text-align: right; }

    /* ===== Empty / flash ===== */
    .empty-state {
      padding: 26px; text-align: center; color: var(--muted); font-size: var(--text-sm);
      border: 1px dashed var(--border); border-radius: var(--radius);
      background: oklch(100% 0 0 / 0.4);
    }
    .flash {
      padding: 11px 14px; border-radius: var(--radius); margin-bottom: var(--space-3);
      background: var(--success-soft); border: 1px solid oklch(86% 0.05 150);
      color: oklch(34% 0.08 150); font-size: var(--text-sm);
    }

    /* ===== Alert region ===== */
    .alert-region { display: grid; gap: 8px; margin-bottom: var(--space-3); }
    .alert-strip {
      display: flex; align-items: flex-start; gap: 10px; flex-wrap: wrap;
      padding: 10px 12px; border-radius: var(--radius);
      border: 1px solid var(--border); font-size: var(--text-sm); font-weight: 550;
    }
    .alert-strip.warn { background: var(--warn-soft); border-color: oklch(88% 0.05 80); color: oklch(40% 0.08 70); }
    .alert-strip.danger { background: var(--danger-soft); border-color: oklch(88% 0.05 20); color: oklch(42% 0.12 20); }
    .alert-strip strong { font-weight: 700; white-space: nowrap; }
    .alert-strip .grow { flex: 1 1 200px; min-width: 0; }
    .alert-list { margin: 0; padding-left: 18px; }

    /* ===== Interaction states (acceptance: running/paused/stale/blocked) ===== */
    body[data-run-state="blocked"] #safety {
      border-color: oklch(86% 0.05 20);
      box-shadow: 0 0 0 1px oklch(86% 0.05 20 / 0.5), var(--shadow-sm);
    }
    body[data-run-state="stale"] .finance-panel {
      box-shadow: 0 0 0 1px oklch(88% 0.05 80 / 0.5), var(--shadow-sm);
    }
    body[data-run-state="blocked"] .finance-panel {
      box-shadow: 0 0 0 1px oklch(86% 0.05 20 / 0.5), var(--shadow-sm);
    }

    /* ===== Responsive ===== */
    @media (max-width: 980px) {
      .hero, .checks-safety-grid, .positions-funnel-grid, .stream-layout { grid-template-columns: 1fr; }
      .stream-viz { border-right: 0; border-bottom: 1px solid var(--border-soft); }
      .command-toolbar { align-items: flex-start; }
      .decision-feed, .pnl-ledger-grid { grid-template-columns: 1fr; }
      .stats-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .mode-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .stream-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .sstat:nth-child(2n) { border-right: 0; }
      .sstat:nth-child(n+3) { border-top: 1px solid var(--border-soft); }
      .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .kpi:nth-child(2) { border-right: 0; }
      .kpi:nth-child(3), .kpi:nth-child(4) { border-top: 1px solid var(--border-soft); }
      .first-run-panel.is-compact .check-list { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 640px) {
      header.app-header { align-items: flex-start; flex-direction: column; padding: 9px 14px; gap: 8px; }
      .top-metrics { width: 100%; }
      .header-cluster { justify-content: flex-start; width: 100%; }
      main { padding: 16px 14px 38px; }
      .hero-copy { min-height: auto; padding: 20px; }
      h1 { font-size: 30px; }
      .path-rail { width: 100%; border-radius: var(--radius); }
      .command-actions, .stats-grid, .mode-grid { grid-template-columns: 1fr; }
      .command-toolbar { flex-direction: column; align-items: stretch; }
      .command-toolbar-state { flex-wrap: wrap; }
      .command-toolbar-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .command-toolbar-actions .btn { width: 100%; }
      .first-run-panel.is-compact .check-list,
      .safety-panel.is-compact .safety-grid { grid-template-columns: 1fr; }
      .command-actions form:first-child { grid-column: auto; }
      .finance-panel .pnl-heading { flex-direction: column; align-items: flex-start; }
      .kpi-grid { grid-template-columns: 1fr 1fr; }
      .stream-foot { grid-template-columns: 1fr; }
      .sfoot { border-right: 0; border-bottom: 1px solid var(--border-soft); }
      .sfoot:last-child { border-bottom: 0; }
      .feed-item { grid-template-columns: 48px 56px minmax(0, 1fr); }
      .feed-item .edge { grid-column: 2 / -1; }
      .pnl-summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .pnl-summary-item:nth-child(3) { border-left: 0; border-top: 1px solid var(--border-soft); }
      .pnl-summary-item:nth-child(4) { border-top: 1px solid var(--border-soft); }
      .decision-summary { grid-template-columns: auto minmax(100px, 1fr) minmax(72px, .6fr) 56px; }
      .decision-summary time { grid-column: 2 / -1; text-align: left; }
      .exit-position-head { flex-direction: column; }
      .exit-action { align-items: flex-start; text-align: left; }
      .exit-reason { grid-template-columns: 1fr; }
    }

    @media (prefers-reduced-motion: reduce) {
      html { scroll-behavior: auto; }
      *, *::before, *::after {
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important;
      }
      .live-chip .pulse, .status-chip .pulse, .pill-live .live-dot, .spark-pulse {
        animation: none !important; box-shadow: none;
      }
      .btn:hover, .mode-card:hover, .kpi:hover { transform: none; }
    }
"""


def render_app(
    repository: Repository,
    settings: Settings,
    lang: str,
    current_path: str,
) -> str:
    labels = _labels(lang)
    # Capture stream high-water BEFORE snapshot/cockpit reads so a concurrent
    # writer cannot advance the cursor past an event the SSR feed never showed.
    stream_cursors = repository.stream_cursor_high_water()
    snapshot = AutopilotService(settings, repository).snapshot()
    cockpit = build_cockpit_snapshot(repository)
    running = snapshot.enabled and not snapshot.blockers
    status_class = "ok" if running else "warn"
    stale_tick = _stale_last_tick(snapshot)
    run_state = _run_state(snapshot, stale_tick)
    setup_complete = _setup_complete(snapshot)

    onboarding = (
        _compact_command_toolbar(snapshot, labels, lang, status_class)
        if setup_complete
        else "".join(
            [
                _path_rail(labels),
                '<section class="hero" data-od-id="row-command">',
                '<div class="window window--aux hero-copy">',
                '<p class="eyebrow">Polymarket Weather</p>',
                f"<h1>{labels['title']}</h1>",
                f'<p class="lede">{labels["subtitle"]}</p>',
                "</div>",
                _hero_command(snapshot, labels, lang, status_class),
                "</section>",
                _mode_panel(snapshot, labels, lang),
            ]
        )
    )

    secondary_ops = "".join(
        [
            _stats_grid(snapshot, labels),
            _ranked_opportunities_panel(repository, labels),
            _advanced_panel(labels, lang),
            _remote_card(settings, labels),
            _decisions_panel(repository, snapshot, labels),
        ]
    )
    if setup_complete:
        health_block = _ops_health_rail(snapshot, settings, labels, repository)
        secondary_block = "".join(
            [
                '<details class="more-ops-disclosure" data-od-id="more-ops">',
                f"<summary>{_e(labels.get('more_ops_title', '更多运维面板'))}"
                f'<span class="muted" style="font-weight:500;margin-left:8px">'
                f"{_e(labels.get('more_ops_hint', '统计 · 机会排序 · 远程 · 决策日志'))}"
                f"</span></summary>",
                f'<div class="more-ops-body">{secondary_ops}</div>',
                "</details>",
            ]
        )
    else:
        health_block = "".join(
            [
                '<section class="checks-safety-grid" data-od-id="row-checks-safety">',
                _first_run_panel(snapshot, labels, compact=False),
                _safety_gate(settings, labels, snapshot, repository),
                "</section>",
            ]
        )
        secondary_block = secondary_ops

    body = "".join(
        [
            _render_flash(_query(current_path), lang),
            onboarding,
            _alert_region(snapshot, stale_tick, labels),
            health_block,
            _verified_pnl_panel(cockpit.realized_pnl, labels),
            render_stream_monitor_panel(
                snapshot,
                cockpit.opportunity_funnel,
                cockpit.realized_pnl,
                labels,
                repository=repository,
                cursors=stream_cursors,
            ),
            '<section class="row-mid positions-funnel-grid" '
            'data-od-id="row-position-funnel-streams" data-od-row="row-positions-funnel">',
            _exit_policy_panel(repository, labels),
            _opportunity_funnel_panel(cockpit.opportunity_funnel, labels),
            "</section>",
            _setup_controls_disclosure(snapshot, labels, lang, status_class)
            if setup_complete
            else "",
            secondary_block,
        ]
    )
    return _app_shell(
        labels["title"],
        body,
        lang,
        current_path,
        snapshot,
        run_state=run_state,
        stale_tick=stale_tick,
        setup_complete=setup_complete,
    )


def _setup_complete(snapshot) -> bool:
    """Infer that first-run setup has already produced an operating console."""
    checks = list(getattr(snapshot, "first_run_checks", ()) or ())
    all_checks_ok = bool(checks) and all(bool(check.ok) for check in checks)
    return bool(
        all_checks_ok
        or int(getattr(snapshot, "tick_count", 0) or 0) > 0
        or getattr(snapshot, "app_mode", "paper") != "paper"
    )


def _ops_health_rail(
    snapshot,
    settings: Settings,
    labels: dict[str, str],
    repository: Repository,
) -> str:
    """V6 compact stream-style strip for checks + safety (honest tick cadence).

    Full dual cards remain available behind an expand disclosure so nothing is lost.
    """
    summary = {"ok": 0, "warn": 0, "bad": 0}
    for check in list(getattr(snapshot, "first_run_checks", ()) or ()):
        if check.ok:
            summary["ok"] += 1
        elif getattr(check, "status", "") in {
            "blocked",
            "missing",
            "adapter-error",
            "adapter-pending",
        }:
            summary["bad"] += 1
        else:
            summary["warn"] += 1

    tick_seconds = int(getattr(snapshot, "tick_seconds", 0) or 0)
    if tick_seconds >= 60 and tick_seconds % 60 == 0:
        cadence_text = f"{tick_seconds // 60}m"
    elif tick_seconds:
        cadence_text = f"{tick_seconds}s"
    else:
        cadence_text = "—"

    order_cap = min(settings.max_order_usdc, HARDCODED_MAX_ORDER_USDC)
    daily_cap = min(settings.max_daily_usdc, HARDCODED_MAX_DAILY_USDC)
    min_edge = settings.min_edge
    auto_exit = (
        labels.get("safety_auto_exit_armed", "ARMED")
        if getattr(snapshot, "auto_exit_armed", False)
        else (
            labels.get("safety_auto_exit_env", "ENV")
            if settings.auto_exit_enabled
            else labels.get("safety_auto_exit_off", "OFF")
        )
    )

    stream_status = "disabled"
    stream_tokens = 0
    rest_fallback = True
    try:
        state = repository.get_autopilot_state()
        if state is not None:
            state_map = dict(state)
            stream_status = str(state_map.get("exchange_stream_status") or "disabled")
            detail_raw = state_map.get("exchange_stream_detail")
            if isinstance(detail_raw, str) and detail_raw.strip():
                import json

                detail = json.loads(detail_raw)
                if isinstance(detail, dict):
                    stream_tokens = int(detail.get("subscribed_token_count") or 0)
                    rest_fallback = bool(
                        detail.get("rest_fallback_active", stream_status != "live")
                    )
    except Exception:
        stream_status = "disabled"
    stream_label = {
        "live": "Live",
        "degraded": "Degraded",
        "stale": "Stale",
        "connecting": "Connecting",
        "disabled": "Disabled",
    }.get(stream_status.lower(), stream_status)
    rest_label = "REST on" if rest_fallback else "REST off"
    weather_usage = open_meteo_usage_snapshot()
    weather_units = int(weather_usage["estimated_units"])
    weather_requests = int(weather_usage["network_requests"])
    weather_429 = int(weather_usage["responses_429"])
    weather_cache_hits = int(weather_usage["cache_hits"])
    weather_cooldown_skips = int(weather_usage["cooldown_skips"])
    weather_usage_class = (
        "summary-bad" if weather_units >= 9000 else "summary-warn" if weather_units >= 8000 else ""
    )

    return "".join(
        [
            '<section class="ops-health-rail" data-od-id="row-checks-safety" '
            'aria-label="health and safety">',
            '<div class="ops-health-main">',
            '<div class="ops-health-left">',
            f'<span class="ops-health-label">{_e(labels.get("ops_rail_title", "运行健康"))}</span>',
            f'<span class="ops-cadence" title="{_e(labels.get("ops_cadence_hint", "按巡航间隔采样，不是逐笔行情"))}">'
            f"{_e(labels.get('ops_cadence_label', '采样节奏'))} "
            f'<span class="mono">{_e(cadence_text)}</span></span>',
            f'<span class="summary-pill summary-ok">{_e(labels.get("checks_ok", "通过"))} {summary["ok"]}</span>',
            f'<span class="summary-pill summary-warn">{_e(labels.get("checks_warn", "关注"))} {summary["warn"]}</span>',
            f'<span class="summary-pill summary-bad">{_e(labels.get("checks_bad", "闸断"))} {summary["bad"]}</span>',
            f'<span class="ops-chip" title="Local /app/stream is SQLite-only; Exchange WS is separate">'
            f"Local SQLite · Exchange WS <strong>{_e(stream_label)}</strong> · "
            f"{_e(str(stream_tokens))} assets · {_e(rest_label)}</span>",
            f'<span class="ops-chip {_e(weather_usage_class)}" data-open-meteo-usage '
            f'title="UTC day · network requests={weather_requests} · cache hits={weather_cache_hits} · '
            f'local cooldown skips={weather_cooldown_skips}">'
            f"Open-Meteo <strong>{weather_units}/10k</strong> · 429 {weather_429}</span>",
            "</div>",
            '<div class="ops-health-right">',
            f'<span class="ops-chip">{_e(labels["safety_order"])} <strong>{_e(str(order_cap))}</strong></span>',
            f'<span class="ops-chip">{_e(labels["safety_daily"])} <strong>{_e(str(daily_cap))}</strong></span>',
            f'<span class="ops-chip">{_e(labels.get("safety_min_edge", "Min edge"))} '
            f"<strong>{_e(str(min_edge))}</strong></span>",
            f'<span class="ops-chip">AUTO EXIT <strong>{_e(str(auto_exit))}</strong></span>',
            "</div>",
            "</div>",
            '<details class="ops-health-details">',
            f"<summary>{_e(labels.get('ops_expand_checks', '展开启动检查与安全闸门明细'))}</summary>",
            '<div class="ops-health-expand">',
            _first_run_panel(snapshot, labels, compact=True),
            _safety_gate(settings, labels, snapshot, repository),
            "</div>",
            "</details>",
            "</section>",
        ]
    )


def _run_state(snapshot, stale_tick: bool) -> str:
    if snapshot.blockers:
        return "blocked"
    if not snapshot.enabled:
        return "paused"
    if stale_tick:
        return "stale"
    return "running"


def _alert_region(snapshot, stale_tick: bool, labels: dict[str, str]) -> str:
    parts: list[str] = []
    if stale_tick:
        parts.append(
            '<div class="alert-strip warn" data-od-id="alert-warn" role="status" '
            'aria-live="polite" data-stale-tick="1">'
            f"<strong>{_e(labels.get('stale_tick_title', 'Stale last tick'))}</strong>"
            f'<span class="grow">{_e(labels.get("stale_tick_body", ""))}</span>'
            "</div>"
        )
    if snapshot.blockers:
        items = "".join(f"<li>{_e(item)}</li>" for item in snapshot.blockers)
        parts.append(
            '<div class="alert-strip danger" data-od-id="alert-danger" role="alert" '
            'aria-live="assertive">'
            f"<strong>{_e(labels['blockers'])}</strong>"
            f'<ul class="grow alert-list">{items}</ul>'
            "</div>"
        )
    if not parts:
        return ""
    return f'<div class="alert-region" data-od-id="alert-region">{"".join(parts)}</div>'


def _path_rail(labels: dict[str, str]) -> str:
    return "".join(
        [
            '<nav class="path-rail" aria-label="Autopilot path">',
            f'<span class="path-label">{_e(labels["path_label"])}</span>',
            f'<a class="path-step is-active" href="#command"><span class="path-number">1</span>{_e(labels["path_command"])}</a>',
            f'<a class="path-step" href="#checks"><span class="path-number">2</span>{_e(labels["path_checks"])}</a>',
            f'<a class="path-step" href="#safety"><span class="path-number">3</span>{_e(labels["path_safety"])}</a>',
            "</nav>",
        ]
    )


def _status_chip(snapshot, labels: dict[str, str], status_class: str) -> str:
    pulse = '<span class="pulse"></span>' if snapshot.enabled else ""
    state = labels["running"] if snapshot.enabled else labels["stopped"]
    return f'<div class="status-chip status-{status_class}">{pulse}<span>{_e(state)}</span></div>'


def _hero_command(snapshot, labels: dict[str, str], lang: str, status_class: str) -> str:
    toggle_label = labels["pause"] if snapshot.enabled else labels["start"]
    toggle_value = "0" if snapshot.enabled else "1"
    toggle_class = "btn btn-danger" if snapshot.enabled else "btn btn-primary"
    mode = labels.get(f"mode_{snapshot.app_mode}", labels["mode_paper"])
    return "".join(
        [
            '<aside id="command" class="window window--primary command-card" data-od-id="panel-command">',
            '<div class="command-top">',
            f'<span class="step-chip">1 {labels["path_command"]}</span>',
            _status_chip(snapshot, labels, status_class),
            "</div>",
            f"<h2>{_e(mode)}</h2>",
            f"<p>{_e(labels['command_hint'])}</p>",
            '<div class="command-actions">',
            f'<form method="post" action="{_href("/app/toggle", lang)}">',
            _hidden_lang(lang),
            f'<input type="hidden" name="enabled" value="{toggle_value}">',
            f'<button type="submit" class="{toggle_class}" data-od-id="btn-toggle" '
            f'aria-label="{_e(toggle_label)}">{toggle_label}</button>',
            "</form>",
            f'<form method="post" action="{_href("/app/tick", lang)}">',
            _hidden_lang(lang),
            f'<button type="submit" class="btn btn-secondary" data-od-id="btn-tick" '
            f'aria-label="{_e(labels["run_once"])}">{labels["run_once"]}</button>',
            "</form>",
            f'<form method="post" action="{_href("/app/reset-history", lang)}" ',
            f"onsubmit=\"return confirm('{labels['reset_confirm'].replace(chr(39), '')}')\">",
            _hidden_lang(lang),
            f'<button type="submit" class="btn btn-ghost" data-od-id="btn-clear-runs">'
            f"{labels['reset_history']}</button>",
            "</form>",
            "</div>",
            f'<p class="refresh-note">{labels["auto_refresh"]}</p>',
            "</aside>",
        ]
    )


def _compact_command_toolbar(
    snapshot,
    labels: dict[str, str],
    lang: str,
    status_class: str,
) -> str:
    toggle_label = labels["pause"] if snapshot.enabled else labels["start"]
    toggle_value = "0" if snapshot.enabled else "1"
    toggle_class = "btn btn-danger" if snapshot.enabled else "btn btn-primary"
    mode = labels.get(f"mode_{snapshot.app_mode}", labels["mode_paper"])
    return "".join(
        [
            '<section id="command" class="command-toolbar" data-od-id="toolbar-command">',
            '<div class="command-toolbar-state">',
            _status_chip(snapshot, labels, status_class),
            f'<span class="toolbar-mode"><span>{_e(labels["mode"])}</span><strong>{_e(mode)}</strong></span>',
            f'<span class="toolbar-tick"><span>{_e(labels["ticks"])}</span><strong class="mono">{snapshot.tick_count}</strong></span>',
            "</div>",
            '<div class="command-toolbar-actions">',
            f'<form method="post" action="{_href("/app/toggle", lang)}">',
            _hidden_lang(lang),
            f'<input type="hidden" name="enabled" value="{toggle_value}">',
            f'<button type="submit" class="{toggle_class}" data-od-id="btn-toggle">{_e(toggle_label)}</button>',
            "</form>",
            f'<form method="post" action="{_href("/app/tick", lang)}">',
            _hidden_lang(lang),
            f'<button type="submit" class="btn btn-secondary" data-od-id="btn-tick">{_e(labels["run_once"])}</button>',
            "</form>",
            "</div>",
            "</section>",
        ]
    )


def _setup_controls_disclosure(
    snapshot,
    labels: dict[str, str],
    lang: str,
    status_class: str,
) -> str:
    mode = labels.get(f"mode_{snapshot.app_mode}", labels["mode_paper"])
    return "".join(
        [
            '<details id="setup-controls" class="setup-disclosure">',
            '<summary class="window window--aux">',
            f"<span><strong>{_e(labels['setup_controls'])}</strong><small>{_e(mode)}</small></span>",
            f'<span class="status-pill status-neutral">{_e(labels["expand"])}</span>',
            "</summary>",
            '<div class="setup-expanded">',
            '<section class="hero" data-od-id="row-command-collapsed">',
            '<div class="window window--aux hero-copy">',
            '<p class="eyebrow">Polymarket Weather</p>',
            f"<h1>{labels['title']}</h1>",
            f'<p class="lede">{labels["subtitle"]}</p>',
            "</div>",
            _hero_command(snapshot, labels, lang, status_class),
            "</section>",
            _mode_panel(snapshot, labels, lang),
            "</div>",
            "</details>",
        ]
    )


def _stats_grid(snapshot, labels: dict[str, str]) -> str:
    mode = labels.get(f"mode_{snapshot.app_mode}", labels["mode_paper"])
    mode_tone = "live" if snapshot.app_mode in {"micro_live", "full_live"} else "dry"
    cards = [
        (labels["mode"], mode, f"badge badge-{mode_tone}", False),
        (labels["ticks"], str(snapshot.tick_count), "mono", False),
        (
            labels["last_tick"],
            _format_app_time(snapshot.last_tick_at, labels["never"]),
            "mono",
            True,
        ),
        (
            labels["last_status"],
            snapshot.last_tick_status or "-",
            f"status-pill status-{_status_tone(snapshot.last_tick_status)}",
            False,
        ),
        (
            labels.get("process_started_at", "Started"),
            _format_app_time(snapshot.process_started_at, labels["never"])
            if snapshot.process_started_at
            else "-",
            "mono",
            True,
        ),
        (
            labels.get("latest_useful", "Last Useful"),
            _format_app_time(snapshot.latest_useful_tick_at, labels["never"])
            if snapshot.latest_useful_tick_at
            else "-",
            "mono",
            True,
        ),
        (
            labels.get("tick_duration", "Duration"),
            f"{snapshot.last_tick_duration_ms}ms"
            if snapshot.last_tick_duration_ms is not None
            else "-",
            "mono",
            True,
        ),
        (
            labels.get("deferred_candidates", "Deferred"),
            str(snapshot.deferred_candidates_count)
            if snapshot.deferred_candidates_count is not None
            else "0",
            "mono",
            True,
        ),
        (labels["llm"], snapshot.llm_status, "mono", False),
    ]
    items = []
    for title, value, value_class, raw_value in cards:
        rendered_value = str(value) if raw_value else _e(str(value))
        items.append(
            f'<article class="stat-card"><span class="stat-label">{_e(title)}</span>'
            f'<span class="stat-value {value_class}">{rendered_value}</span></article>'
        )
    return f'<section class="stats-grid">{"".join(items)}</section>'


def _format_app_time(raw: str | None, empty_label: str) -> str:
    if not raw:
        return _e(empty_label)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return _e(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    utc_time = parsed.astimezone(timezone.utc)
    utc_text = f"UTC: {utc_time:%Y-%m-%d %H:%M:%S}"
    raw_attr = _e(raw)
    return (
        f'<span class="time-stack" data-local-time-target="{raw_attr}" data-utc="{raw_attr}">'
        '<span class="local-time-value">Local time loading...</span>'
        f"<small>{_e(utc_text)}</small>"
        "</span>"
    )


def _mode_panel(snapshot, labels: dict[str, str], lang: str) -> str:
    modes = [
        (
            "observe",
            labels["mode_observe"],
            labels["mode_observe_hint"],
            labels["risk_observe"],
            False,
        ),
        ("paper", labels["mode_paper"], labels["mode_paper_hint"], labels["risk_paper"], False),
        (
            "micro_live",
            labels["mode_micro_live"],
            labels["mode_micro_live_hint"],
            labels["risk_micro_live"],
            False,
        ),
        (
            "full_live",
            labels["mode_full_live"],
            labels["mode_full_live_hint"],
            labels["risk_full_live"],
            False,
        ),
    ]
    cards = []
    for value, title, hint, risk, locked in modes:
        selected = snapshot.app_mode == value
        state_class = "selected" if selected else "locked" if locked else ""
        badge = labels["selected"] if selected else labels["locked"] if locked else ""
        button = ""
        if not selected and not locked:
            button = "".join(
                [
                    f'<form method="post" action="{_href("/app/mode", lang)}">',
                    _hidden_lang(lang),
                    f'<input type="hidden" name="app_mode" value="{_e(value)}">',
                    f'<button type="submit" class="btn btn-ghost">{labels["choose_mode"]}</button>',
                    "</form>",
                ]
            )
        cards.append(
            "".join(
                [
                    f'<article class="mode-card mode-{_e(value)} {state_class}" data-mode="{_e(value)}">',
                    '<div class="mode-media" aria-hidden="true"></div>',
                    '<div class="mode-top">',
                    f"<h3>{_e(title)}</h3>",
                    f"<span>{_e(badge)}</span>" if badge else "",
                    "</div>",
                    f"<p>{_e(hint)}</p>",
                    f'<span class="mode-risk">{_e(risk)}</span>',
                    button,
                    "</article>",
                ]
            )
        )
    return "".join(
        [
            '<section id="modes" class="window window--path mode-panel">',
            f'<div class="panel-head"><span class="step-chip">{_e(labels["modes_step"])}</span><h2>{labels["mode_title"]}</h2>',
            f'<p class="panel-sub">{labels["mode_hint"]}</p></div>',
            f'<div class="mode-grid">{"".join(cards)}</div>',
            "</section>",
        ]
    )


def _first_run_panel(
    snapshot,
    labels: dict[str, str],
    *,
    compact: bool = False,
) -> str:
    rows = []
    summary = {"ok": 0, "warn": 0, "bad": 0}
    for check in snapshot.first_run_checks:
        tone = "ok" if check.ok else "bad" if check.status in {"blocked", "missing"} else "warn"
        summary[tone] += 1
        rows.append(
            "".join(
                [
                    f'<li class="check-row check-{tone}" title="{_e(check.detail)}">',
                    f'<span class="check-dot"></span><strong>{_e(labels.get(f"check_{check.name}", check.name))}</strong>',
                    f'<span class="status-pill status-{tone}">{_e(check.status)}</span>',
                    f"<p>{_e(check.detail)}</p>",
                    "</li>",
                ]
            )
        )
    return "".join(
        [
            f'<section id="checks" class="window window--path first-run-panel{" is-compact" if compact else ""}" '
            'data-od-id="panel-checks" aria-labelledby="checks-title">',
            f'<div class="panel-head"><span class="step-chip">{_e(labels["checks_step"])}</span>'
            f'<h2 id="checks-title">{labels["first_run"]}</h2>',
            f'<p class="panel-sub">{labels["first_run_hint"]}</p></div>',
            '<div class="check-summary">',
            f'<span class="summary-pill summary-ok">{_e(labels["checks_ok"])} {summary["ok"]}</span>',
            f'<span class="summary-pill summary-warn">{_e(labels["checks_warn"])} {summary["warn"]}</span>',
            f'<span class="summary-pill summary-bad">{_e(labels["checks_bad"])} {summary["bad"]}</span>',
            "</div>",
            f'<ul class="check-list" data-od-id="check-list" role="list">{"".join(rows)}</ul>',
            "</section>",
        ]
    )


def _advanced_panel(labels: dict[str, str], lang: str) -> str:
    links = [
        ("/beginner-legacy", labels["advanced_beginner"]),
        ("/live", labels["advanced_live"]),
        ("/calibration", labels["advanced_calibration"]),
        ("/actions", labels["advanced_actions"]),
        ("/overrides", labels["advanced_overrides"]),
    ]
    items = "".join(
        f'<a class="advanced-link" href="{_href(path, lang)}">{_e(label)}</a>'
        for path, label in links
    )
    return "".join(
        [
            '<section class="window window--aux advanced-panel">',
            f'<div class="panel-head"><h2>{labels["advanced"]}</h2>',
            f'<p class="panel-sub">{labels["advanced_hint"]}</p></div>',
            f'<div class="advanced-links">{items}</div>',
            "</section>",
        ]
    )


def _decisions_panel(repository: Repository, snapshot, labels: dict[str, str]) -> str:
    decisions = list(snapshot.decisions)
    if not decisions:
        content = f'<div class="empty-state">{labels["no_decisions"]}</div>'
    else:
        visible = decisions[:6]
        remainder = decisions[6:]
        content = _decision_table(repository, visible, labels)
        if remainder:
            content += (
                '<details class="more-disclosure" data-list="recent-runs-more">'
                f"<summary>{_e(labels['show_more'].format(count=len(remainder)))}</summary>"
                + _decision_table(repository, remainder, labels, include_header=False)
                + "</details>"
            )
    return "".join(
        [
            '<section id="recent-runs" class="window window--aux decisions-panel panel--aux" '
            'data-od-id="panel-runs">',
            f'<div class="panel-head"><h2>{labels["decisions"]}</h2>'
            f'<span class="status-pill status-neutral panel-count">{len(decisions)}</span>',
            f'<p class="panel-sub">{labels["decisions_hint"]}</p></div>',
            content,
            "</section>",
        ]
    )


def _decision_table(
    repository: Repository,
    rows: list[dict[str, object]],
    labels: dict[str, str],
    *,
    include_header: bool = True,
) -> str:
    header = ""
    if include_header:
        header = (
            "<thead><tr>"
            f"<th>{_e(labels['time'])}</th><th>{_e(labels['market'])}</th>"
            f"<th>{_e(labels['action'])}</th><th class='num'>{_e(labels['edge'])}</th>"
            f"<th>{_e(labels['outcome'])}</th></tr></thead>"
        )
    body = "".join(_decision_table_row(repository, row, labels) for row in rows)
    return (
        '<div class="table-scroll"><table class="data-table run-table">'
        f"{header}<tbody data-list='recent-runs-primary'>{body}</tbody></table></div>"
    )


def _decision_table_row(
    repository: Repository,
    row: dict[str, object],
    labels: dict[str, str],
) -> str:
    status = str(row.get("status") or "unknown")
    action = str(row.get("action") or "-")
    edge = row.get("edge")
    edge_text = "-"
    if edge is not None:
        try:
            edge_text = f"{float(edge):.2f}"
        except (TypeError, ValueError):
            edge_text = str(edge)
    llm_provider = row.get("llm_provider")
    llm_confidence = row.get("llm_confidence")
    llm_reason = row.get("llm_reason")
    reason = row.get("reason") or "-"
    llm_block = ""
    if llm_provider:
        conf = (
            f"{llm_confidence:.0%}"
            if isinstance(llm_confidence, (int, float))
            else str(llm_confidence)
        )
        llm_block = (
            f'<div class="decision-llm"><span class="llm-tag">{_e(str(llm_provider))}</span>'
            f'<span class="llm-conf">{_e(conf)}</span>'
            f"<p>{_e(str(llm_reason or '-'))}</p></div>"
        )
    analysis_block = _analysis_block(repository, row.get("market_id"), labels)
    created_at = str(row.get("created_at") or "-")
    compact_time = created_at.replace("T", " ")[:19]
    return "".join(
        [
            "<tr>",
            f'<td class="mono" title="{_e(created_at)}">{_e(compact_time)}</td>',
            f'<td><a class="mono" href="/markets/{_e(str(row.get("market_id") or ""))}">{_e(str(row.get("market_id") or "-"))}</a></td>',
            f'<td><span class="status-pill status-{_status_tone(status)}">{_e(status)}</span><small>{_e(action)}</small></td>',
            f'<td class="num mono">{_e(edge_text)}</td>',
            '<td class="run-result"><details class="row-detail run-detail">',
            f"<summary>{_e(str(reason))}</summary>",
            analysis_block,
            llm_block,
            "</details></td></tr>",
        ]
    )


def _analysis_block(repository: Repository, market_id: object, labels: dict[str, str]) -> str:
    if not market_id:
        return ""
    analysis = repository.latest_analysis(str(market_id))
    forecast = repository.latest_forecast(str(market_id))
    if analysis is None and forecast is None:
        return ""
    rows: list[str] = []
    if analysis is not None:
        fair = (
            f"{_format_number(analysis['fair_lower'])} – {_format_number(analysis['fair_upper'])}"
        )
        rows.append(
            f'<div><span class="meta-label">{labels["fair_range"]}</span>'
            f'<span class="mono">{_e(fair)}</span></div>'
        )
        rows.append(
            f'<div><span class="meta-label">{labels["quant_side"]}</span>'
            f'<span class="action-tag">{_e(str(analysis["side"] or "-"))}</span></div>'
        )
        rows.append(
            f'<div><span class="meta-label">{labels["quant_decision"]}</span>'
            f'<span class="mono">{_e(str(analysis["decision"]))}</span></div>'
        )
        try:
            reasons = json.loads(analysis["reasons"])
        except (TypeError, json.JSONDecodeError):
            reasons = []
        if reasons:
            reason_items = "".join(f"<li>{_e(str(item))}</li>" for item in reasons)
            rows.append(
                f'<div class="analysis-reasons"><span class="meta-label">{labels["quant_reasons"]}</span>'
                f"<ul>{reason_items}</ul></div>"
            )
    if forecast is not None:
        forecast_text = f"{forecast['value']}{forecast['unit'] or ''} ({forecast['provider']})"
        rows.append(
            f'<div><span class="meta-label">{labels["forecast"]}</span>'
            f'<span class="mono">{_e(forecast_text)}</span></div>'
        )
    return f'<div class="analysis-panel"><h3>{labels["analysis"]}</h3><div class="analysis-grid">{"".join(rows)}</div></div>'


def _remote_card(settings: Settings, labels: dict[str, str]) -> str:
    plan = build_deploy_plan(settings)
    if not plan.tunnel_command:
        return ""
    return "".join(
        [
            '<section class="window window--aux remote-panel">',
            f"<h2>{labels['remote_title']}</h2>",
            f'<p class="panel-sub">{labels["remote_body"]}</p>',
            f'<pre class="code-block">{_e(plan.tunnel_command)}</pre>',
            f'<p>{labels["remote_open"]}: <a href="{_e(plan.local_app_url or "")}">',
            f"{_e(plan.local_app_url or '')}</a></p>",
            "</section>",
        ]
    )


def _stale_last_tick(snapshot) -> bool:
    """True when autopilot is enabled but last tick is older than 2 tick intervals."""
    if snapshot is None or not getattr(snapshot, "enabled", False):
        return False
    last_tick_at = getattr(snapshot, "last_tick_at", None)
    if not last_tick_at:
        return True
    try:
        parsed = datetime.fromisoformat(str(last_tick_at).replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    tick_seconds = max(int(getattr(snapshot, "tick_seconds", 300) or 300), 30)
    age = (datetime.now(timezone.utc) - parsed).total_seconds()
    return age > (2 * tick_seconds)


def _safety_gate(
    settings: Settings,
    labels: dict[str, str],
    snapshot=None,
    repository: Repository | None = None,
) -> str:
    caps = [
        (labels["safety_order"], min(settings.max_order_usdc, HARDCODED_MAX_ORDER_USDC)),
        (labels["safety_daily"], min(settings.max_daily_usdc, HARDCODED_MAX_DAILY_USDC)),
        (labels["safety_market"], min(settings.max_market_usdc, HARDCODED_MAX_MARKET_USDC)),
        (labels.get("safety_min_edge", "Min edge"), settings.min_edge),
    ]
    auto_exit_label = labels.get("safety_auto_exit_off", "OFF")
    if snapshot is not None and getattr(snapshot, "auto_exit_armed", False):
        auto_exit_label = labels.get("safety_auto_exit_armed", "ARMED (live mode)")
    elif settings.auto_exit_enabled:
        auto_exit_label = labels.get(
            "safety_auto_exit_env", "ENV on (need micro_live/full_live + Start)"
        )
    whitelist_label = (
        labels.get("safety_whitelist_open", "OPEN (no LIVE_MARKET_IDS)")
        if (snapshot is None or getattr(snapshot, "live_whitelist_open", True))
        else labels.get("safety_whitelist_restricted", "restricted")
    )
    if (
        snapshot is not None
        and not getattr(snapshot, "live_whitelist_open", True)
        and settings.live_market_ids
    ):
        whitelist_label = f"{whitelist_label}: {settings.live_market_ids}"

    recon_label = labels.get("safety_recon_missing", "missing")
    if snapshot is not None:
        recon_status = str(getattr(snapshot, "reconciliation_status", "missing") or "missing")
        recon_detail = str(getattr(snapshot, "reconciliation_detail", "") or "")
        recon_label = labels.get(
            f"capital_{recon_status.replace('-', '_')}",
            recon_status,
        ) + (f": {recon_detail}" if recon_detail else "")
    elif repository is not None:
        latest = repository.latest_successful_reconciliation()
        if latest is not None:
            try:
                created = datetime.fromisoformat(str(latest["created_at"]).replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age_s = int((datetime.now(timezone.utc) - created).total_seconds())
                recon_label = labels.get("safety_recon_age", "age {age}s").format(age=age_s)
            except (ValueError, KeyError, TypeError):
                recon_label = labels.get("safety_recon_unknown", "present")

    open_orders = 0
    positions = 0
    if repository is not None:
        try:
            open_orders = len(repository.list_open_orders(limit=1000))
        except Exception:
            open_orders = 0
        try:
            positions = len(repository.list_positions(limit=1000, nonzero_only=True))
        except Exception:
            positions = 0

    last_error = getattr(snapshot, "last_error", None) if snapshot is not None else None
    recovery = ""
    if last_error:
        recovery = (
            f"<div><span>{_e(labels.get('safety_last_failure', 'Last failure'))}</span>"
            f"<strong>{_e(str(last_error))}</strong></div>"
            f"<div><span>{_e(labels.get('safety_recovery', 'Recovery'))}</span>"
            f"<strong>{_e(labels.get('safety_recovery_action', 'Pause, fix blockers, reconcile, then Start'))}</strong></div>"
        )

    min_edge_label = labels.get("safety_min_edge", "Min edge")
    cap_rows = "".join(
        f"<div><span>{_e(label)}</span><strong>"
        f"{_e(str(value) if label == min_edge_label else f'{value} USDC')}"
        f"</strong></div>"
        for label, value in caps
    )
    status_rows = "".join(
        [
            f"<div><span>{_e(labels.get('safety_auto_exit', 'AUTO EXIT'))}</span>"
            f"<strong>{_e(auto_exit_label)}</strong></div>",
            f"<div><span>{_e(labels.get('safety_whitelist', 'Whitelist'))}</span>"
            f"<strong>{_e(whitelist_label)}</strong></div>",
            f"<div><span>{_e(labels.get('safety_recon', 'Reconciliation'))}</span>"
            f"<strong>{_e(recon_label)}</strong></div>",
            f"<div><span>{_e(labels.get('safety_open_orders', 'Open orders'))}</span>"
            f"<strong>{open_orders}</strong></div>",
            f"<div><span>{_e(labels.get('safety_positions', 'Positions'))}</span>"
            f"<strong>{positions}</strong></div>",
            recovery,
        ]
    )
    return "".join(
        [
            f'<section id="safety" class="window window--gate safety-panel{" is-compact" if snapshot is not None and _setup_complete(snapshot) else ""}" '
            'data-od-id="panel-safety" aria-labelledby="safety-title">',
            f'<div class="safety-banner"><span class="step-chip">{_e(labels["safety_step"])}</span>'
            f"<span>{_e(labels['safety_banner'])}</span></div>",
            f'<h2 id="safety-title">{_e(labels["safety_title"])}</h2>',
            f'<p class="panel-sub">{_e(labels["safety_hint"])}</p>',
            f'<div class="safety-grid" data-od-id="valves-list">{cap_rows}{status_rows}</div>',
            f'<p class="safety-limit">{_e(labels["safety_limit_only"])}</p>',
            "</section>",
        ]
    )


def _status_tone(status: str | None) -> str:
    normalized = (status or "").lower()
    if normalized in {"executed", "ok", "observed"}:
        return "ok"
    if normalized in {"skipped", "idle", "blocked"}:
        return "warn"
    if normalized in {"failed", "rejected"}:
        return "bad"
    return "neutral"


def _app_shell(
    title: str,
    body: str,
    lang: str,
    current_path: str,
    snapshot,
    *,
    run_state: str = "paused",
    stale_tick: bool = False,
    setup_complete: bool = False,
) -> str:
    labels = _labels(lang)
    html_lang = "zh-CN" if lang == "zh" else "en"
    tick_seconds = snapshot.tick_seconds
    refresh_seconds = min(30, max(10, tick_seconds // 2 if tick_seconds else 15))
    mode = labels.get(f"mode_{snapshot.app_mode}", labels["mode_paper"])
    mode_tone = "live" if snapshot.app_mode in {"micro_live", "full_live"} else "safe"
    running_class = "is-running" if snapshot.enabled else "is-paused"
    if run_state == "stale":
        running_label = labels.get("stale", labels["running"])
        running_class = "is-stale"
    else:
        running_label = labels["running"] if snapshot.enabled else labels["stopped"]
    reconciliation_status = str(getattr(snapshot, "reconciliation_status", "missing") or "missing")
    capital_ok = bool(getattr(snapshot, "reconciliation_fresh", False))
    capital_class = "is-running" if capital_ok else "is-blocked"
    capital_label = (
        labels.get("capital_fresh", "Fresh")
        if capital_ok
        else labels.get(
            f"capital_{reconciliation_status.replace('-', '_')}",
            labels.get("capital_blocked", "Blocked"),
        )
    )
    capital_detail = str(getattr(snapshot, "reconciliation_detail", "") or "")
    last_tick_html = _format_app_time(snapshot.last_tick_at, labels["never"])
    language_switcher = "".join(
        [
            '<nav class="lang-nav" aria-label="Language">',
            f'<a class="lang-link {"active" if lang == "zh" else ""}" href="{_href(current_path, "zh")}">中文</a>',
            f'<a class="lang-link {"active" if lang == "en" else ""}" href="{_href(current_path, "en")}">EN</a>',
            "</nav>",
        ]
    )
    return f'''<!doctype html>
<html lang="{html_lang}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="data:,">
  <meta http-equiv="refresh" content="{refresh_seconds}">
  <title>{_e(title)}</title>
  <style>
{_APP_STYLE}
{_STREAM_STYLE}
  </style>
</head>
<body data-run-state="{run_state}" data-capital-status="{_e(reconciliation_status)}" data-stale-tick="{"1" if stale_tick else "0"}" data-setup-complete="{"1" if setup_complete else "0"}">
  <a class="skip-link" href="#main">{_e(labels.get("skip_to_main", "Skip to main content"))}</a>
  <header class="app-header" data-od-id="app-header" role="banner">
    <div class="brand" data-od-id="brand">
      <span class="brand-mark" aria-hidden="true">{brand_mark_html()}</span>
      <span class="brand-copy"><strong>Weather Autopilot</strong><small>{_e(labels["brand_sub"])}</small></span>
    </div>
    <div class="top-metrics" data-od-id="top-metrics" aria-label="{_e(labels.get("status", "Status"))}">
      <span class="metric-chip" data-od-id="chip-run">
        <span>{_e(labels.get("process_health", "Process"))}</span>
        <span class="live-chip {running_class}" role="status" aria-live="polite">
          <span class="pulse" aria-hidden="true"></span>{_e(running_label)}
        </span>
      </span>
      <span class="metric-chip" data-od-id="chip-capital" data-capital-status="{_e(reconciliation_status)}" title="{_e(capital_detail)}">
        <span>{_e(labels.get("capital_health", "Capital path"))}</span>
        <span class="live-chip {capital_class}" role="status" aria-live="polite">
          <span class="pulse" aria-hidden="true"></span>{_e(capital_label)}
        </span>
      </span>
      <span class="metric-chip" data-od-id="chip-mode">
        <span>{_e(labels["mode"])}</span>
        <strong class="header-mode-chip mode-{mode_tone}" style="border:0;background:transparent;padding:0;min-height:auto">{_e(mode)}</strong>
      </span>
      <span class="metric-chip" data-od-id="chip-tick">
        <span>{_e(labels["last_tick"])}</span>
        <strong class="mono">{last_tick_html}</strong>
      </span>
      <span class="metric-chip" data-od-id="chip-ticks">
        <span>{_e(labels["ticks"])}</span>
        <strong>{snapshot.tick_count}</strong>
      </span>
    </div>
    <div class="header-cluster" data-od-id="header-actions">
      {language_switcher}
    </div>
  </header>
  <main id="main" data-od-id="main" tabindex="-1">{body}</main>
  <script>
    function padTimePart(value) {{
      return String(value).padStart(2, '0');
    }}
    function gmtLabel(date) {{
      const offsetMinutes = -date.getTimezoneOffset();
      const sign = offsetMinutes >= 0 ? '+' : '-';
      const absolute = Math.abs(offsetMinutes);
      const hours = Math.floor(absolute / 60);
      const minutes = absolute % 60;
      return minutes ? `GMT${{sign}}${{hours}}:${{padTimePart(minutes)}}` : `GMT${{sign}}${{hours}}`;
    }}
    function localTimeLabel(date) {{
      return [
        date.getFullYear(),
        '-',
        padTimePart(date.getMonth() + 1),
        '-',
        padTimePart(date.getDate()),
        ' ',
        padTimePart(date.getHours()),
        ':',
        padTimePart(date.getMinutes()),
        ':',
        padTimePart(date.getSeconds()),
        ' ',
        gmtLabel(date)
      ].join('');
    }}
    for (const element of document.querySelectorAll('[data-local-time-target]')) {{
      const date = new Date(element.dataset.localTimeTarget);
      if (Number.isNaN(date.getTime())) continue;
      const target = element.querySelector('.local-time-value');
      if (target) target.textContent = localTimeLabel(date);
    }}
  </script>
</body>
</html>'''


def _labels(lang: str) -> dict[str, str]:
    if lang == "zh":
        return {
            "title": "天气自动交易",
            "subtitle": "一个入口完成扫描、分析、模拟交易和微额实盘。高级工具仍保留在下方。",
            "brand_sub": "Polymarket 天气交易 · 主控台 V7",
            "ops_rail_title": "运行健康",
            "ops_cadence_label": "采样节奏",
            "ops_cadence_hint": "按巡航间隔批量采样，不是逐笔行情 tape",
            "deferred_candidates": "轮转/预算等待（非错误）",
            "ops_expand_checks": "展开启动检查与安全闸门明细",
            "more_ops_title": "更多运维面板",
            "more_ops_hint": "统计 · 机会排序 · 远程 · 决策日志",
            "stream_title": "本地决策流监控",
            "stream_sub": "本地 SQLite 增量 · 非交易所 tape · 约 2 秒刷新",
            "stream_live": "local live",
            "stream_conn_live": "local live",
            "stream_conn_reconnecting": "reconnecting",
            "stream_conn_stale": "stale",
            "stream_tick": "tick #{count}",
            "stream_stat_events": "最近事件",
            "stream_stat_cycle": "更新节奏",
            "stream_cadence_format": "本地 {poll}s / 策略 {strategy}s",
            "stream_stat_disc": "漏斗发现",
            "stream_stat_fill": "成交 / 拒绝",
            "stream_feed_title": "事件流水",
            "stream_feed_meta": "{count} 条 · 只读",
            "stream_feed_empty": "暂无事件。启动自动交易或立即执行一轮后，这里会显示真实决策流。",
            "stream_action_entry_minimum_blocked": "最低订单",
            "stream_legend_edge": "决策净优势（selected）",
            "stream_legend_disc": "发现量",
            "stream_legend_pass": "优势达标",
            "stream_legend_fill": "成交",
            "stream_axis_old": "漏斗上游",
            "stream_axis_mid": "风控",
            "stream_axis_now": "成交",
            "stream_axis_time_old": "更早",
            "stream_axis_time_now": "现在",
            "stream_chart_edge": "决策净优势随时间",
            "stream_chart_empty": "尚无带 edge 的决策点（不混入 analyses）",
            "stream_funnel_cat": "机会漏斗（分类）",
            "stream_foot_last": "最近事件",
            "stream_foot_waiting": "等待写入…",
            "stream_foot_source": "数据源",
            "stream_foot_source_val": "local SQLite · 非交易所行情",
            "stream_foot_recon": "对账对齐",
            "stream_foot_recon_fresh": "Fresh",
            "stream_foot_recon_stale": "Stale",
            "pnl_ledgers_expand": "展开持仓台账与已实现明细",
            "path_label": "主路径",
            "path_command": "主控指令",
            "path_checks": "启动检查",
            "path_safety": "安全闸",
            "status": "运行状态",
            "process_health": "进程",
            "capital_health": "资金通道",
            "capital_fresh": "对账新鲜",
            "capital_adapter_error": "对账失败",
            "capital_adapter_pending": "对账未完成",
            "capital_stale": "对账过期",
            "capital_missing": "尚未对账",
            "capital_blocked": "已阻断",
            "command_hint": "选择模式后启动自动巡航，或先手动跑一轮确认链路。",
            "running": "运行中",
            "stopped": "已暂停",
            "blocked": "已阻断",
            "stale": "运行过期",
            "skip_to_main": "跳到主内容",
            "start": "启动自动交易",
            "pause": "暂停",
            "run_once": "立即执行一轮",
            "reset_history": "清空历史记录",
            "reset_confirm": "确定清空所有决策历史并重置轮次计数？",
            "auto_refresh": "事件流每 2 秒轮询本地库；整页仍约 10–30 秒 meta 刷新（兼容，非纯流式）。",
            "setup_controls": "新手设置与交易模式",
            "expand": "展开",
            "show_more": "展开其余 {count} 条",
            "mode": "模式",
            "mode_dry_run": "模拟",
            "mode_live": "实盘",
            "mode_title": "交易模式",
            "mode_hint": "默认使用模拟交易。正式实盘使用配置的风险上限，并自动管理开仓与平仓。",
            "modes_step": "模式身份",
            "mode_observe": "观察模式",
            "mode_observe_hint": "只扫描和分析机会，不生成订单记录，适合第一次打开看系统是否正常。",
            "mode_paper": "模拟交易",
            "mode_paper_hint": "自动扫描、分析并记录 dry-run 订单，不会下真实单。推荐默认模式。",
            "mode_micro_live": "微额实盘",
            "mode_micro_live_hint": "使用 micro-live，仍必须通过白名单、override、对账、合规和风险上限。",
            "mode_full_live": "正式实盘",
            "mode_full_live_hint": "使用配置的实盘上限与最低 edge，自动管理进场与出场（需 Start 启动）。",
            "risk_observe": "只读 · 无订单",
            "risk_paper": "推荐 · 模拟记录",
            "risk_micro_live": "有资金 · 多重门禁",
            "risk_full_live": "配置上限 · 自动进出",
            "choose_mode": "选择",
            "selected": "当前",
            "locked": "锁定",
            "first_run": "启动检查",
            "first_run_hint": "这些检查不主动下单；它们帮助你判断当前环境能跑到哪一步。",
            "checks_step": "2 预检",
            "checks_ok": "通过",
            "checks_warn": "需关注",
            "checks_bad": "阻断",
            "check_database": "数据库",
            "check_weather": "天气源",
            "check_polymarket_reads": "Polymarket 读取",
            "check_compliance": "合规位置",
            "check_reconciliation": "交易所对账",
            "check_trading_disabled": "实盘开关",
            "check_live_credentials": "实盘凭证",
            "check_full_live": "正式实盘就绪",
            "check_resolution_circuit_breaker": "结算熔断",
            "ticks": "轮次",
            "last_tick": "上次运行",
            "last_status": "上次结果",
            "never": "从未",
            "blockers": "当前阻断",
            "decisions": "最近运行记录（只读）",
            "decisions_hint": "这里只展示每轮 tick 的结果，不参与策略计算，也不会改变买卖决策；LLM 仅提供复核意见。",
            "no_decisions": "暂无决策。点击「立即执行一轮」或等待自动 tick。",
            "time": "时间",
            "market": "市场",
            "action": "动作",
            "edge": "Edge",
            "reason": "原因",
            "outcome": "本轮结果",
            "analysis": "量化分析",
            "fair_range": "公允区间",
            "quant_side": "量化方向",
            "quant_decision": "量化决策",
            "quant_reasons": "量化依据",
            "forecast": "天气预报",
            "llm": "LLM 复核（仅供参考）",
            "remote_title": "远程监控",
            "remote_body": "在本地电脑运行 SSH 隧道，即可查看香港 VPS 上的 Autopilot。",
            "remote_open": "隧道建立后打开",
            "advanced": "高级模式",
            "advanced_hint": "原来的谨慎工具没有删除，需要精细审查时从这里进入。",
            "advanced_beginner": "旧 Beginner",
            "advanced_live": "Live Launchpad",
            "advanced_calibration": "Calibration",
            "advanced_actions": "Action Queue",
            "advanced_overrides": "Overrides",
            "safety_step": "3 闸门",
            "safety_banner": "主路径终点 · 所有实盘动作必须经过这里",
            "safety_title": "安全闸门",
            "safety_hint": "显示当前配置与代码硬上限中更严格的一项；这里只读展示，不会修改规则。",
            "safety_order": "每单上限",
            "safety_daily": "每日上限",
            "safety_market": "单市场上限",
            "safety_limit_only": "仅限价单 · TRADING_DISABLED 与全局熔断器仍可随时阻断实盘",
            "safety_auto_exit": "AUTO EXIT",
            "safety_auto_exit_off": "关闭",
            "safety_auto_exit_env": "ENV 已开（需 micro_live/full_live + 启动）",
            "safety_auto_exit_armed": "已武装（实盘模式运行中）",
            "safety_whitelist": "市场白名单",
            "safety_whitelist_open": "全开（未配置 LIVE_MARKET_IDS）",
            "safety_whitelist_restricted": "受限列表",
            "safety_min_edge": "最低 edge",
            "safety_recon": "资金/对账状态",
            "safety_recon_missing": "缺失",
            "safety_recon_unknown": "已有",
            "safety_recon_age": "{age} 秒前",
            "safety_open_orders": "挂单数",
            "safety_positions": "持仓数",
            "safety_last_failure": "最近失败",
            "safety_recovery": "恢复动作",
            "safety_recovery_action": "暂停 → 修复阻断 → 对账 → 再启动",
            "stale_tick_title": "上次 tick 已过期",
            "stale_tick_body": "上次成功/失败周期超过两个 tick 间隔。进程存活不等于 Autopilot 存活。",
            "funnel_title": "机会漏斗流",
            "funnel_kicker": "Funnel stream · tick batch",
            "funnel_pill": "24h 窗口",
            "funnel_hint": "阶段递进 · 数字过渡即可",
            "funnel_stage": "阶段",
            "funnel_count": "市场数",
            "funnel_blockers": "Top Blockers（主要未通过原因）",
            "funnel_drop_baseline": "本轮基线",
            "funnel_drop_filtered": "−{pct}% 过滤",
            "funnel_cvr_label": "转化",
            "funnel_cvr_path": "发现→成交",
            "funnel_hot_label": "活跃阶段",
            "funnel_batch_note": "与事件流同源 · 批更新",
            "funnel_discovered": "已发现",
            "funnel_rule_tradable": "规则可交易",
            "funnel_quote_available": "有盘口报价",
            "funnel_forecast_available": "有天气预测",
            "funnel_analyzed": "已完成量化分析",
            "funnel_quant_trade_signal": "出现交易信号",
            "funnel_live_submitted": "已提交实盘订单",
            "funnel_exchange_fill": "交易所确认成交",
            "ranked_title": "天气机会排行",
            "ranked_hint": "按保守 Edge 排序，包含温度区间市场及其预测来源。",
            "ranked_empty": "市场已发现，正在等待预测与量化分析。",
            "ranked_forecast": "预测",
            "ranked_probability": "概率区间",
            "ranked_price": "市场价",
            "ranked_decision": "结论 / 原因",
            "exit_title": "持仓状态流",
            "exit_kicker": "Position stream",
            "exit_hint": "状态变化写入流水 · 非静态表",
            "exit_empty": "当前无持仓。",
            "exit_unrealized_note": "可执行价值是未实现估值，不是已实现利润。",
            "exit_open_pill": "{count} open",
            "exit_sum_qty": "合计数量",
            "exit_sum_mark": "未实现合计",
            "exit_sum_note_k": "口径",
            "exit_sum_note_v": "可执行−未回收",
            "exit_stage_hold": "Hold",
            "exit_stage_recover": "Recover",
            "exit_stage_settle": "Settlement",
            "exit_stage_exit": "Exit",
            "exit_stage_review": "Review",
            "exit_market": "市场",
            "exit_action": "动作",
            "exit_cost": "已验证成本",
            "exit_proceeds": "已回收净额",
            "exit_unrecovered": "未回收",
            "exit_size": "仓位",
            "exit_runner": "Runner",
            "exit_bid": "买一",
            "exit_exec": "可执行价值",
            "exit_max": "最大赔付",
            "exit_edge": "净 Edge",
            "exit_reason": "原因",
            "pnl_title": "成交盈亏与当前持仓估值",
            "pnl_hint": "成交盈亏按已关联的 BUY/SELL fills 计算；持仓估值按当前对账 currentValue 计算。两组数字口径有重叠，请勿相加。",
            "pnl_scope_warning": "这不是账户总盈亏，也不包含充值、提现或未记录的结算赎回。",
            "pnl_realized_title": "已卖出部分的成交盈亏（全部历史）",
            "pnl_sold_total": "已卖出成交净盈亏",
            "pnl_sold_hint": "仅统计已关联并经过对账的 BUY / SELL 成交。",
            "pnl_open_title": "当前持仓周期估值",
            "pnl_open_hint": "计算方式：已回收卖出款 + 当前仓位价值 - 本轮建仓成本。",
            "pnl_open_unverified": "另有 {count} 个持仓因成交链不完整而未计入。",
            "pnl_market": "市场",
            "pnl_matched": "匹配股数",
            "pnl_unmatched": "未匹配",
            "pnl_exposure": "风险敞口",
            "pnl_recon_fresh": "交易所数据: 新鲜（不代表总盈亏完整）",
            "pnl_recon_stale": "交易所数据: 过期（先重新对账）",
            "pnl_cost": "买入成本",
            "pnl_proceeds": "卖出收入",
            "pnl_fees": "手续费",
            "pnl_net": "净盈亏",
            "pnl_position": "当前仓位",
            "pnl_open_cost": "建仓成本",
            "pnl_open_recovered": "已回收",
            "pnl_current_value": "当前价值",
            "pnl_estimated": "周期估算盈亏",
            "pnl_empty": "暂无已关联的买卖成交记录",
        }
    return {
        "title": "Weather Autopilot",
        "subtitle": "Auto scan, quant execution, advisory LLM review, and position exits.",
        "brand_sub": "Polymarket weather · console V7",
        "ops_rail_title": "Health",
        "ops_cadence_label": "Sample cadence",
        "ops_cadence_hint": "Batch samples on cruise interval — not a trade tape",
        "deferred_candidates": "Rotation/budget backlog (not an error)",
        "ops_expand_checks": "Expand startup checks & safety gates",
        "more_ops_title": "More ops panels",
        "more_ops_hint": "Stats · ranked · remote · decisions",
        "stream_title": "Local decision stream",
        "stream_sub": "Local SQLite deltas · not exchange tape · ~2s poll",
        "stream_live": "local live",
        "stream_conn_live": "local live",
        "stream_conn_reconnecting": "reconnecting",
        "stream_conn_stale": "stale",
        "stream_tick": "tick #{count}",
        "stream_stat_events": "Recent events",
        "stream_stat_cycle": "Cadence",
        "stream_cadence_format": "{poll}s local / {strategy}s strategy",
        "stream_stat_disc": "Funnel discovered",
        "stream_stat_fill": "Fills / rejects",
        "stream_feed_title": "Event feed",
        "stream_feed_meta": "{count} rows · read-only",
        "stream_feed_empty": "No events yet. Start autopilot or run one tick to populate the live decision feed.",
        "stream_action_entry_minimum_blocked": "MIN ORDER",
        "stream_legend_edge": "Decision net edge (selected)",
        "stream_legend_disc": "Discovered",
        "stream_legend_pass": "Edge pass",
        "stream_legend_fill": "Fills",
        "stream_axis_old": "Upstream",
        "stream_axis_mid": "Risk",
        "stream_axis_now": "Fill",
        "stream_axis_time_old": "earlier",
        "stream_axis_time_now": "now",
        "stream_chart_edge": "Decision net edge over time",
        "stream_chart_empty": "No decision edge points yet (analyses not mixed in)",
        "stream_funnel_cat": "Opportunity funnel (categorical)",
        "stream_foot_last": "Latest event",
        "stream_foot_waiting": "Waiting…",
        "stream_foot_source": "Sources",
        "stream_foot_source_val": "local SQLite · not exchange tape",
        "stream_foot_recon": "Reconciliation",
        "stream_foot_recon_fresh": "Fresh",
        "stream_foot_recon_stale": "Stale",
        "pnl_ledgers_expand": "Show open & realized ledgers",
        "path_label": "Primary path",
        "path_command": "Command",
        "path_checks": "Startup checks",
        "path_safety": "Safety gate",
        "status": "Status",
        "process_health": "Process",
        "capital_health": "Capital path",
        "capital_fresh": "Reconciliation fresh",
        "capital_adapter_error": "Reconciliation failed",
        "capital_adapter_pending": "Reconciliation pending",
        "capital_stale": "Reconciliation stale",
        "capital_missing": "Not reconciled",
        "capital_blocked": "Blocked",
        "command_hint": "Start the selected mode, or run one controlled tick first.",
        "running": "Running",
        "stopped": "Paused",
        "blocked": "Blocked",
        "stale": "Stale",
        "skip_to_main": "Skip to main content",
        "start": "Start autopilot",
        "pause": "Pause",
        "run_once": "Run one tick now",
        "reset_history": "Clear history",
        "reset_confirm": "Clear all decision history and reset tick count?",
        "auto_refresh": "Event stream polls local DB every 2s; full page still meta-refreshes every 10–30s (compat, not pure streaming).",
        "setup_controls": "Beginner setup and trading modes",
        "expand": "Expand",
        "show_more": "Show {count} more",
        "mode": "Mode",
        "mode_dry_run": "Dry-run",
        "mode_live": "Live",
        "mode_title": "Trading Mode",
        "mode_hint": "Paper trading is the default. Full live uses configured risk limits and automatically manages entry and exit.",
        "modes_step": "Mode identity",
        "mode_observe": "Observe",
        "mode_observe_hint": "Scan and analyze only. No order intent is created.",
        "mode_paper": "Paper trading",
        "mode_paper_hint": "Auto scan, analyze, and record dry-run orders. Recommended default.",
        "mode_micro_live": "Micro live",
        "mode_micro_live_hint": "Uses micro-live and still requires whitelist, override, reconciliation, compliance, and risk caps.",
        "mode_full_live": "Full live",
        "mode_full_live_hint": "Uses configured live limits and min edge; automatically manages entry and exit (Start required).",
        "risk_observe": "Read-only · no orders",
        "risk_paper": "Recommended · simulated",
        "risk_micro_live": "Funds at risk · gated",
        "risk_full_live": "Configured caps · auto entry/exit",
        "choose_mode": "Choose",
        "selected": "Current",
        "locked": "Locked",
        "first_run": "Startup Checks",
        "first_run_hint": "These checks do not place orders; they show how far the current environment can safely go.",
        "checks_step": "2 Preflight",
        "checks_ok": "Passed",
        "checks_warn": "Review",
        "checks_bad": "Blocked",
        "check_database": "Database",
        "check_weather": "Weather source",
        "check_polymarket_reads": "Polymarket reads",
        "check_compliance": "Compliance location",
        "check_reconciliation": "Exchange reconciliation",
        "check_trading_disabled": "Live switch",
        "check_live_credentials": "Live credentials",
        "check_full_live": "Full live readiness",
        "check_resolution_circuit_breaker": "Resolution breaker",
        "ticks": "Ticks",
        "last_tick": "Last tick",
        "last_status": "Last status",
        "never": "Never",
        "blockers": "Blockers",
        "decisions": "Recent run log (read-only)",
        "decisions_hint": "Displays tick outcomes only. It does not change strategy or trade decisions; LLM review remains advisory.",
        "no_decisions": "No decisions yet. Run a tick or wait for the next cycle.",
        "time": "Time",
        "market": "Market",
        "action": "Action",
        "edge": "Edge",
        "reason": "Reason",
        "outcome": "Tick outcome",
        "analysis": "Quant analysis",
        "fair_range": "Fair range",
        "quant_side": "Quant side",
        "quant_decision": "Quant decision",
        "quant_reasons": "Quant reasons",
        "forecast": "Forecast",
        "llm": "LLM review (advisory)",
        "remote_title": "Remote monitoring",
        "remote_body": "Run this SSH tunnel on your laptop to view the HK VPS autopilot.",
        "remote_open": "Then open",
        "advanced": "Advanced Mode",
        "advanced_hint": "The original cautious tools remain available for detailed review.",
        "advanced_beginner": "Legacy Beginner",
        "advanced_live": "Live Launchpad",
        "advanced_calibration": "Calibration",
        "advanced_actions": "Action Queue",
        "advanced_overrides": "Overrides",
        "safety_step": "3 Gate",
        "safety_banner": "Primary path endpoint · every live action must pass here",
        "safety_title": "Safety Gate",
        "safety_hint": "Shows the stricter value from current settings and hardcoded caps. This panel is read-only.",
        "safety_order": "Per order",
        "safety_daily": "Per day",
        "safety_market": "Per market",
        "safety_limit_only": "Limit orders only · TRADING_DISABLED and the global circuit breaker can always block live execution",
        "funnel_title": "Opportunity funnel stream",
        "funnel_kicker": "Funnel stream · tick batch",
        "funnel_pill": "24h window",
        "funnel_hint": "Stage progression · number transitions only",
        "funnel_stage": "Stage",
        "funnel_count": "Count",
        "funnel_blockers": "Top Blockers",
        "funnel_drop_baseline": "Batch baseline",
        "funnel_drop_filtered": "−{pct}% filtered",
        "funnel_cvr_label": "Conversion",
        "funnel_cvr_path": "Discover → fill",
        "funnel_hot_label": "Active stage",
        "funnel_batch_note": "Same source as event feed · batch",
        "funnel_discovered": "Discovered",
        "funnel_rule_tradable": "Rule Tradable",
        "funnel_quote_available": "Quote Available",
        "funnel_forecast_available": "Forecast Available",
        "funnel_analyzed": "Analyzed",
        "funnel_quant_trade_signal": "Quant Trade Signal",
        "funnel_live_submitted": "Live Submitted",
        "funnel_exchange_fill": "Exchange Fill",
        "ranked_title": "Ranked Weather Opportunities",
        "ranked_hint": "Sorted by conservative edge, including temperature buckets and forecast provenance.",
        "ranked_empty": "Markets discovered; waiting for forecast and quant analysis.",
        "ranked_forecast": "Forecast",
        "ranked_probability": "Probability range",
        "ranked_price": "Market price",
        "ranked_decision": "Decision / reason",
        "exit_title": "Position status stream",
        "exit_kicker": "Position stream",
        "exit_hint": "State changes as a stream · not a static table",
        "exit_empty": "No open positions.",
        "exit_unrealized_note": "Executable value is unrealized; not realized profit.",
        "exit_open_pill": "{count} open",
        "exit_sum_qty": "Total size",
        "exit_sum_mark": "Unrealized total",
        "exit_sum_note_k": "Basis",
        "exit_sum_note_v": "Executable − unrecovered",
        "exit_stage_hold": "Hold",
        "exit_stage_recover": "Recover",
        "exit_stage_settle": "Settlement",
        "exit_stage_exit": "Exit",
        "exit_stage_review": "Review",
        "exit_market": "Market",
        "exit_action": "Action",
        "exit_cost": "Verified cost",
        "exit_proceeds": "Net proceeds",
        "exit_unrecovered": "Unrecovered",
        "exit_size": "Size",
        "exit_runner": "Runner",
        "exit_bid": "Bid",
        "exit_exec": "Executable",
        "exit_max": "Max payout",
        "exit_edge": "Net Edge",
        "exit_reason": "Reason",
        "pnl_title": "Fill PnL and Current Position Value",
        "pnl_hint": "Fill PnL uses linked BUY/SELL fills. Open estimates use reconciled currentValue. The two views overlap and must not be added together.",
        "pnl_scope_warning": "This is not total account PnL and excludes deposits, withdrawals, and unrecorded settlement redemptions.",
        "pnl_realized_title": "Sold-fill PnL (all time)",
        "pnl_sold_total": "Sold-fill net PnL",
        "pnl_sold_hint": "Only linked and reconciled BUY / SELL fills are included.",
        "pnl_open_title": "Current position campaign estimate",
        "pnl_open_hint": "Recovered sell proceeds + current position value - campaign buy cost.",
        "pnl_open_unverified": "{count} additional positions are excluded because their fill linkage is incomplete.",
        "pnl_empty": "No linked BUY/SELL fills yet.",
        "pnl_market": "Market",
        "pnl_matched": "Matched",
        "pnl_unmatched": "Unmatched",
        "pnl_exposure": "Exposure",
        "pnl_recon_fresh": "Exchange data: Fresh (not proof of complete PnL)",
        "pnl_recon_stale": "Exchange data: Stale (reconcile first)",
        "pnl_cost": "Cost",
        "pnl_proceeds": "Proceeds",
        "pnl_fees": "Fees",
        "pnl_net": "Net PnL",
        "pnl_position": "Position",
        "pnl_open_cost": "Buy cost",
        "pnl_open_recovered": "Recovered",
        "pnl_current_value": "Current value",
        "pnl_estimated": "Campaign estimate",
        "safety_auto_exit": "AUTO EXIT",
        "safety_auto_exit_off": "OFF",
        "safety_auto_exit_env": "ENV on (need micro_live/full_live + Start)",
        "safety_auto_exit_armed": "ARMED (live mode running)",
        "safety_whitelist": "Market whitelist",
        "safety_whitelist_open": "OPEN (no LIVE_MARKET_IDS)",
        "safety_whitelist_restricted": "restricted",
        "safety_min_edge": "Min edge",
        "safety_recon": "Capital / reconciliation status",
        "safety_recon_missing": "missing",
        "safety_recon_unknown": "present",
        "safety_recon_age": "{age}s ago",
        "safety_open_orders": "Open orders",
        "safety_positions": "Positions",
        "safety_last_failure": "Last failure",
        "safety_recovery": "Recovery",
        "safety_recovery_action": "Pause → fix blockers → reconcile → Start",
        "stale_tick_title": "Stale last tick",
        "stale_tick_body": "Last successful/failed cycle is older than two tick intervals. Process liveness is not Autopilot liveness.",
    }


def _format_number(value: object) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def _query(current_path: str) -> dict[str, list[str]]:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(current_path).query)


def _opportunity_funnel_panel(funnel: OpportunityFunnel, labels: dict[str, str]) -> str:
    """V7 magazine funnel stream — stage strips with filter % and thin track."""
    stages = [
        (labels["funnel_discovered"], int(funnel.discovered)),
        (labels["funnel_rule_tradable"], int(funnel.rule_tradable)),
        (labels["funnel_quote_available"], int(funnel.quote_available)),
        (labels["funnel_forecast_available"], int(funnel.forecast_available)),
        (labels["funnel_analyzed"], int(funnel.analyzed)),
        (labels["funnel_quant_trade_signal"], int(funnel.quant_trade_signal)),
        (labels["funnel_live_submitted"], int(funnel.live_submitted)),
        (labels["funnel_exchange_fill"], int(funnel.exchange_fill)),
    ]
    baseline = max(stages[0][1], 1)
    # Highlight the deepest non-zero stage (honest bottleneck narrative).
    hot_index = 0
    for index, (_name, count) in enumerate(stages):
        if count > 0:
            hot_index = index

    strips: list[str] = []
    for index, (name, count) in enumerate(stages):
        prev = stages[index - 1][1] if index > 0 else count
        if index == 0:
            drop_label = labels.get("funnel_drop_baseline", "本轮基线")
        elif prev > 0:
            drop_pct = round((1 - (count / prev)) * 100)
            drop_label = labels.get("funnel_drop_filtered", "−{pct}% 过滤").format(pct=drop_pct)
        else:
            drop_label = "—"
        width_pct = max(6.0, (count / baseline) * 100.0) if baseline else 6.0
        hot_cls = " is-hot" if index == hot_index else ""
        strips.append(
            f'<div class="funnel-strip{hot_cls}" role="listitem" data-stage="{index + 1}">'
            f'<span class="f-step">{index + 1:02d}</span>'
            '<div class="f-body">'
            '<div class="f-top">'
            f'<span class="f-lbl">{_e(name)}</span>'
            f'<span class="f-drop">{_e(drop_label)}</span>'
            "</div>"
            f'<div class="f-track"><div class="f-bar" style="width:{width_pct:.1f}%"></div></div>'
            "</div>"
            f'<span class="f-n">{count}</span>'
            "</div>"
        )

    fill_count = stages[-1][1]
    cvr = (fill_count / baseline) * 100.0 if baseline else 0.0
    hot_name = stages[hot_index][0]

    blockers_html = ""
    if funnel.blockers:
        blockers_list = "".join(
            f"<li>{_e(_funnel_blocker_label(b.reason, labels))}: {b.count}</li>"
            for b in funnel.blockers[:5]
        )
        blockers_html = (
            '<details class="funnel-blocker-disclosure" data-od-id="reject-box">'
            f"<summary>{_e(labels['funnel_blockers'])}</summary>"
            f"<ul>{blockers_list}</ul></details>"
        )

    return (
        '<section class="window window--aux mag-stream funnel-panel" '
        'id="panel-funnel" data-od-id="panel-funnel-stream" aria-labelledby="funnel-stream-title">'
        '<div class="panel-head">'
        '<div class="panel-title-wrap">'
        f'<span class="mag-kicker">{_e(labels.get("funnel_kicker", "Funnel stream · tick batch"))}</span>'
        f'<h2 class="panel-title" id="funnel-stream-title">{_e(labels["funnel_title"])}</h2>'
        "</div>"
        '<div class="panel-meta">'
        f'<span class="status-pill status-info">{_e(labels.get("funnel_pill", "24h window"))}</span>'
        f'<span class="panel-sub">{_e(labels["funnel_hint"])}</span>'
        "</div></div>"
        f'<div class="funnel-stream-list" role="list" aria-label="{_e(labels["funnel_title"])}" '
        f'data-od-id="funnel-steps">{"".join(strips)}</div>'
        '<div class="funnel-summary">'
        f"<span>{_e(labels.get('funnel_cvr_label', '转化'))} <strong>{cvr:.1f}%</strong></span>"
        f"<span>{_e(labels.get('funnel_cvr_path', '发现→成交'))}</span>"
        f"<span>{_e(labels.get('funnel_hot_label', '活跃阶段'))} <strong>{_e(hot_name)}</strong></span>"
        f"<span>{_e(labels.get('funnel_batch_note', '与事件流同源 · 批更新'))}</span>"
        f"{blockers_html}"
        "</div></section>"
    )


def _funnel_blocker_label(reason: str, labels: dict[str, str]) -> str:
    if labels.get("funnel_discovered") == "Discovered":
        return reason
    try:
        parsed = json.loads(reason)
    except (TypeError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, list):
        return "; ".join(_funnel_reason_explanation(str(item)) for item in parsed)
    translations = {
        "non-tradable rule": "规则不可交易",
        "missing quote": "缺少盘口报价",
        "missing forecast": "缺少天气预测",
        "missing analysis": "尚未完成量化分析",
        "quant skip/edge": "量化信号或 Edge 不足",
        "live gate": "未通过实盘执行条件",
        "missing fill": "订单尚未成交",
        "risk rejection": "风险额度拒绝",
        "automation rejected": "自动执行被拒绝",
    }
    explanation = translations.get(reason)
    return f"{reason}（{explanation}）" if explanation else reason


def _funnel_reason_explanation(reason: str) -> str:
    explanations = {
        "edge does not clear global bucket cost and safety buffer": "优势不足以覆盖交易成本和安全缓冲",
        "market price outside supported range": "市场价格超出策略支持范围",
        "forecast unavailable": "缺少可用天气预测",
    }
    explanation = explanations.get(reason)
    if explanation is None and reason.startswith("global temperature bucket model sigma="):
        explanation = "温度区间概率模型使用的波动参数"
    return f"{reason}（{explanation}）" if explanation else reason


def _ranked_opportunities_panel(repository: Repository, labels: dict[str, str]) -> str:
    opportunities = repository.list_ranked_weather_opportunities(limit=20)
    if not opportunities:
        content = f'<p class="muted">{_e(labels["ranked_empty"])}</p>'
    else:
        rows: list[str] = []
        for row in opportunities:
            forecast = "-"
            if row["forecast_value"] is not None:
                forecast = (
                    f"{row['forecast_value']}{row['forecast_unit'] or ''} "
                    f"({row['forecast_provider'] or 'unknown'})"
                )
            probability = "-"
            if row["fair_lower"] is not None and row["fair_upper"] is not None:
                probability = f"{float(row['fair_lower']):.1%}–{float(row['fair_upper']):.1%}"
            price = "-" if row["best_ask"] is None else f"{float(row['best_ask']):.3f}"
            edge = "-" if row["edge"] is None else f"{float(row['edge']):.3f}"
            reasons: list[str] = []
            try:
                reasons = [str(item) for item in json.loads(row["analysis_reasons"] or "[]")]
            except (TypeError, json.JSONDecodeError):
                pass
            reason = "; ".join(reasons) or str(
                row["rejection_reason"] or row["notes"] or "waiting for analysis"
            )
            decision = str(row["decision"] or "pending")
            reason_summary = reasons[-1] if reasons else reason
            rows.append(
                "<tr>"
                f'<td><a href="/markets/{_e(row["market_id"])}">{_e(row["title"])}</a>'
                f'<small class="mono">{_e(row["module_id"])}</small></td>'
                f'<td class="mono">{_e(forecast)}</td>'
                f'<td class="num mono">{_e(probability)}</td>'
                f'<td class="num mono">{_e(price)}</td>'
                f'<td class="num mono">{_e(edge)}</td>'
                f'<td><span class="status-pill status-{_status_tone(decision)}">{_e(decision)}</span>'
                '<details class="row-detail">'
                f"<summary>{_e(reason_summary)}</summary><p>{_e(reason)}</p>"
                "</details></td>"
                "</tr>"
            )
        visible = rows[:8]
        remainder = rows[8:]
        content = _ranked_opportunity_table(visible, labels)
        if remainder:
            content += (
                '<details class="more-disclosure" data-list="ranked-opportunities-more">'
                f"<summary>{_e(labels['show_more'].format(count=len(remainder)))}</summary>"
                + _ranked_opportunity_table(remainder, labels, include_header=False)
                + "</details>"
            )
    return (
        '<section class="window window--aux ranked-opportunities">'
        '<div class="panel-head">'
        f"<h2>{_e(labels['ranked_title'])}</h2>"
        f'<span class="status-pill status-neutral panel-count">{len(opportunities)}</span>'
        f'<p class="panel-sub">{_e(labels["ranked_hint"])}</p></div>'
        f"{content}</section>"
    )


def _ranked_opportunity_table(
    rows: list[str],
    labels: dict[str, str],
    *,
    include_header: bool = True,
) -> str:
    header = ""
    if include_header:
        header = (
            "<thead><tr>"
            f"<th>{_e(labels['market'])}</th>"
            f"<th>{_e(labels['ranked_forecast'])}</th>"
            f'<th class="num">{_e(labels["ranked_probability"])}</th>'
            f'<th class="num">{_e(labels["ranked_price"])}</th>'
            f'<th class="num">{_e(labels["edge"])}</th>'
            f"<th>{_e(labels['ranked_decision'])}</th>"
            "</tr></thead>"
        )
    return (
        '<div class="table-scroll"><table class="data-table ranked-table">'
        f"{header}<tbody data-list='ranked-opportunities-primary'>{''.join(rows)}</tbody>"
        "</table></div>"
    )


def _exit_stage_class(policy_stage: str | None, action: str) -> str:
    """Map exit guardian stage/action → V7 stage pill class."""
    key = f"{policy_stage or ''} {action or ''}".lower()
    if any(token in key for token in ("settlement", "near_settlement", "resolution")):
        return "stage-settle"
    if any(token in key for token in ("recover", "full_exit", "exit_full", "principal")):
        return "stage-recover"
    if any(token in key for token in ("hold", "runner")):
        return "stage-hold"
    if any(token in key for token in ("exit", "cancel", "review")):
        return "stage-exit"
    return "stage-neutral"


def _exit_stage_label(policy_stage: str | None, action: str, labels: dict[str, str]) -> str:
    stage = (policy_stage or action or "hold").strip()
    mapping = {
        "hold": labels.get("exit_stage_hold", "Hold"),
        "principal_recovery": labels.get("exit_stage_recover", "Recover"),
        "principal_recovery_blocked": labels.get("exit_stage_recover", "Recover"),
        "runner": labels.get("exit_stage_hold", "Hold"),
        "settlement": labels.get("exit_stage_settle", "Settlement"),
        "near_settlement": labels.get("exit_stage_settle", "Settlement"),
        "full_exit": labels.get("exit_stage_exit", "Exit"),
        "evidence": labels.get("exit_stage_review", "Review"),
        "unverified_accounting": labels.get("exit_stage_review", "Review"),
    }
    return mapping.get(stage, stage.replace("_", " ").title()[:14] or "Hold")


def _exit_mark_pnl(rec) -> Decimal | None:
    """Approximate unrealized mark from executable value vs unrecovered cash."""
    try:
        if rec.executable_value is not None and rec.unrecovered_cash is not None:
            return Decimal(str(rec.executable_value)) - Decimal(str(rec.unrecovered_cash))
        if (
            rec.best_bid is not None
            and rec.actual_position_size is not None
            and rec.verified_buy_cost is not None
        ):
            proceeds = Decimal(str(rec.verified_sell_proceeds or 0))
            remaining_cost = Decimal(str(rec.verified_buy_cost)) - proceeds
            return (
                Decimal(str(rec.best_bid)) * Decimal(str(rec.actual_position_size)) - remaining_cost
            )
    except Exception:
        return None
    return None


def _format_signed_money(value: Decimal | None) -> tuple[str, str]:
    """Return (display, css_class) for strip-edge."""
    if value is None:
        return "—", "muted"
    try:
        amount = Decimal(str(value))
    except Exception:
        return "—", "muted"
    if amount > 0:
        rendered = f"+{_format_exit_value(amount)}"
        return rendered, "pos"
    if amount < 0:
        rendered = f"-{_format_exit_value(abs(amount))}"
        return rendered, "neg"
    return "0", "muted"


def _avg_cost_per_share(rec) -> str:
    try:
        size = Decimal(str(rec.actual_position_size or 0))
        cost = Decimal(str(rec.verified_buy_cost or 0))
        if size > 0 and cost > 0:
            return _format_exit_value(cost / size)
    except Exception:
        pass
    return "—"


def _exit_policy_panel(repository: Repository, labels: dict[str, str]) -> str:
    """V7 position status stream (magazine strips; read-only exit ladder data).

    Uses persisted token market snapshots only — no network from /app.
    """
    from polymarket_weather_arb.domain.position_inventory import best_bid_depth_from_book
    from polymarket_weather_arb.services.exit_guardian_service import ExitGuardianService

    best_bids: dict[tuple[str, str], Decimal] = {}
    bid_depths: dict[tuple[str, str], Decimal] = {}
    try:
        for position in repository.list_positions(limit=1000, nonzero_only=True):
            market_id = str(position["market_id"])
            outcome = str(position["outcome"] or "").strip().upper()
            # Token-scoped bid for the held outcome only (never another side's book).
            snap = repository.latest_pricing_snapshot(market_id, outcome=outcome or "YES")
            if snap is None or snap["best_bid"] is None:
                continue
            bid = Decimal(str(snap["best_bid"]))
            best_bids[(market_id, outcome)] = bid
            raw = snap["raw_payload"] if "raw_payload" in snap.keys() else None
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except Exception:
                    raw = None
            depth = best_bid_depth_from_book(raw if isinstance(raw, dict) else None)
            if depth is not None:
                bid_depths[(market_id, outcome)] = depth
        recs = [
            r
            for r in ExitGuardianService(repository).evaluate(
                min_edge=Decimal("0.05"),
                best_bids=best_bids,
                bid_depths=bid_depths or None,
            )
            if r.kind == "position"
        ]
    except Exception:
        recs = []

    open_pill = labels.get("exit_open_pill", "{count} open").format(count=len(recs))
    head = (
        '<div class="panel-head">'
        '<div class="panel-title-wrap">'
        f'<span class="mag-kicker">{_e(labels.get("exit_kicker", "Position stream"))}</span>'
        f'<h2 class="panel-title" id="pos-stream-title">{_e(labels.get("exit_title", "持仓状态流"))}</h2>'
        "</div>"
        '<div class="panel-meta">'
        f'<span class="pill-live"><span class="live-dot" aria-hidden="true"></span>{_e(open_pill)}</span>'
        f'<span class="panel-sub">{_e(labels.get("exit_hint", ""))}</span>'
        "</div></div>"
    )

    if not recs:
        return (
            '<section id="exit-policy" class="window window--aux mag-stream exit-policy-panel" '
            'data-od-id="panel-position-stream" aria-labelledby="pos-stream-title">'
            f"{head}"
            f'<div class="empty-state">{_e(labels.get("exit_empty", "当前无持仓。"))}</div>'
            f'<p class="empty-hint">{_e(labels.get("exit_unrealized_note", ""))}</p>'
            "</section>"
        )

    strips: list[str] = []
    sum_qty = Decimal("0")
    sum_mark = Decimal("0")
    mark_count = 0
    for index, rec in enumerate(recs[:24], start=1):
        stage_cls = _exit_stage_class(rec.policy_stage, rec.action)
        stage_lbl = _exit_stage_label(rec.policy_stage, rec.action, labels)
        market_line = rec.market_id
        if rec.outcome:
            market_line = f"{rec.market_id} · {rec.outcome}"
        qty = _format_exit_value(rec.actual_position_size)
        cost_avg = _avg_cost_per_share(rec)
        mid = _format_exit_value(rec.best_bid)
        mark = _exit_mark_pnl(rec)
        edge_text, edge_cls = _format_signed_money(mark)
        try:
            if rec.actual_position_size is not None:
                sum_qty += Decimal(str(rec.actual_position_size))
        except Exception:
            pass
        if mark is not None:
            sum_mark += mark
            mark_count += 1
        next_line = rec.reason or rec.action or "—"
        code = f"P{index:02d}"
        strips.append(
            f'<div class="strip-item" role="listitem" data-market="{_e(rec.market_id)}">'
            f'<span class="idx">{code}</span>'
            f'<span class="stage {stage_cls}">{_e(stage_lbl)}</span>'
            '<div class="strip-main">'
            f'<div class="line1"><a href="/markets/{_e(rec.market_id)}">{_e(market_line)}</a></div>'
            f'<div class="line2" title="{_e(next_line)}">{_e(next_line)}</div>'
            "</div>"
            '<div class="strip-nums">'
            f'<div class="row">qty <strong>{_e(qty)}</strong></div>'
            f'<div class="row">cost <strong>{_e(cost_avg)}</strong> · mid <strong>{_e(mid)}</strong></div>'
            "</div>"
            f'<span class="strip-edge {edge_cls}">{_e(edge_text)}</span>'
            "</div>"
        )

    sum_mark_text, sum_mark_cls = _format_signed_money(sum_mark if mark_count else None)
    foot = (
        '<div class="strip-foot">'
        '<div class="cell">'
        f'<div class="k">{_e(labels.get("exit_sum_qty", "合计数量"))}</div>'
        f'<div class="v">{_e(_format_exit_value(sum_qty))}</div>'
        "</div>"
        '<div class="cell">'
        f'<div class="k">{_e(labels.get("exit_sum_mark", "未实现合计"))}</div>'
        f'<div class="v strip-edge {sum_mark_cls}">{_e(sum_mark_text)}</div>'
        "</div>"
        '<div class="cell">'
        f'<div class="k">{_e(labels.get("exit_sum_note_k", "口径"))}</div>'
        f'<div class="v">{_e(labels.get("exit_sum_note_v", "可执行−未回收"))}</div>'
        "</div>"
        "</div>"
    )

    more = ""
    if len(recs) > 24:
        more = f'<p class="empty-hint">{_e(labels["show_more"].format(count=len(recs) - 24))}</p>'

    return (
        '<section id="exit-policy" class="window window--aux mag-stream exit-policy-panel" '
        'data-od-id="panel-position-stream" aria-labelledby="pos-stream-title">'
        f"{head}"
        f'<div class="strip-list" role="list" aria-label="{_e(labels.get("exit_title", "持仓状态流"))}" '
        f'data-od-id="positions-stream">{"".join(strips)}</div>'
        f"{foot}{more}</section>"
    )


def _format_exit_value(value: object) -> str:
    if value is None:
        return "—"
    try:
        rendered = f"{Decimal(str(value)):.4f}".rstrip("0").rstrip(".")
    except Exception:
        return str(value)
    return rendered or "0"


def _verified_pnl_panel(pnl: VerifiedRealizedPnL, labels: dict[str, str]) -> str:
    recon_status = (
        labels["pnl_recon_fresh"] if pnl.reconciliation_fresh else labels["pnl_recon_stale"]
    )
    recon_tone = "ok" if pnl.reconciliation_fresh else "warn"
    recon_bar_class = "recon-bar" if pnl.reconciliation_fresh else "recon-bar is-stale"
    open_count = len(pnl.open_campaigns)

    open_items = list(pnl.open_campaigns)
    open_rows = "".join(_pnl_open_row(m, labels) for m in open_items[:5])
    if len(open_items) > 5:
        open_rows += (
            '<details class="more-disclosure">'
            f"<summary>{_e(labels['show_more'].format(count=len(open_items) - 5))}</summary>"
            + "".join(_pnl_open_row(m, labels) for m in open_items[5:])
            + "</details>"
        )
    if not open_rows:
        open_rows = f'<p class="pnl-empty">{_e(labels["exit_empty"])}</p>'

    realized_items = list(pnl.markets)
    realized_rows = "".join(_pnl_realized_row(m, labels) for m in realized_items[:5])
    if len(realized_items) > 5:
        realized_rows += (
            '<details class="more-disclosure">'
            f"<summary>{_e(labels['show_more'].format(count=len(realized_items) - 5))}</summary>"
            + "".join(_pnl_realized_row(m, labels) for m in realized_items[5:])
            + "</details>"
        )
    if not realized_rows:
        realized_rows = f'<p class="pnl-empty">{_e(labels["pnl_empty"])}</p>'

    unverified = ""
    if pnl.unverified_open_positions:
        unverified = (
            '<p class="pnl-warning">'
            + _e(labels["pnl_open_unverified"].format(count=pnl.unverified_open_positions))
            + "</p>"
        )

    return f"""
    <section class="window window--primary finance-panel pnl-panel" id="panel-finance"
             data-od-id="panel-finance" aria-labelledby="finance-title">
        <div class="pnl-heading">
          <h2 id="finance-title">{_e(labels["pnl_title"])}</h2>
          <span class="status-pill status-{recon_tone}" id="recon-pill" data-od-id="recon-pill">{_e(recon_status)}</span>
        </div>
        <p class="muted">{_e(labels["pnl_hint"])}</p>
        <p class="pnl-warning">{_e(labels["pnl_scope_warning"])}</p>
        <div class="kpi-grid" data-od-id="kpi-grid">
            <div class="kpi" data-od-id="kpi-realized">
              <div class="kpi-label"><span>{_e(labels["pnl_sold_total"])}</span>
                <span class="tag verified">Verified</span></div>
              <div class="kpi-value {_pnl_tone(pnl.total_realized_pnl)}">${pnl.total_realized_pnl:.2f}</div>
              <div class="kpi-foot">{_e(labels["pnl_sold_hint"])}</div>
            </div>
            <div class="kpi" data-od-id="kpi-unrealized">
              <div class="kpi-label"><span>{_e(labels["pnl_estimated"])}</span>
                <span class="tag est">Estimated</span></div>
              <div class="kpi-value {_pnl_tone(pnl.total_open_estimated_pnl)}">${pnl.total_open_estimated_pnl:.2f}</div>
              <div class="kpi-foot">{_e(labels["pnl_open_hint"])}</div>
            </div>
            <div class="kpi" data-od-id="kpi-pos-value">
              <div class="kpi-label"><span>{_e(labels["pnl_current_value"])}</span>
                <span class="tag est">MtM</span></div>
              <div class="kpi-value">${pnl.total_open_current_value:.2f}</div>
              <div class="kpi-foot">{open_count} · {_e(labels["pnl_position"])}</div>
            </div>
            <div class="kpi" data-od-id="kpi-exposure">
              <div class="kpi-label"><span>{_e(labels["pnl_exposure"])}</span></div>
              <div class="kpi-value">${pnl.total_reconciled_exposure:.2f}</div>
              <div class="kpi-foot">{_e(labels["pnl_open_cost"])} ${pnl.total_open_buy_cost:.2f}</div>
            </div>
        </div>
        <div class="{recon_bar_class}" data-od-id="recon-bar">
          <span>{_e(recon_status)}</span>
          <span class="mono">source: fill_ledger + positions_mtm</span>
        </div>
        <details class="pnl-ledgers-disclosure">
          <summary>{_e(labels.get("pnl_ledgers_expand", "Open position & realized ledgers"))}</summary>
          <div class="pnl-ledger-grid">
            <section class="pnl-ledger"><header><h3>{_e(labels["pnl_open_title"])}</h3><p>{_e(labels["pnl_open_hint"])}</p></header><div class="pnl-ledger-list">{open_rows}</div>{unverified}</section>
            <section class="pnl-ledger"><header><h3>{_e(labels["pnl_realized_title"])}</h3><p>{_e(labels["pnl_sold_hint"])}</p></header><div class="pnl-ledger-list">{realized_rows}</div></section>
          </div>
        </details>
    </section>
    """


def _pnl_open_row(m, labels: dict[str, str]) -> str:
    return (
        '<article class="pnl-ledger-row">'
        '<div class="pnl-ledger-primary">'
        f'<a class="mono" href="/markets/{_e(m.market_id)}">{_e(m.market_id)}</a>'
        f"<span>{_e(m.outcome)} · {_e(_format_exit_value(m.position_size))} {_e(labels['pnl_position'])}</span>"
        "</div>"
        f'<strong class="pnl-ledger-result {_pnl_tone(m.estimated_pnl)}">${m.estimated_pnl:.2f}</strong>'
        '<div class="pnl-ledger-meta">'
        f"<span>{_e(labels['pnl_open_cost'])} <b>${m.buy_cost:.2f}</b></span>"
        f"<span>{_e(labels['pnl_open_recovered'])} <b>${m.sell_proceeds:.2f}</b></span>"
        f"<span>{_e(labels['pnl_current_value'])} <b>${m.current_value:.2f}</b></span>"
        "</div></article>"
    )


def _pnl_realized_row(m, labels: dict[str, str]) -> str:
    return (
        '<article class="pnl-ledger-row">'
        '<div class="pnl-ledger-primary">'
        f'<a class="mono" href="/markets/{_e(m.market_id)}">{_e(m.market_id)}</a>'
        f"<span>{_e(labels['pnl_matched'])} {_e(_format_exit_value(m.matched_size))} · "
        f"{_e(labels['pnl_unmatched'])} {_e(_format_exit_value(m.unmatched_size))}</span>"
        "</div>"
        f'<strong class="pnl-ledger-result {_pnl_tone(m.realized_pnl)}">${m.realized_pnl:.2f}</strong>'
        '<div class="pnl-ledger-meta">'
        f"<span>{_e(labels['pnl_cost'])} <b>${m.gross_buy_cost:.2f}</b></span>"
        f"<span>{_e(labels['pnl_proceeds'])} <b>${m.gross_sell_proceeds:.2f}</b></span>"
        f"<span>{_e(labels['pnl_fees'])} <b>${m.fees:.2f}</b></span>"
        "</div></article>"
    )


def _pnl_tone(value: Decimal) -> str:
    if value > 0:
        return "pnl-positive"
    if value < 0:
        return "pnl-negative"
    return "pnl-neutral"
