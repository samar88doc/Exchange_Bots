"""Pure trading math: quantity, ROI, fees, targets. No I/O — fully unit-testable.

ROI convention (margin-based, NOT price-change-based):
    ROI% = P&L / position_margin * 100
For a LONG with leverage L:
    ROI% = (mark - entry) / entry * L * 100
Target +10% ROI at 10x leverage => +1% price move. The UI shows BOTH.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

BPS = Decimal("10000")
ZERO = Decimal("0")


@dataclass(frozen=True)
class QuantityDecision:
    quantity: Decimal
    notional: Decimal
    ok: bool
    reason: str = ""
    message: str = ""


@dataclass(frozen=True)
class PnlBreakdown:
    gross_pnl: Decimal
    entry_fee: Decimal
    exit_fee: Decimal
    funding: Decimal
    net_pnl: Decimal
    roi_percent: Decimal          # net of fees when computed with fees
    gross_roi_percent: Decimal
    price_change_percent: Decimal


def round_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step is None or step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def quantize_precision(value: Decimal, precision: int) -> Decimal:
    q = Decimal("1").scaleb(-precision)
    return value.quantize(q, rounding=ROUND_DOWN)


def fee_from_notional(notional: Decimal, fee_bps: Decimal) -> Decimal:
    return (notional * fee_bps) / BPS


def calculate_quantity(
    notional_target: Decimal,
    entry_price: Decimal,
    quantity_precision: int,
    min_quantity: Decimal = ZERO,
    max_quantity: Decimal | None = None,
    min_notional: Decimal = ZERO,
    quantity_step: Decimal = ZERO,
) -> QuantityDecision:
    """quantity = position_notional / entry_price, then contract rules.

    NEVER silently changes margin/leverage to force an order through: if contract
    rules cannot express the requested risk, the order is REJECTED.
    """
    if entry_price is None or entry_price <= 0:
        return QuantityDecision(ZERO, ZERO, False, "INVALID_QUANTITY", "entry price must be > 0")
    raw_qty = notional_target / entry_price
    qty = quantize_precision(raw_qty, quantity_precision)
    if quantity_step and quantity_step > 0:
        qty = round_to_step(qty, quantity_step)
    if qty <= 0:
        return QuantityDecision(ZERO, ZERO, False, "INVALID_QUANTITY", "quantity rounds to zero at contract precision")
    if max_quantity is not None and qty > max_quantity:
        return QuantityDecision(qty, qty * entry_price, False, "INVALID_QUANTITY", "quantity exceeds contract maximum")
    if min_quantity > 0 and qty < min_quantity:
        return QuantityDecision(
            qty, qty * entry_price, False, "MIN_QUANTITY_NOT_MET",
            f"quantity {qty} < minimum {min_quantity}; refusing to silently resize risk",
        )
    notional = qty * entry_price
    if min_notional > 0 and notional < min_notional:
        return QuantityDecision(
            qty, notional, False, "MIN_NOTIONAL_NOT_MET",
            f"notional {notional} < minimum {min_notional}; refusing to silently resize risk",
        )
    return QuantityDecision(qty, qty * entry_price, True)


def target_price_for_roi(entry_price: Decimal, leverage: int, target_roi_percent: Decimal, direction: str = "LONG") -> Decimal:
    move = target_roi_percent / Decimal(100) / Decimal(leverage)
    if direction == "LONG":
        return entry_price * (Decimal(1) + move)
    return entry_price * (Decimal(1) - move)


def compute_pnl(
    entry_price: Decimal,
    exit_price: Decimal,
    quantity: Decimal,
    margin: Decimal,
    leverage: int,
    direction: str = "LONG",
    entry_fee: Decimal = ZERO,
    exit_fee: Decimal = ZERO,
    funding: Decimal = ZERO,
    include_fees: bool = True,
) -> PnlBreakdown:
    sign = Decimal(1) if direction == "LONG" else Decimal(-1)
    gross = (exit_price - entry_price) * quantity * sign
    fees = entry_fee + exit_fee
    net = gross - fees - funding
    price_change = (exit_price - entry_price) / entry_price * Decimal(100) * sign
    denom = margin if margin > 0 else (entry_price * quantity / Decimal(leverage))
    gross_roi = (gross / denom * Decimal(100)) if denom > 0 else ZERO
    roi = (net / denom * Decimal(100)) if denom > 0 else ZERO
    return PnlBreakdown(
        gross_pnl=gross,
        entry_fee=entry_fee,
        exit_fee=exit_fee,
        funding=funding,
        net_pnl=net if include_fees else gross,
        roi_percent=roi if include_fees else gross_roi,
        gross_roi_percent=gross_roi,
        price_change_percent=price_change,
    )


def roi_reached(roi_percent: Decimal, target_roi_percent: Decimal) -> bool:
    return roi_percent >= target_roi_percent
