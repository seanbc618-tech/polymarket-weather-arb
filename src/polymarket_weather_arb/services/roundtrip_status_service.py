from __future__ import annotations

from dataclasses import dataclass
from sqlite3 import Row

from polymarket_weather_arb.services.live_monitor_service import is_fresh_reconciliation
from polymarket_weather_arb.storage.repositories import Repository


@dataclass
class RoundtripStatusResult:
    market_id: str
    stage: str
    reconciliation_fresh: bool
    latest_reconciliation: Row | None
    buy_intents: list[Row]
    sell_intents: list[Row]
    open_orders: list[Row]
    positions: list[Row]
    buy_fills: list[Row]
    sell_fills: list[Row]


class RoundtripStatusService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def get_status(self, market_id: str) -> RoundtripStatusResult:
        intents = self.repository.list_recent_order_intents(limit=100, market_id=market_id)
        open_orders = self.repository.list_open_orders(limit=100, market_id=market_id)
        positions = self.repository.list_positions(
            limit=100, market_id=market_id, nonzero_only=False
        )
        fills = self.repository.list_fills(limit=100, market_id=market_id)

        latest_recon = self.repository.latest_successful_reconciliation()
        recon_fresh = is_fresh_reconciliation(latest_recon)

        buy_intents = [i for i in intents if str(i["side"]).lower().startswith("buy")]
        sell_intents = [i for i in intents if str(i["side"]).lower().startswith("sell")]

        buy_fills = [f for f in fills if str(dict(f).get("side", "")).lower().startswith("buy")]
        sell_fills = [f for f in fills if str(dict(f).get("side", "")).lower().startswith("sell")]

        run = self.repository.get_active_roundtrip_run(market_id)
        buy_intent_status = None
        if run is not None and run["buy_intent_id"] is not None:
            buy_intent_status = next(
                (
                    str(intent["status"]).lower()
                    for intent in buy_intents
                    if int(intent["id"]) == int(run["buy_intent_id"])
                ),
                None,
            )

        stage = self._infer_stage(
            market_id=market_id,
            run=run,
            buy_intent_status=buy_intent_status,
            reconciliation_fresh=recon_fresh,
            open_orders=open_orders,
            positions=positions,
            fills=fills,
        )
        if (
            run is not None
            and stage in {"completed", "failed"}
            and str(run["status"] if "status" in run.keys() else "") != stage
        ):
            run_id = run["id"] if "id" in run.keys() else None
            if run_id is not None:
                self.repository.update_roundtrip_run_status(int(run_id), stage)

        return RoundtripStatusResult(
            market_id=market_id,
            stage=stage,
            reconciliation_fresh=recon_fresh,
            latest_reconciliation=latest_recon,
            buy_intents=buy_intents,
            sell_intents=sell_intents,
            open_orders=open_orders,
            positions=positions,
            buy_fills=buy_fills,
            sell_fills=sell_fills,
        )

    def _infer_stage(
        self,
        market_id: str,
        run: Row | None,
        buy_intent_status: str | None,
        reconciliation_fresh: bool,
        open_orders: list[Row],
        positions: list[Row],
        fills: list[Row],
    ) -> str:
        if not run:
            return "ready_to_buy"

        open_buy = any(str(o["side"]).lower().startswith("buy") for o in open_orders)
        open_sell = any(str(o["side"]).lower().startswith("sell") for o in open_orders)
        has_pos = any(float(p["size"]) > 0 for p in positions)

        buy_intent_id = run["buy_intent_id"]
        sell_intent_id = run["sell_intent_id"]

        # Check if the specific buy intent generated fills
        buy_filled = False
        if buy_intent_id:
            buy_order_ids = self.repository.get_order_ids_for_intent(buy_intent_id)
            buy_filled = any(f["order_id"] in buy_order_ids for f in fills)
            # In dry-run rehearsals, we might not have exchange_fill_id or order_ids match easily
            # But the user mentioned checking specific orders
            # Wait, if dry run, attempts have no orderID. How to mock fills for rehearsal?
            # Rehearsals use mock_client, which might or might not insert orderID.

        sell_filled = False
        if sell_intent_id:
            sell_order_ids = self.repository.get_order_ids_for_intent(sell_intent_id)
            sell_filled = any(f["order_id"] in sell_order_ids for f in fills)

        if (
            reconciliation_fresh
            and not open_buy
            and not open_sell
            and not has_pos
            and buy_filled
            and sell_filled
        ):
            return "completed"

        if open_buy:
            return "buy_open"
        if open_sell:
            return "sell_open"

        if has_pos:
            if sell_intent_id:
                return "ready_to_sell"
            return "position_confirmed"

        if buy_intent_id:
            if not buy_filled:
                if buy_intent_status in {
                    "failed",
                    "cancelled",
                    "canceled",
                    "rejected",
                    "cancel_failed",
                }:
                    return "failed"
                return "buy_unverified"

        return "ready_to_buy"
