"""PAPER simulation: pending limit, conservative fills, TP after fill, TTL, persistence."""

from datetime import timedelta
from decimal import Decimal

from app.models import Mode, SignalStatus, TradeRecord, TradeStatus
from app.paper_broker import PaperBroker, evaluate_paper_tick, liquidation_price
from app.storage import Storage
from tests.helpers import utc

FEE = Decimal("0.0002")
MMR = Decimal("0.005")


def make_trade(direction="SHORT", status="ENTRY_PENDING", created=None, **over) -> TradeRecord:
    created = created or utc()
    base = dict(
        id=1, signal_id=1, mode="PAPER", client_order_id="TG-1-x", symbol="CRVUSDT", mudrex_asset_id="a",
        direction=direction, entry_price=Decimal("0.3462"), target1=Decimal("0.3361") if direction == "SHORT"
        else Decimal("0.3600"), stop_loss_reference=Decimal("0.3662"), telegram_leverage=Decimal("14"),
        leverage=Decimal("14"), asset_min_leverage=Decimal("1"), asset_max_leverage=Decimal("20"),
        leverage_step=Decimal("1"), quantity=Decimal("1940.7"), notional=Decimal("671.9"),
        est_margin=Decimal("48"), budget=Decimal("48"), status=status, status_reason=None, order_id=None,
        position_id=None, fill_price=None, filled_quantity=None, exit_price=None, exit_reason=None,
        entry_fee=None, exit_fee=None, realized_pnl=None, liquidation_price=None, tp_confirmed=False,
        tp_order_id=None, created_at=created, updated_at=created, filled_at=None, closed_at=None,
    )
    base.update(over)
    return TradeRecord(**base)


def tick(trade, price, now=None, ttl=120):
    return evaluate_paper_tick(trade, Decimal(price), now or trade.created_at, ttl_minutes=ttl,
                               fee_rate=FEE, maintenance_margin_rate=MMR)


def test_short_limit_does_not_fill_below_entry():
    assert tick(make_trade("SHORT"), "0.3400").new_status is None


def test_short_limit_fills_at_or_above_entry_at_limit_price():
    result = tick(make_trade("SHORT"), "0.3470")
    assert result.new_status is TradeStatus.ENTRY_FILLED
    assert result.updates["fill_price"] == Decimal("0.3462")  # filled at the limit, not the market


def test_long_limit_fills_when_price_reaches_or_drops_below_entry():
    trade = make_trade("LONG")
    assert tick(trade, "0.3500").new_status is None
    assert tick(trade, "0.3462").new_status is TradeStatus.ENTRY_FILLED
    assert tick(trade, "0.3300").new_status is TradeStatus.ENTRY_FILLED


def test_take_profit_only_after_fill_tick_and_closes_with_pnl():
    trade = make_trade("SHORT", status="ENTRY_FILLED", entry_fee=Decimal("0.0000"))
    result = tick(trade, "0.3361")
    assert result.new_status is TradeStatus.CLOSED
    assert result.updates["exit_price"] == Decimal("0.3361")
    assert result.updates["exit_reason"] == "TAKE_PROFIT_1"
    gross = Decimal("1940.7") * (Decimal("0.3462") - Decimal("0.3361"))
    assert result.updates["realized_pnl"] < gross  # fees reduce PnL
    assert result.updates["realized_pnl"] > gross - Decimal("1")


def test_no_take_profit_when_price_has_not_reached_target():
    trade = make_trade("SHORT", status="ENTRY_FILLED")
    assert tick(trade, "0.3400").new_status is None


def test_pending_limit_expires_after_ttl():
    created = utc()
    trade = make_trade("SHORT", created=created)
    later = created + timedelta(minutes=121)
    result = tick(trade, "0.3300", now=later, ttl=120)
    assert result.new_status is TradeStatus.EXPIRED


def test_ttl_zero_disables_expiry():
    created = utc()
    trade = make_trade("SHORT", created=created)
    assert tick(trade, "0.3300", now=created + timedelta(days=3), ttl=0).new_status is None


def test_liquidation_is_approximate_and_capped_at_margin():
    liq = liquidation_price("SHORT", Decimal("0.3462"), Decimal("14"), MMR)
    assert liq > Decimal("0.3462")
    trade = make_trade("SHORT", status="ENTRY_FILLED", entry_fee=Decimal("0.1"), liquidation_price=liq)
    result = tick(trade, str(liq + Decimal("0.01")))
    assert result.new_status is TradeStatus.LIQUIDATED
    assert result.updates["realized_pnl"] == -Decimal("48") - Decimal("0.1")


def test_broker_persists_state_across_restart(tmp_path):
    db = tmp_path / "paper.db"
    storage = Storage(db)
    now = utc()
    sig = storage.insert_signal(channel_id=-1, message_id=7, received_at=now, signal_time=now,
                                raw_text="x", status=SignalStatus.RECEIVED.value)
    trade_id = storage.create_trade(now, **{k: v for k, v in make_trade("SHORT").__dict__.items()
                                             if k not in ("id", "signal_id", "created_at", "updated_at")}, signal_id=sig)
    broker = PaperBroker(storage, initial_balance=Decimal("1000"), fee_rate=FEE, maintenance_margin_rate=MMR,
                         ttl_minutes=120)
    assert broker.apply_price("CRVUSDT", Decimal("0.3470"), now)  # fills
    storage.close()

    reopened = Storage(db)
    trade = reopened.get_trade(trade_id)
    assert trade.trade_status is TradeStatus.ENTRY_FILLED
    assert trade.mode == Mode.PAPER.value
    broker2 = PaperBroker(reopened, initial_balance=Decimal("1000"), fee_rate=FEE,
                          maintenance_margin_rate=MMR, ttl_minutes=120)
    assert broker2.apply_price("CRVUSDT", Decimal("0.3361"), now + timedelta(minutes=1))
    closed = reopened.get_trade(trade_id)
    assert closed.trade_status is TradeStatus.CLOSED
    ledger = broker2.ledger()
    assert ledger["realized_pnl"] > 0
    assert ledger["available"] == Decimal("1000") + ledger["realized_pnl"]
    reopened.close()


def test_ledger_locks_margin_for_pending_orders(tmp_path):
    storage = Storage(tmp_path / "l.db")
    now = utc()
    sig = storage.insert_signal(channel_id=-1, message_id=8, received_at=now, signal_time=now,
                                raw_text="x", status="RECEIVED")
    fields = {k: v for k, v in make_trade("SHORT").__dict__.items() if k not in ("id", "signal_id", "created_at", "updated_at")}
    storage.create_trade(now, **fields, signal_id=sig)
    broker = PaperBroker(storage, initial_balance=Decimal("100"), fee_rate=FEE, maintenance_margin_rate=MMR,
                         ttl_minutes=120)
    assert broker.available_balance() == Decimal("52")
    storage.close()
