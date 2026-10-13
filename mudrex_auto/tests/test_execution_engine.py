"""End-to-end decision flows through the real client against the fake Mudrex server."""

import asyncio
import json
from datetime import timedelta
from decimal import Decimal

import httpx

from app.execution_engine import ExecutionEngine, make_client_order_id
from app.models import Mode, TradeStatus
from app.paper_broker import PaperBroker
from app.storage import Storage
from app.symbol_mapper import SymbolMapper
from tests.helpers import (
    CHANNEL_ID,
    SIGNAL_CRV,
    SIGNAL_EIGEN,
    FakeMudrex,
    make_settings,
    utc,
)

FIXED_NOW = utc()


def build(tmp_path, *, mode=Mode.PAPER, fake=None, now=None, **env):
    settings = make_settings(tmp_path, TRADING_MODE=mode.value, **env)
    fake = fake or FakeMudrex()
    client = fake.client(allow_mutations=mode is Mode.REAL)
    storage = Storage(settings.database_file)
    clock = {"now": now or FIXED_NOW}
    paper = None
    if mode is Mode.PAPER:
        paper = PaperBroker(storage, initial_balance=settings.paper_initial_balance_usdt,
                            fee_rate=settings.paper_fee_rate,
                            maintenance_margin_rate=settings.paper_maintenance_margin_rate,
                            ttl_minutes=settings.limit_order_ttl_minutes)
    engine = ExecutionEngine(settings=settings, storage=storage, client=client,
                             mapper=SymbolMapper(client, 600), mode=mode, paper_broker=paper,
                             now=lambda: clock["now"])
    return dict(settings=settings, fake=fake, storage=storage, engine=engine, clock=clock, paper=paper,
                client=client)


def handle(env, text, message_id, signal_time=None):
    return asyncio.run(env["engine"].handle_message(
        channel_id=CHANNEL_ID, message_id=message_id, text=text,
        signal_time=signal_time or env["clock"]["now"]))


def test_paper_crv_creates_pending_limit_trade_and_no_mutation(tmp_path):
    env = build(tmp_path)
    assert handle(env, SIGNAL_CRV, 100) == "ACCEPTED"
    trades = env["storage"].trades(mode="PAPER")
    assert len(trades) == 1
    t = trades[0]
    assert t.symbol == "CRVUSDT" and t.direction == "SHORT"
    assert t.trade_status is TradeStatus.ENTRY_PENDING
    assert t.entry_price == Decimal("0.3462") and t.target1 == Decimal("0.3361")
    assert t.leverage == Decimal("14") and t.telegram_leverage == Decimal("14")
    assert t.quantity == Decimal("1941.0")
    assert t.est_margin <= Decimal("48")
    assert env["fake"].mutating_calls() == []


def test_paper_eigen_fixture_leverage_capped_by_asset_max(tmp_path):
    from tests.helpers import ASSET_EIGEN
    fake = FakeMudrex(assets=[{**ASSET_EIGEN, "max_leverage": "10"}])
    env = build(tmp_path, fake=fake)
    assert handle(env, SIGNAL_EIGEN, 101) == "ACCEPTED"
    t = env["storage"].trades(mode="PAPER")[0]
    assert t.leverage == Decimal("10") and t.telegram_leverage == Decimal("16")


def test_duplicate_message_is_never_executed_twice(tmp_path):
    env = build(tmp_path)
    assert handle(env, SIGNAL_CRV, 100) == "ACCEPTED"
    assert handle(env, SIGNAL_CRV, 100) == "DUPLICATE"
    assert len(env["storage"].trades()) == 1


def test_edited_message_with_new_content_but_same_id_creates_nothing(tmp_path):
    env = build(tmp_path)
    handle(env, SIGNAL_CRV, 100)
    edited = SIGNAL_CRV.replace("Entry: 0.3462", "Entry: 0.3400")
    assert handle(env, edited, 100) == "DUPLICATE"
    assert len(env["storage"].trades()) == 1


def test_duplicate_content_in_new_message_is_rejected(tmp_path):
    env = build(tmp_path)
    handle(env, SIGNAL_CRV, 100)
    env["storage"].update_trade(1, env["clock"]["now"], status="CLOSED")  # first trade finished
    assert handle(env, SIGNAL_CRV, 101) == "REJECTED"
    reason = env["storage"].get_signal(2)["decision_reason"]
    assert "duplicate signal content" in reason


def test_stale_signal_is_rejected_not_queued(tmp_path):
    env = build(tmp_path)
    old = env["clock"]["now"] - timedelta(minutes=6)
    assert handle(env, SIGNAL_CRV, 102, signal_time=old) == "REJECTED"
    assert env["storage"].trades() == []
    assert "stale signal" in env["storage"].get_signal(1)["decision_reason"]


def test_kill_switch_file_blocks_new_trades(tmp_path):
    env = build(tmp_path)
    env["settings"].kill_switch_file.write_text("stop")
    assert handle(env, SIGNAL_CRV, 103) == "REJECTED"
    assert env["storage"].trades() == []


def test_trading_enabled_false_blocks_new_trades(tmp_path):
    env = build(tmp_path, TRADING_ENABLED="false")
    assert handle(env, SIGNAL_CRV, 104) == "REJECTED"


def test_unsupported_symbol_is_rejected_with_reason(tmp_path):
    env = build(tmp_path)
    text = SIGNAL_CRV.replace("#CRV/USDT", "#NOPE/USDT")
    assert handle(env, text, 105) == "REJECTED"
    assert "NOPEUSDT is not listed" in env["storage"].get_signal(1)["decision_reason"]


def test_ordinary_chat_is_ignored_not_rejected(tmp_path):
    env = build(tmp_path)
    assert handle(env, "gm everyone", 106) == "IGNORED"


def test_one_active_trade_per_symbol(tmp_path):
    env = build(tmp_path)
    assert handle(env, SIGNAL_CRV, 107) == "ACCEPTED"
    other = SIGNAL_CRV.replace("Entry: 0.3462", "Entry: 0.3470")
    assert handle(env, other, 108) == "REJECTED"
    assert "active trade already exists" in env["storage"].get_signal(2)["decision_reason"]


def test_max_concurrent_positions_enforced_not_queued(tmp_path):
    from tests.helpers import ASSET_EIGEN
    fake = FakeMudrex(assets=[{**ASSET_EIGEN}, {**__import__("tests.helpers", fromlist=["ASSET_CRV"]).ASSET_CRV}])
    env = build(tmp_path, fake=fake, MAX_CONCURRENT_POSITIONS="1")
    assert handle(env, SIGNAL_CRV, 109) == "ACCEPTED"
    assert handle(env, SIGNAL_EIGEN, 110) == "REJECTED"
    assert "max concurrent positions" in env["storage"].get_signal(2)["decision_reason"]


def test_entry_deviation_skips_instead_of_moving_limit(tmp_path):
    from tests.helpers import ASSET_CRV
    fake = FakeMudrex(assets=[{**ASSET_CRV, "price": "0.4000"}])
    env = build(tmp_path, fake=fake)
    assert handle(env, SIGNAL_CRV, 111) == "REJECTED"
    assert "away from Entry" in env["storage"].get_signal(1)["decision_reason"]


def test_restart_does_not_reexecute_processed_message(tmp_path):
    env = build(tmp_path)
    assert handle(env, SIGNAL_CRV, 112) == "ACCEPTED"
    env["storage"].close()
    env2 = build(tmp_path, fake=env["fake"])
    assert handle(env2, SIGNAL_CRV, 112) == "DUPLICATE"
    assert len(env2["storage"].trades()) == 1


def test_insufficient_paper_balance_rejects(tmp_path):
    env = build(tmp_path, PAPER_INITIAL_BALANCE_USDT="10")
    assert handle(env, SIGNAL_CRV, 113) == "REJECTED"
    assert "insufficient available balance" in env["storage"].get_signal(1)["decision_reason"]


def test_client_order_id_is_deterministic_and_valid():
    cid1 = make_client_order_id(-1001, 77, "CRVUSDT")
    cid2 = make_client_order_id(-1001, 77, "CRVUSDT")
    assert cid1 == cid2
    assert len(cid1) <= 64
    import re
    assert re.fullmatch(r"[A-Za-z0-9_-]+", cid1)


# ----------------------------------------------------------------- REAL mode
def real_env(tmp_path, fake=None, **env):
    return build(tmp_path, mode=Mode.REAL, fake=fake, **env)


def test_real_flow_sets_leverage_before_order_and_places_limit_with_tp_only(tmp_path):
    env = real_env(tmp_path)
    assert handle(env, SIGNAL_CRV, 200) == "ACCEPTED"
    calls = env["fake"].calls
    lev_idx = next(i for i, c in enumerate(calls) if c[0] == "POST" and c[1].endswith("/leverage"))
    order_idx = next(i for i, c in enumerate(calls) if c[0] == "POST" and c[1] == "/fapi/v2/futures/order")
    assert lev_idx < order_idx, "leverage must be configured before the entry order"
    assert calls[lev_idx][3]["leverage"] == "14" and calls[lev_idx][3]["margin_type"] == "ISOLATED"
    body = calls[order_idx][3]
    assert body["trigger_type"] == "LIMIT" and body["order_price"] == "0.3462"
    assert body["takeprofit_price"] == "0.3361" and "stoploss_price" not in body
    t = env["storage"].trades(mode="REAL")[0]
    assert t.trade_status is TradeStatus.ENTRY_PENDING and t.order_id and t.position_id
    assert t.client_order_id == body["client_order_id"]


def test_real_leverage_not_confirmed_places_no_order(tmp_path):
    env = real_env(tmp_path)

    def set_but_report_other_value(request):
        if request.method == "POST" and request.url.path.endswith("/leverage"):
            return httpx.Response(200, json={"success": True, "data": {"margin_type": "ISOLATED", "leverage": "3"}})
        if request.method == "GET" and request.url.path.endswith("/leverage"):
            return None  # real handler answers (404 before set / 3 after a faked set)
        return None

    env["fake"].fail_next.append(set_but_report_other_value)
    assert handle(env, SIGNAL_CRV, 201) == "REJECTED"
    assert [c for c in env["fake"].calls if c[1] == "/fapi/v2/futures/order"] == []
    assert "leverage not confirmed" in env["storage"].trades(mode="REAL")[0].status_reason


def test_real_timeout_after_submit_is_reconciled_by_client_order_id_not_resubmitted(tmp_path):
    env = real_env(tmp_path)
    import httpx

    def timeout_once(request):
        if request.method == "POST" and request.url.path == "/fapi/v2/futures/order":
            # The order reaches Mudrex (created server-side), but the response is lost.
            env["fake"]._create_order({"symbol": "CRVUSDT"}, json.loads(request.content))
            raise httpx.ReadTimeout("response lost", request=request)
        return None

    env["fake"].fail_next.append(timeout_once)
    assert handle(env, SIGNAL_CRV, 202) == "ACCEPTED"
    posts = [c for c in env["fake"].calls if c[0] == "POST" and c[1] == "/fapi/v2/futures/order"]
    assert len(posts) == 1, "must not blindly resubmit after a timeout"
    lookups = [c for c in env["fake"].calls if c[0] == "GET" and c[1] == "/fapi/v1/futures/orders/detail"]
    assert any("client_order_id" in c[2] for c in lookups), "must query by client_order_id"
    t = env["storage"].trades(mode="REAL")[0]
    assert t.trade_status is TradeStatus.ENTRY_PENDING and t.order_id


def test_real_timeout_with_no_order_parks_for_review_without_resubmit(tmp_path):
    env = real_env(tmp_path)
    import httpx

    def timeout_before_creation(request):
        if request.method == "POST" and request.url.path == "/fapi/v2/futures/order":
            raise httpx.ReadTimeout("lost", request=request)
        return None

    env["fake"].fail_next.append(timeout_before_creation)
    handle(env, SIGNAL_CRV, 203)
    posts = [c for c in env["fake"].calls if c[0] == "POST" and c[1] == "/fapi/v2/futures/order"]
    assert len(posts) == 1
    assert env["storage"].trades(mode="REAL")[0].trade_status is TradeStatus.NEEDS_REVIEW


def test_real_definitive_rejection_is_recorded(tmp_path):
    env = real_env(tmp_path)
    env["fake"].fail_next.append(lambda r: __import__("httpx").Response(
        400, json={"success": False, "errors": [{"code": 400, "text": "order value less than minimum required value"}]})
        if r.method == "POST" and r.url.path == "/fapi/v2/futures/order" else None)
    assert handle(env, SIGNAL_CRV, 204) == "REJECTED"
    t = env["storage"].trades(mode="REAL")[0]
    assert t.trade_status is TradeStatus.REJECTED
    assert "minimum required value" in t.status_reason


def test_real_open_position_on_mudrex_blocks_duplicate(tmp_path):
    env = real_env(tmp_path)
    env["fake"].positions["pos-x"] = {"id": "pos-x", "position_id": "pos-x", "symbol": "CRVUSDT",
                                      "order_type": "SHORT", "status": "OPEN", "entry_price": "0.35",
                                      "quantity": "10", "leverage": "5", "takeprofit": None}
    assert handle(env, SIGNAL_CRV, 205) == "REJECTED"
    assert "open position already exists on Mudrex" in env["storage"].get_signal(1)["decision_reason"]
    assert [c for c in env["fake"].calls if c[1] == "/fapi/v2/futures/order"] == []


def test_real_insufficient_futures_balance_rejects(tmp_path):
    env = real_env(tmp_path, fake=FakeMudrex(balance="10"))
    assert handle(env, SIGNAL_CRV, 206) == "REJECTED"
    assert "insufficient available balance" in env["storage"].get_signal(1)["decision_reason"]
    assert [c for c in env["fake"].calls if c[0] == "POST"] == []


def test_paper_signal_with_off_step_target_is_snapped_and_accepted(tmp_path):
    env = build(tmp_path)
    # Signal with extra fractional decimal places in Target 1
    signal_text = (
        "#CRV/USDT - Short\n"
        "Entry: 0.3462\n"
        "Target 1: 0.33614892\n"
        "Leverage: x14\n"
    )
    assert handle(env, signal_text, 207) == "ACCEPTED"
    t = env["storage"].trades(mode="PAPER")[0]
    assert t.target1 == Decimal("0.3361")
