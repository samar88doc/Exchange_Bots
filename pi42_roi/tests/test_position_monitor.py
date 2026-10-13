"""REAL reconciliation: TP confirmation, TTL cancel safety, fills, ROI square-off."""

import asyncio
from datetime import timedelta
from decimal import Decimal

from app.execution_engine import ExecutionEngine
from app.models import Mode, TradeStatus
from app.paper_broker import PaperBroker
from app.position_monitor import PositionMonitor
from app.storage import Storage
from app.symbol_mapper import SymbolMapper
from tests.helpers import SIGNAL_CRV, FakePi42, make_settings, utc

ROI_TARGET = Decimal("0.3185")


def setup(tmp_path, ttl="120", fake=None):
    settings = make_settings(tmp_path, TRADING_MODE="REAL", LIMIT_ORDER_TTL_MINUTES=ttl)
    fake = fake or FakePi42()
    client = fake.client(allow_mutations=True)
    storage = Storage(settings.database_file)
    clock = {"now": utc()}
    engine = ExecutionEngine(settings=settings, storage=storage, client=client,
                             mapper=SymbolMapper(client, 600, market=settings.market),
                             mode=Mode.REAL, now=lambda: clock["now"])
    monitor = PositionMonitor(storage=storage, client=client, mode=Mode.REAL,
                              market=settings.market, margin_asset=settings.margin_asset,
                              paper_broker=None, ttl_minutes=int(ttl),
                              target_roi_pct=settings.target_roi_pct, now=lambda: clock["now"])
    asyncio.run(engine.handle_message(channel_id=-100, message_id=300, text=SIGNAL_CRV,
                                      signal_time=clock["now"]))
    return fake, storage, monitor, clock


def close_orders(fake):
    return [c for c in fake.calls if c[1] == "/v1/order/place-order"
            and c[3].get("placeType") == "POSITION"]


def test_fill_is_recorded_then_the_roi_take_profit_is_confirmed_from_pi42(tmp_path):
    fake, storage, monitor, clock = setup(tmp_path)
    fake.fill_order(storage.trades()[0].client_order_id, price="0.3462")
    asyncio.run(monitor.monitor_once())  # cycle 1: records the fill
    asyncio.run(monitor.monitor_once())  # cycle 2: confirms the ROI target on the order
    t = storage.trades()[0]
    assert t.trade_status is TradeStatus.ENTRY_FILLED
    assert t.fill_price == Decimal("0.3462") and t.filled_quantity == Decimal("1941")
    assert t.tp_confirmed is True and t.tp_order_id is not None
    assert "8.0% ROI" in t.status_reason
    assert "real.tp_confirmed" in [e["event"] for e in storage.recent_events(20)]


def test_a_missing_take_profit_is_reported_never_faked(tmp_path):
    fake, storage, monitor, clock = setup(tmp_path)
    order = fake.fill_order(storage.trades()[0].client_order_id, price="0.3462")
    order["takeProfitPrice"] = None  # Pi42 did not attach the ROI target
    asyncio.run(monitor.monitor_once())
    asyncio.run(monitor.monitor_once())
    t = storage.trades()[0]
    assert t.tp_confirmed is False and t.tp_order_id is None
    assert t.status_reason == "TAKE-PROFIT NOT CONFIRMED"
    assert "real.tp_not_confirmed" in [e["event"] for e in storage.recent_events(20)]


def test_reaching_the_roi_target_squares_off_with_a_reduce_only_market_order(tmp_path):
    fake, storage, monitor, clock = setup(tmp_path)
    fake.fill_order(storage.trades()[0].client_order_id, price="0.3462")
    asyncio.run(monitor.monitor_once())
    fake.set_ticker("CRVUSDT", str(ROI_TARGET))
    asyncio.run(monitor.monitor_once())

    t = storage.trades()[0]
    assert t.trade_status is TradeStatus.CLOSED
    assert t.exit_reason == "TARGET_1_HIT" and t.exit_price == ROI_TARGET
    assert t.targets_hit_set() == {1}
    bodies = [c[3] for c in close_orders(fake)]
    assert len(bodies) == 1
    body = bodies[0]
    assert body["type"] == "MARKET" and body["reduceOnly"] is True
    assert body["side"] == "BUY" and body["positionId"]  # opposite side of the SHORT
    assert body["quantity"] == Decimal("1941")
    assert body["refId"] == f"CLOSE-{t.id}"
    assert "stopLossPrice" not in body


def test_roi_target_is_never_closed_twice(tmp_path):
    fake, storage, monitor, clock = setup(tmp_path)
    fake.fill_order(storage.trades()[0].client_order_id, price="0.3462")
    asyncio.run(monitor.monitor_once())
    fake.set_ticker("CRVUSDT", str(ROI_TARGET))
    asyncio.run(monitor.monitor_once())
    asyncio.run(monitor.monitor_once())
    assert len(close_orders(fake)) == 1


def test_a_price_short_of_the_target_does_not_close_the_position(tmp_path):
    fake, storage, monitor, clock = setup(tmp_path)
    fake.fill_order(storage.trades()[0].client_order_id, price="0.3462")
    asyncio.run(monitor.monitor_once())
    fake.set_ticker("CRVUSDT", "0.3300")
    asyncio.run(monitor.monitor_once())
    assert storage.trades()[0].trade_status is TradeStatus.ENTRY_FILLED
    assert close_orders(fake) == []


def test_no_stop_loss_is_ever_placed_even_when_the_market_moves_hard_against_us(tmp_path):
    """A SHORT that runs away is left open: this bot deliberately has no stop-loss."""
    fake, storage, monitor, clock = setup(tmp_path)
    fake.fill_order(storage.trades()[0].client_order_id, price="0.3462")
    asyncio.run(monitor.monitor_once())
    fake.set_ticker("CRVUSDT", "0.4000")
    asyncio.run(monitor.monitor_once())
    t = storage.trades()[0]
    assert t.trade_status is TradeStatus.ENTRY_FILLED
    assert t.exit_reason is None and t.sl_price is None and t.sl_order_id is None
    for _, _, _, body in fake.calls:
        assert not body or "stopLossPrice" not in body


def test_a_position_that_vanished_is_closed_with_an_unverified_reason(tmp_path):
    fake, storage, monitor, clock = setup(tmp_path)
    fake.fill_order(storage.trades()[0].client_order_id, price="0.3462")
    asyncio.run(monitor.monitor_once())
    fake.positions.clear()
    asyncio.run(monitor.monitor_once())
    t = storage.trades()[0]
    assert t.trade_status is TradeStatus.CLOSED
    assert t.exit_reason == "NO_LONGER_OPEN"
    assert "must be verified" in t.status_reason
    assert "real.position_closed_unverified" in [e["event"] for e in storage.recent_events(20)]


def test_a_position_that_vanished_at_the_target_is_recorded_as_a_target_hit(tmp_path):
    fake, storage, monitor, clock = setup(tmp_path)
    fake.fill_order(storage.trades()[0].client_order_id, price="0.3462")
    asyncio.run(monitor.monitor_once())
    fake.positions.clear()
    fake.set_ticker("CRVUSDT", str(ROI_TARGET))
    asyncio.run(monitor.monitor_once())
    t = storage.trades()[0]
    assert t.trade_status is TradeStatus.CLOSED and t.exit_reason == "TARGET_1_HIT"


def test_ttl_cancels_an_unfilled_entry(tmp_path):
    fake, storage, monitor, clock = setup(tmp_path, ttl="30")
    clock["now"] = clock["now"] + timedelta(minutes=31)
    asyncio.run(monitor.monitor_once())
    t = storage.trades()[0]
    assert t.trade_status is TradeStatus.CANCELLED
    assert "LIMIT_ORDER_TTL_MINUTES" in t.status_reason
    cancels = [c for c in fake.calls if c[0] == "DELETE" and c[1] == "/v1/order/delete-order"]
    assert len(cancels) == 1 and cancels[0][3]["clientOrderId"] == t.client_order_id


def test_ttl_never_cancels_an_order_that_filled_meanwhile(tmp_path):
    fake, storage, monitor, clock = setup(tmp_path, ttl="30")
    clock["now"] = clock["now"] + timedelta(minutes=31)
    fake.fill_order(storage.trades()[0].client_order_id, price="0.3462")
    asyncio.run(monitor.monitor_once())
    assert [c for c in fake.calls if c[0] == "DELETE"] == [], "a filled order must never be cancelled"
    assert storage.trades()[0].trade_status is TradeStatus.ENTRY_FILLED


def test_ttl_is_not_applied_while_the_order_is_partially_filled(tmp_path):
    fake, storage, monitor, clock = setup(tmp_path, ttl="30")
    order = fake.orders[storage.trades()[0].client_order_id]
    order["status"] = "PARTIALLY_FILLED"
    order["filledQty"] = "500"
    clock["now"] = clock["now"] + timedelta(minutes=31)
    asyncio.run(monitor.monitor_once())
    assert [c for c in fake.calls if c[0] == "DELETE"] == []
    assert storage.trades()[0].trade_status is TradeStatus.ENTRY_PENDING


def test_an_uncertain_submission_is_adopted_when_pi42_has_the_order(tmp_path):
    fake, storage, monitor, clock = setup(tmp_path)
    t = storage.trades()[0]
    storage.update_trade(t.id, clock["now"], status=TradeStatus.SUBMIT_UNCERTAIN.value,
                         client_order_id=None)
    asyncio.run(monitor.monitor_once())
    t = storage.trades()[0]
    assert t.trade_status is TradeStatus.ENTRY_PENDING
    assert t.client_order_id is not None
    assert "adopted" in t.status_reason


def test_an_uncertain_submission_without_an_order_parks_for_review_and_never_resubmits(tmp_path):
    fake, storage, monitor, clock = setup(tmp_path)
    t = storage.trades()[0]
    fake.orders.clear()
    storage.update_trade(t.id, clock["now"], status=TradeStatus.SUBMIT_UNCERTAIN.value,
                         client_order_id=None)
    asyncio.run(monitor.monitor_once())
    t = storage.trades()[0]
    assert t.trade_status is TradeStatus.NEEDS_REVIEW
    assert "NOT resubmitted automatically" in t.status_reason
    posts = [c for c in fake.calls if c[1] == "/v1/order/place-order"]
    assert len(posts) == 1, "only the original submission exists"


def test_a_trade_in_needs_review_is_left_alone(tmp_path):
    fake, storage, monitor, clock = setup(tmp_path)
    t = storage.trades()[0]
    storage.update_trade(t.id, clock["now"], status=TradeStatus.NEEDS_REVIEW.value,
                         status_reason="operator decision required")
    fake.calls.clear()
    asyncio.run(monitor.monitor_once())
    assert storage.trades()[0].trade_status is TradeStatus.NEEDS_REVIEW
    assert [c for c in fake.calls if c[0] != "GET"] == [], "a parked trade is never acted on"


def test_a_terminal_cancel_is_recorded_without_further_orders(tmp_path):
    fake, storage, monitor, clock = setup(tmp_path)
    order = fake.orders[storage.trades()[0].client_order_id]
    order["status"] = "CANCELLED"
    fake.calls.clear()
    asyncio.run(monitor.monitor_once())
    t = storage.trades()[0]
    assert t.trade_status is TradeStatus.CANCELLED and t.closed_at is not None
    assert "Pi42 order CANCELLED" in t.status_reason
    assert [c for c in fake.calls if c[0] != "GET"] == []


# --------------------------------------------------------------------- PAPER
def paper_env(tmp_path):
    settings = make_settings(tmp_path, TRADING_MODE="PAPER")
    fake = FakePi42(tickers={"CRVUSDT": "0.3400"})
    client = fake.client(allow_mutations=False)
    storage = Storage(settings.database_file)
    paper = PaperBroker(storage, initial_balance=settings.paper_initial_balance,
                        fee_rate=settings.paper_fee_rate,
                        maintenance_margin_rate=settings.paper_maintenance_margin_rate,
                        ttl_minutes=settings.limit_order_ttl_minutes)
    engine = ExecutionEngine(settings=settings, storage=storage, client=client,
                             mapper=SymbolMapper(client, 600, market=settings.market),
                             mode=Mode.PAPER, paper_broker=paper)
    monitor = PositionMonitor(storage=storage, client=client, mode=Mode.PAPER,
                              market=settings.market, margin_asset=settings.margin_asset,
                              paper_broker=paper, ttl_minutes=settings.limit_order_ttl_minutes,
                              target_roi_pct=settings.target_roi_pct)
    asyncio.run(engine.handle_message(channel_id=-100, message_id=1, text=SIGNAL_CRV,
                                      signal_time=utc()))
    return fake, storage, monitor


def test_paper_monitor_drives_the_whole_lifecycle_with_reads_only(tmp_path):
    fake, storage, monitor = paper_env(tmp_path)

    async def scenario():
        # A SHORT limit at 0.3462 is not filled while the market is below it.
        fake.set_ticker("CRVUSDT", "0.3400")
        await monitor.monitor_once()
        assert storage.trades()[0].trade_status is TradeStatus.ENTRY_PENDING
        # The market rises through the limit -> simulated fill at the limit price.
        fake.set_ticker("CRVUSDT", "0.3470")
        await monitor.monitor_once()
        assert storage.trades()[0].trade_status is TradeStatus.ENTRY_FILLED
        # The market falls to the ROI target -> simulated 8% ROI take-profit.
        fake.set_ticker("CRVUSDT", str(ROI_TARGET))
        await monitor.monitor_once()

    asyncio.run(scenario())
    t = storage.trades()[0]
    assert t.trade_status is TradeStatus.CLOSED
    assert t.exit_reason == "TAKE_PROFIT_1" and t.fill_price == Decimal("0.3462")
    assert t.realized_pnl > 0 and t.mode == "PAPER"
    assert [c for c in fake.calls if c[0] != "GET"] == []
    storage.close()


def test_paper_monitor_survives_a_price_outage(tmp_path):
    fake, storage, monitor = paper_env(tmp_path)
    fake.tickers.pop("CRVUSDT")

    async def scenario():
        await monitor.monitor_once()

    asyncio.run(scenario())
    assert storage.trades()[0].trade_status is TradeStatus.ENTRY_PENDING
    assert [c for c in fake.calls if c[0] != "GET"] == []
    storage.close()
