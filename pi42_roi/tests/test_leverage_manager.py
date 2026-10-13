"""Leverage policy: min(Telegram, contract max), integer, never raised."""

from dataclasses import replace
from decimal import Decimal

import pytest

from app.leverage_manager import LeverageError, select_leverage
from app.models import ContractInfo
from tests.helpers import CONTRACT_CRV


def contract(**overrides) -> ContractInfo:
    return ContractInfo.from_api({**CONTRACT_CRV, **overrides})


@pytest.mark.parametrize("telegram, contract_max, expected", [
    ("14", "20", "14"),   # Telegram below the Pi42 contract max
    ("14", "10", "10"),   # Telegram above the contract max -> capped
    ("16", "15", "15"),
    ("20", "20", "20"),
    ("1", "50", "1"),
])
def test_min_of_telegram_and_contract_max(telegram, contract_max, expected):
    decision = select_leverage(Decimal(telegram), contract(maxLeverage=contract_max))
    assert decision.selected == Decimal(expected)
    assert decision.selected <= Decimal(telegram)
    assert decision.selected <= Decimal(contract_max)


def test_never_raised_above_the_telegram_request():
    assert select_leverage(Decimal("5"), contract(maxLeverage="50")).selected == Decimal("5")


def test_fractional_telegram_leverage_is_floored_never_rounded_up():
    decision = select_leverage(Decimal("14.9"), contract(maxLeverage="50"))
    assert decision.selected == Decimal("14")


def test_maximum_of_one_is_allowed():
    assert select_leverage(Decimal("3"), contract(maxLeverage="1")).selected == Decimal("1")


@pytest.mark.parametrize("telegram", ["0", "-2"])
def test_zero_or_negative_telegram_leverage_is_rejected(telegram):
    with pytest.raises(LeverageError, match="must be positive"):
        select_leverage(Decimal(telegram), contract())


def test_missing_contract_max_leverage_is_rejected():
    with pytest.raises(LeverageError, match="maxLeverage"):
        select_leverage(Decimal("10"), replace(contract(), max_leverage=Decimal("0")))


def test_selected_leverage_is_always_an_integer():
    decision = select_leverage(Decimal("13.4"), contract(maxLeverage="13.9"))
    assert decision.selected == decision.selected.to_integral_value()
    assert decision.selected == Decimal("13")


def test_margin_mode_is_recorded_for_the_preference_call():
    assert select_leverage(Decimal("10"), contract(), margin_mode="isolated").margin_mode == "ISOLATED"
    assert select_leverage(Decimal("10"), contract(), margin_mode="cross").margin_mode == "CROSS"
    assert select_leverage(Decimal("10"), contract()).margin_mode == "ISOLATED"


def test_decision_description_carries_the_audit_trail():
    description = select_leverage(Decimal("16"), contract(maxLeverage="20", name="EIGENUSDT")).describe()
    assert "telegram=16" in description and "selected=16" in description and "contract_max=20" in description
