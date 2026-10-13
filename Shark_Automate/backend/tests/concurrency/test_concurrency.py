"""Concurrency tests: simultaneous signals can NEVER bypass the 3-position limit."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.models.entities import Position, Signal
from tests.conftest import alert_payload


@pytest.mark.asyncio
async def test_four_signals_only_three_slots(ctx, client):
    symbols = ["BTCUSDT.P", "ETHUSDT.P", "SOLUSDT.P", "DOGEUSDT.P"]
    responses = await asyncio.gather(
        *[client.post("/api/webhooks/tradingview", json=alert_payload(symbol=s, action="BUY")) for s in symbols]
    )
    assert all(r.status_code == 202 for r in responses)

    from app.db.session import session_scope

    async with session_scope() as session:
        signals = list((await session.execute(select(Signal))).scalars())
        positions = list((await session.execute(select(Position))).scalars())
    assert len(signals) == 4
    active = [p for p in positions if p.state not in ("CLOSED", "REJECTED", "CANCELLED", "ORDER_FAILED")]
    assert len(active) == 3
    rejected = [s for s in signals if s.rejection_reason == "MAX_OPEN_POSITIONS_REACHED"]
    assert len(rejected) == 1


@pytest.mark.asyncio
async def test_eight_signals_still_max_three(ctx, client):
    symbols = ["BTCUSDT.P", "ETHUSDT.P", "SOLUSDT.P", "DOGEUSDT.P",
               "BASUSDT.P", "BTCUSDT.P", "ETHUSDT.P", "SOLUSDT.P"]
    await asyncio.gather(
        *[client.post("/api/webhooks/tradingview", json=alert_payload(symbol=s, action="BUY")) for s in symbols]
    )
    from app.db.session import session_scope

    async with session_scope() as session:
        positions = list((await session.execute(select(Position))).scalars())
    active = [p for p in positions if p.state not in ("CLOSED", "REJECTED", "CANCELLED", "ORDER_FAILED")]
    assert len(active) <= 3


@pytest.mark.asyncio
async def test_duplicate_canonical_race_bas_variants(ctx, client):
    """BASUSDT and BASUSDT.P racing concurrently must yield at most ONE position."""
    await asyncio.gather(
        client.post("/api/webhooks/tradingview", json=alert_payload(symbol="BASUSDT", action="BUY")),
        client.post("/api/webhooks/tradingview", json=alert_payload(symbol="BASUSDT.P", action="BUY")),
        client.post("/api/webhooks/tradingview", json=alert_payload(symbol="basusdt.p", action="BUY")),
    )
    from app.db.session import session_scope

    async with session_scope() as session:
        positions = list((await session.execute(select(Position))).scalars())
    active = [p for p in positions if p.state not in ("CLOSED", "REJECTED", "CANCELLED", "ORDER_FAILED")]
    assert len(active) == 1
    assert active[0].canonical_symbol == "BASUSDT"
