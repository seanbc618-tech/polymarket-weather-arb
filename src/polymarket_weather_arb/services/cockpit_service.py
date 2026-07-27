from __future__ import annotations
from collections import defaultdict
import datetime
import time

from dataclasses import dataclass
from decimal import Decimal
from sqlite3 import Row
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from polymarket_weather_arb.domain.market_eligibility import resolve_market_timezone
from polymarket_weather_arb.domain.position_inventory import build_campaign_inventory
from polymarket_weather_arb.storage.repositories import Repository


@dataclass(frozen=True)
class NextActionSuggestion:
    label: str
    reason: str
    href: str
    action_kind: str | None = None
    market_id: str | None = None


@dataclass(frozen=True)
class OpportunityFunnelStageBlocker:
    reason: str
    count: int


@dataclass(frozen=True)
class OpportunityFunnel:
    discovered: int
    rule_tradable: int
    quote_available: int
    forecast_available: int
    analyzed: int
    quant_trade_signal: int
    live_submitted: int
    exchange_fill: int
    blockers: list[OpportunityFunnelStageBlocker]


@dataclass(frozen=True)
class MarketPnL:
    market_id: str
    roundtrips: int
    matched_size: Decimal
    unmatched_size: Decimal
    reconciled_exposure: Decimal
    gross_buy_cost: Decimal
    gross_sell_proceeds: Decimal
    fees: Decimal
    realized_pnl: Decimal
    last_completed_at: str


@dataclass(frozen=True)
class OpenCampaignPnL:
    market_id: str
    outcome: str
    position_size: Decimal
    buy_cost: Decimal
    sell_proceeds: Decimal
    current_value: Decimal
    estimated_pnl: Decimal


@dataclass(frozen=True)
class VerifiedRealizedPnL:
    markets: list[MarketPnL]
    open_campaigns: list[OpenCampaignPnL]
    total_roundtrips: int
    total_matched_size: Decimal
    total_unmatched_size: Decimal
    total_reconciled_exposure: Decimal
    total_gross_buy_cost: Decimal
    total_gross_sell_proceeds: Decimal
    total_fees: Decimal
    total_realized_pnl: Decimal
    total_open_buy_cost: Decimal
    total_open_sell_proceeds: Decimal
    total_open_current_value: Decimal
    total_open_estimated_pnl: Decimal
    unverified_open_positions: int
    reconciliation_fresh: bool


@dataclass(frozen=True)
class PortfolioDigestPosition:
    market_id: str
    market_title: str
    city: str
    bucket: str
    outcome: str
    position_size: Decimal
    buy_cost: Decimal
    sell_proceeds: Decimal
    current_value: Decimal
    estimated_pnl: Decimal
    estimated_return_pct: Decimal | None
    target_date: str | None
    timezone_name: str | None
    local_day_offset: int | None
    seconds_to_target_end: int | None


@dataclass(frozen=True)
class PortfolioDigest:
    positions: list[PortfolioDigestPosition]
    open_position_count: int
    open_order_count: int
    unverified_open_positions: int
    total_buy_cost: Decimal
    total_sell_proceeds: Decimal
    total_current_value: Decimal
    total_estimated_pnl: Decimal
    total_estimated_return_pct: Decimal | None
    total_realized_pnl: Decimal
    reconciliation_fresh: bool
    reconciliation_age_minutes: int | None


@dataclass(frozen=True)
class CandidatePipelineSummary:
    found: int
    parsed: int
    quoted: int
    signal_ready: int
    analyzed: int
    dry_run: int


@dataclass(frozen=True)
class CandidateSummary:
    market_id: str
    title: str
    module_id: str
    status: str
    next_step: str
    href: str
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None


@dataclass(frozen=True)
class BlockerSummary:
    message: str
    href: str
    market_id: str | None = None


@dataclass(frozen=True)
class ActionSummary:
    action_id: str
    kind: str
    status: str
    market_id: str
    href: str


@dataclass(frozen=True)
class RunSummary:
    run_id: int
    command: str
    status: str


@dataclass(frozen=True)
class CockpitSnapshot:
    next_action: NextActionSuggestion
    pipeline: CandidatePipelineSummary
    opportunity_funnel: OpportunityFunnel
    realized_pnl: VerifiedRealizedPnL

    top_candidates: list[CandidateSummary]
    blockers: list[BlockerSummary]
    recent_actions: list[ActionSummary]
    recent_runs: list[RunSummary]
    mode: str
    profile: str


def build_cockpit_snapshot(
    repository: Repository, *, profile: str = "dry-run-demo"
) -> CockpitSnapshot:
    candidates = repository.list_candidates(limit=200)
    top_candidates = [_candidate_summary(repository, row) for row in candidates[:5]]
    blockers = _blockers(repository, candidates)
    pipeline = _pipeline(repository, candidates)
    opportunity_funnel = _build_opportunity_funnel(repository)
    realized_pnl = _build_verified_pnl(repository)
    recent_actions = [
        ActionSummary(
            action_id=row["id"],
            kind=row["kind"],
            status=row["status"],
            market_id=row["market_id"],
            href=f"/actions/{row['id']}",
        )
        for row in repository.list_automation_actions(limit=5)
    ]
    recent_runs = [
        RunSummary(run_id=row["id"], command=row["command"], status=row["status"])
        for row in repository.list_recent_runs(limit=5)
    ]
    return CockpitSnapshot(
        next_action=_next_action(repository, candidates, top_candidates, blockers),
        pipeline=pipeline,
        opportunity_funnel=opportunity_funnel,
        realized_pnl=realized_pnl,
        top_candidates=top_candidates,
        blockers=blockers,
        recent_actions=recent_actions,
        recent_runs=recent_runs,
        mode="research / dry-run",
        profile=profile,
    )


def _pipeline(repository: Repository, candidates: list[Row]) -> CandidatePipelineSummary:
    parsed = quoted = signal_ready = analyzed = dry_run = 0
    for row in candidates:
        market_id = row["market_id"]
        if bool(row["tradable"]):
            parsed += 1
        if row["best_bid"] is not None or row["best_ask"] is not None:
            quoted += 1
        if repository.latest_forecast(market_id) is not None:
            signal_ready += 1
        if repository.latest_analysis(market_id) is not None:
            analyzed += 1
        if _latest_dry_run(repository, market_id) is not None:
            dry_run += 1
    return CandidatePipelineSummary(
        found=len(candidates),
        parsed=parsed,
        quoted=quoted,
        signal_ready=signal_ready,
        analyzed=analyzed,
        dry_run=dry_run,
    )


def _candidate_summary(repository: Repository, row: Row) -> CandidateSummary:
    market_id = row["market_id"]
    return CandidateSummary(
        market_id=market_id,
        title=row["title"],
        module_id=row["module_id"],
        status=row["status"],
        best_bid=_decimal(row["best_bid"]),
        best_ask=_decimal(row["best_ask"]),
        next_step=_next_step(repository, row),
        href=f"/markets/{market_id}",
    )


def _blockers(repository: Repository, candidates: list[Row]) -> list[BlockerSummary]:
    blockers: list[BlockerSummary] = []
    for row in candidates:
        market_id = row["market_id"]
        if not bool(row["tradable"]):
            blockers.append(
                BlockerSummary(
                    message=f"{market_id}: rule needs review",
                    href=f"/markets/{market_id}",
                    market_id=market_id,
                )
            )
            continue
        if row["best_bid"] is None and row["best_ask"] is None:
            blockers.append(
                BlockerSummary(
                    message=f"{market_id}: missing quote snapshot",
                    href=f"/markets/{market_id}",
                    market_id=market_id,
                )
            )
            continue
        if repository.latest_forecast(market_id) is None:
            blockers.append(
                BlockerSummary(
                    message=f"{market_id}: missing signal or forecast",
                    href=f"/markets/{market_id}",
                    market_id=market_id,
                )
            )
    return blockers[:5]


def _next_action(
    repository: Repository,
    candidates: list[Row],
    top_candidates: list[CandidateSummary],
    blockers: list[BlockerSummary],
) -> NextActionSuggestion:
    approved = repository.latest_action_by_status("approved")
    if approved is not None and approved["kind"] != "trade_live":
        return NextActionSuggestion(
            label="Run approved action",
            reason=f"{approved['kind']} is approved and waiting",
            href=f"/actions/{approved['id']}",
            action_kind=approved["kind"],
            market_id=approved["market_id"],
        )
    pending = repository.latest_action_by_status("pending")
    if pending is not None and pending["kind"] != "trade_live":
        return NextActionSuggestion(
            label="Review pending action",
            reason=f"{pending['kind']} needs human review",
            href=f"/actions/{pending['id']}",
            action_kind=pending["kind"],
            market_id=pending["market_id"],
        )
    if not candidates:
        return NextActionSuggestion(
            label="Run discovery",
            reason="No candidate markets found yet",
            href="/discovery",
        )
    for candidate in top_candidates:
        if candidate.next_step == "refresh_signal":
            return NextActionSuggestion(
                label="Refresh missing signals",
                reason=f"{candidate.market_id} has a quote but no signal yet",
                href=candidate.href,
                action_kind="refresh_weather",
                market_id=candidate.market_id,
            )
        if candidate.next_step == "analyze":
            return NextActionSuggestion(
                label="Analyze ready candidate",
                reason=f"{candidate.market_id} has quote and signal data",
                href=candidate.href,
                action_kind="analyze",
                market_id=candidate.market_id,
            )
        if candidate.next_step == "dry_run":
            return NextActionSuggestion(
                label="Dry-run latest analysis",
                reason=f"{candidate.market_id} has an analysis ready for simulation",
                href=candidate.href,
                action_kind="dry_run",
                market_id=candidate.market_id,
            )
    if blockers:
        blocker = blockers[0]
        return NextActionSuggestion(
            label="Resolve blocker",
            reason=blocker.message,
            href=blocker.href,
            market_id=blocker.market_id,
        )
    return NextActionSuggestion(
        label="Review candidates",
        reason="Candidates are available for operator review",
        href="/candidates",
    )


def _next_step(repository: Repository, row: Row) -> str:
    market_id = row["market_id"]
    if not bool(row["tradable"]):
        return "inspect"
    if row["best_bid"] is None and row["best_ask"] is None:
        return "refresh_quote"
    if repository.latest_forecast(market_id) is None:
        return "refresh_signal"
    if repository.latest_analysis(market_id) is None:
        return "analyze"
    if _latest_dry_run(repository, market_id) is None:
        return "dry_run"
    return "review"


def _latest_dry_run(repository: Repository, market_id: str) -> Row | None:
    rows = repository.list_recent_order_intents(limit=1, market_id=market_id)
    if rows and bool(rows[0]["dry_run"]):
        return rows[0]
    return None


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _build_opportunity_funnel(repository: Repository, since_hours: int = 24) -> OpportunityFunnel:
    time_cutoff = f"-{since_hours} hours"
    cursor = repository.connection.execute(
        "SELECT * FROM market_candidates WHERE datetime(updated_at) >= datetime('now', ?)",
        (time_cutoff,),
    )
    candidates = cursor.fetchall()

    stages = {
        "discovered": 0,
        "rule_tradable": 0,
        "quote_available": 0,
        "forecast_available": 0,
        "analyzed": 0,
        "quant_trade_signal": 0,
        "live_submitted": 0,
        "exchange_fill": 0,
    }
    blocker_counts = defaultdict(int)

    for row in candidates:
        market_id = row["market_id"]
        stages["discovered"] += 1

        tradable = bool(row["tradable"])
        if not tradable:
            blocker_counts[row["rejection_reason"] or "non-tradable rule"] += 1
            continue

        stages["rule_tradable"] += 1
        has_quote = row["best_bid"] is not None or row["best_ask"] is not None
        if not has_quote:
            blocker_counts["missing quote"] += 1
            continue

        stages["quote_available"] += 1
        forecast = repository.connection.execute(
            "SELECT * FROM weather_forecasts WHERE market_id = ? AND datetime(fetched_at) >= datetime('now', ?) ORDER BY id DESC LIMIT 1",
            (market_id, time_cutoff),
        ).fetchone()
        if not forecast:
            blocker_counts["missing forecast"] += 1
            continue

        stages["forecast_available"] += 1
        analysis = repository.connection.execute(
            "SELECT * FROM analyses WHERE market_id = ? AND datetime(created_at) >= datetime('now', ?) ORDER BY id DESC LIMIT 1",
            (market_id, time_cutoff),
        ).fetchone()
        if not analysis:
            blocker_counts["missing analysis"] += 1
            continue

        stages["analyzed"] += 1
        if analysis["decision"] not in {"buy", "trade"}:
            blocker_counts[analysis["reasons"] or "quant skip/edge"] += 1
            continue

        stages["quant_trade_signal"] += 1
        intents = repository.connection.execute(
            "SELECT * FROM order_intents WHERE market_id = ? AND datetime(created_at) >= datetime('now', ?) ORDER BY id DESC LIMIT 10",
            (market_id, time_cutoff),
        ).fetchall()
        live_intents = [
            i
            for i in intents
            if not bool(i["dry_run"])
            and i["status"] not in {"failed", "rejected", "cancelled", "canceled", "cancel_failed"}
        ]

        if not live_intents:
            entry_block = repository.connection.execute(
                """
                SELECT reason FROM autopilot_decisions
                WHERE market_id = ? AND action = 'entry_minimum_blocked'
                  AND datetime(created_at) >= datetime('now', ?)
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (market_id, time_cutoff),
            ).fetchone()
            if entry_block is not None:
                blocker_counts[entry_block["reason"] or "exchange minimum order"] += 1
                continue
            risk = repository.connection.execute(
                "SELECT * FROM risk_decisions WHERE market_id = ? AND datetime(created_at) >= datetime('now', ?) ORDER BY id DESC LIMIT 1",
                (market_id, time_cutoff),
            ).fetchone()
            if risk and not bool(risk["accepted"]):
                blocker_counts[risk["reasons"] or "risk rejection"] += 1
            else:
                actions = repository.connection.execute(
                    "SELECT status, reason FROM automation_actions WHERE market_id = ? AND datetime(created_at) >= datetime('now', ?) ORDER BY id DESC LIMIT 1",
                    (market_id, time_cutoff),
                ).fetchone()
                if actions and actions["status"] == "rejected":
                    blocker_counts[actions["reason"] or "automation rejected"] += 1
                else:
                    blocker_counts["live gate"] += 1
            continue

        stages["live_submitted"] += 1
        fills = repository.connection.execute(
            "SELECT * FROM fills WHERE market_id = ? AND datetime(filled_at) >= datetime('now', ?) LIMIT 1",
            (market_id, time_cutoff),
        ).fetchall()
        if not fills:
            blocker_counts["missing fill"] += 1
            continue

        stages["exchange_fill"] += 1

    sorted_blockers = [
        OpportunityFunnelStageBlocker(reason=r, count=c)
        for r, c in sorted(blocker_counts.items(), key=lambda x: x[1], reverse=True)
    ]

    return OpportunityFunnel(
        discovered=stages["discovered"],
        rule_tradable=stages["rule_tradable"],
        quote_available=stages["quote_available"],
        forecast_available=stages["forecast_available"],
        analyzed=stages["analyzed"],
        quant_trade_signal=stages["quant_trade_signal"],
        live_submitted=stages["live_submitted"],
        exchange_fill=stages["exchange_fill"],
        blockers=sorted_blockers,
    )


def _build_verified_pnl(repository: Repository) -> VerifiedRealizedPnL:
    # 1. Check if global reconciliation is fresh
    reconciliation = repository.latest_reconciliation()
    if reconciliation and str(reconciliation["status"] or "").lower() == "ok":
        rec_time = datetime.datetime.fromisoformat(reconciliation["created_at"])
        if rec_time.tzinfo is None:
            rec_time = rec_time.replace(tzinfo=datetime.timezone.utc)
        age = time.time() - rec_time.timestamp()
        is_fresh = age < 600
    else:
        is_fresh = False

    cursor = repository.connection.execute(
        """
        SELECT * FROM roundtrip_runs
        WHERE buy_intent_id IS NOT NULL AND sell_intent_id IS NOT NULL
        ORDER BY updated_at ASC
        """
    )
    runs = cursor.fetchall()

    market_stats = {}
    position_exposure: dict[str, Decimal] = {}
    positions = repository.connection.execute(
        """
        SELECT market_id, SUM(ABS(notional)) AS exposure
        FROM positions
        GROUP BY market_id
        """
    ).fetchall()
    for p in positions:
        position_exposure[str(p["market_id"])] = Decimal(str(p["exposure"]))

    for run in runs:
        m_id = run["market_id"]
        buy_intent_ids = repository.roundtrip_intent_ids(int(run["id"]), side_prefix="buy")
        sell_intent_ids = repository.roundtrip_intent_ids(int(run["id"]), side_prefix="sell")
        buy_order_ids = set(repository.get_order_ids_for_intents(buy_intent_ids))
        sell_order_ids = set(repository.get_order_ids_for_intents(sell_intent_ids))

        all_fills = repository.list_fills(market_id=m_id)
        b_fills = [f for f in all_fills if f["order_id"] in buy_order_ids]
        s_fills = [f for f in all_fills if f["order_id"] in sell_order_ids]

        buy_size = sum(Decimal(str(f["size"])) for f in b_fills)
        sell_size = sum(Decimal(str(f["size"])) for f in s_fills)
        matched = min(buy_size, sell_size)
        unmatched = abs(buy_size - sell_size)

        if matched <= 0 and unmatched <= 0:
            continue

        b_fills.sort(key=lambda x: x["filled_at"])
        s_fills.sort(key=lambda x: x["filled_at"])

        cost = Decimal("0")
        fees = Decimal("0")
        rem = matched
        for f in b_fills:
            sz = min(Decimal(str(f["size"])), rem)
            if sz <= 0:
                break
            pr = Decimal(str(f["price"]))
            ratio = sz / Decimal(str(f["size"]))
            fee = Decimal(str(dict(f).get("fee", 0))) * ratio

            cost += sz * pr
            fees += fee
            rem -= sz

        proceeds = Decimal("0")
        rem = matched
        for f in s_fills:
            sz = min(Decimal(str(f["size"])), rem)
            if sz <= 0:
                break
            pr = Decimal(str(f["price"]))
            ratio = sz / Decimal(str(f["size"]))
            fee = Decimal(str(dict(f).get("fee", 0))) * ratio

            proceeds += sz * pr
            fees += fee
            rem -= sz

        pnl = proceeds - cost - fees

        if m_id not in market_stats:
            market_stats[m_id] = {
                "roundtrips": 0,
                "matched": Decimal("0"),
                "unmatched": Decimal("0"),
                "exposure": position_exposure.get(m_id, Decimal("0")),
                "cost": Decimal("0"),
                "proceeds": Decimal("0"),
                "fees": Decimal("0"),
                "pnl": Decimal("0"),
                "last_completed": run["updated_at"],
            }

        st = market_stats[m_id]
        st["roundtrips"] += 1
        st["matched"] += matched
        st["unmatched"] += unmatched
        st["cost"] += cost
        st["proceeds"] += proceeds
        st["fees"] += fees
        st["pnl"] += pnl
        if st["last_completed"] == "N/A":
            st["last_completed"] = run["updated_at"]
        else:
            st["last_completed"] = max(st["last_completed"], run["updated_at"])

    markets_pnl = []
    for m_id, st in market_stats.items():
        markets_pnl.append(
            MarketPnL(
                market_id=m_id,
                roundtrips=st["roundtrips"],
                matched_size=st["matched"],
                unmatched_size=st["unmatched"],
                reconciled_exposure=st["exposure"],
                gross_buy_cost=st["cost"],
                gross_sell_proceeds=st["proceeds"],
                fees=st["fees"],
                realized_pnl=st["pnl"],
                last_completed_at=st["last_completed"],
            )
        )

    markets_pnl.sort(key=lambda x: x.last_completed_at, reverse=True)

    open_campaigns: list[OpenCampaignPnL] = []
    unverified_open_positions = 0
    for position in repository.list_positions(limit=1000, nonzero_only=True):
        market_id = str(position["market_id"])
        market = repository.get_market(market_id)
        if market is None:
            unverified_open_positions += 1
            continue
        inventory = build_campaign_inventory(
            [dict(fill) for fill in repository.list_fills(limit=10_000, market_id=market_id)],
            market_id=market_id,
            outcome=str(position["outcome"]),
            position_size=Decimal(str(position["size"])),
            yes_token_id=market["yes_token_id"],
            no_token_id=market["no_token_id"],
            order_token_ids=repository.order_token_ids_for_market(market_id),
        )
        if not inventory.accounting_verified:
            unverified_open_positions += 1
            continue
        current_value = Decimal(str(position["notional"] or 0))
        estimated_pnl = (
            inventory.verified_sell_proceeds + current_value - inventory.verified_buy_cost
        )
        open_campaigns.append(
            OpenCampaignPnL(
                market_id=market_id,
                outcome=inventory.outcome,
                position_size=inventory.position_size,
                buy_cost=inventory.verified_buy_cost,
                sell_proceeds=inventory.verified_sell_proceeds,
                current_value=current_value,
                estimated_pnl=estimated_pnl,
            )
        )

    open_campaigns.sort(key=lambda item: item.estimated_pnl)

    return VerifiedRealizedPnL(
        markets=markets_pnl,
        open_campaigns=open_campaigns,
        total_roundtrips=sum(m.roundtrips for m in markets_pnl),
        total_matched_size=sum(m.matched_size for m in markets_pnl),
        total_unmatched_size=sum(m.unmatched_size for m in markets_pnl),
        total_reconciled_exposure=sum(m.reconciled_exposure for m in markets_pnl),
        total_gross_buy_cost=sum(m.gross_buy_cost for m in markets_pnl),
        total_gross_sell_proceeds=sum(m.gross_sell_proceeds for m in markets_pnl),
        total_fees=sum(m.fees for m in markets_pnl),
        total_realized_pnl=sum(m.realized_pnl for m in markets_pnl),
        total_open_buy_cost=sum(m.buy_cost for m in open_campaigns),
        total_open_sell_proceeds=sum(m.sell_proceeds for m in open_campaigns),
        total_open_current_value=sum(m.current_value for m in open_campaigns),
        total_open_estimated_pnl=sum(m.estimated_pnl for m in open_campaigns),
        unverified_open_positions=unverified_open_positions,
        reconciliation_fresh=is_fresh,
    )


def build_portfolio_digest(
    repository: Repository,
    *,
    now: datetime.datetime | None = None,
) -> PortfolioDigest:
    """Build the Telegram portfolio view from the existing reconciled PnL ledger."""
    current = now or datetime.datetime.now(datetime.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=datetime.timezone.utc)
    pnl = _build_verified_pnl(repository)
    position_rows = repository.list_positions(limit=1000, nonzero_only=True)
    latest_reconciliation = repository.latest_reconciliation()
    reconciliation_age_minutes: int | None = None
    if (
        latest_reconciliation is not None
        and str(latest_reconciliation["status"] or "").lower() == "ok"
    ):
        reconciled_at = datetime.datetime.fromisoformat(latest_reconciliation["created_at"])
        if reconciled_at.tzinfo is None:
            reconciled_at = reconciled_at.replace(tzinfo=datetime.timezone.utc)
        reconciliation_age_minutes = max(
            0,
            int((current - reconciled_at.astimezone(datetime.timezone.utc)).total_seconds() / 60),
        )

    digest_positions: list[PortfolioDigestPosition] = []
    for campaign in pnl.open_campaigns:
        market = repository.get_market(campaign.market_id)
        if market is None:
            continue
        title = str(market["title"] or campaign.market_id)
        rule = repository.get_temperature_bucket_rule(campaign.market_id)
        city = str(rule["city"] or "") if rule is not None else ""
        bucket = _portfolio_bucket_label(rule, title=title)
        target_date = str(rule["target_date"] or "") if rule is not None else ""
        stored_timezone = str(rule["settlement_timezone"] or "") if rule is not None else ""
        timezone_name = resolve_market_timezone(
            title=title,
            location_hint=city or None,
        ) or (stored_timezone or None)
        local_day_offset, seconds_to_target_end = _portfolio_target_timing(
            target_date=target_date or None,
            timezone_name=timezone_name,
            now=current,
        )
        return_pct = (
            campaign.estimated_pnl / campaign.buy_cost * Decimal("100")
            if campaign.buy_cost > 0
            else None
        )
        digest_positions.append(
            PortfolioDigestPosition(
                market_id=campaign.market_id,
                market_title=title,
                city=city,
                bucket=bucket,
                outcome=campaign.outcome.upper(),
                position_size=campaign.position_size,
                buy_cost=campaign.buy_cost,
                sell_proceeds=campaign.sell_proceeds,
                current_value=campaign.current_value,
                estimated_pnl=campaign.estimated_pnl,
                estimated_return_pct=return_pct,
                target_date=target_date or None,
                timezone_name=timezone_name,
                local_day_offset=local_day_offset,
                seconds_to_target_end=seconds_to_target_end,
            )
        )

    digest_positions.sort(key=lambda item: abs(item.estimated_pnl), reverse=True)
    total_return_pct = (
        pnl.total_open_estimated_pnl / pnl.total_open_buy_cost * Decimal("100")
        if pnl.total_open_buy_cost > 0
        else None
    )
    return PortfolioDigest(
        positions=digest_positions,
        open_position_count=len(position_rows),
        open_order_count=len(repository.list_open_orders(limit=1000)),
        unverified_open_positions=pnl.unverified_open_positions,
        total_buy_cost=pnl.total_open_buy_cost,
        total_sell_proceeds=pnl.total_open_sell_proceeds,
        total_current_value=pnl.total_open_current_value,
        total_estimated_pnl=pnl.total_open_estimated_pnl,
        total_estimated_return_pct=total_return_pct,
        total_realized_pnl=pnl.total_realized_pnl,
        reconciliation_fresh=pnl.reconciliation_fresh,
        reconciliation_age_minutes=reconciliation_age_minutes,
    )


def _portfolio_bucket_label(rule: Row | None, *, title: str) -> str:
    if rule is None:
        return title[:72]
    unit = str(rule["unit"] or "C").upper()
    center = _optional_decimal(rule["bucket_center_c"])
    lower = _optional_decimal(rule["bucket_lower_c"])
    upper = _optional_decimal(rule["bucket_upper_c"])
    if center is not None and center == center.to_integral_value():
        return f"{_compact_decimal(center)}°{unit}"
    if (
        lower is not None
        and upper is not None
        and lower == lower.to_integral_value()
        and upper == upper.to_integral_value()
    ):
        return f"{_compact_decimal(lower)}-{_compact_decimal(upper)}°{unit}"
    if center is not None:
        return f"{_compact_decimal(center)}°{unit}"
    return title[:72]


def _portfolio_target_timing(
    *,
    target_date: str | None,
    timezone_name: str | None,
    now: datetime.datetime,
) -> tuple[int | None, int | None]:
    if not target_date or not timezone_name:
        return None, None
    try:
        target_day = datetime.date.fromisoformat(target_date[:10])
        local_tz = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError):
        return None, None
    local_now = now.astimezone(local_tz)
    target_end = datetime.datetime.combine(target_day, datetime.time.max, tzinfo=local_tz)
    return (target_day - local_now.date()).days, int((target_end - local_now).total_seconds())


def _optional_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _compact_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")
