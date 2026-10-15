from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Base, make_engine
from app.main import create_app
from app.models import Trade
from tests.conftest import valid_alert


def test_webhook_is_tokenized_durable_and_idempotent(
    test_client: TestClient,
    dashboard_headers: dict[str, str],
    webhook_url: str,
) -> None:
    client = test_client
    no_token = client.post("/api/webhooks/tradingview", json=valid_alert())
    assert no_token.status_code == 401
    wrong_token = client.post("/api/webhooks/tradingview/wrong", json=valid_alert())
    assert wrong_token.status_code == 401

    first = client.post(webhook_url, json=valid_alert())
    assert first.status_code == 202
    first_body = first.json()
    assert first_body["accepted"] is True
    assert first_body["duplicate"] is False

    second = client.post(webhook_url, json=valid_alert())
    assert second.status_code == 202
    second_body = second.json()
    assert second_body["duplicate"] is True
    assert second_body["event_id"] == first_body["event_id"]

    events = client.get("/api/events", headers=dashboard_headers)
    assert events.status_code == 200
    assert len(events.json()["items"]) == 1
    stored = events.json()["items"][0]
    assert stored["tradingview_symbol"] == "BTCUSDT.P"
    assert stored["symbol"] == "BTCUSDT"
    assert stored["signal_close"] == "102.75"
    assert stored["entry_price"] == "101.5"
    assert len(stored["outbox"]) == 2
    assert {job["kind"] for job in stored["outbox"]} == {"EXECUTE_SIGNAL", "TELEGRAM_SIGNAL"}

    history = client.get("/api/history", headers=dashboard_headers)
    assert history.status_code == 200
    assert len(history.json()["items"]) == 1
    assert history.json()["items"][0]["planned_entry"] == "101.5"
    assert history.json()["items"][0]["signal_close"] == "102.75"


def test_webhook_accepts_user_demo_format(test_client: TestClient) -> None:
    # User's exact demo payload format:
    # {"symbol": "{{ticker}}", "action": "BUY", "price": "{{close}}", "secret": "demo-webhook-secret-12345"}
    resp = test_client.post(
        "/api/webhooks/tradingview",
        json={
            "symbol": "BTCUSDT.P",
            "action": "BUY",
            "price": "81250.50",
            "secret": "demo-webhook-secret-12345",
        },
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["accepted"] is True
    assert data["status"] in ("QUEUED", "ENTRY_PENDING")


def test_dashboard_api_requires_auth_but_health_is_safe_and_public(
    test_client: TestClient,
    dashboard_headers: dict[str, str],
) -> None:
    assert test_client.get("/api/settings").status_code == 401
    health = test_client.get("/api/health")
    assert health.status_code == 200
    body = health.json()
    serialized = str(body).lower()
    assert "test_only_dashboard_token" not in serialized
    assert "test_only_webhook_token" not in serialized
    assert test_client.get("/api/settings", headers=dashboard_headers).status_code == 200


def test_risk_cap_reserves_slots_and_rejects_another_active_trade(
    tmp_path,
) -> None:
    database_path = tmp_path / "risk-cap.sqlite3"
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path}",
        dashboard_api_token="TEST_ONLY_DASHBOARD_TOKEN_NOT_A_SECRET",
        tradingview_webhook_token="TEST_ONLY_WEBHOOK_TOKEN_NOT_A_SECRET",
        max_open_positions=1,
        run_workers=False,
        run_market_stream=False,
        run_market_poll=False,
    )
    engine = make_engine(settings.database_url)
    import asyncio

    async def create_schema() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(create_schema())
    app = create_app(settings, engine=engine)
    with TestClient(app, base_url="https://testserver") as client:
        first = client.post(
            "/api/webhooks/tradingview/TEST_ONLY_WEBHOOK_TOKEN_NOT_A_SECRET",
            json=valid_alert(event_id="risk-slot-one", symbol="BTCUSDT.P"),
        )
        second = client.post(
            "/api/webhooks/tradingview/TEST_ONLY_WEBHOOK_TOKEN_NOT_A_SECRET",
            json=valid_alert(event_id="risk-slot-two", symbol="ETHUSDT.P", bar_time="2026-10-15T10:30:00Z"),
        )
        assert first.status_code == 202
        assert first.json()["status"] == "QUEUED"
        assert second.status_code == 202
        assert second.json()["status"] == "REJECTED"
        history = client.get("/api/history", headers={"X-Dashboard-Token": settings.dashboard_api_token})
        assert len(history.json()["items"]) == 1
        events = client.get("/api/events", headers={"X-Dashboard-Token": settings.dashboard_api_token})
        rejected = next(item for item in events.json()["items"] if item["supplied_event_id"] == "risk-slot-two")
        assert rejected["status_reason"] == "MAX_OPEN_POSITIONS_REACHED"


def test_live_mode_remains_blocked_and_settings_are_server_enforced(
    test_client: TestClient,
    dashboard_headers: dict[str, str],
) -> None:
    bad_risk = test_client.patch(
        "/api/settings",
        headers=dashboard_headers,
        json={"target1_percent": "3", "target2_percent": "2", "target3_percent": "4"},
    )
    assert bad_risk.status_code == 422
    blocked = test_client.post(
        "/api/control/mode/live",
        headers=dashboard_headers,
        json={"confirmation_phrase": "ENABLE LIVE TRADING", "acknowledge_exchange_risk": True},
    )
    assert blocked.status_code == 409
    assert "server gate is closed" in blocked.json()["detail"]


def test_pending_entry_is_listed_and_can_be_cancelled_by_trade_id(
    test_client: TestClient,
    dashboard_headers: dict[str, str],
    webhook_url: str,
) -> None:
    accepted = test_client.post(webhook_url, json=valid_alert(event_id="cancel-single-entry"))
    assert accepted.status_code == 202
    events = test_client.get("/api/events", headers=dashboard_headers).json()["items"]
    trade_id = events[0]["trade_id"]
    assert trade_id

    pending = test_client.get("/api/pending-entries", headers=dashboard_headers)
    assert pending.status_code == 200
    assert pending.json()["items"][0]["id"] == trade_id
    assert pending.json()["items"][0]["status"] == "QUEUED"

    phrase = {"confirmation_phrase": "CANCEL PENDING ENTRIES"}
    too_early = test_client.post(
        f"/api/trades/{trade_id}/cancel-entry", headers=dashboard_headers, json=phrase
    )
    assert too_early.status_code == 409
    bad_phrase = test_client.post(
        f"/api/trades/{trade_id}/cancel-entry",
        headers=dashboard_headers,
        json={"confirmation_phrase": "CANCEL"},
    )
    assert bad_phrase.status_code == 422

    async def confirm_simulated_entry_pending() -> None:
        async with test_client.app.state.session_factory() as session:
            trade = await session.get(Trade, trade_id)
            assert trade is not None
            trade.status = "ENTRY_PENDING"
            await session.commit()

    test_client.portal.call(confirm_simulated_entry_pending)
    cancelled = test_client.post(
        f"/api/trades/{trade_id}/cancel-entry", headers=dashboard_headers, json=phrase
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["confirmed_cancellations"] == 1

    remaining = test_client.get("/api/pending-entries", headers=dashboard_headers)
    assert remaining.json()["count"] == 0
    history = test_client.get("/api/history", headers=dashboard_headers).json()["items"]
    assert history[0]["status"] == "CANCELLED"


def test_legacy_payload_is_persisted_as_rejected_if_required_fields_are_missing(
    test_client: TestClient,
    dashboard_headers: dict[str, str],
    webhook_url: str,
) -> None:
    response = test_client.post(webhook_url, content=b"BUY BTCUSDT now", headers={"content-type": "text/plain"})
    assert response.status_code == 202
    assert response.json()["status"] == "REJECTED"
    events = test_client.get("/api/events", headers=dashboard_headers).json()["items"]
    assert len(events) == 1
    assert events[0]["symbol"] is None
    assert "MISSING_SYMBOL" in events[0]["status_reason"]


def test_dashboard_auth_enable_disable_feature(
    test_client: TestClient,
    dashboard_headers: dict[str, str],
) -> None:
    # 1. When auth is enabled, missing token returns 401
    unauth = test_client.get("/api/settings")
    assert unauth.status_code == 401

    # 2. Valid token returns 200 and reports dashboard_auth_enabled: True
    authed = test_client.get("/api/settings", headers=dashboard_headers)
    assert authed.status_code == 200
    assert authed.json()["dashboard_auth_enabled"] is True

    # 3. Disable dashboard auth via control API
    toggle_off = test_client.post(
        "/api/control/dashboard-auth",
        headers=dashboard_headers,
        json={"enabled": False},
    )
    assert toggle_off.status_code == 200
    assert toggle_off.json()["dashboard_auth_enabled"] is False

    # 4. Now requests without token succeed with 200
    open_access = test_client.get("/api/settings")
    assert open_access.status_code == 200
    assert open_access.json()["dashboard_auth_enabled"] is False

    # 5. Enable dashboard auth again
    toggle_on = test_client.post(
        "/api/control/dashboard-auth",
        json={"enabled": True},
    )
    assert toggle_on.status_code == 200
    assert toggle_on.json()["dashboard_auth_enabled"] is True

    # 6. Once re-enabled, missing token returns 401 again
    unauth_again = test_client.get("/api/settings")
    assert unauth_again.status_code == 401

