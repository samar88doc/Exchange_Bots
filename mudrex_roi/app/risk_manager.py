"""Position sizing and pre-trade risk checks. Pure functions, no I/O.

Initial margin = notional / leverage. A $50 margin budget is NOT a maximum
loss: without a stop-loss the loss can be the full allocated margin or more
in adverse conditions (liquidation).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.models import AssetInfo
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


def size_position(*, entry: Decimal, leverage: Decimal, budget: Decimal, asset: AssetInfo) -> SizingResult:
    """Size a USDT-quoted linear position so initial margin <= budget.

    raw_qty = budget * leverage / entry, rounded DOWN to quantity_step.
    The quantity is never increased to satisfy a minimum; if a minimum cannot
    be met inside the budget the signal is rejected.
    """
    if entry <= 0 or leverage <= 0 or budget <= 0:
        raise RiskRejection("entry, leverage and budget must be positive")
    raw_quantity = (budget * leverage) / entry
    quantity = floor_to_step(raw_quantity, asset.quantity_step)
    if quantity <= 0:
        raise RiskRejection(
            f"budget {budget} USDT at {leverage}x cannot buy one quantity_step "
            f"({asset.quantity_step}) at entry {entry}"
        )
    if quantity < asset.min_contract:
        raise RiskRejection(
            f"quantity {quantity} is below min_contract {asset.min_contract} within the margin budget"
        )
    if quantity > asset.max_contract:
        raise RiskRejection(f"quantity {quantity} exceeds max_contract {asset.max_contract}")
    notional = quantity * entry
    if asset.min_notional_value is None:
        raise RiskRejection("asset min_notional_value is unavailable; refusing to size")
    if notional < asset.min_notional_value:
        raise RiskRejection(
            f"notional {notional} is below min_notional_value {asset.min_notional_value} "
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


def check_price_grid(name: str, value: Decimal, asset: AssetInfo) -> None:
    """Price must lie within min/max price and sit on price_step. Never rounded."""
    if asset.price_step is None or asset.price_step <= 0:
        raise RiskRejection(f"{name} cannot be validated: asset price_step unavailable")
    if value % asset.price_step != 0:
        raise RiskRejection(f"{name} {value} is not a multiple of price_step {asset.price_step}")
    if asset.min_price is not None and value < asset.min_price:
        raise RiskRejection(f"{name} {value} is below min_price {asset.min_price}")
    if asset.max_price is not None and value > asset.max_price:
        raise RiskRejection(f"{name} {value} is above max_price {asset.max_price}")


def snap_to_price_grid(name: str, value: Decimal, asset: AssetInfo) -> Decimal:
    """Snap a price to the asset's price_step grid and check min/max range boundaries."""
    if asset.price_step is None or asset.price_step <= 0:
        raise RiskRejection(f"{name} cannot be validated: asset price_step unavailable")
    snapped = round_to_step(value, asset.price_step)
    if snapped <= 0:
        raise RiskRejection(f"{name} {value} snapped to non-positive price {snapped}")
    if asset.min_price is not None and snapped < asset.min_price:
        raise RiskRejection(f"{name} {snapped} is below min_price {asset.min_price}")
    if asset.max_price is not None and snapped > asset.max_price:
        raise RiskRejection(f"{name} {snapped} is above max_price {asset.max_price}")
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


def check_funds(available: Decimal, estimated_margin: Decimal, reserve: Decimal) -> None:
    """Available futures balance must cover the new margin plus the retained reserve."""
    required = estimated_margin + reserve
    if available < required:
        raise RiskRejection(
            f"insufficient available balance: have {available}, need {required} "
            f"(margin {estimated_margin} + reserve {reserve})"
        )


def check_concurrency(active_count: int, max_positions: int) -> None:
    if active_count >= max_positions:
        raise RiskRejection(f"max concurrent positions reached ({active_count}/{max_positions})")
