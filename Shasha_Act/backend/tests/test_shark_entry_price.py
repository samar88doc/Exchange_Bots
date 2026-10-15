from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db import Base, make_engine
from app.domain import compute_long_levels, split_quantity
from app.market_data import MarketDataService, TickerSnapshot
from app.models import Order, RiskControl, SignalEvent, Trade
from app.services.execution import TradeExecutor, _normalize_shark_interval
from app.services.paper import PaperExecutionAdapter
from app.shark import SharkClient


def test_normalize_shark_interval() -> None:
    assert _normalize_shark_interval("15") == "15m"
    assert _normalize_shark_interval("15m") == "15m"
    assert _normalize_shark_interval("1h") == "1h"
    assert _normalize_shark_interval("60") == "1h"
    assert _normalize_shark_interval("4h") == "4h"
    assert _normalize_shark_interval("1d") == "1d"
    assert _normalize_shark_interval(None) == "15m"


@pytest.mark.asyncio
async def test_resolve_entry_price_from_shark_15m_candle_close(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        entry_price_source="SHARK_15M_CLOSE",
    )
    shark = MagicMock(spec=SharkClient)
    # Shark returns completed 15m candle close as 80515.2
    shark.klines = AsyncMock(
        return_value=[
            {"startTime": "1792069200000", "close": "80515.2"},
            {"startTime": "1792070100000", "close": "80540.0"},
        ]
    )
    market = MagicMock(spec=MarketDataService)
    market.tickers = {}
    paper = PaperExecutionAdapter(settings)
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'test1.sqlite3'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    executor = TradeExecutor(settings, sessions, market, shark, paper)

    # 1. Tests successful Shark 15m candle close resolution
    price, source = await executor._resolve_entry_price("BTCUSDT", "15", Decimal("80500.0"))
    assert price == Decimal("80515.2")
    assert "Shark 15m completed candle close" in source
    shark.klines.assert_awaited_once_with("BTCUSDT", interval="15m", limit=2)

    # 2. Tests fallback to Shark live ticker last price if klines fails
    shark.klines = AsyncMock(side_effect=Exception("API timeout"))
    market.tickers["BTCUSDT"] = TickerSnapshot(
        symbol="BTCUSDT",
        last_price=Decimal("80520.5"),
        change_24h_percent=Decimal("1.5"),
        volume_24h=Decimal("100"),
        high_24h=Decimal("81000"),
        low_24h=Decimal("80000"),
        source="shark_ws",
        received_at=datetime.now(UTC),
    )
    price_live, source_live = await executor._resolve_entry_price("BTCUSDT", "15", Decimal("80500.0"))
    assert price_live == Decimal("80520.5")
    assert "Shark live ticker last price" in source_live

    # 3. Tests fallback to TradingView signal price if both fail
    market.tickers.clear()
    price_fb, source_fb = await executor._resolve_entry_price("BTCUSDT", "15", Decimal("80500.0"))
    assert price_fb == Decimal("80500.0")
    assert "TradingView signal close fallback" in source_fb


@pytest.mark.asyncio
async def test_all_orders_entry_sl_targets_are_limit(tmp_path) -> None:
    database_path = tmp_path / "limit_orders.sqlite3"
    engine = make_engine(f"sqlite+aiosqlite:///{database_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(
        _env_file=None,
        environment="test",
        entry_price_source="SHARK_15M_CLOSE",
    )
    paper = PaperExecutionAdapter(settings)

    async with sessions() as session:
        trade = Trade(
            id="trade-limit-test",
            event_id="event-1",
            mode="PAPER",
            tradingview_symbol="BTCUSDT.P",
            symbol="BTCUSDT",
            direction="LONG",
            status="QUEUED",
            planned_entry=Decimal("80500.0"),
            signal_close=Decimal("80500.0"),
            actual_entry=Decimal("80515.0"),
            stop_price=Decimal("78900.0"),
            target1_price=Decimal("81700.0"),
            target2_price=Decimal("82500.0"),
            target3_price=Decimal("83700.0"),
            target1_quantity=Decimal("0.001"),
            target2_quantity=Decimal("0.001"),
            target3_quantity=Decimal("0.001"),
            quantity=Decimal("0.003"),
            remaining_quantity=Decimal("0.003"),
            margin=Decimal("100"),
            notional=Decimal("1000"),
            leverage=10,
        )
        session.add(trade)
        await session.commit()

        # Create entry order via paper adapter
        entry_order = await paper.create_limit_entry(session, trade)
        assert entry_order.order_type == "LIMIT"

        # Ensure protective stop and targets via paper adapter
        now = datetime.now(UTC)
        await paper._ensure_paper_protection_orders(session, trade, now)
        await session.commit()

        orders = (await session.scalars(select(Order).where(Order.trade_id == trade.id))).all()
        assert len(orders) == 5  # ENTRY, STOP_LOSS, TARGET_1, TARGET_2, TARGET_3
        for order in orders:
            assert order.order_type == "LIMIT", f"Order {order.kind} has type {order.order_type}, expected LIMIT"


@pytest.mark.asyncio
async def test_process_signal_end_to_end_uses_shark_15m_close(tmp_path) -> None:
    database_path = tmp_path / "e2e_exec.sqlite3"
    engine = make_engine(f"sqlite+aiosqlite:///{database_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(
        _env_file=None,
        environment="test",
        entry_price_source="SHARK_15M_CLOSE",
    )
    shark = MagicMock(spec=SharkClient)
    # Shark returns completed 15m candle close as 80515.2 (different from TradingView 80000.0)
    shark.klines = AsyncMock(
        return_value=[
            {"startTime": "1792069200000", "close": "80515.2"},
            {"startTime": "1792070100000", "close": "80540.0"},
        ]
    )
    market = MagicMock(spec=MarketDataService)
    market.tickers = {}
    from app.symbols import SymbolResolver, parse_exchange_info
    resolver = SymbolResolver()
    resolver.replace(
        parse_exchange_info({
            "data": {
                "contracts": [
                    {
                        "name": "BTCUSDT",
                        "baseAsset": "BTC",
                        "quoteAsset": "USDT",
                        "contractType": "PERPETUAL",
                        "marginAssetsSupported": ["USDT"],
                        "orderTypes": ["LIMIT", "MARKET"],
                        "pricePrecision": 2,
                        "quantityPrecision": 3,
                        "maxLeverage": 100,
                        "filters": [
                            {"filterType": "LIMIT_QTY_SIZE", "minQty": "0.001"},
                            {"filterType": "MIN_NOTIONAL", "notional": "5"},
                        ],
                    }
                ]
            }
        })
    )
    market.resolver = resolver
    paper = PaperExecutionAdapter(settings)
    executor = TradeExecutor(settings, sessions, market, shark, paper)

    async with sessions() as session:
        control = RiskControl(
            id=1,
            trading_mode="PAPER",
            margin_per_trade=Decimal("100"),
            leverage=10,
            max_open_positions=5,
            target1_percent=Decimal("1"),
            target2_percent=Decimal("2"),
            target3_percent=Decimal("3"),
            stop_loss_percent=Decimal("2"),
            max_notional_per_trade=Decimal("2000"),
            max_daily_loss=Decimal("500"),
            limit_entry_expiry_seconds=900,
        )
        session.add(control)

        event = SignalEvent(
            id="event-e2e-1",
            idempotency_key="e2e-key-1",
            strategy="SHASHA",
            tradingview_symbol="BTCUSDT.P",
            symbol="BTCUSDT",
            direction="LONG",
            timeframe="15",
            bar_time=datetime.now(UTC),
            signal_close=Decimal("80000.0"),  # TradingView Binance price
            entry_price=Decimal("80000.0"),
            raw_payload={},
            raw_body_hash="b" * 64,
            status="QUEUED",
            status_reason="",
            mode="PAPER",
        )
        session.add(event)

        trade = Trade(
            id="trade-e2e-1",
            event_id=event.id,
            mode="PAPER",
            tradingview_symbol="BTCUSDT.P",
            symbol="BTCUSDT",
            direction="LONG",
            status="QUEUED",
            planned_entry=Decimal("80000.0"),
            signal_close=Decimal("80000.0"),
            margin=Decimal("100"),
            notional=Decimal("1000"),
            leverage=10,
        )
        session.add(trade)
        await session.commit()

    # Process signal through TradeExecutor
    await executor.process_signal("event-e2e-1")

    async with sessions() as session:
        updated_trade = await session.get(Trade, "trade-e2e-1")
        assert updated_trade is not None
        assert abs(updated_trade.planned_entry - Decimal("80515.2")) < Decimal("0.001")
        # Status must be ENTRY_PENDING
        assert updated_trade.status == "ENTRY_PENDING"

        # Check entry order
        entry_order = await session.scalar(
            select(Order).where(Order.trade_id == updated_trade.id, Order.kind == "ENTRY")
        )
        assert entry_order is not None
        assert entry_order.order_type == "LIMIT"
        assert entry_order.price is not None
        assert abs(entry_order.price - Decimal("80515.2")) < Decimal("0.001")

