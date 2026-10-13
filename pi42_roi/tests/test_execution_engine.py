"""End-to-end decision flows through the client against the fake Pi42 API."""

import asyncio
import json
import re
from datetime import timedelta
from decimal import Decimal

import httpx

from app.execution_engine import ExecutionEngine, make_ref_id, make_tp_ref_id
from app.models import Mode, TradeStatus
from app.paper_broker import PaperBroker
from app.storage import Storage
from app.symbol_mapper import SymbolMapper
from tests.helpers import (
    CHANNEL_ID,
    CONTRACT_BTC_INR,
    CONTRACT_CRV,
    CONTRACT_EIGEN,
    SIGNAL_CRV,
    SIGNAL_EIGEN,
    FakePi42,
    make_settings,
    utc,
)

FIXED_NOW = utc()
ROI_TARGET_CRV = Decimal("0.3185")  # 0.3462 - 8% ROI, snapped to Pi42 price precision


def build(tmp_path, *, mode=Mode.PAPER, fake=None, now=None, **env):
    settings = make_settings(tmp_path, TRADING_MODE=mode.value, **env)
    fake = fake or FakePi42()
    client = fake.client(allow_mutations=mode is Mode.REAL)
    storage = Storage(settings.database_file)
    clock = {"now": now or FIXED_NOW}
    paper = None
    if mode is Mode.PAPER:
        paper = PaperBroker(storage, initial_balance=settings.paper_initial_balance,
                            fee_rate=settings.paper_fee_rate,
                            maintenance_margin_rate=settings.paper_maintenance_margin_rate,
                            ttl_minutes=settings.limit_order_ttl_minutes)
    engine = ExecutionEngine(settings=settings, storage=storage, client=client,
                             mapper=SymbolMapper(client, 600, market=settings.market), mode=mode,
                             paper_broker=paper, now=lambda: clock["now"])
    return dict(settings=settings, fake=fake, storage=storage, engine=engine, clock=clock,
                paper=paper, client=client)


def handle(env, text, message_id, signal_time=None):
    return asyncio.run(env["engine"].handle_message(
        channel_id=CHANNEL_ID, message_id=message_id, text=text,
        signal_time=signal_time or env["clock"]["now"]))


def order_bodies(fake, path="/v1/order/place-order"):
    return [c[3] for c in fake.calls if c[0] == "POST" and c[1] == path]


# ------------------------------------------------------------------- PAPER
def test_paper_crv_creates_a_pending_limit_trade_and_never_mutates(tmp_path):
    env = build(tmp_path)
    assert handle(env, SIGNAL_CRV, 100) == "ACCEPTED"
    trades = env["storage"].trades(mode="PAPER")
    assert len(trades) == 1
    t = trades[0]
    assert t.symbol == "CRVUSDT" and t.direction == "SHORT"
    assert t.market == "USDT" and t.margin_asset == "USDT"
    assert t.trade_status is TradeStatus.ENTRY_PENDING
    assert t.entry_price == Decimal("0.3462") and t.target1 == ROI_TARGET_CRV
    assert t.leverage == Decimal("14") and t.telegram_leverage == Decimal("14")
    assert t.quantity == Decimal("1941.0")
    assert t.est_margin <= Decimal("48")
    assert t.stop_loss_reference == Decimal("0.3662"), "signal SL is kept for reference only"
    assert env["fake"].mutating_calls() == []


def test_the_exit_target_is_roi_based_not_the_signal_target(tmp_path):
    """TARGET_ROI_PCT drives the exit, never the Telegram target prices."""
    env = build(tmp_path)
    handle(env, SIGNAL_CRV, 100)
    t = env["storage"].trades(mode="PAPER")[0]
    assert t.target1 == ROI_TARGET_CRV
    assert t.target1 != Decimal("0.3361")  # Telegram Target 1
    assert t.target2 is None and t.target3 is None and t.target4 is None and t.target5 is None


def test_target_roi_pct_is_configurable(tmp_path):
    env = build(tmp_path, TARGET_ROI_PCT="5")
    handle(env, SIGNAL_CRV, 100)
    t = env["storage"].trades(mode="PAPER")[0]
    assert t.target1 == Decimal("0.3289")  # 0.3462 * 0.95 = 0.32889 -> 4 decimals


def test_paper_eigen_leverage_is_capped_by_the_pi42_contract_maximum(tmp_path):
    capped = {**CONTRACT_EIGEN, "maxLeverage": "10"}
    fake = FakePi42(contracts=[CONTRACT_CRV, capped], tickers={"CRVUSDT": "0.3500",
                                                              "EIGENUSDT": "0.2440"})
    env = build(tmp_path, fake=fake)
    assert handle(env, SIGNAL_EIGEN, 101) == "ACCEPTED"
    t = env["storage"].trades(mode="PAPER")[0]
    assert t.leverage == Decimal("10") and t.telegram_leverage == Decimal("16")
    assert t.symbol == "EIGENUSDT"


def test_duplicate_message_is_never_executed_twice(tmp_path):
    env = build(tmp_path)
    assert handle(env, SIGNAL_CRV, 100) == "ACCEPTED"
    assert handle(env, SIGNAL_CRV, 100) == "DUPLICATE"
    assert len(env["storage"].trades()) == 1


def test_edited_message_with_new_content_but_the_same_id_creates_nothing(tmp_path):
    env = build(tmp_path)
    handle(env, SIGNAL_CRV, 100)
    edited = SIGNAL_CRV.replace("Entry: 0.3462", "Entry: 0.3400")
    assert handle(env, edited, 100) == "DUPLICATE"
    assert len(env["storage"].trades()) == 1


def test_duplicate_content_in_a_new_message_is_rejected(tmp_path):
    env = build(tmp_path)
    handle(env, SIGNAL_CRV, 100)
    env["storage"].update_trade(1, env["clock"]["now"], status="CLOSED")
    assert handle(env, SIGNAL_CRV, 101) == "REJECTED"
    assert "duplicate signal content" in env["storage"].get_signal(2)["decision_reason"]


def test_stale_signal_is_rejected_not_queued(tmp_path):
    env = build(tmp_path)
    old = env["clock"]["now"] - timedelta(minutes=6)
    assert handle(env, SIGNAL_CRV, 102, signal_time=old) == "REJECTED"
    assert env["storage"].trades() == []
    assert "stale signal" in env["storage"].get_signal(1)["decision_reason"]


def test_signal_from_the_future_is_rejected(tmp_path):
    env = build(tmp_path)
    ahead = env["clock"]["now"] + timedelta(minutes=5)
    assert handle(env, SIGNAL_CRV, 103, signal_time=ahead) == "REJECTED"
    assert "future" in env["storage"].get_signal(1)["decision_reason"]


def test_kill_switch_file_blocks_new_trades(tmp_path):
    env = build(tmp_path)
    env["settings"].kill_switch_file.write_text("stop")
    assert handle(env, SIGNAL_CRV, 104) == "REJECTED"
    assert env["storage"].trades() == []


def test_trading_enabled_false_blocks_new_trades(tmp_path):
    env = build(tmp_path, TRADING_ENABLED="false")
    assert handle(env, SIGNAL_CRV, 105) == "REJECTED"


def test_an_unlisted_symbol_is_rejected_with_the_reason(tmp_path):
    env = build(tmp_path)
    text = SIGNAL_CRV.replace("#CRV/USDT", "#NOPE/USDT")
    assert handle(env, text, 106) == "REJECTED"
    assert "NOPEUSDT is not listed" in env["storage"].get_signal(1)["decision_reason"]


def test_a_signal_quoted_in_another_currency_is_rejected_not_converted(tmp_path):
    env = build(tmp_path)  # PI42_MARKET=USDT
    text = SIGNAL_CRV.replace("#CRV/USDT", "#CRV/INR")
    assert handle(env, text, 107) == "REJECTED"
    assert "never converted" in env["storage"].get_signal(1)["decision_reason"]


def test_inr_market_maps_signals_to_inr_contracts(tmp_path):
    fake = FakePi42(contracts=[CONTRACT_BTC_INR, CONTRACT_CRV],
                    tickers={"BTCINR": "8100000"})
    env = build(tmp_path, fake=fake, PI42_MARKET="INR")
    text = "#BTC/INR Long\nEntry: 8000000\nTarget 1: 8800000\nLeverage: x5"
    assert handle(env, text, 108) == "ACCEPTED"
    t = env["storage"].trades(mode="PAPER")[0]
    assert t.symbol == "BTCINR" and t.market == "INR" and t.margin_asset == "INR"
    assert t.target1 == Decimal("8640000.0")  # 8% ROI, snapped to 0.1


def test_ordinary_chat_is_ignored_not_rejected(tmp_path):
    env = build(tmp_path)
    assert handle(env, "gm everyone", 109) == "IGNORED"


def test_only_one_active_trade_per_symbol(tmp_path):
    env = build(tmp_path)
    assert handle(env, SIGNAL_CRV, 110) == "ACCEPTED"
    other = SIGNAL_CRV.replace("Entry: 0.3462", "Entry: 0.3470")
    assert handle(env, other, 111) == "REJECTED"
    assert "active trade already exists" in env["storage"].get_signal(2)["decision_reason"]


def test_max_concurrent_positions_is_enforced_not_queued(tmp_path):
    env = build(tmp_path, MAX_CONCURRENT_POSITIONS="1",
                PI42_MARKET="USDT")
    env["fake"].set_ticker("EIGENUSDT", "0.2440")
    assert handle(env, SIGNAL_CRV, 112) == "ACCEPTED"
    assert handle(env, SIGNAL_EIGEN, 113) == "REJECTED"
    assert "max concurrent positions" in env["storage"].get_signal(2)["decision_reason"]


def test_entry_deviation_skips_instead_of_moving_the_limit(tmp_path):
    fake = FakePi42(tickers={"CRVUSDT": "0.4000"})
    env = build(tmp_path, fake=fake)
    assert handle(env, SIGNAL_CRV, 114) == "REJECTED"
    assert "away from Entry" in env["storage"].get_signal(1)["decision_reason"]


def test_restart_does_not_reexecute_a_processed_message(tmp_path):
    env = build(tmp_path)
    assert handle(env, SIGNAL_CRV, 115) == "ACCEPTED"
    env["storage"].close()
    env2 = build(tmp_path, fake=env["fake"])
    assert handle(env2, SIGNAL_CRV, 115) == "DUPLICATE"
    assert len(env2["storage"].trades()) == 1


def test_insufficient_paper_balance_rejects_and_places_nothing(tmp_path):
    env = build(tmp_path, PAPER_INITIAL_BALANCE="10")
    assert handle(env, SIGNAL_CRV, 116) == "REJECTED"
    assert "insufficient" in env["storage"].get_signal(1)["decision_reason"]
    assert env["fake"].mutating_calls() == []


def test_take_profit_that_would_trigger_instantly_is_rejected_before_placing(tmp_path):
    # SHORT entry 0.3462 with an 8% ROI target of 0.3185: a market at 0.3000 is
    # already through the target, and Pi42 refuses such an order (error 3011).
    # ROI 2% keeps the target inside the 5% deviation window so this check, not
    # the deviation check, is what rejects the signal.
    fake = FakePi42(tickers={"CRVUSDT": "0.3350"})
    env = build(tmp_path, fake=fake, TARGET_ROI_PCT="2", MAX_ENTRY_DEVIATION_PCT="5")
    assert handle(env, SIGNAL_CRV, 117) == "REJECTED"
    assert "instantly" in env["storage"].get_signal(1)["decision_reason"]
    assert env["fake"].mutating_calls() == []


def test_unsupported_order_type_contract_is_rejected(tmp_path):
    market_only = {**CONTRACT_CRV, "orderTypes": ["MARKET"]}
    env = build(tmp_path, fake=FakePi42(contracts=[market_only]))
    assert handle(env, SIGNAL_CRV, 118) == "REJECTED"
    assert "does not support LIMIT" in env["storage"].get_signal(1)["decision_reason"]


def test_metadata_outage_skips_the_signal_instead_of_guessing(tmp_path):
    fake = FakePi42()
    fake.fail_next.append(lambda r: httpx.Response(500, json={"message": "down"})
                          if r.url.path == "/v1/exchange/exchangeInfo" else None)
    env = build(tmp_path, fake=fake)
    assert handle(env, SIGNAL_CRV, 119) == "REJECTED"
    assert "unavailable" in env["storage"].get_signal(1)["decision_reason"]


def test_reference_ids_are_deterministic_and_pi42_safe():
    first = make_ref_id(-1001, 77, "CRVUSDT")
    second = make_ref_id(-1001, 77, "CRVUSDT")
    other = make_ref_id(-1001, 78, "CRVUSDT")
    assert first == second and first != other
    assert re.fullmatch(r"[A-Za-z0-9-]+", first)
    assert first.startswith("TG-77-")
    assert len(first) <= 64
    assert re.fullmatch(r"[A-Za-z0-9-]+", make_tp_ref_id(1, "TP1"))


# -------------------------------------------------------------------- REAL
def real_env(tmp_path, fake=None, **env):
    return build(tmp_path, mode=Mode.REAL, fake=fake, **env)


def test_real_flow_sets_leverage_before_the_order_and_sends_tp_but_no_sl(tmp_path):
    env = real_env(tmp_path)
    assert handle(env, SIGNAL_CRV, 200) == "ACCEPTED"
    calls = env["fake"].calls
    pref_idx = next(i for i, c in enumerate(calls) if c[1] == "/v1/exchange/update/preference")
    order_idx = next(i for i, c in enumerate(calls) if c[1] == "/v1/order/place-order")
    assert pref_idx < order_idx, "leverage and margin mode must be set before the entry order"
    preference = {k: v for k, v in calls[pref_idx][3].items() if k != "timestamp"}
    assert preference == {"leverage": 14, "marginMode": "ISOLATED", "contractName": "CRVUSDT"}

    body = calls[order_idx][3]
    assert body["type"] == "LIMIT" and body["placeType"] == "ORDER_FORM"
    assert body["price"] == Decimal("0.3462")
    assert body["symbol"] == "CRVUSDT" and body["side"] == "SELL"
    assert body["takeProfitPrice"] == Decimal("0.3185")
    assert body["reduceOnly"] is False and "positionId" not in body
    assert body["refId"] == make_ref_id(CHANNEL_ID, 200, "CRVUSDT")

    t = env["storage"].trades(mode="REAL")[0]
    assert t.trade_status is TradeStatus.ENTRY_PENDING
    assert t.client_order_id and t.client_order_id == env["fake"].latest_order()["clientOrderId"]
    assert t.status_reason.startswith("accepted by Pi42")


def test_no_stop_loss_field_is_ever_sent_to_pi42(tmp_path):
    env = real_env(tmp_path)
    assert handle(env, SIGNAL_CRV, 201) == "ACCEPTED"
    for body in order_bodies(env["fake"]):
        assert "stopLossPrice" not in body and "stop_price" not in body
    trade = env["storage"].trades(mode="REAL")[0]
    assert trade.sl_price is None and trade.sl_order_id is None


def test_leverage_not_confirmed_by_pi42_places_no_order(tmp_path):
    env = real_env(tmp_path)

    def report_a_different_leverage(request):
        if request.method == "POST" and request.url.path == "/v1/exchange/update/preference":
            return httpx.Response(200, json={"contractName": "CRVUSDT", "updatedLeverage": 3,
                                             "marginMode": "ISOLATED"})
        return None

    env["fake"].fail_next.append(report_a_different_leverage)
    assert handle(env, SIGNAL_CRV, 202) == "REJECTED"
    assert order_bodies(env["fake"]) == []
    t = env["storage"].trades(mode="REAL")[0]
    assert t.trade_status is TradeStatus.REJECTED
    assert "leverage not confirmed" in t.status_reason


def test_leverage_refused_while_an_order_is_open_places_nothing(tmp_path):
    env = real_env(tmp_path)
    env["fake"].orders["cli-existing"] = {
        "clientOrderId": "cli-existing", "symbol": "CRVUSDT", "status": "NEW",
        "type": "LIMIT", "side": "BUY", "orderAmount": "1", "filledAmount": "0",
    }
    assert handle(env, SIGNAL_CRV, 203) == "REJECTED"
    assert "open order already exists on Pi42" in env["storage"].get_signal(1)["decision_reason"]
    assert env["fake"].mutating_calls() == []


def test_open_position_on_pi42_blocks_a_duplicate(tmp_path):
    env = real_env(tmp_path)
    env["fake"].positions["pos-x"] = {
        "positionId": "pos-x", "contractPair": "CRVUSDT", "positionType": "SHORT",
        "positionStatus": "OPEN", "entryPrice": "0.35", "positionAmount": "10", "leverage": "5",
    }
    assert handle(env, SIGNAL_CRV, 204) == "REJECTED"
    assert "open position already exists on Pi42" in env["storage"].get_signal(1)["decision_reason"]
    assert env["fake"].mutating_calls() == []


def test_insufficient_futures_wallet_balance_rejects_without_posting(tmp_path):
    env = real_env(tmp_path, fake=FakePi42(balance="10"))
    assert handle(env, SIGNAL_CRV, 205) == "REJECTED"
    assert "insufficient futures-wallet balance" in env["storage"].get_signal(1)["decision_reason"]
    assert env["fake"].mutating_calls() == []


def test_duplicate_ref_error_adopts_the_existing_order_instead_of_resubmitting(tmp_path):
    """Pi42 error 3019 means our refId already produced an order: adopt, never duplicate."""
    env = real_env(tmp_path)
    ref_id = make_ref_id(CHANNEL_ID, 206, "CRVUSDT")
    seeded = env["fake"]._place_order({
        "placeType": "ORDER_FORM", "symbol": "CRVUSDT", "side": "SELL", "type": "LIMIT",
        "quantity": 1941, "price": "0.3462", "reduceOnly": False, "marginAsset": "USDT",
        "takeProfitPrice": "0.3185", "refId": ref_id,
    }).json()
    # The order was cancelled earlier, so it is not "open" any more - only its refId remains.
    env["fake"].orders[seeded["clientOrderId"]]["status"] = "CANCELLED"

    assert handle(env, SIGNAL_CRV, 206) == "ACCEPTED"
    attempts = [c for c in env["fake"].calls if c[1] == "/v1/order/place-order"]
    assert len(attempts) == 1, "exactly one place-order attempt is made"
    assert len([o for o in env["fake"].orders.values() if o.get("refId") == ref_id]) == 1
    t = env["storage"].trades(mode="REAL")[0]
    assert t.client_order_id == seeded["clientOrderId"]
    assert t.trade_status is TradeStatus.CANCELLED
    assert "adopted Pi42 order status CANCELLED" in t.status_reason
    assert "real.reconciled" in [e["event"] for e in env["storage"].recent_events(20)]


def test_lost_response_is_reconciled_by_ref_id_and_never_resubmitted(tmp_path):
    env = real_env(tmp_path)

    def place_then_lose_the_response(request):
        if request.url.path != "/v1/order/place-order":
            return None
        body = json.loads(request.content.decode())
        env["fake"]._place_order(body)
        raise httpx.ReadTimeout("response lost", request=request)

    env["fake"].fail_next.append(place_then_lose_the_response)
    assert handle(env, SIGNAL_CRV, 207) == "ACCEPTED"
    posts = order_bodies(env["fake"])
    assert len(posts) == 1, "must not blindly resubmit after an ambiguous mutation"
    lookups = [c for c in env["fake"].calls if c[1] == "/v1/order/ref-id"]
    assert lookups and lookups[-1][2]["ref_id"] == make_ref_id(CHANNEL_ID, 207, "CRVUSDT")
    t = env["storage"].trades(mode="REAL")[0]
    assert t.trade_status is TradeStatus.ENTRY_PENDING and t.client_order_id


def test_lost_response_with_no_order_parks_the_trade_for_review(tmp_path):
    env = real_env(tmp_path)

    def lose_the_response(request):
        if request.url.path == "/v1/order/place-order":
            raise httpx.ReadTimeout("lost", request=request)
        return None

    env["fake"].fail_next.append(lose_the_response)
    handle(env, SIGNAL_CRV, 208)
    assert len(order_bodies(env["fake"])) == 1
    t = env["storage"].trades(mode="REAL")[0]
    assert t.trade_status is TradeStatus.NEEDS_REVIEW
    assert "NOT resubmitted automatically" in t.status_reason


def test_definitive_rejection_is_recorded_with_the_pi42_message(tmp_path):
    env = real_env(tmp_path)
    env["fake"].fail_next.append(lambda r: httpx.Response(
        400, json={"code": 3018, "message": "Insufficient margin available"})
        if r.url.path == "/v1/order/place-order" else None)
    assert handle(env, SIGNAL_CRV, 209) == "REJECTED"
    t = env["storage"].trades(mode="REAL")[0]
    assert t.trade_status is TradeStatus.REJECTED
    assert "Insufficient margin" in t.status_reason and "3018" in t.status_reason


def test_trade_row_exists_before_the_order_is_submitted(tmp_path):
    """The trade is persisted as SUBMITTING before the Pi42 call, so a crash
    between the two can never lose the fact that a request may have gone out."""
    env = real_env(tmp_path)
    seen = {}

    def inspect_state(request):
        if request.url.path == "/v1/order/place-order":
            seen["statuses"] = [t.status for t in env["storage"].trades(mode="REAL")]
            seen["signal"] = env["storage"].get_signal(1)["status"]
        return None

    env["fake"].fail_next.append(inspect_state)
    handle(env, SIGNAL_CRV, 210)
    assert seen["statuses"] == [TradeStatus.SUBMITTING.value]
    assert seen["signal"] == "ACCEPTED"


def test_same_signal_never_produces_two_orders(tmp_path):
    env = real_env(tmp_path)
    assert handle(env, SIGNAL_CRV, 211) == "ACCEPTED"
    assert handle(env, SIGNAL_CRV, 211) == "DUPLICATE"
    assert len(order_bodies(env["fake"])) == 1
    assert len(env["storage"].trades(mode="REAL")) == 1
