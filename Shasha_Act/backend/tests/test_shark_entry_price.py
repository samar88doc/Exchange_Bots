"""Entry price, sizing and paper protection behaviour end to end."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db import Base, make_engine
from app.market_data import MarketDataService, TickerSnapshot
from app.models import Order, RiskControl, SignalEvent, Trade
from app.services.execution import EntryPriceUnavailable, TradeExecutor, _normalize_shark_interval
from app.services.paper import PaperExecutionAdapter
from app.shark import SharkClient


def _approx(actual: Decimal | None, expected: str, tolerance: str = "0.0001") -> bool:
    """SQLite returns NUMERIC columns through a float, so compare with a tight tolerance."""
    return actual is not None and abs(actual - Decimal(expected)) < Decimal(tolerance)


def _klines(*closes: tuple[str, int]) -> list[dict[str, str]]:
    """Build a kline response; each tuple is (close, minutes_ago_when_the_candle_closed)."""
    now = datetime.now(UTC)
    rows = []
    for close, minutes_ago in closes:
        end = now - timedelta(minutes=minutes_ago)
        start = end - timedelta(minutes=5)
        rows.append(
            {
                "startTime": str(int(start.timestamp() * 1000)),
                "endTime": str(int(end.timestamp() * 1000)),
                "open": close,
                "high": close,
                "low": close,
                "close": close,
            }
        )
    return rows


def test_normalize_shark_interval() -> None:
    assert _normalize_shark_interval("15") == "15m"
    assert _normalize_shark_interval("15m") == "15m"
    assert _normalize_shark_interval("1h") == "1h"
    assert _normalize_shark_interval("60") == "1h"
    assert _normalize_shark_interval("4h") == "4h"
    assert _normalize_shark_interval("1d") == "1d"
    assert _normalize_shark_interval(None) == "15m"


def _executor(tmp_path, settings, shark, market):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'entry.sqlite3'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return TradeExecutor(settings, sessions, market, shark, PaperExecutionAdapter(settings))


@pytest.mark.asyncio
async def test_entry_price_uses_last_closed_five_minute_shark_candle(tmp_path) -> None:
    settings = Settings(_env_file=None, environment="test", entry_candle_interval="5m")
    shark = MagicMock(spec=SharkClient)
    # Newest candle is still forming; the entry must come from the last *completed* candle.
    shark.klines = AsyncMock(
        return_value={
            "data": [
                {"startTime": "0", "close": "0.00250", "endTime": "0"},
                *_klines(("0.00231", 30), ("0.00239", 5), ("0.00248", -3)),
            ]
        }
    )
    market = MagicMock(spec=MarketDataService)
    market.tickers = {}
    executor = _executor(tmp_path, settings, shark, market)

    price, source, candle = await executor._resolve_entry_price(
        "PEPEUSDT", "15", Decimal("0.00246")
    )
    assert price == Decimal("0.00239")
    assert "closed-candle close" in source
    assert candle is not None and candle.close == Decimal("0.00239")
    shark.klines.assert_awaited_with("PEPEUSDT", interval="5m", limit=12)

    # SIGNAL_PAYLOAD mode is explicit opt-in and never used silently.
    payload_mode = Settings(_env_file=None, environment="test", entry_price_source="SIGNAL_PAYLOAD")
    payload_executor = _executor(tmp_path, payload_mode, shark, market)
    fallback_price, fallback_source, fallback_candle = await payload_executor._resolve_entry_price(
        "PEPEUSDT", "15", Decimal("0.00246")
    )
    assert fallback_price == Decimal("0.00246")
    assert "TradingView" in fallback_source
    assert fallback_candle is None


@pytest.mark.asyncio
async def test_entry_price_is_strictly_the_shark_closed_candle_by_default(tmp_path) -> None:
    """The documented entry price is the Shark candle close, never a live tick by default."""
    settings = Settings(_env_file=None, environment="test", entry_candle_interval="5m")
    assert settings.entry_allow_live_ticker_fallback is False
    shark = MagicMock(spec=SharkClient)
    shark.klines = AsyncMock(side_effect=Exception("API timeout"))
    market = MagicMock(spec=MarketDataService)
    market.tickers = {
        "PEPEUSDT": TickerSnapshot(
            symbol="PEPEUSDT",
            last_price=Decimal("0.00246"),
            change_24h_percent=Decimal("1.5"),
            volume_24h=Decimal("100"),
            high_24h=Decimal("0.0025"),
            low_24h=Decimal("0.0023"),
            source="SHARK_WS_LAST",
            received_at=datetime.now(UTC),
        )
    }
    executor = _executor(tmp_path, settings, shark, market)
    # Even a completely fresh Shark tick is refused while the strict default is in force: the
    # signal is deferred (and retried) instead of entering at a non-candle price.
    with pytest.raises(EntryPriceUnavailable):
        await executor._resolve_entry_price("PEPEUSDT", "5", Decimal("0.00240"))

    # An operator who explicitly opts in still gets the fallback, and the provenance text
    # makes the deviation from the documented entry rule visible in the dashboard.
    permissive = Settings(
        _env_file=None,
        environment="test",
        entry_candle_interval="5m",
        entry_allow_live_ticker_fallback=True,
    )
    fallback_executor = _executor(tmp_path, permissive, shark, market)
    price, source, candle = await fallback_executor._resolve_entry_price(
        "PEPEUSDT", "5", Decimal("0.00240")
    )
    assert price == Decimal("0.00246")
    assert "Shark live last price" in source
    assert candle is None

    # A stale ticker is still not a price, with or without the fallback enabled.
    market.tickers["PEPEUSDT"] = TickerSnapshot(
        symbol="PEPEUSDT",
        last_price=Decimal("0.00246"),
        change_24h_percent=None,
        volume_24h=None,
        high_24h=None,
        low_24h=None,
        source="SHARK_WS_LAST",
        received_at=datetime.now(UTC) - timedelta(minutes=30),
    )
    with pytest.raises(EntryPriceUnavailable):
        await fallback_executor._resolve_entry_price("PEPEUSDT", "5", Decimal("0.00240"))


@pytest.mark.asyncio
async def test_live_fill_flow_is_unchanged_by_the_strategy_rework() -> None:
    """The LIVE path still fails closed on unverified Shark contract metadata."""
    from app.symbols import ContractSpec

    spec = ContractSpec(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        contract_type="PERPETUAL",
        margin_assets=("USDT",),
        order_types=("LIMIT", "MARKET"),
        minimum_quantity=Decimal("0.001"),
        minimum_notional=Decimal("5"),
        quantity_precision=3,
        price_precision=2,
        maximum_leverage=100,
    )
    assert spec.live_safe is False  # publish/exchange tick sizes are still unverified


@pytest.mark.asyncio
async def test_process_signal_uses_shark_close_and_supervises_paper_trade(tmp_path) -> None:
    database_path = tmp_path / "e2e_exec.sqlite3"
    engine = make_engine(f"sqlite+aiosqlite:///{database_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(_env_file=None, environment="test", entry_candle_interval="5m")
    shark = MagicMock(spec=SharkClient)
    shark.klines = AsyncMock(return_value=_klines(("80000.0", 20), ("80515.2", 5)))
    market = MagicMock(spec=MarketDataService)
    market.tickers = {}
    from app.symbols import SymbolResolver, parse_exchange_info

    resolver = SymbolResolver()
    resolver.replace(
        parse_exchange_info(
            {
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
            }
        )
    )
    market.resolver = resolver
    paper = PaperExecutionAdapter(settings)
    executor = TradeExecutor(settings, sessions, market, shark, paper)

    async with sessions() as session:
        session.add(
            RiskControl(
                id=1,
                trading_mode="PAPER",
                margin_per_trade=Decimal("100"),
                leverage=10,
                max_open_positions=5,
                target1_percent=Decimal("2"),
                target2_percent=Decimal("3"),
                target3_percent=Decimal("4"),
                stop_loss_percent=Decimal("2"),
                target1_close_percent=Decimal("50"),
                trailing_stop_percent=Decimal("1"),
                trailing_enabled=True,
                enable_secondary_targets=False,
                entry_candle_interval="5m",
                max_notional_per_trade=Decimal("2000"),
                max_daily_loss=Decimal("500"),
                limit_entry_expiry_seconds=900,
            )
        )
        event = SignalEvent(
            id="event-e2e-1",
            idempotency_key="e2e-key-1",
            strategy="SHASHA",
            tradingview_symbol="BTCUSDT.P",
            symbol="BTCUSDT",
            direction="LONG",
            timeframe="15",
            bar_time=datetime.now(UTC),
            signal_close=Decimal("80000.0"),
            entry_price=Decimal("80000.0"),
            raw_payload={},
            raw_body_hash="b" * 64,
            status="QUEUED",
            status_reason="",
            mode="PAPER",
        )
        session.add(event)
        session.add(
            Trade(
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
        )
        await session.commit()

    await executor.process_signal("event-e2e-1")

    async with sessions() as session:
        trade = await session.get(Trade, "trade-e2e-1")
        assert trade is not None
        # Entry is the last completed 5m Shark candle (80515.2), not the TradingView price.
        assert _approx(trade.planned_entry, "80515.2")
        assert trade.entry_price_source.startswith("Shark 5m closed-candle close")
        assert trade.entry_candle_time is not None
        assert trade.status == "ENTRY_PENDING"
        # Stop 2% below entry, Target 1 2% above entry.
        assert _approx(trade.stop_price, "78904.90")
        assert _approx(trade.target1_price, "82125.51")
        assert trade.target2_price is None and trade.target3_price is None
        assert trade.partial_close_percent == Decimal("50")
        assert trade.trailing_stop_percent == Decimal("1")
        assert trade.trailing_enabled is True
        # 1000 USDT notional ÷ 80515.2 with a 0.001 step → 50% partial and a 50% runner.
        assert _approx(trade.quantity, "0.012", "0.000001")
        assert _approx(trade.target1_quantity, "0.006", "0.000001")
        assert _approx(trade.remaining_quantity, "0.012", "0.000001")

        orders = list((await session.scalars(select(Order).where(Order.trade_id == trade.id))).all())
        assert sorted(order.kind for order in orders) == ["ENTRY"]
        entry_order = orders[0]
        assert entry_order.order_type == "LIMIT"
        assert _approx(entry_order.price, "80515.2")

    # The simulated limit fill then builds the protective plan: stop 2% below entry and
    # Target 1 (50% of the position) 2% above entry, with the runner left to trail.
    ticker = TickerSnapshot(
        symbol="BTCUSDT",
        last_price=Decimal("80515.2"),
        change_24h_percent=None,
        volume_24h=None,
        high_24h=None,
        low_24h=None,
        source="SHARK_WS_LAST",
        received_at=datetime.now(UTC),
    )
    async with sessions() as session:
        trade = await session.get(Trade, "trade-e2e-1")
        event = await session.get(SignalEvent, "event-e2e-1")
        await paper.process_ticker(session, trade, ticker, event)
        await session.commit()

    async with sessions() as session:
        trade = await session.get(Trade, "trade-e2e-1")
        assert trade.status == "OPEN"
        assert _approx(trade.actual_entry, "80515.2")
        orders = list((await session.scalars(select(Order).where(Order.trade_id == trade.id))).all())
        kinds = sorted(order.kind for order in orders)
        assert kinds == ["ENTRY", "STOP_LOSS", "TARGET_1"]
        stop_order = next(order for order in orders if order.kind == "STOP_LOSS")
        assert _approx(stop_order.price, "78904.90")
        assert stop_order.quantity == trade.remaining_quantity
        target_order = next(order for order in orders if order.kind == "TARGET_1")
        assert _approx(target_order.price, "82125.51")
        assert _approx(target_order.quantity, "0.006", "0.000001")



async def test_shark_sell_side_maps_to_short_entry_and_buy_exit_for_shorts() -> None:
    """A SHORT opens with SELL and closes reduce-only with BUY; LONG keeps BUY open / SELL close."""
    from app.config import Settings as _Settings

    sent: list[dict] = []

    async def _fake_request(self, method, path, *, body=None, **_kwargs):
        sent.append(dict(body or {}))
        return {"ok": True}

    settings = _Settings()
    shark = SharkClient(settings, MagicMock(), clock_ms=lambda: 123)
    shark._request = _fake_request.__get__(shark)  # type: ignore[method-assign]

    await shark.place_limit_order("BTCUSDT", Decimal("1"), Decimal("100"), side="SELL")
    await shark.place_reduce_only_market("BTCUSDT", Decimal("1"), side="BUY")
    await shark.place_limit_buy("BTCUSDT", Decimal("1"), Decimal("100"))
    await shark.place_reduce_only_market_sell("BTCUSDT", Decimal("1"))

    assert [(b["type"], b["side"], b["reduceOnly"]) for b in sent] == [
        ("LIMIT", "SELL", False),
        ("MARKET", "BUY", True),
        ("LIMIT", "BUY", False),
        ("MARKET", "SELL", True),
    ]
    with pytest.raises(ValueError):
        await shark.place_limit_order("BTCUSDT", Decimal("1"), Decimal("100"), side="HOLD")
