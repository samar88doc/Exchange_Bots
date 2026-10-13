"""PAPER simulation: pending limit, conservative fills, ROI take-profit, TTL, ledger."""

from datetime import timedelta
from decimal import Decimal

from app.models import Mode, SignalStatus, TradeRecord, TradeStatus
from app.paper_broker import PaperBroker, evaluate_paper_tick, liquidation_price
from app.storage import Storage
from tests.helpers import utc

FEE = Decimal("0.0005")
MMR = Decimal("0.005")
ROI_TARGET = Decimal("0.3185")  # 0.3462 - 8% (ROI take-profit for the CRV fixture)


def make_trade(direction="SHORT", status="ENTRY_PENDING", created=None, **over) -> TradeRecord:
    created = created or utc()
    base = dict(
        id=1, signal_id=1, mode="PAPER", ref_id="TG-1-x", client_order_id=None, order_id=None,
        symbol="CRVUSDT", market="USDT", margin_asset="USDT", direction=direction,
        entry_price=Decimal("0.3462"),
        target1=ROI_TARGET if direction == "SHORT" else Decimal("0.3739"),
        stop_loss_reference=Decimal("0.3662"), telegram_leverage=Decimal("14"),
        leverage=Decimal("14"), contract_max_leverage=Decimal("20"), quantity=Decimal("1941"),
        notional=Decimal("671.7"), est_margin=Decimal("48"), budget=Decimal("48"),
        status=status, status_reason=None, position_id=None, fill_price=None,
        filled_quantity=None, exit_price=None, exit_reason=None, entry_fee=None, exit_fee=None,
        realized_pnl=None, liquidation_price=None, tp_confirmed=False, tp_order_id=None,
        created_at=created, updated_at=created, filled_at=None, closed_at=None,
    )
    base.update(over)
    return TradeRecord(**base)


def tick(trade, price, now=None, ttl=120):
    return evaluate_paper_tick(trade, Decimal(price), now or trade.created_at, ttl_minutes=ttl,
                               fee_rate=FEE, maintenance_margin_rate=MMR)


def test_short_limit_does_not_fill_below_the_entry():
    assert tick(make_trade("SHORT"), "0.3400").new_status is None


def test_short_limit_fills_at_or_above_the_entry_at_the_limit_price():
    result = tick(make_trade("SHORT"), "0.3470")
    assert result.new_status is TradeStatus.ENTRY_FILLED
    assert result.updates["fill_price"] == Decimal("0.3462")  # the limit, not the tick price
    assert result.updates["filled_quantity"] == Decimal("1941")
    assert result.updates["liquidation_price"] > Decimal("0.3462")  # SHORT liquidates above


def test_long_limit_fills_when_the_price_reaches_or_drops_below_the_entry():
    trade = make_trade("LONG")
    assert tick(trade, "0.3500").new_status is None
    assert tick(trade, "0.3462").new_status is TradeStatus.ENTRY_FILLED
    assert tick(trade, "0.3300").new_status is TradeStatus.ENTRY_FILLED


def test_roi_take_profit_closes_with_pnl_only_after_the_fill_tick():
    trade = make_trade("SHORT", status="ENTRY_FILLED", entry_fee=Decimal("0"))
    result = tick(trade, str(ROI_TARGET))
    assert result.new_status is TradeStatus.CLOSED
    assert result.updates["exit_price"] == ROI_TARGET
    assert result.updates["exit_reason"] == "TAKE_PROFIT_1"
    gross = Decimal("1941") * (Decimal("0.3462") - ROI_TARGET)
    assert result.updates["realized_pnl"] < gross  # fees reduce PnL
    assert result.updates["realized_pnl"] > gross - Decimal("2")


def test_no_take_profit_before_the_target_is_reached():
    trade = make_trade("SHORT", status="ENTRY_FILLED")
    assert tick(trade, "0.3400").new_status is None


def test_long_take_profit_only_triggers_above_the_target():
    trade = make_trade("LONG", status="ENTRY_FILLED")
    assert tick(trade, "0.3600").new_status is None
    assert tick(trade, "0.3739").new_status is TradeStatus.CLOSED


def test_pending_limit_expires_after_the_ttl():
    created = utc()
    trade = make_trade("SHORT", created=created)
    result = tick(trade, "0.3300", now=created + timedelta(minutes=121), ttl=120)
    assert result.new_status is TradeStatus.EXPIRED
    assert result.event == "paper.entry_expired"


def test_ttl_zero_disables_the_expiry():
    created = utc()
    trade = make_trade("SHORT", created=created)
    assert tick(trade, "0.3300", now=created + timedelta(days=3), ttl=0).new_status is None


def test_liquidation_is_approximate_and_capped_at_the_allocated_margin():
    liq = liquidation_price("SHORT", Decimal("0.3462"), Decimal("14"), MMR)
    assert liq > Decimal("0.3462")
    trade = make_trade("SHORT", status="ENTRY_FILLED", entry_fee=Decimal("0.1"),
                       liquidation_price=liq)
    result = tick(trade, str(liq + Decimal("0.01")))
    assert result.new_status is TradeStatus.LIQUIDATED
    assert result.updates["realized_pnl"] == -Decimal("48") - Decimal("0.1")
    assert "margin" in result.updates["status_reason"]


def test_target_reached_before_the_fill_does_not_close_the_trade():
    """A take-profit tick that happens while the LIMIT is still pending is ignored."""
    trade = make_trade("SHORT", status="ENTRY_PENDING")
    assert tick(trade, "0.3000").new_status is None


def test_invalid_price_is_ignored():
    assert tick(make_trade("SHORT"), "0").new_status is None


def _seed_trade(storage: Storage, now, **over) -> int:
    fields = {k: v for k, v in make_trade("SHORT", **over).__dict__.items()
              if k not in ("id", "signal_id", "created_at", "updated_at")}
    signal_id = storage.insert_signal(channel_id=-1, message_id=7, received_at=now,
                                      signal_time=now, raw_text="x",
                                      status=SignalStatus.RECEIVED.value)
    return storage.create_trade(now, **fields, signal_id=signal_id)


def test_broker_persists_state_across_a_restart(tmp_path):
    db = tmp_path / "paper.db"
    storage = Storage(db)
    now = utc()
    trade_id = _seed_trade(storage, now)
    broker = PaperBroker(storage, initial_balance=Decimal("1000"), fee_rate=FEE,
                         maintenance_margin_rate=MMR, ttl_minutes=120)
    assert broker.apply_price("CRVUSDT", Decimal("0.3470"), now)  # fills the limit
    storage.close()

    reopened = Storage(db)
    trade = reopened.get_trade(trade_id)
    assert trade.trade_status is TradeStatus.ENTRY_FILLED
    assert trade.mode == Mode.PAPER.value
    broker2 = PaperBroker(reopened, initial_balance=Decimal("1000"), fee_rate=FEE,
                          maintenance_margin_rate=MMR, ttl_minutes=120)
    assert broker2.apply_price("CRVUSDT", ROI_TARGET, now + timedelta(minutes=1))
    closed = reopened.get_trade(trade_id)
    assert closed.trade_status is TradeStatus.CLOSED
    assert closed.exit_reason == "TAKE_PROFIT_1"
    ledger = broker2.ledger()
    assert ledger["realized_pnl"] > 0
    assert ledger["available"] == Decimal("1000") + ledger["realized_pnl"]
    reopened.close()


def test_ledger_locks_margin_while_the_entry_is_pending(tmp_path):
    storage = Storage(tmp_path / "l.db")
    now = utc()
    _seed_trade(storage, now)
    broker = PaperBroker(storage, initial_balance=Decimal("100"), fee_rate=FEE,
                         maintenance_margin_rate=MMR, ttl_minutes=120)
    assert broker.available_balance() == Decimal("52")  # 100 - 48 locked
    ledger = broker.ledger()
    assert ledger["locked_margin"] == Decimal("48") and ledger["realized_pnl"] == 0
    storage.close()


def test_broker_only_touches_trades_for_the_observed_symbol(tmp_path):
    storage = Storage(tmp_path / "s.db")
    now = utc()
    _seed_trade(storage, now)
    broker = PaperBroker(storage, initial_balance=Decimal("1000"), fee_rate=FEE,
                         maintenance_margin_rate=MMR, ttl_minutes=120)
    assert broker.apply_price("BTCUSDT", Decimal("0.3470"), now) == []
    assert storage.get_trade(1).trade_status is TradeStatus.ENTRY_PENDING
    storage.close()


def test_every_transition_is_recorded_as_an_event(tmp_path):
    storage = Storage(tmp_path / "e.db")
    now = utc()
    _seed_trade(storage, now)
    broker = PaperBroker(storage, initial_balance=Decimal("1000"), fee_rate=FEE,
                         maintenance_margin_rate=MMR, ttl_minutes=120)
    broker.apply_price("CRVUSDT", Decimal("0.3470"), now)
    broker.apply_price("CRVUSDT", ROI_TARGET, now + timedelta(minutes=1))
    events = [e["event"] for e in storage.recent_events(20)]
    assert "paper.entry_filled" in events and "paper.take_profit_hit" in events
    storage.close()
