"""Integration tests: webhook->DB, symbol flow, paper exchange, order lifecycle,
Shark adapter (mocked transport), dry-run, restart, live activation gates."""
from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.models.entities import Order, Position, Trade
from tests.conftest import alert_payload

D = Decimal


@pytest.mark.asyncio
async def test_full_order_lifecycle_states(ctx, client):
    resp = await client.post("/api/webhooks/tradingview",
                             json=alert_payload(symbol="BTCUSDT.P", action="BUY"))
    body = resp.json()
    assert body["shark_symbol"] == "BTCUSDT"
    from app.db.session import session_scope

    async with session_scope() as session:
        position = (await session.execute(select(Position))).scalars().first()
        order = (await session.execute(select(Order))).scalars().first()
    assert position.state in ("OPEN", "MONITORING")
    assert order.status in ("FILLED", "DRY_RUN")
    assert order.avg_fill_price == D("100")

    # timeline explains the trade
    tl = await client.get(f"/api/positions/{position.position_id}/timeline")
    names = [e["event"] for e in tl.json()]
    assert "POSITION_OPENED" in names
    assert "SIGNAL_RECEIVED" in names


@pytest.mark.asyncio
async def test_symbol_normalization_flow_api(ctx, client):
    resp = await client.post("/api/webhooks/tradingview",
                             json=alert_payload(symbol="BASUSDT.P", action="BUY"))
    body = resp.json()
    assert (body["tradingview_raw_symbol"], body["canonical_symbol"], body["shark_symbol"]) == (
        "BASUSDT.P", "BASUSDT", "BASUSDT")


@pytest.mark.asyncio
async def test_paper_balance_and_fees(ctx, client):
    ctx.settings.paper_taker_fee_bps = D("5")  # 5 bps
    ctx.settings.paper_slippage_bps = D("0")
    await client.post("/api/webhooks/tradingview", json=alert_payload(symbol="BTCUSDT.P"))
    # fees included in ROI: +1% price (=+10% gross ROI) is NOT enough to hit +10% net
    ctx.market_data.seed_price("BTCUSDT", D("101"))
    from app.db.session import session_scope

    async with session_scope() as session:
        await ctx.roi_monitor.tick(session)
    async with session_scope() as session:
        open_still = (await session.execute(select(Position).where(Position.state.in_(["OPEN", "MONITORING"])))).scalars().all()
    assert len(open_still) == 1  # correctly NOT closed at 10% gross

    # move past fee-adjusted target -> closes
    ctx.market_data.seed_price("BTCUSDT", D("102"))
    async with session_scope() as session:
        await ctx.roi_monitor.tick(session)
    async with session_scope() as session:
        trade = (await session.execute(select(Trade))).scalars().first()
    assert trade is not None
    assert trade.entry_fee == (D("1200") * D("5") / D("10000"))  # 0.6
    assert trade.exit_fee > 0
    assert trade.net_pnl == trade.gross_pnl - trade.entry_fee - trade.exit_fee


@pytest.mark.asyncio
async def test_manual_close_and_close_all(ctx, client):
    await client.post("/api/webhooks/tradingview", json=alert_payload(symbol="BTCUSDT.P"))
    await client.post("/api/webhooks/tradingview", json=alert_payload(symbol="ETHUSDT.P"))
    from app.db.session import session_scope

    async with session_scope() as session:
        position = (await session.execute(select(Position).where(Position.canonical_symbol == "BTCUSDT"))).scalars().first()
    r = await client.post(f"/api/positions/{position.position_id}/close", json={"reason": "MANUAL", "confirm": True})
    assert r.status_code == 200
    r2 = await client.post("/api/positions/close-all", json={"reason": "MANUAL", "confirm": True})
    assert r2.status_code == 200
    async with session_scope() as session:
        trades = list((await session.execute(select(Trade))).scalars())
    assert len(trades) == 2


@pytest.mark.asyncio
async def test_dry_run_live_creates_no_position(ctx, client):
    """LIVE DRY RUN: exact payload logged, NOT submitted, no open position."""
    from app.exchanges.base import OrderRequest

    ctx.settings.trading_mode = "LIVE"
    ctx.settings.live_trading_enabled = True
    ctx.settings.live_trading_confirmation = "secret-phrase"
    ctx.settings.shark_api_key = "k"
    ctx.settings.shark_api_secret = "s"
    ctx.settings.shark_dry_run = True

    result = await ctx.shark.place_market_order(
        OrderRequest(symbol="BASUSDT", side="BUY", quantity=D("12"))
    )
    assert result.dry_run is True
    assert result.status == "DRY_RUN"
    payload = result.raw["payload"]
    assert payload["symbol"] == "BASUSDT"          # shark symbol, NEVER .P
    assert payload["side"] == "BUY"
    assert payload["quantity"] == 12.0
    assert payload["marginAsset"] == "INR"
    assert "BASUSDT.P" not in str(payload)


@pytest.mark.asyncio
async def test_shark_adapter_refuses_tradingview_symbol():
    from app.core.config import AppSettings
    from app.core.errors import ExchangeError
    from app.exchanges.base import OrderRequest
    from app.exchanges.shark import SharkExchangeAdapter

    settings = AppSettings(testing=True, shark_dry_run=True, shark_api_key="k", shark_api_secret="s")
    shark = SharkExchangeAdapter(settings)
    with pytest.raises(ExchangeError) as exc:
        await shark.place_market_order(
            OrderRequest(symbol="BASUSDT.P", side="BUY", quantity=D("1"))
        )
    assert "Refusing" in exc.value.message


@pytest.mark.asyncio
async def test_shark_signing_matches_docs():
    """HMAC-SHA256 over JSON body, hex digest — per official docs."""
    from app.core.security import shark_sign_body

    sig = shark_sign_body("secret", {"a": 1, "timestamp": "123"})
    import hashlib
    import hmac as hmac_mod
    import json

    expected = hmac_mod.new(b"secret", json.dumps({"a": 1, "timestamp": "123"}, separators=(",", ":")).encode(),
                            hashlib.sha256).hexdigest()
    assert sig == expected


@pytest.mark.asyncio
async def test_shark_order_lifecycle_mocked():
    """Shark adapter against a mocked HTTP transport (responses per docs)."""
    from app.core.config import AppSettings
    from app.exchanges.base import OrderRequest
    from app.exchanges.shark import SharkExchangeAdapter

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content
        return httpx.Response(200, json={
            "clientOrderId": "abc-123-ext", "symbol": "BASUSDT", "type": "MARKET",
            "side": "BUY", "price": 100.0, "orderAmount": 12, "filledAmount": 12,
        })

    settings = AppSettings(testing=True, shark_dry_run=False,
                           shark_api_key="key", shark_api_secret="secret")
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="https://api.sharkexchange.in")
    shark = SharkExchangeAdapter(settings, http_client=client)
    result = await shark.place_market_order(
        OrderRequest(symbol="BASUSDT", side="BUY", quantity=D("12"))
    )
    assert result.status == "FILLED"
    assert captured["path"] == "/v1/order/place-order"
    assert captured["headers"]["api-key"] == "key"
    assert "signature" in captured["headers"]
    import json

    body = json.loads(captured["body"])
    assert body["symbol"] == "BASUSDT"
    assert body["type"] == "MARKET"
    assert body["reduceOnly"] is False
    assert "timestamp" in body


@pytest.mark.asyncio
async def test_live_activation_gates(ctx, client):
    """GO LIVE requires the exact phrase + env gate + config acknowledgements."""
    r = await client.post("/api/live/activate", json={
        "confirmation_phrase": "enable live trading",   # wrong case
        "acknowledge_margin_per_trade": 120, "acknowledge_leverage": 10,
        "acknowledge_max_positions": 3, "acknowledge_target_roi": 10,
        "acknowledge_stop_loss_disabled": True,
    })
    assert r.status_code == 400

    r2 = await client.post("/api/live/activate", json={
        "confirmation_phrase": "ENABLE LIVE TRADING",
        "acknowledge_margin_per_trade": 120, "acknowledge_leverage": 10,
        "acknowledge_max_positions": 3, "acknowledge_target_roi": 10,
        "acknowledge_stop_loss_disabled": True,
    })
    assert r2.status_code == 403  # LIVE_TRADING_ENABLED=false in env

    ctx.settings.live_trading_enabled = True
    ctx.settings.live_trading_confirmation = "s3cret"
    ctx.settings.shark_api_key = "k"
    ctx.settings.shark_api_secret = "s"
    r3 = await client.post("/api/live/activate", json={
        "confirmation_phrase": "ENABLE LIVE TRADING",
        "acknowledge_margin_per_trade": 120, "acknowledge_leverage": 10,
        "acknowledge_max_positions": 3, "acknowledge_target_roi": 10,
        "acknowledge_stop_loss_disabled": True,
        "secret_confirmation": "s3cret",
    })
    assert r3.status_code == 200
    assert r3.json()["mode"] == "LIVE"
    assert ctx.settings.trading_mode == "LIVE"


@pytest.mark.asyncio
async def test_replay_reprocesses_events(ctx, client):
    await client.post("/api/webhooks/tradingview", json=alert_payload(symbol="BTCUSDT.P"))
    r = await client.post("/api/replay", json={"speed": 0})
    assert r.status_code == 200
    report = r.json()
    assert report["total"] == 1
    # replay must NOT create a second live position for the same canonical symbol
    from app.db.session import session_scope

    async with session_scope() as session:
        positions = list((await session.execute(select(Position))).scalars())
    active = [p for p in positions if p.state not in ("CLOSED", "REJECTED", "CANCELLED", "ORDER_FAILED")]
    assert len(active) == 1


@pytest.mark.asyncio
async def test_dashboard_status_shape(ctx, client):
    await client.post("/api/webhooks/tradingview", json=alert_payload(symbol="BTCUSDT.P"))
    r = await client.get("/api/status")
    data = r.json()
    assert data["mode"] == "PAPER"
    assert data["risk"]["open_positions"] == 1
    assert data["risk"]["available_slots"] == 2
    assert data["risk"]["margin_per_trade"] == "120"
    assert data["risk"]["stop_loss_enabled"] is False
    health = (await client.get("/api/health")).json()
    assert health["components"]["database"] == "CONNECTED"


@pytest.mark.asyncio
async def test_signals_and_trades_listing(ctx, client):
    await client.post("/api/webhooks/tradingview", json=alert_payload(symbol="BTCUSDT.P"))
    ctx.market_data.seed_price("BTCUSDT", D("101"))
    from app.db.session import session_scope

    async with session_scope() as session:
        await ctx.roi_monitor.tick(session)
    signals = (await client.get("/api/signals")).json()
    assert signals[0]["tradingview_raw_symbol"] == "BTCUSDT.P"
    trades = (await client.get("/api/trades")).json()
    assert trades[0]["shark_symbol"] == "BTCUSDT"
    assert D(trades[0]["roi_percent"]) == D("10")
    assert D(trades[0]["price_change_percent"]) == D("1")
    csv_resp = await client.get("/api/trades/export/csv")
    assert csv_resp.status_code == 200
    assert "tradingview_symbol" in csv_resp.text
