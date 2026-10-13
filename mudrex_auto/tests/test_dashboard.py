import json
import threading
from http.client import HTTPConnection
from http.server import HTTPServer

from app.dashboard import DashboardRequestHandler, _fetch_dashboard_data
from tests.helpers import make_settings


def test_fetch_dashboard_data(tmp_path):
    settings = make_settings(tmp_path)
    data = _fetch_dashboard_data(settings)

    assert "bot" in data
    assert "metrics" in data
    assert "ledger" in data
    assert "active_trades" in data
    assert "recent_signals" in data
    assert "closed_trades" in data
    assert "events" in data

    assert data["bot"]["mode"] == "PAPER"
    assert data["bot"]["max_concurrent_positions"] == 3
    assert data["metrics"]["active_count"] == 0


def test_dashboard_http_server(tmp_path):
    settings = make_settings(tmp_path)
    DashboardRequestHandler.settings = settings

    server = HTTPServer(("127.0.0.1", 0), DashboardRequestHandler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        conn = HTTPConnection("127.0.0.1", port, timeout=5)

        # 1. GET /
        conn.request("GET", "/")
        res = conn.getresponse()
        assert res.status == 200
        assert "text/html" in res.getheader("Content-Type", "")
        body = res.read().decode("utf-8")
        assert "Mudrex Signal Trader" in body

        # 2. GET /api/status
        conn.request("GET", "/api/status")
        res2 = conn.getresponse()
        assert res2.status == 200
        assert "application/json" in res2.getheader("Content-Type", "")
        data = json.loads(res2.read().decode("utf-8"))
        assert data["bot"]["mode"] == "PAPER"

        # 3. GET /api/health
        conn.request("GET", "/api/health")
        res3 = conn.getresponse()
        assert res3.status == 200
        assert json.loads(res3.read().decode("utf-8")) == {"status": "ok"}

        # 4. GET /nonexistent -> 404
        conn.request("GET", "/unknown-route")
        res4 = conn.getresponse()
        assert res4.status == 404

        # 5. POST /api/logs/clear
        conn.request("POST", "/api/logs/clear")
        res5 = conn.getresponse()
        assert res5.status == 200
        data5 = json.loads(res5.read().decode("utf-8"))
        assert data5["success"] is True

        conn.close()
    finally:
        server.shutdown()
        server.server_close()


def test_dashboard_edit_target(tmp_path):
    from datetime import datetime, timezone
    from decimal import Decimal

    from app.storage import Storage

    settings = make_settings(tmp_path)
    DashboardRequestHandler.settings = settings
    storage = Storage(settings.database_file)
    now = datetime.now(timezone.utc)
    sig_id = storage.insert_signal(
        channel_id=-100,
        message_id=1,
        raw_text="TEST",
        received_at=now,
        signal_time=now,
        status="ACCEPTED",
    )
    t_id = storage.create_trade(
        now,
        signal_id=sig_id,
        mode="PAPER",
        client_order_id="TG-1-x",
        symbol="BTCUSDT",
        direction="LONG",
        entry_price=Decimal("80000"),
        target1=Decimal("82000"),
        telegram_leverage=Decimal("10"),
        leverage=Decimal("10"),
        quantity=Decimal("1"),
        notional=Decimal("80000"),
        est_margin=Decimal("8000"),
        budget=Decimal("8000"),
        status="ENTRY_PENDING",
    )
    storage.close()

    server = HTTPServer(("127.0.0.1", 0), DashboardRequestHandler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        conn = HTTPConnection("127.0.0.1", port, timeout=5)

        # Successful edit
        body = json.dumps({"trade_id": t_id, "target_price": "83500"}).encode("utf-8")
        conn.request("POST", "/api/trades/edit-target", body=body, headers={"Content-Type": "application/json"})
        res = conn.getresponse()
        assert res.status == 200
        payload = json.loads(res.read().decode("utf-8"))
        assert payload["success"] is True
        assert payload["new_target"] == "83500"

        # Verify in DB
        storage = Storage(settings.database_file)
        updated = storage.get_trade(t_id)
        assert updated.target1 == Decimal("83500")
        storage.close()

        # Invalid direction (target below entry for LONG)
        body_inv = json.dumps({"trade_id": t_id, "target_price": "79000"}).encode("utf-8")
        conn.request("POST", "/api/trades/edit-target", body=body_inv, headers={"Content-Type": "application/json"})
        res_inv = conn.getresponse()
        assert res_inv.status == 400
        payload_inv = json.loads(res_inv.read().decode("utf-8"))
        assert payload_inv["success"] is False

        conn.close()
    finally:
        server.shutdown()
        server.server_close()


def test_fetch_dashboard_data_real_mode(tmp_path, monkeypatch):
    import app.dashboard as db_mod

    monkeypatch.setattr(
        db_mod,
        "_get_live_funds",
        lambda base_url, secret, currency="USDT": {"balance": 177.71, "locked": 0.0},
    )

    settings = make_settings(
        tmp_path,
        TRADING_MODE="REAL",
        ENABLE_REAL_TRADING="true",
        LIVE_TRADING_ACKNOWLEDGEMENT="I ACCEPT NO-STOP-LOSS LIVE TRADING RISK",
    )
    data = _fetch_dashboard_data(settings)

    assert data["bot"]["mode"] == "REAL"
    assert data["ledger"]["is_real"] is True
    assert data["ledger"]["title"] == "Real Futures Wallet"
    assert data["ledger"]["initial"] == 177.71
    assert data["ledger"]["available"] == 177.71
    assert data["ledger"]["reserved_margin"] == 0.0


def test_dashboard_positions_orders_and_signals_details(tmp_path, monkeypatch):
    import app.dashboard as db_mod
    from datetime import datetime, timezone
    from decimal import Decimal
    from app.storage import Storage

    settings = make_settings(tmp_path)
    storage = Storage(settings.database_file)
    now = datetime.now(timezone.utc)

    # 1. Insert signal
    sig_id = storage.insert_signal(
        channel_id=-1002450079596,
        message_id=66668,
        raw_text="#OP/USDT - Long\n\nEntry: 0.1476\nStop Loss: 0.08846\nTarget 1: 0.16257\nLeverage: x16",
        received_at=now,
        signal_time=now,
        status="ACCEPTED",
    )
    storage.update_signal(
        sig_id,
        now,
        symbol="OPUSDT",
        direction="LONG",
        entry=Decimal("0.1476"),
        target1=Decimal("0.16257"),
        target2=Decimal("0.17000"),
        target3=Decimal("0.18500"),
        decision_reason="Valid signal",
    )

    # 2. Insert active trade with multi-targets
    t_id = storage.create_trade(
        now,
        signal_id=sig_id,
        mode="PAPER",
        client_order_id="TG-66668-1",
        symbol="OPUSDT",
        direction="LONG",
        entry_price=Decimal("0.1476"),
        target1=Decimal("0.16257"),
        target2=Decimal("0.17000"),
        target3=Decimal("0.18500"),
        telegram_leverage=Decimal("16"),
        leverage=Decimal("16"),
        quantity=Decimal("3000"),
        notional=Decimal("442.8"),
        est_margin=Decimal("27.675"),
        budget=Decimal("27.675"),
        status="ENTRY_FILLED",
    )
    storage.close()

    # Mock live price
    monkeypatch.setattr(db_mod, "_get_live_price", lambda sym, base, sec: Decimal("0.1550"))

    data = _fetch_dashboard_data(settings)

    # Verify positions
    assert len(data["active_trades"]) == 1
    pos = data["active_trades"][0]
    assert pos["symbol"] == "OPUSDT"
    assert pos["current_price"] == "0.1550"
    assert len(pos["targets_detail"]) == 3
    assert pos["targets_detail"][0]["target_num"] == 1
    assert pos["targets_detail"][0]["price"] == "0.16257"
    assert pos["emergency_sl_price"] is not None
    assert pos["unrealized_pnl"] > 0
    assert pos["roe_pct"] > 0

    # Verify orders (scheduled multi-target exit legs)
    orders = data["orders"]
    assert len(orders) >= 3
    t1_order = next(o for o in orders if "T1" in o["id"])
    assert t1_order["symbol"] == "OPUSDT"
    assert t1_order["side"] == "SELL"
    assert t1_order["status"] == "SCHEDULED"

    # Verify signals
    signals = data["recent_signals"]
    assert len(signals) == 1
    sig = signals[0]
    assert sig["message_id"] == 66668
    assert "#OP/USDT - Long" in sig["raw_text"]
    assert len(sig["targets"]) == 3
    assert sig["formatted_latency"] is not None


def test_dashboard_close_trade_and_cancel_order(tmp_path, monkeypatch):
    import app.dashboard as db_mod
    from datetime import datetime, timezone
    from decimal import Decimal
    from app.storage import Storage

    settings = make_settings(tmp_path)
    DashboardRequestHandler.settings = settings
    storage = Storage(settings.database_file)
    now = datetime.now(timezone.utc)

    sig_id = storage.insert_signal(
        channel_id=-100, message_id=999, raw_text="TEST", received_at=now, signal_time=now, status="ACCEPTED"
    )
    t_id = storage.create_trade(
        now,
        signal_id=sig_id,
        mode="PAPER",
        client_order_id="TG-999-1",
        symbol="BTCUSDT",
        direction="LONG",
        entry_price=Decimal("60000"),
        target1=Decimal("62000"),
        telegram_leverage=Decimal("10"),
        leverage=Decimal("10"),
        quantity=Decimal("0.1"),
        notional=Decimal("6000"),
        est_margin=Decimal("600"),
        budget=Decimal("600"),
        status="ENTRY_FILLED",
    )
    storage.close()

    server = HTTPServer(("127.0.0.1", 0), DashboardRequestHandler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        conn = HTTPConnection("127.0.0.1", port, timeout=5)

        # Close trade via POST /api/trades/close
        body = json.dumps({"trade_id": t_id}).encode("utf-8")
        conn.request("POST", "/api/trades/close", body=body, headers={"Content-Type": "application/json"})
        res = conn.getresponse()
        assert res.status == 200
        payload = json.loads(res.read().decode("utf-8"))
        assert payload["success"] is True

        # Verify trade is CLOSED in DB
        storage = Storage(settings.database_file)
        closed_trade = storage.get_trade(t_id)
        assert closed_trade.status == "CLOSED"
        assert closed_trade.exit_reason == "manual_dashboard_close"
        storage.close()

        conn.close()
    finally:
        server.shutdown()
        server.server_close()

