"""Position sizing never exceeds the margin budget; Decimal rounding; pre-trade checks."""

from decimal import Decimal

import pytest

from app.models import AssetInfo
from app.risk_manager import (
    RiskRejection,
    check_concurrency,
    check_entry_deviation,
    check_funds,
    check_price_grid,
    compute_budget,
    size_position,
    snap_to_price_grid,
)
from tests.helpers import ASSET_CRV


def asset(**overrides) -> AssetInfo:
    return AssetInfo.from_api({**ASSET_CRV, **overrides})


def test_crv_fixture_sizing_stays_within_budget():
    budget = compute_budget(Decimal("50"), Decimal("2"))  # 48
    result = size_position(entry=Decimal("0.3462"), leverage=Decimal("14"), budget=budget, asset=asset())
    assert result.quantity == Decimal("1941.0")  # 48*14/0.3462 = 1941.07..., floored to 0.1
    assert result.estimated_margin <= budget
    assert result.estimated_margin == result.notional / Decimal(14)


def test_quantity_rounds_down_never_up():
    budget = Decimal("10")
    result = size_position(entry=Decimal("3"), leverage=Decimal("1"), budget=budget,
                           asset=asset(quantity_step="1", min_contract="1", min_notional_value="1"))
    assert result.raw_quantity == Decimal("10") / Decimal("3")
    assert result.quantity == Decimal("3")  # not 4


def test_decimal_precision_no_float_artifacts():
    # 0.1 + 0.2 style artefacts must not appear in the quantity
    result = size_position(entry=Decimal("0.3"), leverage=Decimal("7"), budget=Decimal("13.7"),
                           asset=asset(quantity_step="0.1", min_contract="0.1", min_notional_value="1"))
    assert str(result.quantity) == str(result.quantity.quantize(Decimal("0.1")))


@pytest.mark.parametrize("entry", ["0.0001", "0.3462", "1", "57.5", "1234.5678"])
@pytest.mark.parametrize("lev", ["1", "3", "14", "20"])
@pytest.mark.parametrize("budget", ["1", "10", "48", "50"])
def test_margin_never_exceeds_budget_across_grid(entry, lev, budget):
    a = asset(quantity_step="0.001", min_contract="0.001", max_contract="10000000",
              min_notional_value="0.01", max_leverage="150")
    try:
        result = size_position(entry=Decimal(entry), leverage=Decimal(lev), budget=Decimal(budget), asset=a)
    except RiskRejection:
        return  # rejections are fine; exceeding the budget is not
    assert result.estimated_margin <= Decimal(budget)


def test_below_min_contract_is_rejected_not_increased():
    with pytest.raises(RiskRejection, match="below min_contract"):
        size_position(entry=Decimal("0.3462"), leverage=Decimal("1"), budget=Decimal("0.5"),
                      asset=asset(min_contract="100"))


def test_above_max_contract_is_rejected():
    with pytest.raises(RiskRejection, match="exceeds max_contract"):
        size_position(entry=Decimal("0.0001"), leverage=Decimal("20"), budget=Decimal("48"),
                      asset=asset(max_contract="100"))


def test_min_notional_breach_is_rejected_without_increasing_quantity():
    with pytest.raises(RiskRejection, match="min_notional_value"):
        size_position(entry=Decimal("0.3462"), leverage=Decimal("1"), budget=Decimal("2"),
                      asset=asset(min_notional_value="50", min_contract="0.1"))


def test_missing_min_notional_refuses_to_size():
    with pytest.raises(RiskRejection, match="min_notional_value"):
        size_position(entry=Decimal("0.3462"), leverage=Decimal("14"), budget=Decimal("48"),
                      asset=asset(min_notional_value=None))


def test_budget_must_be_positive():
    with pytest.raises(RiskRejection):
        compute_budget(Decimal("2"), Decimal("2"))


def test_price_grid_rejects_off_step_and_out_of_range():
    a = asset()
    check_price_grid("Entry", Decimal("0.3462"), a)
    with pytest.raises(RiskRejection, match="price_step"):
        check_price_grid("Entry", Decimal("0.34625"), a)
    with pytest.raises(RiskRejection, match="max_price"):
        check_price_grid("Entry", Decimal("2000"), a)


def test_snap_to_price_grid():
    a = asset(price_step=Decimal("0.1"), min_price=Decimal("10"), max_price=Decimal("100000"))
    # BTC signal case: Target 1 84342.67489 snapped to 84342.7
    assert snap_to_price_grid("Target 1", Decimal("84342.67489"), a) == Decimal("84342.7")
    assert snap_to_price_grid("Entry", Decimal("83249.2"), a) == Decimal("83249.2")
    with pytest.raises(RiskRejection, match="max_price"):
        snap_to_price_grid("Entry", Decimal("200000"), a)


def test_entry_deviation_limit():
    assert check_entry_deviation(Decimal("0.3462"), Decimal("0.3500"), Decimal("3")) > 0
    with pytest.raises(RiskRejection, match="away from Entry"):
        check_entry_deviation(Decimal("0.3462"), Decimal("0.3800"), Decimal("3"))


def test_funds_must_cover_margin_plus_reserve():
    check_funds(Decimal("50"), Decimal("48"), Decimal("2"))
    with pytest.raises(RiskRejection, match="insufficient available balance"):
        check_funds(Decimal("49"), Decimal("48"), Decimal("2"))


def test_concurrency_limit_enforced():
    check_concurrency(2, 3)
    with pytest.raises(RiskRejection, match="max concurrent"):
        check_concurrency(3, 3)
