"""Acceptance tests TEST 1 – TEST 18 from the specification."""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.entities import Order, Position, Signal, Trade
from tests.conftest import alert_payload

D = Decimal


async def post_alert(client, **kwargs):
    payload = alert_payload(**kwargs)
    resp = await client.post("/api/webhooks/tradingview", json=payload)
    return resp


async def get_positions(ctx):
    from app.db.session import session_scope

    async with session_scope() as session:
        return list((await session.execute(select(Position))).scalars())


async def get_trades(ctx):
    from app.db.session import session_scope

    async with session_scope() as session:
        return list((await session.execute(select(Trade))).scalars())


async def active_positions(ctx):
    return [p for p in await get_positions(ctx) if p.state in
            ("RESERVED", "ORDER_PENDING", "ORDER_SUBMITTED", "OPEN", "MONITORING",
             "TP_TRIGGERED", "CLOSE_PENDING", "PARTIALLY_FILLED", "UNKNOWN")]


@pytest.mark.asyncio
async def test_01_buy_btcusdt_creates_one_paper_position(ctx, client):
    """TEST 1: BUY BTCUSDT -> exactly 1 paper position."""
    resp = await post_alert(client, symbol="BTCUSDT.P", action="BUY")
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "ACCEPTED"
    assert body["tradingview_raw_symbol"] == "BTCUSDT.P"
    assert body["canonical_symbol"] == "BTCUSDT"
    assert body["shark_symbol"] == "BTCUSDT"

    positions = await active_positions(ctx)
    assert len(positions) == 1
    p = positions[0]
    assert p.canonical_symbol == "BTCUSDT"
    assert p.state in ("OPEN", "MONITORING")
    assert p.mode == "PAPER"
    assert p.quantity == D("12")          # 1200 / 100
    assert p.margin == D("120")
    assert p.leverage == 10
    assert p.entry_price == D("100")      # slippage 0 in tests
    assert p.target_price == D("101")     # +10% ROI at 10x = +1% price


@pytest.mark.asyncio
async def test_02_second_btcusdt_buy_rejected_duplicate_symbol(ctx, client):
    """TEST 2: second BTCUSDT BUY -> REJECT SYMBOL_ALREADY_OPEN."""
    r1 = await post_alert(client, symbol="BTCUSDT.P", action="BUY")
    assert r1.json()["status"] == "ACCEPTED"
    r2 = await post_alert(client, symbol="BTCUSDT.P", action="BUY")
    body = r2.json()
    assert body["status"] in ("REJECTED", "ACCEPTED")  # accepted at ingest, rejected at process
    positions = await active_positions(ctx)
    assert len(positions) == 1  # exactly one trade/position

    from app.db.session import session_scope

    async with session_scope() as session:
        signals = list((await session.execute(select(Signal).order_by(Signal.id))).scalars())
    rejected = [s for s in signals if s.rejection_reason == "SYMBOL_ALREADY_OPEN"]
    assert len(rejected) == 1


@pytest.mark.asyncio
async def test_03_max_open_positions_rejects_doge(ctx, client):
    """TEST 3: BTC/ETH/SOL open + DOGE BUY -> REJECT MAX_OPEN_POSITIONS_REACHED."""
    for symbol in ("BTCUSDT.P", "ETHUSDT.P", "SOLUSDT.P"):
        r = await post_alert(client, symbol=symbol, action="BUY")
        assert r.json()["status"] == "ACCEPTED"
    assert len(await active_positions(ctx)) == 3

    r = await post_alert(client, symbol="DOGEUSDT.P", action="BUY")
    from app.db.session import session_scope

    async with session_scope() as session:
        signal = (await session.execute(select(Signal).order_by(Signal.id.desc()).limit(1))).scalar_one()
    assert signal.rejection_reason == "MAX_OPEN_POSITIONS_REACHED"
    assert len(await active_positions(ctx)) == 3


@pytest.mark.asyncio
async def test_04_roi_10_percent_closes_position(ctx, client):
    """TEST 4: +10% ROI -> CLOSE position."""
    await post_alert(client, symbol="BTCUSDT.P", action="BUY")
    positions = await active_positions(ctx)
    assert len(positions) == 1
    assert positions[0].entry_price == D("100")

    # +1% price at 10x = +10% ROI on margin
    ctx.market_data.seed_price("BTCUSDT", D("101"))
    from app.db.session import session_scope

    async with session_scope() as session:
        closed = await ctx.roi_monitor.tick(session)

    assert len(closed) == 1
    trades = await get_trades(ctx)
    assert len(trades) == 1
    trade = trades[0]
    assert trade.close_reason == "TP_10_ROI"
    assert trade.net_pnl == D("12")            # 120 * 10%
    assert trade.roi_percent == D("10")
    assert trade.price_change_percent == D("1")
    assert trade.result == "WIN"
    remaining = await active_positions(ctx)
    assert all(p.canonical_symbol != "BTCUSDT" or p.state == "CLOSED" for p in remaining)
    assert len([p for p in await get_positions(ctx) if p.state == "CLOSED"]) == 1


@pytest.mark.asyncio
async def test_05_slot_frees_after_close(ctx, client):
    """TEST 5: one position closes -> slot becomes available."""
    for symbol in ("BTCUSDT.P", "ETHUSDT.P", "SOLUSDT.P"):
        await post_alert(client, symbol=symbol, action="BUY")
    await post_alert(client, symbol="DOGEUSDT.P", action="BUY")
    from app.db.session import session_scope

    async with session_scope() as session:
        doge = (await session.execute(select(Signal).where(Signal.canonical_symbol == "DOGEUSDT"))).scalar_one()
    assert doge.rejection_reason == "MAX_OPEN_POSITIONS_REACHED"

    # close BTC at TP
    ctx.market_data.seed_price("BTCUSDT", D("101"))
    async with session_scope() as session:
        await ctx.roi_monitor.tick(session)

    # now DOGE fits
    await post_alert(client, symbol="DOGEUSDT.P", action="BUY")
    async with session_scope() as session:
        signals = list((await session.execute(select(Signal).where(Signal.canonical_symbol == "DOGEUSDT").order_by(Signal.id))).scalars())
    assert signals[-1].status_display == "EXECUTED"
    active = await active_positions(ctx)
    assert len(active) == 3


@pytest.mark.asyncio
async def test_06_two_identical_webhooks_exactly_one_trade(ctx, client):
    """TEST 6: two identical webhooks simultaneously -> exactly ONE trade."""
    payload = alert_payload(symbol="BTCUSDT.P", action="BUY", event_id="fixed-event-001")
    r1, r2 = await asyncio.gather(
        client.post("/api/webhooks/tradingview", json=payload),
        client.post("/api/webhooks/tradingview", json=payload),
    )
    statuses = sorted([r1.json()["status"], r2.json()["status"]])
    assert statuses == ["ACCEPTED", "DUPLICATE"] or statuses == ["ACCEPTED", "ACCEPTED"]
    # Exactly one trade regardless of interleaving (unique constraints):
    await asyncio.sleep(0.1)
    positions = await active_positions(ctx)
    assert len(positions) == 1
    from app.db.session import session_scope

    async with session_scope() as session:
        orders = list((await session.execute(select(Order).where(Order.entry_exit == "ENTRY"))).scalars())
    assert len(orders) == 1


@pytest.mark.asyncio
async def test_07_four_simultaneous_signals_max_three_accepted(ctx, client):
    """TEST 7: 4 simultaneous new symbols, 0 existing -> exactly THREE accepted."""
    responses = await asyncio.gather(
        post_alert(client, symbol="BTCUSDT.P", action="BUY"),
        post_alert(client, symbol="ETHUSDT.P", action="BUY"),
        post_alert(client, symbol="SOLUSDT.P", action="BUY"),
        post_alert(client, symbol="DOGEUSDT.P", action="BUY"),
    )
    assert all(r.status_code == 202 for r in responses)
    active = await active_positions(ctx)
    assert len(active) == 3

    from app.db.session import session_scope

    async with session_scope() as session:
        signals = list((await session.execute(select(Signal))).scalars())
    executed = [s for s in signals if s.status_display == "EXECUTED"]
    rejected = [s for s in signals if s.rejection_reason == "MAX_OPEN_POSITIONS_REACHED"]
    assert len(executed) == 3
    assert len(rejected) == 1


@pytest.mark.asyncio
async def test_08_restart_recovery_restores_positions(ctx, client):
    """TEST 8: application restarts with open paper positions -> recovered."""
    await post_alert(client, symbol="BTCUSDT.P", action="BUY")
    await post_alert(client, symbol="ETHUSDT.P", action="BUY")

    # simulate restart: build a fresh runtime over the SAME database
    from app.db.session import session_scope
    from app.services.bootstrap import recover_state

    async with session_scope() as session:
        report = await recover_state(session, ctx.settings)
    assert len(report["recovered_positions"]) == 2

    # ROI monitoring continues seamlessly after 'restart'
    ctx.market_data.seed_price("BTCUSDT", D("101"))
    async with session_scope() as session:
        closed = await ctx.roi_monitor.tick(session)
    assert closed  # monitoring works on recovered state
    trades = await get_trades(ctx)
    assert any(t.canonical_symbol == "BTCUSDT" for t in trades)


@pytest.mark.asyncio
async def test_09_invalid_payload_rejected_with_reason(ctx, client):
    """TEST 9: invalid TradingView payload -> REJECT with reason."""
    r = await client.post("/api/webhooks/tradingview",
                          json={"action": "BUY", "symbol": "", "secret": "test-secret"})
    assert r.status_code in (400, 422)
    assert "INVALID_PAYLOAD" in str(r.json()["detail"])

    r1b = await client.post("/api/webhooks/tradingview", json={"action": "BUY", "secret": "test-secret"})
    assert r1b.status_code == 400
    assert "INVALID_PAYLOAD" in str(r1b.json()["detail"])

    r2 = await client.post("/api/webhooks/tradingview", content=b"not-json",
                           headers={"content-type": "application/json",
                                    "x-webhook-secret": "test-secret"})
    assert r2.status_code == 400
    assert len(await active_positions(ctx)) == 0


@pytest.mark.asyncio
async def test_10_trading_halted_blocks_new_entries(ctx, client):
    """TEST 10: trading halted -> no new entry orders."""
    from app.db.session import session_scope

    async with session_scope() as session:
        await ctx.risk._acquire_db_lock(session) if False else None
    resp = await client.post("/api/risk/halt")
    assert resp.status_code == 200

    r = await post_alert(client, symbol="BTCUSDT.P", action="BUY")
    assert r.status_code == 202
    async with session_scope() as session:
        signal = (await session.execute(select(Signal))).scalars().first()
    assert signal.rejection_reason == "TRADING_HALTED"
    assert len(await active_positions(ctx)) == 0

    # resume requires confirmation
    assert (await client.post("/api/risk/resume", json={"confirm": False})).status_code == 400
    assert (await client.post("/api/risk/resume", json={"confirm": True})).status_code == 200
    await post_alert(client, symbol="BTCUSDT.P", action="BUY")
    assert len(await active_positions(ctx)) == 1


@pytest.mark.asyncio
async def test_11_live_disabled_no_shark_order_possible(ctx, client):
    """TEST 11: LIVE mode disabled -> no Shark live order possible."""
    from app.exchanges.base import OrderRequest
    from app.services.execution import new_order_id

    ctx.settings.trading_mode = "LIVE"
    assert ctx.settings.is_live_capable() is False

    from app.db.session import session_scope

    async with session_scope() as session:
        order = Order(order_id=new_order_id(), mode="LIVE", side="BUY",
                      quantity=D("1"), status="PENDING")
        session.add(order)
        await session.flush()
        result = await ctx.execution.submit(
            session,
            order=order,
            request=OrderRequest(symbol="BTCUSDT", side="BUY", quantity=D("1")),
        )
    assert result.status == "REJECTED"
    assert result.error_code == "LIVE_NOT_ENABLED"
    assert result.raw == {} or "payload" not in (result.raw or {})


@pytest.mark.asyncio
async def test_12_shark_unavailable_no_unsafe_duplicate_order(ctx):
    """TEST 12: Shark API unavailable -> no unsafe duplicate live order."""
    from app.core.errors import OrderStateUncertain
    from app.exchanges.base import Capability, OrderRequest
    from app.models.entities import Order
    from app.services.execution import new_order_id

    calls = {"count": 0}

    class FailingShark:
        name = "shark"

        def supports(self, cap: Capability) -> bool:
            return True

        async def place_market_order(self, request):
            calls["count"] += 1
            from app.core.errors import OrderStateUncertain

            raise OrderStateUncertain("network dropped mid-request")

    ctx.settings.trading_mode = "LIVE"
    ctx.settings.live_trading_enabled = True
    ctx.settings.live_trading_confirmation = "x"
    ctx.settings.shark_api_key = "k"
    ctx.settings.shark_api_secret = "s"
    ctx.execution.adapters["shark"] = FailingShark()

    from app.db.session import session_scope

    async with session_scope() as session:
        order = Order(order_id=new_order_id(), mode="LIVE", side="BUY",
                      quantity=D("1"), status="PENDING")
        session.add(order)
        await session.flush()
        with pytest.raises(OrderStateUncertain):
            await ctx.execution.submit(
                session,
                order=order,
                request=OrderRequest(symbol="BTCUSDT", side="BUY", quantity=D("1")),
            )
        await session.commit()

        # order is UNKNOWN — must not be blindly retried
        stored = (await session.execute(select(Order).where(Order.order_id == order.order_id))).scalar_one()
        assert stored.status == "UNKNOWN"
        assert calls["count"] == 1

        # reconciliation flags it instead of resubmitting
        await ctx.reconciliation.run(session)
        assert calls["count"] == 1  # still no new submission


@pytest.mark.asyncio
async def test_13_invalid_contract_quantity_rejected(ctx, client):
    """TEST 13: invalid contract quantity -> order rejected, risk unchanged."""
    from decimal import Decimal as Dc

    from app.db.session import session_scope
    from app.models.entities import SymbolMapping

    async with session_scope() as session:
        mapping = (await session.execute(
            select(SymbolMapping).where(SymbolMapping.canonical_symbol == "BTCUSDT")
        )).scalar_one()
        mapping.minimum_quantity = Dc("50")  # 1200/100 = 12 < 50 -> impossible
        mapping.quantity_precision = 3

    await post_alert(client, symbol="BTCUSDT.P", action="BUY")
    async with session_scope() as session:
        signal = (await session.execute(select(Signal))).scalars().first()
    assert signal.rejection_reason == "MIN_QUANTITY_NOT_MET"
    positions = await get_positions(ctx)
    assert all(p.state in ("REJECTED", "CANCELLED", "RESERVED") or p.rejection_reason for p in positions)
    assert len(await active_positions(ctx)) == 0
    # margin/leverage never silently changed
    assert ctx.settings.margin_per_trade == D("120")
    assert ctx.settings.leverage == 10


@pytest.mark.asyncio
async def test_14_bas_symbol_triple_preserved(ctx, client):
    """TEST 14: BASUSDT.P -> raw BASUSDT.P / canonical BASUSDT / shark BASUSDT."""
    r = await post_alert(client, symbol="BASUSDT.P", action="BUY")
    body = r.json()
    assert body["tradingview_raw_symbol"] == "BASUSDT.P"
    assert body["canonical_symbol"] == "BASUSDT"
    assert body["shark_symbol"] == "BASUSDT"

    positions = await get_positions(ctx)
    p = positions[0]
    assert p.tradingview_raw_symbol == "BASUSDT.P"
    assert p.canonical_symbol == "BASUSDT"
    assert p.shark_symbol == "BASUSDT"

    from app.db.session import session_scope

    async with session_scope() as session:
        order = (await session.execute(select(Order))).scalars().first()
    assert order.tradingview_raw_symbol == "BASUSDT.P"
    assert order.canonical_symbol == "BASUSDT"
    assert order.shark_symbol == "BASUSDT"


@pytest.mark.asyncio
async def test_15_bas_open_plus_bas_dot_p_rejected(ctx, client):
    """TEST 15: existing BASUSDT + incoming BASUSDT.P -> REJECT SYMBOL_ALREADY_OPEN."""
    await post_alert(client, symbol="BASUSDT", action="BUY")
    await post_alert(client, symbol="BASUSDT.P", action="BUY")

    from app.db.session import session_scope

    async with session_scope() as session:
        signals = list((await session.execute(select(Signal))).scalars())
    assert len(signals) == 2
    # same internal position identity
    assert signals[0].canonical_symbol == signals[1].canonical_symbol == "BASUSDT"
    rejected = [s for s in signals if s.rejection_reason == "SYMBOL_ALREADY_OPEN"]
    assert len(rejected) == 1
    assert len(await active_positions(ctx)) == 1


@pytest.mark.asyncio
async def test_16_unknown_asset_rejected_shark_symbol_not_found(ctx, client):
    """TEST 16: UNKNOWNASSET.P -> normalize first -> SHARK_SYMBOL_NOT_FOUND, no order."""
    r = await post_alert(client, symbol="UNKNOWNASSET.P", action="BUY")
    body = r.json()
    # normalization happened (raw preserved, canonical computed) but resolution failed
    assert body["status"] in ("REJECTED", "ACCEPTED")
    from app.db.session import session_scope

    async with session_scope() as session:
        signals = list((await session.execute(select(Signal))).scalars())
        orders = list((await session.execute(select(Order))).scalars())
    assert len(signals) == 1
    assert signals[0].tradingview_raw_symbol == "UNKNOWNASSET.P"
    assert signals[0].canonical_symbol == "UNKNOWNASSET"
    assert signals[0].rejection_reason == "SHARK_SYMBOL_NOT_FOUND"
    assert len(orders) == 0
    assert len(await active_positions(ctx)) == 0


@pytest.mark.asyncio
async def test_17_shark_order_symbol_never_raw_tradingview(ctx, client):
    """TEST 17: Shark order symbol == BASUSDT, NEVER BASUSDT.P."""
    await post_alert(client, symbol="BASUSDT.P", action="BUY")
    from app.db.session import session_scope

    async with session_scope() as session:
        order = (await session.execute(select(Order).where(Order.entry_exit == "ENTRY"))).scalars().first()
    assert order.shark_symbol == "BASUSDT"
    assert order.submitted_payload["symbol"] == "BASUSDT"
    assert order.submitted_payload["symbol"] != "BASUSDT.P"
    assert not order.submitted_payload["symbol"].endswith(".P")


@pytest.mark.asyncio
async def test_18_four_simultaneous_bas_btc_eth_sol(ctx, client):
    """TEST 18: BAS/BTC/ETH/SOL.P simultaneous -> exactly 3 accepted, 4th MAX_OPEN."""
    responses = await asyncio.gather(
        post_alert(client, symbol="BASUSDT.P", action="BUY"),
        post_alert(client, symbol="BTCUSDT.P", action="BUY"),
        post_alert(client, symbol="ETHUSDT.P", action="BUY"),
        post_alert(client, symbol="SOLUSDT.P", action="BUY"),
    )
    assert all(r.status_code == 202 for r in responses)
    from app.db.session import session_scope

    async with session_scope() as session:
        signals = list((await session.execute(select(Signal))).scalars())
    executed = sorted(s.canonical_symbol for s in signals if s.status_display == "EXECUTED")
    rejected = [s for s in signals if s.rejection_reason == "MAX_OPEN_POSITIONS_REACHED"]
    assert len(executed) == 3
    assert len(rejected) == 1
    assert len(await active_positions(ctx)) == 3
