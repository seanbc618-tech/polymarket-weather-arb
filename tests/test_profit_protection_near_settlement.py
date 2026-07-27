"""Near-settlement and timezone boundaries for the exit ladder."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from polymarket_weather_arb.domain.markets import Market
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.services.exit_guardian_service import ExitGuardianService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


def _repo(tmp_path):
    db = Database(tmp_path / "ns.db")
    db.init_schema()
    conn = db.connect()
    return conn, Repository(conn)


def test_unknown_timezone_skips_near_settlement(tmp_path):
    conn, repo = _repo(tmp_path)
    repo.upsert_market(
        Market(
            id="m-unk",
            title="Will the highest temperature in Unknownville be 80F on July 1, 2020?",
            yes_token_id="y",
            no_token_id="n",
            is_weather=True,
            close_time="2099-12-31T00:00:00+00:00",
            status="active",
        ),
        {
            "id": "m-unk",
            "closed": False,
            "acceptingOrders": True,
            "endDate": "2099-12-31T00:00:00+00:00",
        },
    )
    repo.connection.execute(
        "INSERT INTO positions (market_id, outcome, size, notional) VALUES ('m-unk','YES',10,1)"
    )
    repo.save_analysis(
        Analysis(
            market_id="m-unk",
            model_version="t",
            fair_lower=Decimal("0.7"),
            fair_upper=Decimal("0.8"),
            reference_price=Decimal("0.5"),
            edge=Decimal("0.2"),
            side="buy_yes",
            decision="trade",
            reasons=["ok"],
        )
    )
    rec = [r for r in ExitGuardianService(repo).evaluate() if r.kind == "position"][0]
    assert rec.policy_stage != "near_settlement"
    assert rec.action in {"hold_for_resolution", "review_no_analysis"}
    conn.close()


def test_chicago_near_end_of_day_can_hold_for_resolution(tmp_path):
    conn, repo = _repo(tmp_path)
    # Local Chicago evening of the event day
    event = "July 15, 2026"
    repo.upsert_market(
        Market(
            id="m-chi",
            title=f"Will the highest temperature in Chicago be 80F or higher on {event}?",
            description="NOAA station KORD reports the high temperature for the day.",
            yes_token_id="y",
            no_token_id="n",
            is_weather=True,
            close_time="2026-07-16T05:00:00+00:00",
            status="active",
        ),
        {
            "id": "m-chi",
            "closed": False,
            "acceptingOrders": True,
            "timezone": "America/Chicago",
            "endDate": "2026-07-16T05:00:00+00:00",
            "feesEnabled": True,
            "feeType": "weather_fees",
        },
    )
    repo.connection.execute(
        "INSERT INTO positions (market_id, outcome, size, notional) VALUES ('m-chi','YES',10,2)"
    )
    repo.connection.execute(
        """
        INSERT INTO fills (exchange_fill_id, market_id, side, price, size, fee, filled_at, raw_payload)
        VALUES ('b', 'm-chi', 'BUY', 0.2, 10, 0, '2026-07-15T12:00:00+00:00', '{"outcome":"YES"}')
        """
    )
    # 22:00 America/Chicago on event day
    now = datetime(2026, 7, 16, 3, 0, tzinfo=timezone.utc)  # 22:00 CDT
    assert now.astimezone(ZoneInfo("America/Chicago")).hour == 22
    repo.save_analysis(
        Analysis(
            market_id="m-chi",
            model_version="t",
            fair_lower=Decimal("0.85"),
            fair_upper=Decimal("0.95"),
            reference_price=Decimal("0.40"),
            edge=Decimal("0.40"),
            side="buy_yes",
            decision="trade",
            reasons=["strong"],
            created_at=now,
        )
    )
    rec = [
        r
        for r in ExitGuardianService(repo).evaluate(
            min_edge=Decimal("0.05"),
            best_bids={("m-chi", "YES"): Decimal("0.40")},
            now=now,
        )
        if r.kind == "position"
    ][0]
    assert rec.policy_stage == "near_settlement"
    assert rec.action == "hold_for_resolution"
    conn.close()
