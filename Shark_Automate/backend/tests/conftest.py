"""Shared test fixtures: real Postgres (preferred) or SQLite fallback.

Every test runs against a freshly created schema with seeded reference data.
"""
from __future__ import annotations

import os
from datetime import UTC
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.config import AppSettings, clear_settings_cache
from app.db.session import Base, dispose_engine, get_engine, init_engine, session_scope
from app.services.bootstrap import seed_reference_data
from app.services.runtime import AppContext, build_context

PG_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://trading:trading@127.0.0.1:5432/trading_test",
)
SQLITE_URL = "sqlite+aiosqlite:///:memory:"

SYMBOLS = {
    "BASUSDT": Decimal("100"),
    "BTCUSDT": Decimal("100"),
    "ETHUSDT": Decimal("100"),
    "SOLUSDT": Decimal("100"),
    "DOGEUSDT": Decimal("100"),
}


def make_settings(**overrides) -> AppSettings:
    clear_settings_cache()
    defaults = dict(
        testing=True,
        inline_processing=True,
        environment="test",
        webhook_secret="test-secret",
        database_url_test=PG_URL if _pg_available() else SQLITE_URL,
        paper_slippage_bps=Decimal("0"),
        paper_taker_fee_bps=Decimal("0"),
        paper_maker_fee_bps=Decimal("0"),
        paper_funding_rate_bps_8h=Decimal("0"),
        paper_latency_ms=0,
        roi_monitor_interval_seconds=0.05,
        log_level="WARNING",
        live_trading_enabled=False,
        live_trading_confirmation="",
        shark_dry_run=True,
    )
    defaults.update(overrides)
    return AppSettings(**defaults)


def _pg_available() -> bool:
    if os.environ.get("FORCE_SQLITE"):
        return False
    try:
        import psycopg2  # noqa: F401
    except Exception:
        pass
    return not PG_URL.startswith("sqlite")


@pytest_asyncio.fixture
async def ctx(request: pytest.FixtureRequest) -> AppContext:
    overrides = getattr(request, "param", {}) or {}
    settings = make_settings(**overrides)
    init_engine(settings, force=True)
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    context = build_context(settings)
    for symbol, price in SYMBOLS.items():
        context.market_data.seed_price(symbol, price)
    async with session_scope() as session:
        await seed_reference_data(session, settings)
    yield context
    await context.paper._orders.clear() if False else None
    await dispose_engine()


@pytest_asyncio.fixture
async def client(ctx: AppContext):
    from app.core.config import get_settings
    from app.db.session import get_db_session
    from app.main import create_app

    app = create_app()
    app.state.ctx = ctx
    app.dependency_overrides[get_settings] = lambda: ctx.settings

    async def _override_session():
        factory = __import__("app.db.session", fromlist=["get_session_factory"]).get_session_factory()
        session = factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    app.dependency_overrides[get_db_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def alert_payload(symbol: str, action: str = "BUY", **overrides) -> dict:
    from datetime import datetime

    payload = {
        "event_id": overrides.pop("event_id", None) or f"ev-{symbol}-{action}-{os.urandom(4).hex()}",
        "strategy_id": "strategy_1",
        "strategy_name": "Strategy 1",
        "symbol": symbol,
        "action": action,
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "timeframe": "5m",
        "price": 100.0,
        "source": "TRADINGVIEW",
        "secret": "test-secret",
    }
    payload.update(overrides)
    if payload.get("event_id") is None:
        payload.pop("event_id", None)
    return payload
