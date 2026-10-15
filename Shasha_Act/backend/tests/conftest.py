from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Base, make_engine
from app.main import create_app


@pytest.fixture
def test_client(tmp_path) -> Iterator[TestClient]:
    database_path = tmp_path / "shasha-act-test.sqlite3"
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=f"sqlite+aiosqlite:///{database_path}",
        dashboard_api_token="TEST_ONLY_DASHBOARD_TOKEN_NOT_A_SECRET",
        tradingview_webhook_token="TEST_ONLY_WEBHOOK_TOKEN_NOT_A_SECRET",
        webhook_rate_limit_per_minute=30,
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


@pytest.fixture
def dashboard_headers() -> dict[str, str]:
    return {"X-Dashboard-Token": "TEST_ONLY_DASHBOARD_TOKEN_NOT_A_SECRET"}


@pytest.fixture
def webhook_url() -> str:
    return "/api/webhooks/tradingview/TEST_ONLY_WEBHOOK_TOKEN_NOT_A_SECRET"


def valid_alert(
    *,
    event_id: str = "test-event-001",
    symbol: str = "BTCUSDT.P",
    bar_time: str = "2026-10-15T10:15:00Z",
) -> dict[str, str]:
    return {
        "strategy": "SHASHA",
        "event_id": event_id,
        "symbol": symbol,
        "direction": "LONG",
        "timeframe": "15",
        "bar_time": bar_time,
        "signal_close": "102.75",
        "entry_price": "101.50",
    }
