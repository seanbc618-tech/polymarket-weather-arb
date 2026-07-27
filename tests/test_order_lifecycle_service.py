from __future__ import annotations

from polymarket_weather_arb.domain.markets import Market
from polymarket_weather_arb.services.order_lifecycle_service import OrderLifecycleService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


class FakeClient:
    def __init__(self):
        self.cancelled: list[str] = []
        self.orders = [
            {
                "id": "order-1",
                "market": "m1",
                "asset_id": "yes-token",
                "side": "BUY",
                "price": "0.25",
                "size": "10",
                "status": "live",
            }
        ]

    def get_orders(self):
        return self.orders

    def get_order(self, order_id: str):
        return {**self.orders[0], "id": order_id, "status": "live"}

    def cancel_order(self, order_id: str):
        self.cancelled.append(order_id)
        return {"id": order_id, "status": "cancelled"}


def test_order_lifecycle_refreshes_open_orders(tmp_path):
    _, connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo)
        count = OrderLifecycleService(FakeClient(), repo).refresh_open_orders()

        rows = repo.list_open_orders()
        assert count == 1
        assert rows[0]["exchange_order_id"] == "order-1"
        assert rows[0]["status"] == "live"
    finally:
        connection.close()


def test_order_lifecycle_cancels_and_marks_local_order(tmp_path):
    _, connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo)
        client = FakeClient()
        service = OrderLifecycleService(client, repo)
        service.refresh_open_orders()

        response = service.cancel_order("order-1")

        assert response["status"] == "cancelled"
        assert client.cancelled == ["order-1"]
        assert repo.get_open_order("order-1")["status"] == "cancelled"
    finally:
        connection.close()


def test_order_lifecycle_cancels_stale_orders_only(tmp_path):
    from datetime import datetime, timedelta, timezone

    _, connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo)
        now = datetime.now(timezone.utc)
        stale_time = now - timedelta(seconds=600)
        fresh_time = now - timedelta(seconds=60)
        repo.connection.execute(
            """
            INSERT INTO open_orders (
                exchange_order_id, market_id, side, price, size, notional,
                status, updated_at, raw_payload
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("order-stale", "m1", "buy", 0.5, 10, 5, "open", stale_time.isoformat(), "{}"),
        )
        repo.connection.execute(
            """
            INSERT INTO open_orders (
                exchange_order_id, market_id, side, price, size, notional,
                status, updated_at, raw_payload
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("order-fresh", "m1", "sell", 0.6, 20, 12, "open", fresh_time.isoformat(), "{}"),
        )
        connection.commit()
        client = FakeClient()

        result = OrderLifecycleService(client, repo).cancel_stale_orders()

        assert [item["id"] for item in result.cancelled] == ["order-stale"]
        assert result.failures == []
        assert client.cancelled == ["order-stale"]
        assert repo.get_open_order("order-stale")["status"] == "cancelled"
        assert repo.get_open_order("order-fresh")["status"] == "open"
    finally:
        connection.close()


def test_order_lifecycle_cancels_all_open_orders(tmp_path):
    _, connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo)
        client = FakeClient()
        service = OrderLifecycleService(client, repo)
        service.refresh_open_orders()

        cancelled = service.cancel_all_open_orders()

        assert [item["id"] for item in cancelled] == ["order-1"]
        assert client.cancelled == ["order-1"]
        assert repo.get_open_order("order-1")["status"] == "cancelled"
    finally:
        connection.close()


def _repo(tmp_path):
    database = Database(tmp_path / "orders.db")
    database.init_schema()
    connection = database.connect()
    return database, connection, Repository(connection)


def _seed_market(repo: Repository) -> None:
    repo.upsert_market(
        Market(
            id="m1",
            slug="m1",
            title="Will NYC high temperature exceed 80F?",
            description="NOAA station KNYC",
            yes_token_id="yes-token",
            no_token_id="no-token",
            status="active",
            is_weather=True,
        ),
        {"id": "m1"},
    )


def test_detect_stale_orders(tmp_path):
    """测试检测过期挂单"""
    from datetime import datetime, timedelta, timezone

    _, connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo)
        client = FakeClient()
        service = OrderLifecycleService(client, repo)

        # 插入一个过期的挂单
        now = datetime.now(timezone.utc)
        stale_time = now - timedelta(seconds=600)  # 10 分钟前
        repo.connection.execute(
            """
            INSERT INTO open_orders (exchange_order_id, market_id, side, price, size, notional, status, updated_at, raw_payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("order-1", "m1", "buy", 0.5, 10, 5, "open", stale_time.isoformat(), "{}"),
        )

        # 插入一个新鲜的挂单
        fresh_time = now - timedelta(seconds=60)  # 1 分钟前
        repo.connection.execute(
            """
            INSERT INTO open_orders (exchange_order_id, market_id, side, price, size, notional, status, updated_at, raw_payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("order-2", "m1", "sell", 0.6, 20, 12, "open", fresh_time.isoformat(), "{}"),
        )
        connection.commit()

        # 检测过期挂单（阈值 300 秒）
        stale_orders = service.detect_stale_orders(stale_threshold_seconds=300)

        assert len(stale_orders) == 1
        assert stale_orders[0]["exchange_order_id"] == "order-1"
        assert stale_orders[0]["is_stale"] is True
        assert stale_orders[0]["age_seconds"] > 300
    finally:
        connection.close()


def test_get_position_exposure(tmp_path):
    """测试获取持仓敞口"""
    from datetime import datetime, timezone

    _, connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo)
        client = FakeClient()
        service = OrderLifecycleService(client, repo)

        # 插入持仓数据
        now = datetime.now(timezone.utc)
        repo.connection.execute(
            """
            INSERT INTO positions (market_id, outcome, size, notional, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("m1", "YES", 10, 5.0, now.isoformat()),
        )
        repo.connection.execute(
            """
            INSERT INTO positions (market_id, outcome, size, notional, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("m1", "NO", 0, 0, now.isoformat()),
        )
        connection.commit()

        exposure = service.get_position_exposure()

        assert exposure["total_positions"] == 2
        assert exposure["nonzero_positions"] == 1
        assert exposure["total_exposure"] == 5.0
        assert "m1" in exposure["market_exposures"]
    finally:
        connection.close()


def test_get_fill_summary(tmp_path):
    """测试获取成交摘要"""
    from datetime import datetime, timedelta, timezone

    _, connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo)
        client = FakeClient()
        service = OrderLifecycleService(client, repo)

        # 插入成交数据
        now = datetime.now(timezone.utc)
        recent_time = now - timedelta(days=3)
        old_time = now - timedelta(days=10)

        repo.connection.execute(
            """
            INSERT INTO fills (exchange_fill_id, order_id, market_id, side, price, size, fee, filled_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("fill-1", "order-1", "m1", "buy", 0.5, 10, 0.1, recent_time.isoformat()),
        )
        repo.connection.execute(
            """
            INSERT INTO fills (exchange_fill_id, order_id, market_id, side, price, size, fee, filled_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("fill-2", "order-2", "m1", "sell", 0.6, 20, 0.2, recent_time.isoformat()),
        )
        repo.connection.execute(
            """
            INSERT INTO fills (exchange_fill_id, order_id, market_id, side, price, size, fee, filled_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("fill-3", "order-3", "m1", "buy", 0.7, 30, 0.3, old_time.isoformat()),
        )
        connection.commit()

        # 获取最近 7 天的成交摘要
        summary = service.get_fill_summary(days=7)

        assert summary["period_days"] == 7
        assert summary["total_fills"] == 2
        assert summary["total_volume"] == 30.0
        assert abs(summary["total_fees"] - 0.3) < 0.001  # 浮点数精度
    finally:
        connection.close()


def test_get_order_statistics_counts_total_stale_and_notional(tmp_path):
    """测试订单统计"""
    from datetime import datetime, timedelta, timezone

    _, connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo)
        client = FakeClient()
        service = OrderLifecycleService(client, repo)

        # 插入订单数据
        now = datetime.now(timezone.utc)
        stale_time = now - timedelta(seconds=600)
        fresh_time = now - timedelta(seconds=60)

        repo.connection.execute(
            """
            INSERT INTO open_orders (exchange_order_id, market_id, side, price, size, notional, status, updated_at, raw_payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("order-stale", "m1", "buy", 0.5, 10, 5, "open", stale_time.isoformat(), "{}"),
        )
        repo.connection.execute(
            """
            INSERT INTO open_orders (exchange_order_id, market_id, side, price, size, notional, status, updated_at, raw_payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("order-fresh", "m1", "sell", 0.6, 20, 12, "open", fresh_time.isoformat(), "{}"),
        )
        connection.commit()

        stats = service.get_order_statistics()

        assert stats["total_orders"] == 2
        assert stats["stale_orders"] == 1
        assert stats["total_notional"] == 17.0
    finally:
        connection.close()


def test_get_position_risk_summary_empty(tmp_path):
    """测试空持仓风险摘要"""
    _, connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo)
        client = FakeClient()
        service = OrderLifecycleService(client, repo)

        summary = service.get_position_risk_summary()

        assert summary["total_positions"] == 0
        assert summary["nonzero_positions"] == 0
        assert summary["total_exposure"] == 0
        assert summary["max_market_exposure"] == 0
        assert summary["concentration_risk"] == 0
        assert summary["market_exposures"] == {}
    finally:
        connection.close()


def test_get_position_risk_summary_counts_negative_size_as_nonzero(tmp_path):
    """测试负 size 也被计为非零"""
    from datetime import datetime, timezone

    _, connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo)
        client = FakeClient()
        service = OrderLifecycleService(client, repo)

        now = datetime.now(timezone.utc)
        # 正 size
        repo.connection.execute(
            """
            INSERT INTO positions (market_id, outcome, size, notional, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("m1", "YES", 10, 5.0, now.isoformat()),
        )
        # 负 size
        repo.connection.execute(
            """
            INSERT INTO positions (market_id, outcome, size, notional, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("m1", "NO", -5, -2.5, now.isoformat()),
        )
        # 零 size
        repo.connection.execute(
            """
            INSERT INTO positions (market_id, outcome, size, notional, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("m1", "OTHER", 0, 0, now.isoformat()),
        )
        connection.commit()

        summary = service.get_position_risk_summary()

        assert summary["total_positions"] == 3
        assert summary["nonzero_positions"] == 2
        assert summary["total_exposure"] == 7.5  # abs(5.0) + abs(-2.5)
    finally:
        connection.close()


def test_get_position_risk_summary_concentration(tmp_path):
    """测试集中度风险计算"""
    from datetime import datetime, timezone

    _, connection, repo = _repo(tmp_path)
    try:
        _seed_market(repo)

        # 添加第二个 market
        repo.upsert_market(
            Market(
                id="m2",
                slug="m2",
                title="Will LA high temperature exceed 85F?",
                description="NOAA station KLAX",
                yes_token_id="yes-token-2",
                no_token_id="no-token-2",
                status="active",
                is_weather=True,
            ),
            {"id": "m2"},
        )

        client = FakeClient()
        service = OrderLifecycleService(client, repo)

        now = datetime.now(timezone.utc)
        # m1 持仓
        repo.connection.execute(
            """
            INSERT INTO positions (market_id, outcome, size, notional, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("m1", "YES", 10, 8.0, now.isoformat()),
        )
        # m2 持仓
        repo.connection.execute(
            """
            INSERT INTO positions (market_id, outcome, size, notional, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("m2", "YES", 5, 2.0, now.isoformat()),
        )
        connection.commit()

        summary = service.get_position_risk_summary()

        assert summary["total_positions"] == 2
        assert summary["nonzero_positions"] == 2
        assert summary["total_exposure"] == 10.0
        assert summary["max_market_exposure"] == 8.0
        assert summary["concentration_risk"] == 0.8  # 8.0 / 10.0
        assert "m1" in summary["market_exposures"]
        assert "m2" in summary["market_exposures"]
        assert summary["market_exposures"]["m1"] == 8.0
        assert summary["market_exposures"]["m2"] == 2.0
    finally:
        connection.close()
