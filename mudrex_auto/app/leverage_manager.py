"""Leverage selection.

candidate = min(telegram_leverage, asset.max_leverage)
selected  = the largest value on the asset's leverage grid
            (min_leverage + k * leverage_step) that is <= candidate.

The result never exceeds the Telegram-requested leverage and never drops
below the asset minimum. If no such value exists the signal is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from app.models import AssetInfo


class LeverageError(Exception):
    """No valid leverage satisfies the policy for this asset."""


@dataclass(frozen=True)
class LeverageDecision:
    telegram_leverage: Decimal
    candidate: Decimal
    selected: Decimal
    min_leverage: Decimal
    max_leverage: Decimal
    leverage_step: Decimal

    def describe(self) -> str:
        return (
            f"telegram={self.telegram_leverage} candidate={self.candidate} "
            f"selected={self.selected} asset_min={self.min_leverage} "
            f"asset_max={self.max_leverage} step={self.leverage_step}"
        )


def select_leverage(telegram_leverage: Decimal, asset: AssetInfo) -> LeverageDecision:
    if telegram_leverage <= 0:
        raise LeverageError(f"Telegram leverage must be positive (got {telegram_leverage})")
    min_lev = asset.min_leverage
    max_lev = asset.max_leverage
    if max_lev is None or min_lev is None or min_lev <= 0 or max_lev < min_lev:
        raise LeverageError("asset leverage limits are missing or inconsistent")
    step = asset.leverage_step
    if step is None or step <= 0:
        raise LeverageError("asset has no valid leverage_step; refusing to guess a leverage grid")

    candidate = min(telegram_leverage, max_lev)
    if candidate < min_lev:
        raise LeverageError(
            f"candidate leverage {candidate} is below the asset minimum {min_lev}; "
            "signal rejected rather than raising leverage"
        )
    steps = ((candidate - min_lev) / step).to_integral_value(rounding=ROUND_FLOOR)
    selected = min_lev + steps * step
    if selected < min_lev or selected > candidate or selected > max_lev or selected <= 0:
        raise LeverageError(f"no valid leverage on the asset grid <= {candidate}")
    return LeverageDecision(
        telegram_leverage=telegram_leverage,
        candidate=candidate,
        selected=selected,
        min_leverage=min_lev,
        max_leverage=max_lev,
        leverage_step=step,
    )
