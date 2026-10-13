"""REAL reconciliation: TP confirmation, TTL cancel safety, fills, closure detection."""

import asyncio
from datetime import timedelta

from app.execution_engine import ExecutionEngine
from app.models import Mode, TradeStatus
from app.position_monitor import PositionMonitor
from app.storage import Storage
from app.symbol_mapper import SymbolMapper
from tests.helpers import SIGNAL_CRV, FakeMudrex, make_settings, utc


def setup(tmp_path, ttl="120"):
    settings = make_settings(tmp_path, TRADING_MODE="REAL", LIMIT_ORDER_TTL_MINUTES=ttl)
    fake = FakeMudrex()
    client = fake.client(allow_mutations=True)
    storage = Storage(settings.database_file)
    clock = {"now": utc()}
    engine = ExecutionEngine(settings=settings, storage=storage, client=client,
                             mapper=SymbolMapper(client, 600), mode=Mode.REAL, now=lambda: clock["now"])
    monitor = PositionMonitor(storage=storage, client=client, mode=Mode.REAL, currency="USDT",
                              paper_broker=None, ttl_minutes=int(ttl), now=lambda: clock["now"])
    asyncio.run(engine.handle_message(channel_id=-100, message_id=300, text=SIGNAL_CRV,
                                      signal_time=clock["now"]))
    return fake, storage, monitor, clock


def test_fill_then_tp_confirmed(tmp_path):
    fake, storage, monitor, clock = setup(tmp_path)
    order_id = storage.trades()[0].order_id
    fake.fill_order(order_id, price="0.3462")
    asyncio.run(monitor.monitor_once())  # cycle 1: records the fill
    asyncio.run(monitor.monitor_once())  # cycle 2: confirms Target 1 on the open position
    t = storage.trades()[0]
    assert t.trade_status is TradeStatus.ENTRY_FILLED
    assert t.tp_confirmed is True and t.tp_order_id is not None
    assert fake.mutating_calls()[-1][1] != "/fapi/v1/futures/orders/detail"


def test_tp_missing_on_mudrex_is_reported_not_faked(tmp_path):
    fake, storage, monitor, clock = setup(tmp_path)
    order_id = storage.trades()[0].order_id
    fake.fill_order(order_id, price="0.3462")
    pos = next(iter(fake.positions.values()))
    pos["takeprofit"] = None  # Mudrex did not attach Target 1
    asyncio.run(monitor.monitor_once())
    asyncio.run(monitor.monitor_once())
    t = storage.trades()[0]
    assert t.tp_confirmed is False
    assert t.status_reason == "TAKE-PROFIT NOT CONFIRMED"
    events = [e["event"] for e in storage.recent_events(50)]
    assert "real.tp_not_confirmed" in events


def test_position_disappearing_marks_closed_with_unverified_reason(tmp_path):
    fake, storage, monitor, clock = setup(tmp_path)
    fake.fill_order(storage.trades()[0].order_id, price="0.3462")
    asyncio.run(monitor.monitor_once())
    asyncio.run(monitor.monitor_once())
    fake.positions.clear()
    asyncio.run(monitor.monitor_once())
    t = storage.trades()[0]
    assert t.trade_status is TradeStatus.CLOSED
    assert "position history" in t.status_reason


def test_ttl_cancels_only_unfilled_order(tmp_path):
    fake, storage, monitor, clock = setup(tmp_path, ttl="30")
    clock["now"] = clock["now"] + timedelta(minutes=31)
    asyncio.run(monitor.monitor_once())
    assert storage.trades()[0].trade_status is TradeStatus.CANCELLED
    assert any(c[0] == "DELETE" for c in fake.calls)


def test_ttl_never_cancels_an_order_that_filled_meanwhile(tmp_path):
    fake, storage, monitor, clock = setup(tmp_path, ttl="30")
    clock["now"] = clock["now"] + timedelta(minutes=31)
    order_id = storage.trades()[0].order_id
    fake.fill_order(order_id, price="0.3462")  # fills just before the TTL check
    asyncio.run(monitor.monitor_once())
    assert not any(c[0] == "DELETE" for c in fake.calls), "a filled order must never be cancelled"
    assert storage.trades()[0].trade_status is TradeStatus.ENTRY_FILLED


def test_uncertain_submit_is_adopted_when_mudrex_has_the_order(tmp_path):
    fake, storage, monitor, clock = setup(tmp_path)
    t = storage.trades()[0]
    storage.update_trade(t.id, clock["now"], status=TradeStatus.SUBMIT_UNCERTAIN.value)
    asyncio.run(monitor.monitor_once())
    t = storage.trades()[0]
    assert t.trade_status is TradeStatus.ENTRY_PENDING and t.order_id is not None


def test_uncertain_submit_without_order_needs_review(tmp_path):
    fake, storage, monitor, clock = setup(tmp_path)
    t = storage.trades()[0]
    fake.orders.clear()
    storage.update_trade(t.id, clock["now"], status=TradeStatus.SUBMIT_UNCERTAIN.value)
    asyncio.run(monitor.monitor_once())
    assert storage.trades()[0].trade_status is TradeStatus.NEEDS_REVIEW
    assert len([c for c in fake.calls if c[0] == "POST" and c[1] == "/fapi/v2/futures/order"]) == 1


def test_multi_target_progression_and_trailing_sl(tmp_path):
    """Target 1 -> 1/3 close + SL to Break-Even -> Target 2 -> 1/3 close + SL to T1 -> Target 3 -> Square off."""
    from decimal import Decimal
    fake, storage, monitor, clock = setup(tmp_path)
    order_id = storage.trades()[0].order_id
    fake.fill_order(order_id, price="0.3462")
    asyncio.run(monitor.monitor_once())  # cycle 1: records fill
    asyncio.run(monitor.monitor_once())  # cycle 2: confirms TP

    # Step 1: Market reaches Target 1 (0.3361)
    fake.assets["CRVUSDT"]["price"] = "0.3361"
    asyncio.run(monitor.monitor_once())

    t = storage.trades()[0]
    assert 1 in t.targets_hit_set()
    assert t.sl_price == Decimal("0.3462")  # Break-Even!
    # Check that partial close was called
    partial_calls = [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/close/partial")]
    assert len(partial_calls) == 1
    # Check that SL was set to break-even (0.3462)
    risk_calls = [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/riskorder")]
    assert len(risk_calls) == 1
    assert risk_calls[0][3]["stoploss_price"] == "0.3462"

    # Step 2: Market reaches Target 2 (0.32627)
    fake.assets["CRVUSDT"]["price"] = "0.32627"
    asyncio.run(monitor.monitor_once())

    t = storage.trades()[0]
    assert 2 in t.targets_hit_set()
    assert t.sl_price == Decimal("0.3361")  # Trailed to Target 1 price!
    partial_calls = [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/close/partial")]
    assert len(partial_calls) == 2
    risk_calls = [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/riskorder")]
    assert len(risk_calls) == 2
    assert risk_calls[1][3]["stoploss_price"] == "0.3361"

    # Step 3: Market reaches Final Target 3 (0.28398)
    fake.assets["CRVUSDT"]["price"] = "0.28398"
    asyncio.run(monitor.monitor_once())

    t = storage.trades()[0]
    assert 3 in t.targets_hit_set()
    assert t.trade_status is TradeStatus.CLOSED
    assert t.exit_reason == "TARGET_3_HIT"
    # Check that full close (square off) was called
    square_calls = [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/close")]
    assert len(square_calls) == 1


def test_emergency_sl_placed_at_75_percent_margin_loss(tmp_path):
    """When unrealised loss hits -75% of margin before Target 1, emergency SL is placed."""
    from decimal import Decimal
    fake, storage, monitor, clock = setup(tmp_path)
    order_id = storage.trades()[0].order_id
    fake.fill_order(order_id, price="0.3462")
    asyncio.run(monitor.monitor_once())  # cycle 1: records fill
    asyncio.run(monitor.monitor_once())  # cycle 2: confirms TP

    t = storage.trades()[0]
    # SHORT trade: price rising increases loss. Calculate price for -76% loss:
    # unrealised_pnl = (0.3462 - current_price) * qty
    # margin_loss_pct = unrealised_pnl / est_margin <= -0.75
    # Let price jump up to 0.3650
    fake.assets["CRVUSDT"]["price"] = "0.3650"
    asyncio.run(monitor.monitor_once())

    t = storage.trades()[0]
    assert t.sl_price == Decimal("0.3662")  # signal SL placed!
    risk_calls = [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/riskorder")]
    assert len(risk_calls) == 1
    assert risk_calls[0][3]["stoploss_price"] == "0.3662"
