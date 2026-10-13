"""Position sizing and pre-trade risk checks. Pure functions, no I/O.

Initial margin = notional / leverage. A margin budget is NOT a maximum loss:
without a stop-loss the loss can be the full allocated margin or more in
adverse conditions (liquidation).

Pi42 constraints honoured here:
* ``quantityPrecision`` / ``pricePrecision`` from ``exchangeInfo`` define the
  quantity and price steps (``10 ** -precision``).
* ``minQty`` / ``maxQty`` (MARKET_QTY_SIZE / LIMIT_QTY_SIZE filters) and the
  ``MIN_NOTIONAL`` filter bound the order size.
* Pi42 declines an order whose take-profit would trigger immediately
  (error 3011), so the target is validated against the live price too.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.models import ContractInfo
from app.numeric import floor_to_step, round_to_step


class RiskRejection(Exception):
    """A pre-trade check failed. The signal is skipped, never adjusted."""


@dataclass(frozen=True)
class SizingResult:
    leverage: Decimal
    budget: Decimal
    raw_quantity: Decimal
    quantity: Decimal
    notional: Decimal
    estimated_margin: Decimal


def compute_budget(max_margin: Decimal, reserve: Decimal) -> Decimal:
    budget = max_margin - reserve
    if budget <= 0:
        raise RiskRejection("effective margin budget is not positive")
    return budget


def size_position(
    *, entry: Decimal, leverage: Decimal, budget: Decimal, contract: ContractInfo
) -> SizingResult:
    """Size a linear contract so the initial margin stays within ``budget``.

    raw_qty = budget * leverage / entry, rounded DOWN to the quantity step.
    The quantity is never increased to satisfy a minimum; if a minimum cannot be
    met inside the budget the signal is rejected.
    """
    if entry <= 0 or leverage <= 0 or budget <= 0:
        raise RiskRejection("entry, leverage and budget must be positive")
    raw_quantity = (budget * leverage) / entry
    quantity = floor_to_step(raw_quantity, contract.quantity_step)
    if quantity <= 0:
        raise RiskRejection(
            f"budget {budget} at {leverage}x cannot buy one quantity step "
            f"({contract.quantity_step}) at entry {entry}"
        )
    if contract.min_qty is not None and quantity < contract.min_qty:
        raise RiskRejection(
            f"quantity {quantity} is below the contract minQty {contract.min_qty} within the margin budget"
        )
    if contract.max_qty is not None and quantity > contract.max_qty:
        raise RiskRejection(f"quantity {quantity} exceeds the contract maxQty {contract.max_qty}")
    notional = quantity * entry
    if contract.min_notional is None:
        # Without a known minimum notional the order could be rejected by Pi42
        # (or worse, accepted at a size we did not intend). Refuse to size.
        raise RiskRejection(
            f"contract {contract.symbol} does not publish a minNotional; refusing to size blind"
        )
    if notional < contract.min_notional:
        raise RiskRejection(
            f"notional {notional} is below the contract minNotional {contract.min_notional} "
            "within the margin budget"
        )
    estimated_margin = notional / leverage
    if estimated_margin > budget:  # defensive: rounding down should make this impossible
        raise RiskRejection(f"estimated margin {estimated_margin} exceeds budget {budget}")
    return SizingResult(
        leverage=leverage,
        budget=budget,
        raw_quantity=raw_quantity,
        quantity=quantity,
        notional=notional,
        estimated_margin=estimated_margin,
    )


def check_price_grid(name: str, value: Decimal, contract: ContractInfo) -> None:
    """Price must sit on the contract's price step. Never rounded."""
    if contract.price_step <= 0:
        raise RiskRejection(f"{name} cannot be validated: contract price precision is unusable")
    if value % contract.price_step != 0:
        raise RiskRejection(f"{name} {value} is not a multiple of the price step {contract.price_step}")


def snap_to_price_grid(name: str, value: Decimal, contract: ContractInfo) -> Decimal:
    """Snap a price onto the contract's price precision (half-up)."""
    if contract.price_step <= 0:
        raise RiskRejection(f"{name} cannot be validated: contract price precision is unusable")
    snapped = round_to_step(value, contract.price_step)
    if snapped <= 0:
        raise RiskRejection(f"{name} {value} snapped to a non-positive price {snapped}")
    return snapped


def check_entry_deviation(entry: Decimal, market_price: Decimal, max_pct: Decimal) -> Decimal:
    """Return the deviation percentage; reject if above the configured maximum."""
    if market_price is None or market_price <= 0:
        raise RiskRejection("no valid live market price for the entry-deviation check")
    deviation = abs(market_price - entry) / entry * Decimal(100)
    if deviation > max_pct:
        raise RiskRejection(
            f"market {market_price} is {deviation:.2f}% away from Entry {entry}; "
            f"limit is {max_pct}% (skipped, limit price not changed)"
        )
    return deviation


def check_target_not_instant(
    *, direction: str, target: Decimal, market_price: Decimal, entry: Decimal
) -> None:
    """Pi42 declines orders whose TP/SL would trigger immediately (error 3011).

    LONG: the take-profit must sit strictly above the live price.
    SHORT: the take-profit must sit strictly below the live price.
    """
    if target is None or target <= 0:
        raise RiskRejection("take-profit target is missing or non-positive")
    if market_price is None or market_price <= 0:
        raise RiskRejection("no valid live market price for the take-profit check")
    if direction == "LONG" and target <= market_price:
        raise RiskRejection(
            f"take-profit {target} is at or below the live price {market_price}; "
            "Pi42 would trigger it instantly (entry {0})".format(entry)
        )
    if direction == "SHORT" and target >= market_price:
        raise RiskRejection(
            f"take-profit {target} is at or above the live price {market_price}; "
            "Pi42 would trigger it instantly (entry {0})".format(entry)
        )


def check_funds(available: Decimal, estimated_margin: Decimal, reserve: Decimal) -> None:
    """Futures-wallet balance must cover the new margin plus the retained reserve."""
    required = estimated_margin + reserve
    if available < required:
        raise RiskRejection(
            f"insufficient futures-wallet balance: have {available}, need {required} "
            f"(margin {estimated_margin} + reserve {reserve}). Transfer funds into the Pi42 "
            "futures wallet; this bot never moves money."
        )


def check_concurrency(active_count: int, max_positions: int) -> None:
    if active_count >= max_positions:
        raise RiskRejection(f"max concurrent positions reached ({active_count}/{max_positions})")
