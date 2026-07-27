"""Verified inventory campaign accounting for profit-protection exits.

Pure domain math over reconciled fills. Ownership: used by ExitGuardianService
only. No I/O, no strategy engine.

Fill matching prioritizes the account leg (``raw_payload._account_fill``) and
intent→exchange order ID linkage, matching live Polymarket maker/taker trade
payloads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_UP
from typing import Any, Iterable, Mapping

from polymarket_weather_arb.domain.fees import (
    WEATHER_TAKER_FEE_RATE,
    compute_taker_fee,
    expected_taker_fee_per_share,
)
from polymarket_weather_arb.domain.order_constraints import (
    DEFAULT_SIZE_STEP,
    OrderConstraints,
    residual_is_dust,
)

SIZE_TOLERANCE = Decimal("0.0001")


@dataclass(frozen=True)
class CampaignInventory:
    market_id: str
    outcome: str
    verified_buy_cost: Decimal
    verified_sell_proceeds: Decimal
    unrecovered_cash: Decimal
    buy_size: Decimal
    sell_size: Decimal
    net_fill_size: Decimal
    position_size: Decimal
    accounting_verified: bool
    evidence_reason: str
    campaign_fill_count: int


@dataclass(frozen=True)
class PrincipalRecoveryPlan:
    size: Decimal
    price: Decimal
    expected_fee: Decimal
    expected_net_proceeds: Decimal
    runner_size_after: Decimal
    recovers_principal: bool
    reason: str


def build_campaign_inventory(
    fills: Iterable[Mapping[str, Any]],
    *,
    market_id: str,
    outcome: str,
    position_size: Decimal,
    yes_token_id: str | None = None,
    no_token_id: str | None = None,
    order_token_ids: Mapping[str, str] | None = None,
    size_tolerance: Decimal = SIZE_TOLERANCE,
) -> CampaignInventory:
    """Derive current inventory campaign after the last zero-position crossing.

    ``order_token_ids`` maps exchange order_id → token_id from local intents so
    maker legs without top-level outcome still match via intent linkage.
    """
    outcome_norm = _normalize_outcome(outcome)
    if outcome_norm is None:
        return CampaignInventory(
            market_id=market_id,
            outcome=str(outcome),
            verified_buy_cost=Decimal("0"),
            verified_sell_proceeds=Decimal("0"),
            unrecovered_cash=Decimal("0"),
            buy_size=Decimal("0"),
            sell_size=Decimal("0"),
            net_fill_size=Decimal("0"),
            position_size=position_size,
            accounting_verified=False,
            evidence_reason="unsupported outcome for inventory accounting",
            campaign_fill_count=0,
        )

    target_token = yes_token_id if outcome_norm == "YES" else no_token_id
    order_tokens = {str(k): str(v) for k, v in (order_token_ids or {}).items()}
    chron = sorted(
        list(fills),
        key=lambda f: (
            str(_account_field(f, "filled_at") or f.get("filled_at") or ""),
            int(f.get("id") or 0) if str(f.get("id") or "").isdigit() else 0,
        ),
    )
    matched: list[tuple[str, Decimal, Decimal, Decimal]] = []
    for fill in chron:
        view = account_fill_view(fill)
        if not _fill_matches_outcome(view, outcome_norm, target_token, order_tokens):
            continue
        side = _fill_side(view)
        if side is None:
            continue
        size = _dec(view.get("size") or view.get("quantity") or view.get("matched_amount"))
        price = _dec(view.get("price"))
        fee = _dec(view.get("fee") or fill.get("fee") or 0)
        if size is None or price is None or size <= 0 or price < 0:
            continue
        matched.append((side, size, price, fee if fee is not None else Decimal("0")))

    campaigns: list[list[tuple[str, Decimal, Decimal, Decimal]]] = []
    current: list[tuple[str, Decimal, Decimal, Decimal]] = []
    net = Decimal("0")
    for item in matched:
        side, size, price, fee = item
        delta = size if side == "BUY" else -size
        prev = net
        net = net + delta
        if not current and side == "SELL":
            continue
        current.append(item)
        if net <= size_tolerance and prev > size_tolerance:
            campaigns.append(current)
            current = []
            net = Decimal("0") if abs(net) <= size_tolerance else net
    if current:
        campaigns.append(current)

    campaign = campaigns[-1] if campaigns else []
    buy_cost = Decimal("0")
    sell_proceeds = Decimal("0")
    buy_size = Decimal("0")
    sell_size = Decimal("0")
    for side, size, price, fee in campaign:
        if side == "BUY":
            buy_cost += size * price + fee
            buy_size += size
        else:
            sell_proceeds += size * price - fee
            sell_size += size

    net_fill = buy_size - sell_size
    unrecovered = max(Decimal("0"), buy_cost - sell_proceeds)
    pos = abs(position_size)
    verified = True
    reason = "campaign accounting verified from reconciled fills"
    if not campaign and pos > size_tolerance:
        verified = False
        reason = "no matched fills for current position outcome; accounting unverified"
    elif abs(net_fill - pos) > size_tolerance:
        verified = False
        reason = (
            f"net fill size {net_fill} disagrees with position {pos} "
            f"(tolerance {size_tolerance}); accounting unverified"
        )
    elif not campaign:
        reason = "flat inventory; no open campaign"

    return CampaignInventory(
        market_id=market_id,
        outcome=outcome_norm,
        verified_buy_cost=buy_cost,
        verified_sell_proceeds=sell_proceeds,
        unrecovered_cash=unrecovered,
        buy_size=buy_size,
        sell_size=sell_size,
        net_fill_size=net_fill,
        position_size=pos,
        accounting_verified=verified,
        evidence_reason=reason,
        campaign_fill_count=len(campaign),
    )


def plan_principal_recovery(
    *,
    unrecovered_cash: Decimal,
    position_size: Decimal,
    best_bid: Decimal,
    fee_rate: Decimal = WEATHER_TAKER_FEE_RATE,
    constraints: OrderConstraints | None = None,
    available_depth: Decimal | None = None,
) -> PrincipalRecoveryPlan | None:
    """Minimum size to recover unrecovered_cash after expected taker fee at best_bid."""
    if unrecovered_cash <= 0 or position_size <= 0 or best_bid <= 0:
        return None
    fee_ps = expected_taker_fee_per_share(price=best_bid, fee_rate=fee_rate)
    net_per_share = best_bid - fee_ps
    if net_per_share <= 0:
        return None
    step = constraints.size_step if constraints else DEFAULT_SIZE_STEP
    min_size = constraints.min_size if constraints and constraints.min_size else None
    raw_q = (unrecovered_cash / net_per_share).quantize(step, rounding=ROUND_UP)
    if min_size is not None and raw_q < min_size:
        raw_q = min_size.quantize(step, rounding=ROUND_UP)
    if available_depth is not None and available_depth > 0:
        depth_cap = available_depth.quantize(step, rounding=ROUND_UP)
        if depth_cap < raw_q:
            fee_d = compute_taker_fee(shares=depth_cap, price=best_bid, fee_rate=fee_rate)
            net_d = depth_cap * best_bid - fee_d
            if net_d + Decimal("0.00001") < unrecovered_cash:
                return PrincipalRecoveryPlan(
                    size=depth_cap,
                    price=best_bid,
                    expected_fee=fee_d,
                    expected_net_proceeds=net_d,
                    runner_size_after=position_size - depth_cap,
                    recovers_principal=False,
                    reason="available bid depth cannot recover principal",
                )
            raw_q = depth_cap
    if raw_q > position_size:
        raw_q = position_size.quantize(step, rounding=ROUND_UP)
        if raw_q > position_size:
            raw_q = position_size
    fee = compute_taker_fee(shares=raw_q, price=best_bid, fee_rate=fee_rate)
    net = raw_q * best_bid - fee
    while net + Decimal("0.00001") < unrecovered_cash and raw_q < position_size:
        raw_q = (raw_q + step).quantize(step, rounding=ROUND_UP)
        if raw_q > position_size:
            raw_q = position_size
        fee = compute_taker_fee(shares=raw_q, price=best_bid, fee_rate=fee_rate)
        net = raw_q * best_bid - fee
        if raw_q >= position_size:
            break
    runner = position_size - raw_q
    recovers = net + Decimal("0.00001") >= unrecovered_cash
    if runner > 0:
        is_dust, dust_reason = residual_is_dust(
            residual_size=runner, price=best_bid, constraints=constraints
        )
        if is_dust:
            return PrincipalRecoveryPlan(
                size=raw_q,
                price=best_bid,
                expected_fee=fee,
                expected_net_proceeds=net,
                runner_size_after=runner,
                recovers_principal=False,
                reason=f"principal recovery would create dust: {dust_reason}",
            )
    if not recovers:
        return PrincipalRecoveryPlan(
            size=raw_q,
            price=best_bid,
            expected_fee=fee,
            expected_net_proceeds=net,
            runner_size_after=runner,
            recovers_principal=False,
            reason="cannot recover principal under size/price/fee constraints",
        )
    return PrincipalRecoveryPlan(
        size=raw_q,
        price=best_bid,
        expected_fee=fee,
        expected_net_proceeds=net,
        runner_size_after=runner,
        recovers_principal=True,
        reason="minimum fee-adjusted size recovers verified principal",
    )


def account_fill_view(fill: Mapping[str, Any]) -> dict[str, Any]:
    """Prefer reconciled ``_account_fill`` account leg over top-level trade fields."""
    raw = fill.get("raw_payload")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = None
    base = dict(fill)
    if isinstance(raw, dict):
        base["raw_payload"] = raw
        account = raw.get("_account_fill")
        if isinstance(account, dict):
            # Account leg wins for size/side/price/order_id/token/outcome.
            for key in (
                "order_id",
                "orderId",
                "orderID",
                "side",
                "price",
                "size",
                "quantity",
                "matched_amount",
                "token_id",
                "asset_id",
                "assetId",
                "outcome",
                "fee",
            ):
                if account.get(key) is not None and account.get(key) != "":
                    base[key] = account[key]
    return base


def best_bid_depth_from_book(raw_book: Mapping[str, Any] | None) -> Decimal | None:
    """Size available at the best bid from a CLOB book payload."""
    if not isinstance(raw_book, dict):
        return None
    bids = raw_book.get("bids") or []
    if not isinstance(bids, list) or not bids:
        return None
    best_price: Decimal | None = None
    best_size: Decimal | None = None
    for level in bids:
        if isinstance(level, dict):
            p = _dec(level.get("price"))
            s = _dec(level.get("size") or level.get("quantity"))
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            p = _dec(level[0])
            s = _dec(level[1])
        else:
            continue
        if p is None or s is None:
            continue
        if best_price is None or p > best_price:
            best_price = p
            best_size = s
    return best_size


def _fill_matches_outcome(
    view: Mapping[str, Any],
    outcome: str,
    target_token: str | None,
    order_tokens: Mapping[str, str],
) -> bool:
    # 1) Explicit outcome on account leg / top-level / nested payload
    fill_outcome = _normalize_outcome(
        view.get("outcome")
        or _nested(view, "raw_payload", "_account_fill", "outcome")
        or _nested(view, "raw_payload", "outcome")
    )
    if fill_outcome is not None:
        return fill_outcome == outcome

    # 2) Token id on account leg / top-level
    token = (
        view.get("token_id")
        or view.get("asset_id")
        or view.get("assetId")
        or _nested(view, "raw_payload", "_account_fill", "token_id")
        or _nested(view, "raw_payload", "_account_fill", "asset_id")
        or _nested(view, "raw_payload", "asset_id")
        or _nested(view, "raw_payload", "token_id")
    )
    if target_token and token and str(token) == str(target_token):
        return True

    # 3) Intent → exchange order id linkage (maker/taker without outcome fields)
    order_id = (
        view.get("order_id")
        or view.get("orderId")
        or view.get("orderID")
        or _nested(view, "raw_payload", "_account_fill", "order_id")
        or _nested(view, "raw_payload", "taker_order_id")
        or _nested(view, "raw_payload", "order_id")
    )
    if order_id and target_token and str(order_id) in order_tokens:
        return order_tokens[str(order_id)] == str(target_token)

    return False


def _fill_side(view: Mapping[str, Any]) -> str | None:
    side = str(view.get("side") or "").strip().upper()
    if side in {"BUY", "B"}:
        return "BUY"
    if side in {"SELL", "S"}:
        return "SELL"
    if side.startswith("BUY"):
        return "BUY"
    if side.startswith("SELL"):
        return "SELL"
    return None


def _account_field(fill: Mapping[str, Any], key: str) -> Any:
    view = account_fill_view(fill)
    return view.get(key)


def _normalize_outcome(value: object) -> str | None:
    text = str(value or "").strip().upper()
    if text in {"YES", "Y"}:
        return "YES"
    if text in {"NO", "N"}:
        return "NO"
    return None


def _dec(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _nested(payload: Mapping[str, Any], *keys: str) -> Any:
    cur: Any = payload
    for key in keys:
        if not isinstance(cur, Mapping):
            if isinstance(cur, str):
                try:
                    cur = json.loads(cur)
                except Exception:
                    return None
            else:
                return None
        if not isinstance(cur, Mapping) or key not in cur:
            return None
        cur = cur[key]
    return cur
