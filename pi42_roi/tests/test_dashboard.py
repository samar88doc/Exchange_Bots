"""The standalone dashboard: data assembly, HTTP routes and the manual actions."""

import json
import threading
from datetime import datetime, timezone
from decimal import Decimal
from http.client import HTTPConnection
from http.server import HTTPServer

from app.config import REAL_ACK_PHRASE
from app.dashboard import (
    DashboardRequestHandler,
    _fetch_dashboard_data,
    _get_live_price,
)
from app.storage import Storage
from tests.helpers import make_settings


def seed_trade(tmp_path, *, mode="PAPER", status="ENTRY_FILLED", direction="SHORT",
               entry="0.3462", target="0.3185", symbol="CRVUSDT", market="USDT",
               margin_asset="USDT"):
    settings = make_settings(tmp_path, PI42_MARKET=market)
    storage = Storage(settings.database_file)
    now = datetime.now(timezone.utc)
    signal_id = storage.insert_signal(channel_id=-100, message_id=1, raw_text="#CRV/USDT - Short",
                                     received_at=now, signal_time=now, status="ACCEPTED")
    trade_id = storage.create_trade(
        now, signal_id=signal_id, mode=mode, ref_id="TG-1-x", client_order_id=None, order_id=None,
        symbol=symbol, market=market, margin_asset=margin_asset, direction=direction,
        entry_price=Decimal(entry), target1=Decimal(target), stop_loss_reference=Decimal("0.3662"),
        telegram_leverage=Decimal("14"), leverage=Decimal("14"),
        contract_max_leverage=Decimal("20"), quantity=Decimal("1941"), notional=Decimal("671.7"),
        est_margin=Decimal("48"), budget=Decimal("48"), status=status,
        filled_at=now if status == "ENTRY_FILLED" else None,
        fill_price=Decimal(entry) if status == "ENTRY_FILLED" else None,
        filled_quantity=Decimal("1941") if status == "ENTRY_FILLED" else None,
    )
    storage.close()
    return settings, trade_id


def test_fetch_dashboard_data_paper_defaults(tmp_path):
    settings = make_settings(tmp_path)
    data = _fetch_dashboard_data(settings)
    for key in ("bot", "metrics", "ledger", "active_trades", "recent_signals", "closed_trades",
                "events", "orders"):
        assert key in data
    assert data["bot"]["mode"] == "PAPER"
    assert data["bot"]["market"] == "USDT" and data["bot"]["margin_asset"] == "USDT"
    assert data["bot"]["target_roi_pct"] == 8.0
    assert data["metrics"]["active_count"] == 0
    assert data["ledger"]["is_real"] is False
    assert data["ledger"]["title"] == "Simulated Balance"


def test_dashboard_data_shows_positions_orders_and_signals(tmp_path, monkeypatch):
    import app.dashboard as dashboard

    settings, trade_id = seed_trade(tmp_path)
    monkeypatch.setattr(dashboard, "_get_live_price",
                        lambda symbol, public_base_url=None: Decimal("0.3300"))
    data = _fetch_dashboard_data(settings)

    assert len(data["active_trades"]) == 1
    position = data["active_trades"][0]
    assert position["symbol"] == "CRVUSDT" and position["current_price"] == "0.3300"
    assert position["unrealized_pnl"] > 0
    assert position["targets_detail"][0]["target_num"] == 1
    assert position["targets_detail"][0]["price"] == "0.3185"
    assert position["emergency_sl_price"] == "-", "no stop-loss is ever armed"
    assert len(data["closed_trades"]) == 0

    legs = [o for o in data["orders"] if o["source"] == "BOT_TARGET_LEG"]
    assert len(legs) == 1 and legs[0]["id"] == f"LEG-T1-{trade_id}"
    assert legs[0]["status"] == "SCHEDULED" and legs[0]["price"] == "0.3185"
    assert "ROI Target Exit" in legs[0]["milestone"]

    assert len(data["recent_signals"]) == 1
    assert data["recent_signals"][0]["message_id"] == 1


def test_real_mode_ledger_uses_the_pi42_futures_wallet(tmp_path, monkeypatch):
    import app.dashboard as dashboard

    captured = {}

    def fake_funds(base_url, api_key, secret, margin_asset="USDT"):
        captured.update({"base_url": base_url, "key": api_key, "margin_asset": margin_asset})
        return {"balance": 177.71, "locked": 0.0, "available": 177.71, "margin_balance": 177.71,
                "margin_asset": margin_asset}

    monkeypatch.setattr(dashboard, "_get_live_funds", fake_funds)
    settings = make_settings(tmp_path, TRADING_MODE="REAL", ENABLE_REAL_TRADING="true",
                             LIVE_TRADING_ACKNOWLEDGEMENT=REAL_ACK_PHRASE)
    data = _fetch_dashboard_data(settings)
    assert data["bot"]["mode"] == "REAL"
    assert data["ledger"]["is_real"] is True and data["ledger"]["title"] == "Pi42 Futures Wallet"
    assert data["ledger"]["initial"] == 177.71 and data["ledger"]["available"] == 177.71
    assert captured["margin_asset"] == "USDT"
    assert "pi42.com" in captured["base_url"]


def test_dashboard_http_routes_and_branding(tmp_path):
    settings = make_settings(tmp_path)
    DashboardRequestHandler.settings = settings
    server = HTTPServer(("127.0.0.1", 0), DashboardRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)

        conn.request("GET", "/")
        response = conn.getresponse()
        assert response.status == 200 and "text/html" in response.getheader("Content-Type", "")
        page = response.read().decode("utf-8")
        assert "Pi42 Signal Trader" in page and "Mudrex" not in page

        conn.request("GET", "/api/status")
        response = conn.getresponse()
        assert response.status == 200
        payload = json.loads(response.read().decode("utf-8"))
        assert payload["bot"]["mode"] == "PAPER" and payload["bot"]["market"] == "USDT"

        conn.request("GET", "/api/health")
        response = conn.getresponse()
        assert json.loads(response.read().decode("utf-8")) == {"status": "ok"}

        conn.request("GET", "/unknown-route")
        assert conn.getresponse().status == 404

        conn.request("POST", "/api/logs/clear")
        response = conn.getresponse()
        assert response.status == 200
        assert json.loads(response.read().decode("utf-8"))["success"] is True
        conn.close()
    finally:
        server.shutdown()
        server.server_close()


def test_edit_target_updates_the_database_and_rejects_a_target_on_the_wrong_side(tmp_path):
    settings, trade_id = seed_trade(tmp_path, direction="SHORT")
    DashboardRequestHandler.settings = settings
    server = HTTPServer(("127.0.0.1", 0), DashboardRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        body = json.dumps({"trade_id": trade_id, "target_price": "0.3300"}).encode()
        conn.request("POST", "/api/trades/edit-target", body=body,
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        assert response.status == 200
        assert json.loads(response.read().decode())["new_target"] == "0.3300"

        storage = Storage(settings.database_file)
        assert storage.get_trade(trade_id).target1 == Decimal("0.3300")
        storage.close()

        # SHORT targets must stay below the entry price.
        bad = json.dumps({"trade_id": trade_id, "target_price": "0.3600"}).encode()
        conn.request("POST", "/api/trades/edit-target", body=bad,
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        assert response.status == 400
        assert json.loads(response.read().decode())["success"] is False
        conn.close()
    finally:
        server.shutdown()
        server.server_close()


def test_manual_close_marks_the_trade_closed(tmp_path, monkeypatch):
    import app.dashboard as dashboard

    settings, trade_id = seed_trade(tmp_path)
    monkeypatch.setattr(dashboard, "_get_live_price",
                        lambda symbol, public_base_url=None: Decimal("0.3300"))
    DashboardRequestHandler.settings = settings
    server = HTTPServer(("127.0.0.1", 0), DashboardRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        body = json.dumps({"trade_id": trade_id}).encode()
        conn.request("POST", "/api/trades/close", body=body,
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        assert response.status == 200 and json.loads(response.read().decode())["success"] is True
        storage = Storage(settings.database_file)
        closed = storage.get_trade(trade_id)
        assert closed.status == "CLOSED"
        assert closed.exit_reason == "manual_dashboard_close"
        assert closed.exit_price == Decimal("0.3300")
        storage.close()
        conn.close()
    finally:
        server.shutdown()
        server.server_close()


def test_cancel_order_calls_the_pi42_delete_endpoint(tmp_path, monkeypatch):
    import app.dashboard as dashboard

    sent = {}

    def fake_request(method, path, body, base_url, api_key, secret, timeout=5.0):
        sent.update({"method": method, "path": path, "body": body})
        return {"message": "cancelled"}

    monkeypatch.setattr(dashboard, "_pi42_signed_request", fake_request)
    settings = make_settings(tmp_path)
    DashboardRequestHandler.settings = settings
    server = HTTPServer(("127.0.0.1", 0), DashboardRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        body = json.dumps({"order_id": "cli-42"}).encode()
        conn.request("POST", "/api/orders/cancel", body=body,
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        assert response.status == 200
        assert sent["method"] == "DELETE" and sent["path"] == "/v1/order/delete-order"
        assert sent["body"] == {"clientOrderId": "cli-42"}
        conn.close()
    finally:
        server.shutdown()
        server.server_close()


def test_live_price_uses_the_public_ticker_and_caches_it(monkeypatch):
    import app.dashboard as dashboard

    dashboard._LIVE_PRICE_CACHE.clear()
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request.full_url)

        class Response:
            def read(self):
                return json.dumps({"pair": "CRVUSDT", "c": "0.3512"}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return Response()

    monkeypatch.setattr(dashboard.urllib.request, "urlopen", fake_urlopen)
    assert _get_live_price("CRVUSDT", "https://api.pi42.com") == Decimal("0.3512")
    assert _get_live_price("CRVUSDT", "https://api.pi42.com") == Decimal("0.3512")
    assert len(calls) == 1, "the price is cached for the TTL window"
    assert calls[0] == "https://api.pi42.com/v1/market/ticker24Hr/CRVUSDT"


def test_live_price_outage_is_survivable(monkeypatch):
    import app.dashboard as dashboard

    dashboard._LIVE_PRICE_CACHE.clear()

    def boom(request, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(dashboard.urllib.request, "urlopen", boom)
    assert _get_live_price("CRVUSDT", "https://api.pi42.com") is None


def test_dashboard_pages_render_inr_market(tmp_path):
    settings, _ = seed_trade(tmp_path, market="INR", symbol="BTCINR", entry="8000000",
                             target="7360000", margin_asset="INR")
    data = _fetch_dashboard_data(settings)
    assert data["bot"]["market"] == "INR" and data["bot"]["margin_asset"] == "INR"
    assert data["active_trades"][0]["symbol"] == "BTCINR"
