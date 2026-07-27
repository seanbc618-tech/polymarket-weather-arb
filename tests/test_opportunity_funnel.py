from datetime import datetime, timedelta, timezone

from polymarket_weather_arb.services.cockpit_service import _build_opportunity_funnel
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository
from polymarket_weather_arb.config import Settings


def test_opportunity_funnel_empty(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "cockpit.db")
    Database(settings.database_path).init_schema()
    db = Database(settings.database_path)
    with db.transaction() as conn:
        repository = Repository(conn)
        conn.execute("INSERT INTO markets (id, title, raw_payload) VALUES ('m1', 'T', '{}')")
        funnel = _build_opportunity_funnel(repository)
        assert funnel.discovered == 0
        assert funnel.blockers == []


def test_opportunity_funnel_stages(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "cockpit.db")
    Database(settings.database_path).init_schema()
    db = Database(settings.database_path)
    with db.transaction() as conn:
        repository = Repository(conn)
        for i in range(1, 9):
            conn.execute(
                f"INSERT INTO markets (id, title, raw_payload) VALUES ('m{i}', 'T', '{{}}')"
            )

        # 1. discovered (non-tradable)
        conn.execute(
            "INSERT INTO market_candidates (market_id, status, tradable, rejection_reason) VALUES ('m1', 'discovered', 0, 'bad rule')"
        )

        # 2. rule_tradable (tradable but no quote)
        conn.execute(
            "INSERT INTO market_candidates (market_id, status, tradable) VALUES ('m2', 'tradable', 1)"
        )

        # 3. quote_available (has quote, no forecast)
        conn.execute(
            "INSERT INTO market_candidates (market_id, status, tradable, best_bid, best_ask) VALUES ('m3', 'quoted', 1, 0.5, 0.6)"
        )

        # 4. forecast_available (has forecast, no analysis)
        conn.execute(
            "INSERT INTO market_candidates (market_id, status, tradable, best_bid, best_ask) VALUES ('m4', 'forecast', 1, 0.5, 0.6)"
        )
        conn.execute(
            "INSERT INTO weather_forecasts (market_id, provider, issue_time, valid_time, variable, value, unit, raw_payload) VALUES ('m4', 'open-meteo', datetime('now'), datetime('now'), 'temp', 10, 'C', '{}')"
        )

        # 5. analyzed (decision skip)
        conn.execute(
            "INSERT INTO market_candidates (market_id, status, tradable, best_bid, best_ask) VALUES ('m5', 'analyzed', 1, 0.5, 0.6)"
        )
        conn.execute(
            "INSERT INTO weather_forecasts (market_id, provider, issue_time, valid_time, variable, value, unit, raw_payload) VALUES ('m5', 'open-meteo', datetime('now'), datetime('now'), 'temp', 10, 'C', '{}')"
        )
        conn.execute(
            "INSERT INTO analyses (market_id, model_version, fair_lower, fair_upper, edge, decision, reasons) VALUES ('m5', 'v1', 0, 1, 0, 'reject', 'no edge')"
        )

        # 6. quant_trade_signal (decision trade, but no live intent)
        conn.execute(
            "INSERT INTO market_candidates (market_id, status, tradable, best_bid, best_ask) VALUES ('m6', 'signal', 1, 0.5, 0.6)"
        )
        conn.execute(
            "INSERT INTO weather_forecasts (market_id, provider, issue_time, valid_time, variable, value, unit, raw_payload) VALUES ('m6', 'open-meteo', datetime('now'), datetime('now'), 'temp', 10, 'C', '{}')"
        )
        conn.execute(
            "INSERT INTO analyses (market_id, model_version, fair_lower, fair_upper, edge, decision, reasons) VALUES ('m6', 'v1', 0, 1, 0.1, 'trade', 'looks good')"
        )

        # 7. live_submitted (decision trade, live intent, no fill)
        conn.execute(
            "INSERT INTO market_candidates (market_id, status, tradable, best_bid, best_ask) VALUES ('m7', 'submitted', 1, 0.5, 0.6)"
        )
        conn.execute(
            "INSERT INTO weather_forecasts (market_id, provider, issue_time, valid_time, variable, value, unit, raw_payload) VALUES ('m7', 'open-meteo', datetime('now'), datetime('now'), 'temp', 10, 'C', '{}')"
        )
        conn.execute(
            "INSERT INTO analyses (market_id, model_version, fair_lower, fair_upper, edge, decision, reasons) VALUES ('m7', 'v1', 0, 1, 0.1, 'trade', 'looks good')"
        )
        conn.execute(
            "INSERT INTO order_intents (market_id, side, limit_price, size, notional, rationale, dry_run, status) VALUES ('m7', 'BUY', 0.5, 10, 5, 'ok', 0, 'open')"
        )

        # 8. exchange_fill
        conn.execute(
            "INSERT INTO market_candidates (market_id, status, tradable, best_bid, best_ask) VALUES ('m8', 'filled', 1, 0.5, 0.6)"
        )
        conn.execute(
            "INSERT INTO weather_forecasts (market_id, provider, issue_time, valid_time, variable, value, unit, raw_payload) VALUES ('m8', 'open-meteo', datetime('now'), datetime('now'), 'temp', 10, 'C', '{}')"
        )
        conn.execute(
            "INSERT INTO analyses (market_id, model_version, fair_lower, fair_upper, edge, decision, reasons) VALUES ('m8', 'v1', 0, 1, 0.1, 'buy', 'looks good')"
        )
        conn.execute(
            "INSERT INTO order_intents (market_id, side, limit_price, size, notional, rationale, dry_run, status) VALUES ('m8', 'BUY', 0.5, 10, 5, 'ok', 0, 'filled')"
        )
        conn.execute(
            "INSERT INTO fills (exchange_fill_id, market_id, side, price, size, fee, filled_at) VALUES ('f1', 'm8', 'BUY', 0.5, 10, 0, datetime('now'))"
        )

        funnel = _build_opportunity_funnel(repository)
        assert funnel.discovered == 8
        assert funnel.rule_tradable == 7
        assert funnel.quote_available == 6
        assert funnel.forecast_available == 5
        assert funnel.analyzed == 4
        assert funnel.quant_trade_signal == 3
        assert funnel.live_submitted == 2
        assert funnel.exchange_fill == 1

        blocker_dict = {b.reason: b.count for b in funnel.blockers}
        assert blocker_dict["bad rule"] == 1
        assert blocker_dict["missing quote"] == 1
        assert blocker_dict["missing forecast"] == 1
        assert blocker_dict["missing analysis"] == 1
        assert blocker_dict["no edge"] == 1
        assert blocker_dict["live gate"] == 1
        assert blocker_dict["missing fill"] == 1


def test_opportunity_funnel_parses_iso_timestamps_before_cutoff(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "cockpit.db")
    Database(settings.database_path).init_schema()
    stale_iso = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()

    db = Database(settings.database_path)
    with db.transaction() as conn:
        repository = Repository(conn)
        conn.execute("INSERT INTO markets (id, title, raw_payload) VALUES ('m1', 'T', '{}')")
        conn.execute(
            """
            INSERT INTO market_candidates
                (market_id, status, tradable, best_bid, best_ask)
            VALUES ('m1', 'quoted', 1, 0.5, 0.6)
            """
        )
        conn.execute(
            """
            INSERT INTO weather_forecasts
                (market_id, provider, issue_time, valid_time, variable, value,
                 unit, raw_payload, fetched_at)
            VALUES ('m1', 'open-meteo', ?, ?, 'temp', 10, 'C', '{}', ?)
            """,
            (stale_iso, stale_iso, stale_iso),
        )

        funnel = _build_opportunity_funnel(repository)

        assert funnel.discovered == 1
        assert funnel.quote_available == 1
        assert funnel.forecast_available == 0
        assert {blocker.reason: blocker.count for blocker in funnel.blockers}[
            "missing forecast"
        ] == 1
