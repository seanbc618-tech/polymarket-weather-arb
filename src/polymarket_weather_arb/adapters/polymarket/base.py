from __future__ import annotations

from typing import Any, Callable, Protocol

from polymarket_weather_arb.domain.markets import Market, MarketSnapshot


class PolymarketClient(Protocol):
    def list_markets(
        self, limit: int = 100, offset: int = 0
    ) -> list[tuple[Market, dict[str, Any]]]: ...

    def find_markets_by_condition_ids(
        self, condition_ids: list[str]
    ) -> list[tuple[Market, dict[str, Any]]]: ...

    def get_order_book(self, market: Market) -> tuple[MarketSnapshot, dict[str, Any]]: ...

    def get_token_order_book(self, token_id: str) -> tuple[MarketSnapshot, dict[str, Any]]: ...

    def place_limit_order(
        self,
        *,
        token_id: str,
        side: str,
        price: str,
        size: str,
    ) -> dict[str, Any]: ...

    def place_sell_limit_order(
        self,
        *,
        token_id: str,
        price: str,
        size: str,
    ) -> dict[str, Any]: ...

    def get_balances(self) -> dict[str, Any]: ...

    def get_positions(self) -> list[dict[str, Any]]: ...

    def get_orders(self) -> list[dict[str, Any]]: ...

    def get_order(self, order_id: str) -> dict[str, Any]: ...

    def cancel_order(self, order_id: str) -> dict[str, Any]: ...

    def validate_redemption_signing(self) -> dict[str, Any]: ...

    def redeem_positions(
        self,
        *,
        condition_id: str,
        on_submitted: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]: ...

    def get_trades(self) -> list[dict[str, Any]]: ...

    def validate_order_signing(self) -> dict[str, Any]: ...
