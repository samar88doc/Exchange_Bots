"""Regression coverage for first-owner setup, session lifetime and privilege boundaries."""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Base, make_engine
from app.main import create_app


@pytest.fixture
def auth_client(tmp_path) -> Iterator[TestClient]:
    database_path = tmp_path / "access-control.sqlite3"
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path}",
        dashboard_api_token="TEST_AUTOMATION_ONLY_TOKEN",
        dashboard_username="operator",
        dashboard_password="correct-horse-battery-staple",
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
        yield client


def _session_headers(client: TestClient, username: str, password: str) -> dict[str, str]:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"X-Session-Token": response.json()["session_token"]}


def test_initial_owner_signup_is_one_time_and_opt_in(tmp_path) -> None:
    database_path = tmp_path / "first-owner.sqlite3"
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path}",
        initial_owner_signup_enabled=True,
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
        config = client.get("/api/public/config")
        assert config.status_code == 200
        assert config.json()["initial_owner_signup_enabled"] is True

        created = client.post(
            "/api/auth/signup",
            json={"username": "first.owner", "password": "first-owner-password", "display_name": "First Owner"},
        )
        assert created.status_code == 201, created.text
        assert created.json()["user"]["role"] == "OWNER"
        assert created.json()["session_token"]
        assert client.get("/api/public/config").json()["initial_owner_signup_enabled"] is False

        # This route is deliberately not public account registration after setup.
        second = client.post(
            "/api/auth/signup",
            json={"username": "takeover", "password": "another-owner-password"},
        )
        assert second.status_code == 409
        assert _session_headers(client, "first.owner", "first-owner-password")


def test_admin_cannot_escalate_or_modify_owner_and_password_change_revokes_sessions(auth_client: TestClient) -> None:
    owner = _session_headers(auth_client, "operator", "correct-horse-battery-staple")
    created = auth_client.post(
        "/api/users",
        headers=owner,
        json={"username": "admin.one", "password": "admin-one-password", "role": "ADMIN"},
    )
    assert created.status_code == 200, created.text
    admin = _session_headers(auth_client, "admin.one", "admin-one-password")

    # Administrators manage operators but cannot create, demote, disable or reset an OWNER.
    assert auth_client.post(
        "/api/users",
        headers=admin,
        json={"username": "escalation", "password": "escalation-password", "role": "OWNER"},
    ).status_code == 403
    owner_id = auth_client.get("/api/auth/me", headers=owner).json()["user"]["id"]
    assert auth_client.patch(
        f"/api/users/{owner_id}", headers=admin, json={"password": "replaced-owner-password"}
    ).status_code == 403
    assert auth_client.patch(f"/api/users/{owner_id}", headers=admin, json={"is_active": False}).status_code == 403
    # Even the server-side automation identity cannot leave the console ownerless.
    assert auth_client.patch(
        f"/api/users/{owner_id}",
        headers={"X-Dashboard-Token": "TEST_AUTOMATION_ONLY_TOKEN"},
        json={"is_active": False},
    ).status_code == 422

    # Resetting an operator password invalidates its prior remembered/session credentials.
    admin_id = auth_client.get("/api/auth/me", headers=admin).json()["user"]["id"]
    changed = auth_client.patch(
        f"/api/users/{admin_id}", headers=owner, json={"password": "admin-one-new-password"}
    )
    assert changed.status_code == 200, changed.text
    assert auth_client.get("/api/settings", headers=admin).status_code == 401
    assert _session_headers(auth_client, "admin.one", "admin-one-new-password")


def test_dashboard_is_not_frameable_by_default(auth_client: TestClient) -> None:
    response = auth_client.get("/")
    assert response.status_code == 200
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_remembered_login_has_a_longer_but_finite_server_session(auth_client: TestClient) -> None:
    short = auth_client.post(
        "/api/auth/login",
        json={"username": "operator", "password": "correct-horse-battery-staple", "remember": False},
    )
    remembered = auth_client.post(
        "/api/auth/login",
        json={"username": "operator", "password": "correct-horse-battery-staple", "remember": True},
    )
    assert short.status_code == 200 and remembered.status_code == 200
    short_expiry = datetime.fromisoformat(short.json()["session_expires_at"])
    remembered_expiry = datetime.fromisoformat(remembered.json()["session_expires_at"])
    assert remembered_expiry > short_expiry
    assert (remembered_expiry - datetime.now(UTC)).days >= 29
