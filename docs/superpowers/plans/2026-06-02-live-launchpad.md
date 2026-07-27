# Live Launchpad Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dedicated `/live` dashboard page that concentrates live readiness, exchange state, eligible candidates, micro-live preview, and locked execution status without loosening any existing live safety gate.

**Architecture:** Add a focused `live_launchpad_service.py` read model that composes existing readiness, live monitor, repository, profile, and exchange-state data. Add a `dashboard_ui/live.py` renderer and narrow dashboard routes for `/live`, `/live/refresh`, `/live/preview`, and `/live/propose`; execution remains locked in the first implementation. Keep actual order placement behind existing services and unavailable from the browser until follow-up gate coverage is implemented.

**Tech Stack:** Python 3.14, Typer, stdlib HTTP dashboard, SQLite repository facade, pytest, ruff.

---

## File Structure

- Create `src/polymarket_weather_arb/services/live_launchpad_service.py`
  - Builds a single `LiveLaunchpadSnapshot` from repository state, live readiness checks, live monitor gates, micro-live profile caps, candidates, and optional preview state.
  - Does not place orders and does not call live execution services.
- Create `src/polymarket_weather_arb/dashboard_ui/live.py`
  - Renders `/live` status, blockers, candidates, preview panel, confirmation form, and locked execution panel.
  - Uses existing `dashboard_ui.html` and `dashboard_ui.i18n` helpers.
- Modify `src/polymarket_weather_arb/dashboard.py`
  - Adds `GET /live` and POST handlers for `/live/refresh`, `/live/preview`, and `/live/propose`.
  - Keeps `/live/execute` locked with a clear error until execution is separately reviewed.
  - Adds `/live` fallback redirect for live POST errors.
- Modify `src/polymarket_weather_arb/dashboard_ui/html.py`
  - Adds navigation link for Live Launchpad if needed.
- Modify `src/polymarket_weather_arb/dashboard_ui/i18n.py`
  - Adds English and Chinese labels/flash/error strings.
- Modify `src/polymarket_weather_arb/dashboard_ui/beginner.py`
  - Adds a next-step link to `/live` after dry-run success without adding live controls to beginner mode.
- Test `tests/test_live_launchpad_service.py`
  - Unit coverage for snapshot gates, candidates, blockers, and preview locks.
- Test `tests/test_dashboard_live_launchpad.py`
  - Route and POST behavior coverage for browser safety.

## Task 1: Read-Only Launchpad Snapshot

**Files:**
- Create: `src/polymarket_weather_arb/services/live_launchpad_service.py`
- Test: `tests/test_live_launchpad_service.py`

- [ ] **Step 1: Write failing service tests**

Create `tests/test_live_launchpad_service.py`:

```python
from __future__ import annotations

from decimal import Decimal

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.markets import Market, MarketSnapshot
from polymarket_weather_arb.domain.rules import ResolutionRule
from polymarket_weather_arb.services.live_launchpad_service import build_live_launchpad_snapshot
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


def _repo(tmp_path):
    database = Database(tmp_path / "launchpad.db")
    database.init_schema()
    connection = database.connect()
    return connection, Repository(connection)


def _seed_market(repo: Repository, market_id: str = "m1") -> None:
    market = Market(
        id=market_id,
        slug=market_id,
        title="NYC high temp live candidate",
        description="Will NYC high temperature be above 80F?",
        end_date_iso="2026-06-10T00:00:00Z",
        active=True,
        closed=False,
        archived=False,
        liquidity=Decimal("100"),
        volume=Decimal("1000"),
        raw={},
    )
    rule = ResolutionRule(
        raw_text=market.description,
        location="New York",
        source="NOAA",
        station="KNYC",
        variable="temperature_high",
        threshold=Decimal("80"),
        operator=">",
        window_start="2026-06-09",
        window_end=None,
        unit="F",
        confidence=0.9,
        tradable=True,
        rejection_reason=None,
    )
    snapshot = MarketSnapshot(
        market_id=market_id,
        best_bid=Decimal("0.40"),
        best_ask=Decimal("0.45"),
        spread=Decimal("0.05"),
        liquidity=Decimal("100"),
        volume=Decimal("1000"),
        fetched_at="2026-06-02T00:00:00Z",
        raw={},
    )
    repo.upsert_market(market)
    repo.save_resolution_rule(market_id, rule)
    repo.save_market_snapshot(snapshot)
    repo.save_analysis(
        market_id=market_id,
        probability=Decimal("0.60"),
        fair_price=Decimal("0.60"),
        edge=Decimal("0.15"),
        decision="trade",
        rationale="edge exists",
    )
    repo.save_order_intent(
        market_id=market_id,
        side="buy",
        limit_price=Decimal("0.45"),
        size=Decimal("4"),
        notional=Decimal("1.80"),
        rationale="dry run",
        dry_run=True,
        status="recorded",
    )


def test_launchpad_snapshot_is_locked_without_live_gates(tmp_path):
    connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo)
        connection.commit()

        snapshot = build_live_launchpad_snapshot(
            repo,
            Settings(TRADING_DISABLED=True),
            check_exchange=False,
            live_market_ids=set(),
        )

        assert snapshot.can_execute is False
        assert snapshot.candidates
        candidate = snapshot.candidates[0]
        assert candidate.market_id == "m1"
        assert candidate.can_preview is False
        assert "market is not whitelisted" in candidate.blockers
        assert any(gate.name == "credentials" for gate in snapshot.readiness_gates)
    finally:
        connection.close()


def test_launchpad_candidate_can_preview_with_whitelist_override_and_fresh_reconciliation(tmp_path):
    connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo)
        repo.upsert_strategy_override(
            market_id="m1",
            profile="micro-live",
            live_auto_enabled=True,
            max_order_usdc="2",
        )
        repo.save_reconciliation(
            status="ok",
            details={"balances": [], "orders": [], "positions": []},
        )
        connection.commit()

        snapshot = build_live_launchpad_snapshot(
            repo,
            Settings(
                POLYMARKET_PRIVATE_KEY="key",
                POLYMARKET_FUNDER="funder",
                POLYMARKET_SIGNATURE_TYPE=1,
                COMPLIANCE_CHECK_ENABLED=False,
            ),
            check_exchange=False,
            live_market_ids={"m1"},
        )

        candidate = snapshot.candidates[0]
        assert candidate.can_preview is True
        assert candidate.profile == "micro-live"
        assert candidate.max_order_usdc == "2"
        assert candidate.blockers == []
    finally:
        connection.close()
```

- [ ] **Step 2: Run RED tests**

Run:

```bash
uv run pytest tests/test_live_launchpad_service.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'polymarket_weather_arb.services.live_launchpad_service'`.

- [ ] **Step 3: Implement snapshot service**

Create `src/polymarket_weather_arb/services/live_launchpad_service.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from sqlite3 import Row

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.profiles import get_profile, live_auto_enabled_by_override, settings_for_profile
from polymarket_weather_arb.services.live_monitor_service import is_fresh_reconciliation
from polymarket_weather_arb.services.live_readiness_service import LiveReadinessCheck, LiveReadinessService
from polymarket_weather_arb.storage.repositories import Repository


@dataclass(frozen=True)
class LiveLaunchpadGate:
    name: str
    ok: bool
    status: str
    detail: str


@dataclass(frozen=True)
class LiveLaunchpadCandidate:
    market_id: str
    title: str
    module_id: str
    profile: str
    best_bid: str | None
    best_ask: str | None
    latest_analysis_id: int | None
    latest_dry_run_id: int | None
    max_order_usdc: str
    gates: list[LiveLaunchpadGate]
    blockers: list[str]
    can_preview: bool


@dataclass(frozen=True)
class LiveLaunchpadPreview:
    market_id: str
    side: str
    limit_price: str
    size: str
    notional: str
    rationale: str
    risk_reasons: list[str]


@dataclass(frozen=True)
class LiveLaunchpadSnapshot:
    readiness_gates: list[LiveLaunchpadGate]
    reconciliation_status: str
    open_orders_count: int
    positions_count: int
    nonzero_positions_count: int
    max_order_usdc: str
    max_daily_usdc: str
    max_market_usdc: str
    candidates: list[LiveLaunchpadCandidate]
    preview: LiveLaunchpadPreview | None
    pending_live_action_id: str | None
    can_execute: bool
    blockers: list[str]


def build_live_launchpad_snapshot(
    repository: Repository,
    settings: Settings,
    *,
    check_exchange: bool = False,
    live_market_ids: set[str] | None = None,
    preview_market_id: str | None = None,
) -> LiveLaunchpadSnapshot:
    profile = get_profile("micro-live")
    effective_settings = settings_for_profile(settings, profile)
    readiness = LiveReadinessService(settings, repository, client=None).check(
        check_exchange=check_exchange
    )
    readiness_gates = [_readiness_gate(check) for check in readiness]
    latest_success = repository.latest_successful_reconciliation()
    reconciliation_fresh = is_fresh_reconciliation(latest_success)
    reconciliation_status = "fresh" if reconciliation_fresh else "missing_or_stale"
    allowed_market_ids = live_market_ids or set()
    candidates = _candidate_rows(repository)
    launchpad_candidates = [
        _candidate_from_row(
            repository,
            row,
            profile_name=profile.name,
            live_market_ids=allowed_market_ids,
            reconciliation_fresh=reconciliation_fresh,
            max_order_usdc=str(effective_settings.max_order_usdc),
        )
        for row in candidates
    ]
    preview = _preview_for_market(launchpad_candidates, preview_market_id)
    blockers = _unique(
        [
            gate.detail
            for gate in readiness_gates
            if not gate.ok
        ]
        + [
            blocker
            for candidate in launchpad_candidates
            for blocker in candidate.blockers
        ]
    )
    return LiveLaunchpadSnapshot(
        readiness_gates=readiness_gates,
        reconciliation_status=reconciliation_status,
        open_orders_count=len(repository.list_open_orders(limit=100)),
        positions_count=len(repository.list_positions(limit=100)),
        nonzero_positions_count=repository.nonzero_positions_count(),
        max_order_usdc=str(effective_settings.max_order_usdc),
        max_daily_usdc=str(effective_settings.max_daily_usdc),
        max_market_usdc=str(effective_settings.max_market_usdc),
        candidates=launchpad_candidates,
        preview=preview,
        pending_live_action_id=_pending_live_action_id(repository),
        can_execute=False,
        blockers=blockers,
    )


def _readiness_gate(check: LiveReadinessCheck) -> LiveLaunchpadGate:
    return LiveLaunchpadGate(check.name, check.ok, check.status, check.detail)


def _candidate_rows(repository: Repository) -> list[Row]:
    return [
        row
        for row in repository.list_markets(limit=100)
        if repository.latest_analysis(row["id"]) is not None
        and _latest_dry_run(repository, row["id"]) is not None
        and repository.latest_market_snapshot(row["id"]) is not None
    ]


def _candidate_from_row(
    repository: Repository,
    row: Row,
    *,
    profile_name: str,
    live_market_ids: set[str],
    reconciliation_fresh: bool,
    max_order_usdc: str,
) -> LiveLaunchpadCandidate:
    market_id = row["id"]
    snapshot = repository.latest_market_snapshot(market_id)
    analysis = repository.latest_analysis(market_id)
    dry_run = _latest_dry_run(repository, market_id)
    override = repository.effective_strategy_override(market_id, profile_name)
    gates = [
        LiveLaunchpadGate(
            "whitelist",
            market_id in live_market_ids,
            "ok" if market_id in live_market_ids else "blocked",
            "market is whitelisted" if market_id in live_market_ids else "market is not whitelisted",
        ),
        LiveLaunchpadGate(
            "override",
            live_auto_enabled_by_override(override),
            "ok" if live_auto_enabled_by_override(override) else "blocked",
            "live auto override is enabled"
            if live_auto_enabled_by_override(override)
            else "live auto override is not enabled",
        ),
        LiveLaunchpadGate(
            "reconciliation",
            reconciliation_fresh,
            "fresh" if reconciliation_fresh else "blocked",
            "fresh reconciliation is present" if reconciliation_fresh else "fresh reconciliation is missing",
        ),
        LiveLaunchpadGate(
            "analysis",
            analysis is not None,
            "ok" if analysis is not None else "blocked",
            "analysis exists" if analysis is not None else "analysis is missing",
        ),
        LiveLaunchpadGate(
            "dry_run",
            dry_run is not None,
            "ok" if dry_run is not None else "blocked",
            "dry-run exists" if dry_run is not None else "dry-run is missing",
        ),
    ]
    blockers = [gate.detail for gate in gates if not gate.ok]
    return LiveLaunchpadCandidate(
        market_id=market_id,
        title=row["title"],
        module_id=row["module_id"],
        profile=profile_name,
        best_bid=str(snapshot["best_bid"]) if snapshot and snapshot["best_bid"] is not None else None,
        best_ask=str(snapshot["best_ask"]) if snapshot and snapshot["best_ask"] is not None else None,
        latest_analysis_id=analysis["id"] if analysis else None,
        latest_dry_run_id=dry_run["id"] if dry_run else None,
        max_order_usdc=max_order_usdc,
        gates=gates,
        blockers=blockers,
        can_preview=not blockers,
    )


def _latest_dry_run(repository: Repository, market_id: str) -> Row | None:
    for row in repository.list_recent_order_intents(limit=50, market_id=market_id):
        if row["dry_run"]:
            return row
    return None


def _preview_for_market(
    candidates: list[LiveLaunchpadCandidate],
    preview_market_id: str | None,
) -> LiveLaunchpadPreview | None:
    if not preview_market_id:
        return None
    candidate = next((item for item in candidates if item.market_id == preview_market_id), None)
    if candidate is None or not candidate.can_preview:
        return None
    limit_price = candidate.best_ask or "0"
    return LiveLaunchpadPreview(
        market_id=candidate.market_id,
        side="buy",
        limit_price=limit_price,
        size="0",
        notional="0",
        rationale="Preview only. No live order has been placed.",
        risk_reasons=[],
    )


def _pending_live_action_id(repository: Repository) -> str | None:
    row = repository.latest_action_by_status("pending")
    if row is not None and row["kind"] == "trade_live":
        return row["id"]
    return None


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
```

- [ ] **Step 4: Run service tests**

Run:

```bash
uv run pytest tests/test_live_launchpad_service.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

Run:

```bash
git add src/polymarket_weather_arb/services/live_launchpad_service.py tests/test_live_launchpad_service.py
git commit -m "Add live launchpad snapshot service"
```

## Task 2: Read-Only `/live` Dashboard Page

**Files:**
- Create: `src/polymarket_weather_arb/dashboard_ui/live.py`
- Modify: `src/polymarket_weather_arb/dashboard.py`
- Modify: `src/polymarket_weather_arb/dashboard_ui/html.py`
- Modify: `src/polymarket_weather_arb/dashboard_ui/i18n.py`
- Test: `tests/test_dashboard_live_launchpad.py`

- [ ] **Step 1: Write failing dashboard route tests**

Create `tests/test_dashboard_live_launchpad.py`:

```python
from __future__ import annotations

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.dashboard import render_dashboard_path
from polymarket_weather_arb.storage.db import Database


def test_live_launchpad_renders_locked_by_default(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db", TRADING_DISABLED=True)
    Database(settings.database_path).init_schema()

    response = render_dashboard_path(settings, "/live?lang=zh")

    assert response.status.value == 200
    assert "Live Launchpad" in response.body
    assert "真实执行已锁定" in response.body
    assert "Refresh exchange state" not in response.body


def test_live_launchpad_nav_link_is_visible(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db", TRADING_DISABLED=True)
    Database(settings.database_path).init_schema()

    response = render_dashboard_path(settings, "/?lang=en")

    assert response.status.value == 200
    assert "/live?lang=en" in response.body
    assert "Live" in response.body
```

- [ ] **Step 2: Run RED tests**

Run:

```bash
uv run pytest tests/test_dashboard_live_launchpad.py -q
```

Expected: FAIL because `/live` is not routed and nav has no live link.

- [ ] **Step 3: Add i18n labels**

Add these English and Chinese labels to `dashboard_ui/i18n.py`:

```python
"nav.live": "Live",
"live.title": "Live Launchpad",
"live.subtitle": "One narrow place for readiness, preview, and locked micro-live execution.",
"live.readiness": "Readiness",
"live.exchange_state": "Exchange state",
"live.candidates": "Candidates",
"live.preview": "Preview",
"live.execution_locked": "Live execution is locked",
"live.execution_locked_body": "Browser execution remains disabled until every live gate has dedicated test coverage. Use preview and pending action proposal first.",
"live.refresh": "Refresh exchange state",
"live.preview_button": "Preview",
"live.no_candidates": "No live-ready dry-run candidates yet.",
"live.blockers": "Blockers",
"live.confirmation": "Final confirmation",
```

```python
"nav.live": "Live",
"live.title": "Live Launchpad",
"live.subtitle": "集中查看 readiness、预览和已锁定的 micro-live 执行。",
"live.readiness": "Readiness",
"live.exchange_state": "交易所状态",
"live.candidates": "候选市场",
"live.preview": "订单预览",
"live.execution_locked": "真实执行已锁定",
"live.execution_locked_body": "浏览器真实执行保持禁用，直到每个 live gate 都有专门测试覆盖。请先使用预览和 pending action proposal。",
"live.refresh": "刷新交易所状态",
"live.preview_button": "预览",
"live.no_candidates": "还没有接近 live-ready 的 dry-run 候选。",
"live.blockers": "阻塞原因",
"live.confirmation": "最终确认",
```

- [ ] **Step 4: Add nav link**

Modify the nav list in `dashboard_ui/html.py` so it includes:

```python
("/live", _t(lang, "nav.live")),
```

Place it near Beginner, Operator, or Actions so it is visible but not the first item.

- [ ] **Step 5: Add live renderer**

Create `src/polymarket_weather_arb/dashboard_ui/live.py`:

```python
from __future__ import annotations

from polymarket_weather_arb.dashboard_ui.html import _e, _hidden_lang, _href, _render_flash, _section, _table, render_page
from polymarket_weather_arb.dashboard_ui.i18n import _t
from polymarket_weather_arb.services.live_launchpad_service import LiveLaunchpadSnapshot


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
            f"<p class=\"muted\">{_t(lang, 'live.subtitle')}</p>",
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
    rows = [
        ["reconciliation", snapshot.reconciliation_status],
        ["open_orders", str(snapshot.open_orders_count)],
        ["positions", str(snapshot.positions_count)],
        ["nonzero_positions", str(snapshot.nonzero_positions_count)],
        ["max_order_usdc", snapshot.max_order_usdc],
        ["max_daily_usdc", snapshot.max_daily_usdc],
        ["max_market_usdc", snapshot.max_market_usdc],
    ]
    refresh = (
        f'<form method="post" action="{_href("/live/refresh", lang)}">'
        f'{_hidden_lang(lang)}'
        f'<button type="submit">{_t(lang, "live.refresh")}</button>'
        "</form>"
    )
    return refresh + _table(["Field", "Value"], rows, lang)


def _candidates_table(snapshot: LiveLaunchpadSnapshot, lang: str) -> str:
    if not snapshot.candidates:
        return f"<p class=\"muted\">{_t(lang, 'live.no_candidates')}</p>"
    return _table(
        ["Market", "Bid", "Ask", "Profile", "Blockers", "Action"],
        [
            [
                candidate.title,
                candidate.best_bid or "-",
                candidate.best_ask or "-",
                candidate.profile,
                "; ".join(candidate.blockers) if candidate.blockers else "-",
                _preview_button(candidate.market_id, candidate.can_preview, lang),
            ]
            for candidate in snapshot.candidates
        ],
        lang,
        raw_columns={5},
    )


def _preview_button(market_id: str, enabled: bool, lang: str) -> str:
    disabled = "" if enabled else " disabled"
    return (
        f'<form method="post" action="{_href("/live/preview", lang)}">'
        f'{_hidden_lang(lang)}'
        f'<input type="hidden" name="market_id" value="{_e(market_id)}">'
        f'<button type="submit"{disabled}>{_t(lang, "live.preview_button")}</button>'
        "</form>"
    )


def _preview_panel(snapshot: LiveLaunchpadSnapshot, lang: str) -> str:
    if snapshot.preview is None:
        return f"<p class=\"muted\">{_t(lang, 'live.preview')}</p>"
    preview = snapshot.preview
    return _table(
        ["Field", "Value"],
        [
            ["market", preview.market_id],
            ["side", preview.side],
            ["limit_price", preview.limit_price],
            ["size", preview.size],
            ["notional", preview.notional],
            ["rationale", preview.rationale],
        ],
        lang,
    )


def _execution_locked(lang: str) -> str:
    return (
        f"<p>{_t(lang, 'live.execution_locked_body')}</p>"
        '<button class="danger" disabled>'
        f"{_t(lang, 'live.execution_locked')}"
        "</button>"
    )
```

- [ ] **Step 6: Route `/live`**

Modify `dashboard.py` imports:

```python
from polymarket_weather_arb.dashboard_ui.live import render_live_launchpad
from polymarket_weather_arb.services.live_launchpad_service import build_live_launchpad_snapshot
```

Add GET route before the final 404:

```python
if route_path == "/live":
    snapshot = build_live_launchpad_snapshot(repository, settings, check_exchange=False)
    return DashboardResponse(
        HTTPStatus.OK,
        render_live_launchpad(snapshot, lang, current_path, query),
        headers,
    )
```

- [ ] **Step 7: Run dashboard route tests**

Run:

```bash
uv run pytest tests/test_dashboard_live_launchpad.py tests/test_dashboard.py::test_dashboard_renders_bilingual_overview_and_actions -q
```

Expected: PASS.

- [ ] **Step 8: Commit Task 2**

Run:

```bash
git add src/polymarket_weather_arb/dashboard.py src/polymarket_weather_arb/dashboard_ui tests/test_dashboard_live_launchpad.py
git commit -m "Add read-only live launchpad page"
```

## Task 3: Read-Only Refresh And Preview POSTs

**Files:**
- Modify: `src/polymarket_weather_arb/dashboard.py`
- Modify: `src/polymarket_weather_arb/services/live_launchpad_service.py`
- Test: `tests/test_dashboard_live_launchpad.py`

- [ ] **Step 1: Add failing POST tests**

Append to `tests/test_dashboard_live_launchpad.py`:

```python
from polymarket_weather_arb.dashboard import handle_dashboard_post


class FakeReconciliationService:
    def __init__(self, repository):
        self.repository = repository

    def reconcile(self):
        self.repository.replace_open_orders([])
        self.repository.replace_positions([])
        self.repository.save_reconciled_fills([])
        self.repository.save_reconciliation(status="ok", details={"source": "fake"})
        return {"status": "ok"}


def test_live_refresh_redirects_to_live(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    Database(settings.database_path).init_schema()

    response = handle_dashboard_post(
        settings,
        "/live/refresh?lang=en",
        b"lang=en",
        None,
        reconciliation_service_factory=lambda repo: FakeReconciliationService(repo),
    )

    assert response.status.value == 303
    assert response.headers["Location"].startswith("/live?lang=en")
    assert "flash=flash.live_refreshed" in response.headers["Location"]


def test_live_preview_redirects_with_market_id(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    Database(settings.database_path).init_schema()

    response = handle_dashboard_post(
        settings,
        "/live/preview?lang=en",
        b"lang=en&market_id=m1",
        None,
    )

    assert response.status.value == 303
    assert response.headers["Location"].startswith("/live?lang=en")
    assert "preview_market_id=m1" in response.headers["Location"]
```

- [ ] **Step 2: Run RED tests**

Run:

```bash
uv run pytest tests/test_dashboard_live_launchpad.py::test_live_refresh_redirects_to_live tests/test_dashboard_live_launchpad.py::test_live_preview_redirects_with_market_id -q
```

Expected: FAIL because live POST routes are not implemented.

- [ ] **Step 3: Add flash translation**

Add to `dashboard_ui/i18n.py`:

```python
"flash.live_refreshed": "Live exchange state refreshed.",
"flash.live_preview_ready": "Live preview prepared.",
"error.live_market_required": "A market must be selected for preview.",
"error.live_execution_locked": "Browser live execution is locked.",
```

```python
"flash.live_refreshed": "Live 交易所状态已刷新。",
"flash.live_preview_ready": "Live 预览已准备好。",
"error.live_market_required": "必须选择一个市场才能预览。",
"error.live_execution_locked": "浏览器真实执行已锁定。",
```

- [ ] **Step 4: Handle `/live/refresh`**

Add to `_handle_dashboard_post()` before market/action handlers:

```python
if path == "/live/refresh":
    reconciliation = (
        reconciliation_service_factory(repository)
        if reconciliation_service_factory
        else ReconciliationService(GammaPolymarketClient(settings), repository)
    )
    reconciliation.reconcile()
    return "/live", "flash.live_refreshed"
```

- [ ] **Step 5: Handle `/live/preview` and `/live/execute` lock**

Add:

```python
if path == "/live/preview":
    market_id = form.get("market_id", "").strip()
    if not market_id:
        raise DashboardError("error.live_market_required")
    return f"/live?preview_market_id={quote(market_id)}", "flash.live_preview_ready"
if path == "/live/execute":
    raise DashboardError("error.live_execution_locked")
```

Update `_fallback_post_location()`:

```python
if path.startswith("/live"):
    return "/live"
```

Update GET `/live` route to read preview id:

```python
preview_market_id = _single(query, "preview_market_id")
snapshot = build_live_launchpad_snapshot(
    repository,
    settings,
    check_exchange=False,
    preview_market_id=preview_market_id,
)
```

- [ ] **Step 6: Run POST tests**

Run:

```bash
uv run pytest tests/test_dashboard_live_launchpad.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit Task 3**

Run:

```bash
git add src/polymarket_weather_arb/dashboard.py src/polymarket_weather_arb/dashboard_ui/i18n.py tests/test_dashboard_live_launchpad.py
git commit -m "Add live launchpad refresh and preview routes"
```

## Task 4: Pending Live Proposal With Typed Confirmation

**Files:**
- Modify: `src/polymarket_weather_arb/dashboard.py`
- Modify: `src/polymarket_weather_arb/dashboard_ui/live.py`
- Modify: `src/polymarket_weather_arb/dashboard_ui/i18n.py`
- Test: `tests/test_dashboard_live_launchpad.py`

- [ ] **Step 1: Add failing confirmation tests**

Append:

```python
def test_live_propose_requires_confirmation_phrase(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    Database(settings.database_path).init_schema()

    response = handle_dashboard_post(
        settings,
        "/live/propose?lang=en",
        b"lang=en&market_id=m1&confirmation=wrong",
        None,
    )

    assert response.status.value == 303
    assert response.headers["Location"].startswith("/live?lang=en")
    assert "error.live_confirmation_required" in response.headers["Location"]


def test_live_execute_stays_locked(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    Database(settings.database_path).init_schema()

    response = handle_dashboard_post(settings, "/live/execute?lang=en", b"lang=en", None)

    assert response.status.value == 303
    assert response.headers["Location"].startswith("/live?lang=en")
    assert "error.live_execution_locked" in response.headers["Location"]
```

- [ ] **Step 2: Run RED tests**

Run:

```bash
uv run pytest tests/test_dashboard_live_launchpad.py::test_live_propose_requires_confirmation_phrase tests/test_dashboard_live_launchpad.py::test_live_execute_stays_locked -q
```

Expected: first test FAIL because `/live/propose` is not implemented; execute may already pass from Task 3.

- [ ] **Step 3: Add i18n labels**

Add:

```python
"live.propose": "Create pending live action",
"live.confirm_phrase": "Type LIVE 2 USDC",
"live.real_money_ack": "I understand this is a real-money live action proposal.",
"error.live_confirmation_required": "Type LIVE 2 USDC and check the acknowledgement before proposing live.",
"flash.live_action_proposed": "Pending live action created.",
```

```python
"live.propose": "创建 pending live action",
"live.confirm_phrase": "输入 LIVE 2 USDC",
"live.real_money_ack": "我理解这是一个真实资金 live action 提案。",
"error.live_confirmation_required": "创建 live 提案前，请输入 LIVE 2 USDC 并勾选确认。",
"flash.live_action_proposed": "Pending live action 已创建。",
```

- [ ] **Step 4: Add locked confirmation form to preview panel**

In `dashboard_ui/live.py`, when `snapshot.preview is not None`, append:

```python
def _proposal_form(preview: LiveLaunchpadPreview, lang: str) -> str:
    return (
        f'<form method="post" action="{_href("/live/propose", lang)}">'
        f'{_hidden_lang(lang)}'
        f'<input type="hidden" name="market_id" value="{_e(preview.market_id)}">'
        f'<label><input type="checkbox" name="ack" value="true"> {_t(lang, "live.real_money_ack")}</label>'
        f'<label>{_t(lang, "live.confirm_phrase")} '
        '<input name="confirmation" value=""></label>'
        f'<button type="submit" class="danger">{_t(lang, "live.propose")}</button>'
        "</form>"
    )
```

- [ ] **Step 5: Handle `/live/propose` as gated pending action only**

Add to `_handle_dashboard_post()`:

```python
if path == "/live/propose":
    market_id = form.get("market_id", "").strip()
    confirmation = form.get("confirmation", "").strip()
    ack = form.get("ack") == "true"
    if not market_id:
        raise DashboardError("error.live_market_required")
    if not ack or confirmation != "LIVE 2 USDC":
        raise DashboardError("error.live_confirmation_required")
    service.propose(
        kind="trade_live",
        market_id=market_id,
        reason="Live Launchpad pending micro-live proposal",
        ttl_minutes=10,
        requested_by="live-launchpad",
    )
    return "/live", "flash.live_action_proposed"
```

Do not approve or execute the action in this route.

- [ ] **Step 6: Run proposal tests**

Run:

```bash
uv run pytest tests/test_dashboard_live_launchpad.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit Task 4**

Run:

```bash
git add src/polymarket_weather_arb/dashboard.py src/polymarket_weather_arb/dashboard_ui/live.py src/polymarket_weather_arb/dashboard_ui/i18n.py tests/test_dashboard_live_launchpad.py
git commit -m "Add live launchpad pending proposal flow"
```

## Task 5: Beginner Link And Full Verification

**Files:**
- Modify: `src/polymarket_weather_arb/dashboard_ui/beginner.py`
- Test: `tests/test_dashboard.py`
- Test: `tests/test_dashboard_live_launchpad.py`

- [ ] **Step 1: Add failing beginner link test**

Update `test_dashboard_renders_beginner_mode_with_safe_actions` in `tests/test_dashboard.py`:

```python
assert "/live?lang=zh" in response.body
assert "Live Launchpad" in response.body
```

- [ ] **Step 2: Run RED test**

Run:

```bash
uv run pytest tests/test_dashboard.py::test_dashboard_renders_beginner_mode_with_safe_actions -q
```

Expected: FAIL because beginner has no `/live` next-step link.

- [ ] **Step 3: Add beginner next-step label**

In `dashboard_ui/beginner.py`, add labels:

```python
"step_live_launchpad": "Open Live Launchpad",
```

```python
"step_live_launchpad": "打开 Live Launchpad",
```

Update `_next_steps()`:

```python
steps = [
    (labels["step_load_demo"], "/beginner"),
    (labels["step_review_orders"], "/orders"),
    (labels["step_readiness"], "/doctor"),
    (labels["step_candidates"], "/candidates"),
    (labels["step_live_launchpad"], "/live"),
]
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
uv run pytest tests/test_dashboard.py::test_dashboard_renders_beginner_mode_with_safe_actions tests/test_dashboard_live_launchpad.py -q
```

Expected: PASS.

- [ ] **Step 5: Run full verification**

Run:

```bash
uv run pytest -q
uv run ruff check src/ tests/ scripts/rehearse_live_readiness.py scripts/backup_restore_check.py scripts/check_deployment_files.py
bash -n scripts/install_systemd_units.sh
```

Expected:

- pytest reports all tests passing.
- ruff reports `All checks passed!`.
- `bash -n` exits with no output.

- [ ] **Step 6: Commit Task 5**

Run:

```bash
git add src/polymarket_weather_arb/dashboard_ui/beginner.py tests/test_dashboard.py
git commit -m "Link beginner mode to live launchpad"
```

- [ ] **Step 7: Push main**

Run:

```bash
git push origin main
```

Expected: `main -> main`.

## Self-Review

Spec coverage:

- `/live` page: Task 2.
- Readiness and exchange state: Tasks 1 and 2.
- Refresh exchange state: Task 3.
- Candidate selection: Tasks 1 and 2.
- Preview-only flow: Task 3.
- Typed confirmation and pending live action proposal: Task 4.
- Browser live execution locked: Tasks 2, 3, and 4.
- Beginner remains dry-run and links onward only: Task 5.

No incomplete steps are intentionally left. Browser execution remains explicitly locked in this plan; enabling real browser execution requires a follow-up design/review after preview and pending proposal prove stable.
