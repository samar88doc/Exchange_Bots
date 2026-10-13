"""Leverage selection for Pi42.

Pi42 leverage is a whole number between 1 and the contract's ``maxLeverage``
(there is no public min/step grid, unlike Mudrex). The rule is deliberately
conservative:

    candidate = min(telegram_leverage, contract.max_leverage)
    selected  = floor(candidate)      # never rounds up over the Telegram value

If nothing valid remains the signal is rejected. Pi42 refuses leverage changes
while a position or order is open on the contract (errors 2305/2306), so the
engine always configures leverage *before* submitting the entry order.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from app.models import ContractInfo

MIN_LEVERAGE = Decimal(1)


class LeverageError(Exception):
    """No valid leverage satisfies the policy for this contract."""


@dataclass(frozen=True)
class LeverageDecision:
    telegram_leverage: Decimal
    candidate: Decimal
    selected: Decimal
    contract_max_leverage: Decimal
    leverage_step: Decimal = Decimal(1)
    margin_mode: str = "ISOLATED"

    def describe(self) -> str:
        return (
            f"telegram={self.telegram_leverage} candidate={self.candidate} "
            f"selected={self.selected} contract_max={self.contract_max_leverage} "
            f"step={self.leverage_step} margin_mode={self.margin_mode}"
        )


def select_leverage(
    telegram_leverage: Decimal,
    contract: ContractInfo,
    *,
    margin_mode: str = "ISOLATED",
) -> LeverageDecision:
    if telegram_leverage <= 0:
        raise LeverageError(f"Telegram leverage must be positive (got {telegram_leverage})")
    max_lev = contract.max_leverage
    if max_lev is None or max_lev < MIN_LEVERAGE:
        raise LeverageError("contract maxLeverage is missing or inconsistent")
    if not contract.margin_assets_supported and contract.market not in ("USDT", "INR"):
        raise LeverageError(f"contract {contract.symbol} has an unknown market")

    candidate = min(telegram_leverage, max_lev)
    if candidate < MIN_LEVERAGE:
        raise LeverageError(
            f"candidate leverage {candidate} is below the minimum {MIN_LEVERAGE}; signal rejected"
        )
    selected = candidate.to_integral_value(rounding=ROUND_FLOOR)
    if selected < MIN_LEVERAGE or selected > candidate or selected > max_lev:
        raise LeverageError(f"no valid integer leverage <= {candidate} for {contract.symbol}")
    return LeverageDecision(
        telegram_leverage=telegram_leverage,
        candidate=candidate,
        selected=selected,
        contract_max_leverage=max_lev,
        leverage_step=Decimal(1),
        margin_mode=margin_mode.upper() if margin_mode else "ISOLATED",
    )
