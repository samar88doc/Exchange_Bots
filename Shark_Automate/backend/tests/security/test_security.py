"""Security tests: invalid webhook, missing secret, malformed JSON, replay, duplicate."""
from __future__ import annotations

import pytest

from tests.conftest import alert_payload


@pytest.mark.asyncio
async def test_missing_secret_rejected(ctx, client):
    payload = alert_payload(symbol="BTCUSDT.P")
    payload.pop("secret", None)
    r = await client.post("/api/webhooks/tradingview", json=payload)
    assert r.status_code == 401
    assert "INVALID_SIGNATURE" in r.json()["detail"]


@pytest.mark.asyncio
async def test_wrong_secret_rejected(ctx, client):
    payload = alert_payload(symbol="BTCUSDT.P", secret="wrong")
    r = await client.post("/api/webhooks/tradingview", json=payload)
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_header_secret_accepted(ctx, client):
    payload = alert_payload(symbol="BTCUSDT.P")
    payload.pop("secret", None)
    r = await client.post("/api/webhooks/tradingview", json=payload,
                          headers={"x-webhook-secret": "test-secret"})
    assert r.status_code == 202


@pytest.mark.asyncio
async def test_malformed_json_rejected(ctx, client):
    r = await client.post("/api/webhooks/tradingview", content=b"{bad json",
                          headers={"content-type": "application/json",
                                   "x-webhook-secret": "test-secret"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_stale_event_replay_rejected(ctx, client):
    payload = alert_payload(symbol="BTCUSDT.P", timestamp="2020-01-01T00:00:00Z")
    r = await client.post("/api/webhooks/tradingview", json=payload)
    assert r.status_code == 400
    assert "STALE_EVENT" in r.json()["detail"]


@pytest.mark.asyncio
async def test_duplicate_event_suppressed(ctx, client):
    payload = alert_payload(symbol="BTCUSDT.P", event_id="dup-1")
    r1 = await client.post("/api/webhooks/tradingview", json=payload)
    r2 = await client.post("/api/webhooks/tradingview", json=payload)
    assert r1.json()["status"] == "ACCEPTED"
    assert r2.json()["status"] == "DUPLICATE"


@pytest.mark.asyncio
async def test_duplicate_fingerprint_without_event_id_suppressed(ctx, client):
    payload = alert_payload(symbol="BTCUSDT.P")
    payload.pop("event_id", None)
    await client.post("/api/webhooks/tradingview", json=payload)
    p2 = dict(payload)
    r2 = await client.post("/api/webhooks/tradingview", json=p2)
    assert r2.json()["status"] == "DUPLICATE"


@pytest.mark.asyncio
async def test_secrets_never_persisted(ctx, client):
    payload = alert_payload(symbol="BTCUSDT.P", event_id="sec-1")
    await client.post("/api/webhooks/tradingview", json=payload)
    from sqlalchemy import select

    from app.db.session import session_scope
    from app.models.entities import WebhookEvent

    async with session_scope() as session:
        event = (await session.execute(select(WebhookEvent))).scalars().first()
    assert "secret" not in (event.raw_payload or {})
    assert "test-secret" not in str(event.raw_payload)


@pytest.mark.asyncio
async def test_oversized_payload_rejected(ctx, client):
    payload = alert_payload(symbol="BTCUSDT.P", metadata={"junk": "x" * 70000})
    r = await client.post("/api/webhooks/tradingview", json=payload)
    assert r.status_code in (400, 413)
