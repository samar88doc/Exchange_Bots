"""Leverage selection policy: min(Telegram, Mudrex max), grid-aligned, never raised."""

from decimal import Decimal

import pytest

from app.leverage_manager import LeverageError, select_leverage
from app.models import AssetInfo
from tests.helpers import ASSET_CRV


def asset(**overrides) -> AssetInfo:
    return AssetInfo.from_api({**ASSET_CRV, **overrides})


@pytest.mark.parametrize("tg, mx, expected", [
    ("14", "20", "14"),   # Telegram below asset max
    ("14", "10", "10"),   # Telegram above asset max -> capped
    ("16", "15", "15"),
    ("20", "20", "20"),
])
def test_min_of_telegram_and_asset_max(tg, mx, expected):
    decision = select_leverage(Decimal(tg), asset(max_leverage=mx))
    assert decision.selected == Decimal(expected)
    assert decision.selected <= Decimal(tg)


def test_never_raised_above_telegram_request():
    decision = select_leverage(Decimal("5"), asset(max_leverage="150"))
    assert decision.selected == Decimal("5")


def test_fractional_leverage_step_rounds_down_on_grid():
    decision = select_leverage(Decimal("14.3"), asset(leverage_step="0.5", min_leverage="1", max_leverage="50"))
    assert decision.selected == Decimal("14")
    decision = select_leverage(Decimal("14.99"), asset(leverage_step="0.01", min_leverage="1", max_leverage="50"))
    assert decision.selected == Decimal("14.99")


def test_grid_anchored_at_minimum_leverage():
    # min 2, step 3 -> valid values 2, 5, 8, 11, 14 ...
    decision = select_leverage(Decimal("13"), asset(min_leverage="2", leverage_step="3", max_leverage="50"))
    assert decision.selected == Decimal("11")


def test_candidate_below_minimum_is_rejected_not_raised():
    with pytest.raises(LeverageError, match="below the asset minimum"):
        select_leverage(Decimal("3"), asset(min_leverage="5", max_leverage="50"))


def test_missing_leverage_step_is_rejected():
    with pytest.raises(LeverageError, match="leverage_step"):
        select_leverage(Decimal("10"), asset(leverage_step=None))


def test_zero_or_negative_telegram_leverage_rejected():
    with pytest.raises(LeverageError):
        select_leverage(Decimal("0"), asset())
    with pytest.raises(LeverageError):
        select_leverage(Decimal("-2"), asset())
