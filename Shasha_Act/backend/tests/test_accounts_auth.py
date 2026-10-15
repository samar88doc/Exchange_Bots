"""Username/password sign-in, session tokens, subscription and disclaimer enforcement."""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Base, make_engine
from app.main import create_app

OPERATOR_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def auth_client(tmp_path) -> Iterator[TestClient]:
    database_path = tmp_path / "shasha-act-auth.sqlite3"
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path}",
        dashboard_api_token="TEST_ONLY_DASHBOARD_TOKEN_NOT_A_SECRET",
        tradingview_webhook_token="TEST_ONLY_WEBHOOK_TOKEN_NOT_A_SECRET",
        dashboard_username="operator",
        dashboard_password=OPERATOR_PASSWORD,
        dashboard_session_hours=4,
        subscription_plan_name="SHASHA ACT PRO",
        subscription_fee="999",
        subscription_currency="INR",
        subscription_expires_at=(datetime.now(UTC) + timedelta(days=30)).isoformat(),
        require_disclaimer_ack=True,
        enforce_subscription=False,
        run_workers=False,
        run_market_stream=False,
        run_market_poll=False,
    )
    engine = make_engine(settings.database_url)

    async def create_schema() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(create_schema())
    app = create_app(settings, engine=engine)
    with TestClient(app, base_url="https://testserver") as client:
        yield client


def _login(client: TestClient, password: str = OPERATOR_PASSWORD) -> dict:
    response = client.post(
        "/api/auth/login", json={"username": "operator", "password": password}
    )
    return {"status": response.status_code, "body": response.json() if response.content else {}}


def _session_headers(client: TestClient, *, accept_disclaimer: bool = True) -> dict[str, str]:
    """Sign in the way the dashboard does, optionally acknowledging the disclaimer."""
    token = _login(client)["body"]["session_token"]
    headers = {"X-Session-Token": token}
    if accept_disclaimer:
        assert client.post("/api/auth/disclaimer", headers=headers).status_code == 200
    return headers


def test_public_config_exposes_login_metadata_without_secrets(auth_client: TestClient) -> None:
    body = auth_client.get("/api/public/config").json()
    assert body["auth_enabled"] is True
    assert body["username_signin_available"] is True
    assert body["subscription"]["plan_name"] == "SHASHA ACT PRO"
    assert body["subscription"]["fee"] == "999"
    assert body["subscription"]["currency"] == "INR"
    assert "disclaimer" in body
    # No credential or infrastructure detail is ever returned here.
    serialized = str(body)
    assert "TEST_ONLY_DASHBOARD_TOKEN_NOT_A_SECRET" not in serialized
    assert "sqlite" not in serialized
    assert "database_url" not in serialized
    assert OPERATOR_PASSWORD not in serialized


def test_login_rejects_bad_credentials_and_accepts_the_bootstrapped_account(auth_client: TestClient) -> None:
    bad = _login(auth_client, "wrong-password")
    assert bad["status"] == 401
    assert "Invalid username or password" in bad["body"]["detail"]

    unknown = auth_client.post("/api/auth/login", json={"username": "nobody", "password": "x" * 12})
    assert unknown.status_code == 401

    good = _login(auth_client)
    assert good["status"] == 200
    assert good["body"]["authenticated"] is True
    assert good["body"]["user"]["username"] == "operator"
    assert good["body"]["user"]["role"] == "OWNER"
    assert good["body"]["session_token"]
    assert good["body"]["disclaimer"]["required"] is True
    assert good["body"]["disclaimer"]["accepted_at"] is None


def test_session_token_authorizes_dashboard_calls_and_logout_revokes_it(auth_client: TestClient) -> None:
    headers = _session_headers(auth_client)


    assert auth_client.get("/api/settings", headers=headers).status_code == 200
    me = auth_client.get("/api/auth/me", headers=headers).json()
    assert me["auth_method"] == "SESSION"
    assert me["permissions"]["can_manage_users"] is True

    assert auth_client.get("/api/settings").status_code == 401
    assert auth_client.get("/api/settings", headers={"X-Session-Token": "bogus"}).status_code == 401

    # The server token remains available for automation and its own access level.
    assert (
        auth_client.get(
            "/api/settings", headers={"X-Dashboard-Token": "TEST_ONLY_DASHBOARD_TOKEN_NOT_A_SECRET"}
        ).status_code
        == 200
    )

    assert auth_client.post("/api/auth/logout", headers=headers).json()["revoked"] is True
    assert auth_client.get("/api/settings", headers=headers).status_code == 401


def test_disclaimer_must_be_accepted_before_state_changes(auth_client: TestClient) -> None:
    headers = _session_headers(auth_client, accept_disclaimer=False)

    blocked = auth_client.post(
        "/api/control/pause", headers=headers, json={"paused": True, "confirmation_phrase": "PAUSE SHASHA ACT"}
    )
    assert blocked.status_code == 403
    assert "DISCLAIMER_NOT_ACCEPTED" in blocked.json()["detail"]

    accepted = auth_client.post("/api/auth/disclaimer", headers=headers)
    assert accepted.status_code == 200
    assert accepted.json()["accepted"] is True

    allowed = auth_client.post(
        "/api/control/pause", headers=headers, json={"paused": True, "confirmation_phrase": "PAUSE SHASHA ACT"}
    )
    assert allowed.status_code == 200
    assert allowed.json()["trading_paused"] is True


def test_subscription_can_be_read_and_updated_by_an_admin(auth_client: TestClient) -> None:
    headers = _session_headers(auth_client)

    service = auth_client.get("/api/subscription", headers=headers).json()
    assert service["service"]["plan_name"] == "SHASHA ACT PRO"
    assert service["service"]["fee"] == "999"
    assert service["service"]["status"] == "ACTIVE"
    assert service["service"]["days_remaining"] is not None
    assert service["user"]["status"] in {"ACTIVE", "EXPIRING_SOON"}

    updated = auth_client.patch(
        "/api/subscription",
        headers=headers,
        json={
            "plan_name": "SHASHA ACT ELITE",
            "fee": "1499",
            "currency": "INR",
            "billing_cycle": "MONTHLY",
            "expires_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
        },
    )
    assert updated.status_code == 200
    assert updated.json()["service"]["plan_name"] == "SHASHA ACT ELITE"
    assert updated.json()["service"]["status"] == "EXPIRING_SOON"

    assert (
        auth_client.patch("/api/subscription", headers=headers, json={"status": "NONSENSE"}).status_code == 422
    )


def test_admin_can_create_update_and_disable_operator_accounts(auth_client: TestClient) -> None:
    admin_headers = _session_headers(auth_client)

    created = auth_client.post(
        "/api/users",
        headers=admin_headers,
        json={
            "username": "viewer.one",
            "password": "another-long-password",
            "role": "VIEWER",
            "display_name": "Read only",
            "subscription_plan": "TRIAL",
            "subscription_fee": "0",
        },
    )
    assert created.status_code == 200
    user_id = created.json()["user"]["id"]
    assert created.json()["user"]["role"] == "VIEWER"

    duplicate = auth_client.post(
        "/api/users",
        headers=admin_headers,
        json={"username": "viewer.one", "password": "another-long-password"},
    )
    assert duplicate.status_code == 409

    listing = auth_client.get("/api/users", headers=admin_headers).json()
    assert listing["count"] == 2

    # A viewer session cannot manage accounts.
    viewer_login = auth_client.post(
        "/api/auth/login", json={"username": "viewer.one", "password": "another-long-password"}
    ).json()
    viewer_headers = {"X-Session-Token": viewer_login["session_token"]}
    assert auth_client.post("/api/auth/disclaimer", headers=viewer_headers).status_code == 200
    assert auth_client.get("/api/users", headers=viewer_headers).status_code == 403
    assert (
        auth_client.post(
            "/api/users",
            headers=viewer_headers,
            json={"username": "third.user", "password": "third-long-password"},
        ).status_code
        == 403
    )

    updated = auth_client.patch(
        f"/api/users/{user_id}", headers=admin_headers, json={"is_active": False, "role": "OPERATOR"}
    )
    assert updated.status_code == 200
    assert updated.json()["user"]["is_active"] is False

    # A disabled account can no longer sign in.
    assert _login(auth_client, OPERATOR_PASSWORD)["status"] == 200
    disabled = auth_client.post(
        "/api/auth/login", json={"username": "viewer.one", "password": "another-long-password"}
    )
    assert disabled.status_code == 403


def test_expired_subscription_blocks_changes_when_enforcement_is_on(tmp_path) -> None:
    database_path = tmp_path / "shasha-act-expired.sqlite3"
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path}",
        dashboard_api_token="TEST_ONLY_DASHBOARD_TOKEN_NOT_A_SECRET",
        dashboard_username="owner",
        dashboard_password="owner-long-password",
        subscription_expires_at=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
        enforce_subscription=True,
        require_disclaimer_ack=False,
        run_workers=False,
        run_market_stream=False,
        run_market_poll=False,
    )
    engine = make_engine(settings.database_url)

    async def create_schema() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(create_schema())
    app = create_app(settings, engine=engine)
    with TestClient(app, base_url="https://testserver") as client:
        login = client.post(
            "/api/auth/login", json={"username": "owner", "password": "owner-long-password"}
        ).json()
        headers = {"X-Session-Token": login["session_token"]}
        assert login["subscription"]["status"] == "EXPIRED"

        blocked = client.post(
            "/api/control/pause",
            headers=headers,
            json={"paused": True, "confirmation_phrase": "PAUSE SHASHA ACT"},
        )
        assert blocked.status_code == 402
        assert "SUBSCRIPTION_EXPIRED" in blocked.json()["detail"]

        # Reads keep working so the operator can renew.
        assert client.get("/api/subscription", headers=headers).status_code == 200
        assert client.get("/api/overview", headers=headers).status_code == 200


def test_webhook_is_rejected_while_the_subscription_is_inactive(tmp_path) -> None:
    database_path = tmp_path / "shasha-act-webhook-sub.sqlite3"
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path}",
        tradingview_webhook_token="TEST_ONLY_WEBHOOK_TOKEN_NOT_A_SECRET",
        dashboard_api_token="TEST_ONLY_DASHBOARD_TOKEN_NOT_A_SECRET",
        subscription_expires_at=(datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
        enforce_subscription=True,
        run_workers=False,
        run_market_stream=False,
        run_market_poll=False,
    )
    engine = make_engine(settings.database_url)

    async def create_schema() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(create_schema())
    app = create_app(settings, engine=engine)
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/api/webhooks/tradingview/TEST_ONLY_WEBHOOK_TOKEN_NOT_A_SECRET",
            json={
                "strategy": "SHASHA",
                "event_id": "expired-subscription-signal",
                "symbol": "BTCUSDT.P",
                "direction": "LONG",
                "timeframe": "5",
                "bar_time": datetime.now(UTC).isoformat(),
                "signal_close": "100",
                "entry_price": "99",
            },
        )
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "REJECTED"
        events = client.get(
            "/api/events", headers={"X-Dashboard-Token": "TEST_ONLY_DASHBOARD_TOKEN_NOT_A_SECRET"}
        ).json()
        items = events["items"] if isinstance(events, dict) else events
        assert any("SUBSCRIPTION_INACTIVE" in (item.get("status_reason") or "") for item in items)


def _create_user(client: TestClient, headers: dict[str, str], username: str, role: str, password: str) -> str:
    response = client.post(
        "/api/users",
        json={"username": username, "password": password, "role": role},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["user"]["id"]


def _operator_headers(client: TestClient, username: str, password: str) -> dict[str, str]:
    login = client.post("/api/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200, login.text
    headers = {"X-Session-Token": login.json()["session_token"]}
    accepted = client.post("/api/auth/disclaimer", headers=headers)
    assert accepted.status_code == 200, accepted.text
    return headers


def test_viewer_role_cannot_change_settings_pause_or_toggle_authentication(auth_client: TestClient) -> None:
    """Regression: a read-only operator once reached every mutating route.

    Because a resolved session must satisfy the minimum role and the server automation token
    keeps owner-level access, a VIEWER has to be refused everywhere that changes state.
    """
    owner = _session_headers(auth_client)
    _create_user(auth_client, owner, "readonly", "VIEWER", "viewer-password-1")
    viewer = _operator_headers(auth_client, "readonly", "viewer-password-1")

    baseline = auth_client.get("/api/settings", headers=viewer).json()
    forbidden = [
        ("PATCH", "/api/settings", {"target1_percent": "9"}),
        ("POST", "/api/control/pause", {"paused": True, "confirmation_phrase": "PAUSE SHASHA ACT"}),
        ("POST", "/api/control/cancel-pending", {"confirmation_phrase": "CANCEL PENDING ENTRIES"}),
        ("POST", "/api/control/mode/paper", {"confirmation_phrase": "SWITCH TO PAPER"}),
        (
            "POST",
            "/api/control/mode/live",
            {"confirmation_phrase": "ENABLE LIVE TRADING", "acknowledge_exchange_risk": True},
        ),
        ("POST", "/api/control/dashboard-auth", {"enabled": False}),
        ("PATCH", "/api/subscription", {"fee": "1"}),
        ("POST", "/api/users", {"username": "sneaky", "password": "another-password-1", "role": "ADMIN"}),
    ]
    for method, path, body in forbidden:
        response = auth_client.request(method, path, json=body, headers=viewer)
        assert response.status_code == 403, f"{method} {path} returned {response.status_code}"

    # Nothing changed: settings, trading state and authentication are untouched.
    after = auth_client.get("/api/settings", headers=viewer).json()
    assert after["dashboard_auth_enabled"] is True
    assert after["trading_paused"] is False
    assert after["risk"]["target1_percent"] == baseline["risk"]["target1_percent"]
    assert auth_client.get("/api/positions", headers=viewer).status_code == 200


def test_operator_can_pause_but_only_admin_can_toggle_authentication(auth_client: TestClient) -> None:
    owner = _session_headers(auth_client)
    _create_user(auth_client, owner, "shiftop", "OPERATOR", "operator-password-1")
    operator = _operator_headers(auth_client, "shiftop", "operator-password-1")

    paused = auth_client.post(
        "/api/control/pause",
        json={"paused": True, "confirmation_phrase": "PAUSE SHASHA ACT"},
        headers=operator,
    )
    assert paused.status_code == 200 and paused.json()["trading_paused"] is True
    resumed = auth_client.post(
        "/api/control/pause",
        json={"paused": False, "confirmation_phrase": "RESUME SHASHA ACT"},
        headers=operator,
    )
    assert resumed.status_code == 200 and resumed.json()["trading_paused"] is False

    assert (
        auth_client.post("/api/control/dashboard-auth", json={"enabled": False}, headers=operator).status_code
        == 403
    )
    assert (
        auth_client.post("/api/control/dashboard-auth", json={"enabled": True}, headers=owner).status_code == 200
    )


def test_server_token_keeps_owner_level_access_for_automation(auth_client: TestClient) -> None:
    """The automation token has no operator identity and must not be blocked by role gates."""
    token_headers = {"X-Dashboard-Token": "TEST_ONLY_DASHBOARD_TOKEN_NOT_A_SECRET"}
    assert auth_client.get("/api/users", headers=token_headers).status_code == 200
    paused = auth_client.post(
        "/api/control/pause",
        json={"paused": True, "confirmation_phrase": "PAUSE SHASHA ACT"},
        headers=token_headers,
    )
    assert paused.status_code == 200
    assert (
        auth_client.post(
            "/api/control/pause",
            json={"paused": False, "confirmation_phrase": "RESUME SHASHA ACT"},
            headers=token_headers,
        ).status_code
        == 200
    )
    assert (
        auth_client.patch("/api/subscription", json={"fee": "1499"}, headers=token_headers).status_code == 200
    )
