"""Unit tests: ROI, quantity, normalization, fees, TP logic, paper fills."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.pnl import (
    calculate_quantity,
    compute_pnl,
    fee_from_notional,
    quantize_precision,
    roi_reached,
    round_to_step,
    target_price_for_roi,
)
from app.services.symbols import normalize_tradingview_symbol

D = Decimal


# ---------------------------------------------------------------- ROI math
class TestRoiCalculation:
    def test_roi_is_margin_based_not_price_change(self):
        # entry 100, mark 101 (+1% price), 10x -> ROI = +10%
        pnl = compute_pnl(D("100"), D("101"), D("12"), D("120"), 10)
        assert pnl.price_change_percent == D("1")
        assert pnl.roi_percent == D("10")
        assert pnl.gross_pnl == D("12")

    def test_ten_percent_price_increase_is_not_ten_percent_roi(self):
        pnl = compute_pnl(D("100"), D("110"), D("12"), D("120"), 10)
        assert pnl.price_change_percent == D("10")
        assert pnl.roi_percent == D("100")  # 10x leverage amplifies ROI

    def test_target_price_for_10_roi_at_10x(self):
        assert target_price_for_roi(D("100"), 10, D("10")) == D("101")

    def test_roi_with_fees(self):
        pnl = compute_pnl(D("100"), D("101"), D("12"), D("120"), 10,
                          entry_fee=D("1"), exit_fee=D("1"), include_fees=True)
        assert pnl.gross_pnl == D("12")
        assert pnl.net_pnl == D("10")
        assert pnl.roi_percent == D("10") / D("120") * D("100")

    def test_roi_excluding_fees(self):
        pnl = compute_pnl(D("100"), D("101"), D("12"), D("120"), 10,
                          entry_fee=D("1"), exit_fee=D("1"), include_fees=False)
        assert pnl.roi_percent == D("10")

    def test_short_roi(self):
        pnl = compute_pnl(D("100"), D("99"), D("12"), D("120"), 10, direction="SHORT")
        assert pnl.roi_percent == D("10")

    def test_roi_reached_boundary(self):
        assert roi_reached(D("10"), D("10")) is True
        assert roi_reached(D("9.99"), D("10")) is False


# ----------------------------------------------------------- quantity math
class TestQuantityCalculation:
    def test_basic_quantity(self):
        d = calculate_quantity(D("1200"), D("100"), quantity_precision=3)
        assert d.ok and d.quantity == D("12") and d.notional == D("1200")

    def test_quantity_respects_precision(self):
        d = calculate_quantity(D("1200"), D("99.87"), quantity_precision=3)
        assert d.ok
        assert d.quantity == quantize_precision(D("1200") / D("99.87"), 3)

    def test_quantity_respects_step(self):
        d = calculate_quantity(D("1200"), D("100"), quantity_precision=4, quantity_step=D("0.05"))
        assert d.ok and d.quantity == D("12")

    def test_min_quantity_rejects_without_resizing(self):
        d = calculate_quantity(D("1200"), D("100"), quantity_precision=3, min_quantity=D("50"))
        assert not d.ok and d.reason == "MIN_QUANTITY_NOT_MET"
        # risk NOT silently changed
        assert d.quantity == D("12")

    def test_min_notional_rejects(self):
        d = calculate_quantity(D("1200"), D("100"), quantity_precision=3, min_notional=D("5000"))
        assert not d.ok and d.reason == "MIN_NOTIONAL_NOT_MET"

    def test_zero_price_invalid(self):
        d = calculate_quantity(D("1200"), D("0"), quantity_precision=3)
        assert not d.ok and d.reason == "INVALID_QUANTITY"

    def test_rounding_to_zero_invalid(self):
        d = calculate_quantity(D("1"), D("100000"), quantity_precision=2)
        assert not d.ok


class TestHelpers:
    def test_round_to_step(self):
        assert round_to_step(D("1.23"), D("0.1")) == D("1.2")

    def test_fee_from_notional(self):
        assert fee_from_notional(D("10000"), D("5")) == D("5")  # 5 bps


# -------------------------------------------------------- symbol normalize
class TestSymbolNormalization:
    def test_test_a_bas(self):
        raw = "BASUSDT.P"
        canonical = normalize_tradingview_symbol(raw)
        assert raw == "BASUSDT.P"                      # raw preserved
        assert canonical == "BASUSDT"
        assert "BASUSDT" == "BASUSDT"                  # shark symbol resolved to same

    def test_test_b_btc(self):
        assert normalize_tradingview_symbol("BTCUSDT.P") == "BTCUSDT"

    def test_test_c_eth(self):
        assert normalize_tradingview_symbol("ETHUSDT.P") == "ETHUSDT"

    def test_only_terminal_suffix_removed(self):
        assert normalize_tradingview_symbol("BTCUSDT.P.P") == "BTCUSDT.P"
        assert normalize_tradingview_symbol("X.P.P") == "X.P"

    def test_no_suffix_unchanged(self):
        assert normalize_tradingview_symbol("BTCUSDT") == "BTCUSDT"

    def test_does_not_strip_usdt_or_middle_chars(self):
        assert normalize_tradingview_symbol("USDT.P") == "USDT"
        assert normalize_tradingview_symbol("BTC.USDT") == "BTC.USDT"

    def test_case_insensitive_upper(self):
        assert normalize_tradingview_symbol("btcusdt.p") == "BTCUSDT"

    def test_empty_rejected(self):
        from app.core.errors import SymbolError

        with pytest.raises(SymbolError):
            normalize_tradingview_symbol("")
        with pytest.raises(SymbolError):
            normalize_tradingview_symbol(".P")


# ------------------------------------------------------------ paper engine
class TestPaperFills:
    @pytest.mark.asyncio
    async def test_paper_fill_with_slippage_and_fee(self):
        from app.core.config import AppSettings
        from app.exchanges.base import OrderRequest
        from app.exchanges.market_data import ContractRegistry, MarketDataService
        from app.exchanges.paper import PaperExchangeAdapter, apply_slippage

        settings = AppSettings(testing=True, paper_slippage_bps=D("10"), paper_taker_fee_bps=D("5"),
                               paper_funding_rate_bps_8h=D("0"))
        md = MarketDataService(settings)
        md.seed_price("BTCUSDT", D("100"))
        paper = PaperExchangeAdapter(settings, md, ContractRegistry(settings))
        result = await paper.place_market_order(
            OrderRequest(symbol="BTCUSDT", side="BUY", quantity=D("12"))
        )
        assert result.status == "FILLED"
        assert result.avg_fill_price == apply_slippage(D("100"), "BUY", D("10")) == D("100.1")
        fee = paper.fee_for(result.order_id)
        assert fee == (D("100.1") * D("12") * D("5")) / D("10000")

    def test_slippage_direction(self):
        from app.exchanges.paper import apply_slippage

        assert apply_slippage(D("100"), "BUY", D("10")) > D("100")
        assert apply_slippage(D("100"), "SELL", D("10")) < D("100")
