"""Position sizing never exceeds the margin budget; Price grids use Pi42 precision."""

from decimal import Decimal

import pytest

from app.models import ContractInfo
from app.risk_manager import (
    RiskRejection,
    check_concurrency,
    check_entry_deviation,
    check_funds,
    check_price_grid,
    check_target_not_instant,
    compute_budget,
    size_position,
    snap_to_price_grid,
)
from tests.helpers import CONTRACT_CRV


def contract(**overrides) -> ContractInfo:
    return ContractInfo.from_api({**CONTRACT_CRV, **overrides})


def test_crv_fixture_sizing_stays_within_the_budget():
    budget = compute_budget(Decimal("50"), Decimal("2"))  # 48
    result = size_position(entry=Decimal("0.3462"), leverage=Decimal("14"), budget=budget,
                           contract=contract())
    assert result.quantity == Decimal("1941.0")  # 48*14/0.3462 = 1941.07..., floored to 0.1
    assert result.estimated_margin <= budget
    assert result.estimated_margin == result.notional / Decimal("14")


def test_quantity_is_floored_to_the_pi42_quantity_precision():
    result = size_position(entry=Decimal("3"), leverage=Decimal("1"), budget=Decimal("10"),
                           contract=contract(minQty="0.1", maxQty="1000000", minNotional="1"))
    assert result.quantity == Decimal("3.3")  # 10/3 = 3.333..., step 0.1
    assert result.raw_quantity > result.quantity


def test_five_decimal_quantity_precision_is_respected():
    btc = ContractInfo.from_api({
        "name": "BTCINR", "market": "INR", "pricePrecision": 1, "quantityPrecision": 5,
        "maxLeverage": "50", "orderTypes": ["LIMIT", "MARKET"],
        "filters": [{"filterType": "LIMIT_QTY_SIZE", "minQty": "0.00001", "maxQty": "100"},
                    {"filterType": "MIN_NOTIONAL", "notional": "100"}],
    })
    result = size_position(entry=Decimal("8000000"), leverage=Decimal("10"),
                           budget=Decimal("50000"), contract=btc)
    assert result.quantity == Decimal("0.0625")  # step 0.00001


@pytest.mark.parametrize("entry", ["0.0001", "0.3462", "1", "57.5", "1234.5678"])
@pytest.mark.parametrize("leverage", ["1", "3", "14", "20"])
@pytest.mark.parametrize("budget", ["1", "10", "48", "50"])
def test_margin_never_exceeds_the_budget_across_the_grid(entry, leverage, budget):
    tight = contract(minQty="0.0001", maxQty="1000000000", minNotional="0.01", pricePrecision=4,
                     quantityPrecision=4, maxLeverage="150")
    try:
        result = size_position(entry=Decimal(entry), leverage=Decimal(leverage),
                               budget=Decimal(budget), contract=tight)
    except RiskRejection:
        return  # rejections are acceptable; exceeding the budget is not
    assert result.estimated_margin <= Decimal(budget)


def test_below_min_qty_is_rejected_never_increased():
    with pytest.raises(RiskRejection, match="below the contract minQty"):
        size_position(entry=Decimal("0.3462"), leverage=Decimal("1"), budget=Decimal("0.5"),
                      contract=contract(minQty="100"))


def test_above_max_qty_is_rejected():
    with pytest.raises(RiskRejection, match="exceeds the contract maxQty"):
        size_position(entry=Decimal("0.0001"), leverage=Decimal("20"), budget=Decimal("48"),
                      contract=contract(minQty="0.1", maxQty="100"))


def test_min_notional_breach_is_rejected_without_increasing_the_quantity():
    with pytest.raises(RiskRejection, match="minNotional"):
        size_position(entry=Decimal("0.3462"), leverage=Decimal("1"), budget=Decimal("2"),
                      contract=contract(minQty="0.1", minNotional="50"))


def test_missing_min_notional_refuses_to_size():
    no_notional = contract(filters=[{"filterType": "LIMIT_QTY_SIZE", "minQty": "0.1",
                                     "maxQty": "1000000"}], minNotional=None)
    assert no_notional.min_notional is None
    with pytest.raises(RiskRejection, match="minNotional"):
        size_position(entry=Decimal("0.3462"), leverage=Decimal("14"), budget=Decimal("48"),
                      contract=no_notional)


def test_budget_must_be_positive():
    with pytest.raises(RiskRejection, match="not positive"):
        compute_budget(Decimal("2"), Decimal("2"))


def test_price_grid_uses_pi42_price_precision():
    crv = contract()
    check_price_grid("Entry", Decimal("0.3462"), crv)
    with pytest.raises(RiskRejection, match="price step"):
        check_price_grid("Entry", Decimal("0.34625"), crv)
    btc = ContractInfo.from_api({**CONTRACT_CRV, "name": "BTCUSDT", "pricePrecision": 1,
                                 "quantityPrecision": 3})
    with pytest.raises(RiskRejection, match="price step"):
        check_price_grid("Entry", Decimal("83249.25"), btc)


def test_snap_to_price_grid():
    btc = ContractInfo.from_api({**CONTRACT_CRV, "name": "BTCUSDT", "pricePrecision": 1,
                                 "quantityPrecision": 3})
    assert snap_to_price_grid("Target 1", Decimal("84342.67489"), btc) == Decimal("84342.7")
    assert snap_to_price_grid("Entry", Decimal("83249.2"), btc) == Decimal("83249.2")
    assert snap_to_price_grid("Entry", Decimal("0.34625"), contract()) == Decimal("0.3463")


def test_entry_deviation_limit():
    assert check_entry_deviation(Decimal("0.3462"), Decimal("0.3500"), Decimal("3")) > 0
    with pytest.raises(RiskRejection, match="away from Entry"):
        check_entry_deviation(Decimal("0.3462"), Decimal("0.3800"), Decimal("3"))


def test_take_profit_that_would_trigger_instantly_is_refused():
    # Pi42 error 3011: an entry whose TP is already through the market is rejected.
    check_target_not_instant(direction="SHORT", target=Decimal("0.3185"),
                             market_price=Decimal("0.3462"), entry=Decimal("0.3462"))
    with pytest.raises(RiskRejection, match="instantly"):
        check_target_not_instant(direction="SHORT", target=Decimal("0.3500"),
                                 market_price=Decimal("0.3462"), entry=Decimal("0.3462"))
    check_target_not_instant(direction="LONG", target=Decimal("0.115"),
                             market_price=Decimal("0.1"), entry=Decimal("0.1"))
    with pytest.raises(RiskRejection, match="instantly"):
        check_target_not_instant(direction="LONG", target=Decimal("0.09"),
                                 market_price=Decimal("0.1"), entry=Decimal("0.1"))


def test_funds_must_cover_margin_plus_reserve():
    check_funds(Decimal("50"), Decimal("48"), Decimal("2"))
    with pytest.raises(RiskRejection, match="insufficient futures-wallet balance"):
        check_funds(Decimal("49"), Decimal("48"), Decimal("2"))


def test_concurrency_limit_is_enforced():
    check_concurrency(2, 3)
    with pytest.raises(RiskRejection, match="max concurrent"):
        check_concurrency(3, 3)
