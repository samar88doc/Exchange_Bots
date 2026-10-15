"""SHASHA strategy behaviour: Shark closed-candle entry, 2% stop, 50% target 1, 1% trailing."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db import Base, make_engine
from app.domain import (
    ValidationProblem,
    compute_strategy_levels,
    last_closed_candle,
    next_trailing_stop,
    normalize_interval,
    parse_candles,
    quantize_price,
    split_partial_quantity,
)
from app.market_data import TickerSnapshot
from app.models import Order, SignalEvent, Trade
from app.services.paper import PaperExecutionAdapter


def _ticker(symbol: str, price: str, *, seconds_ago: int = 0) -> TickerSnapshot:
    return TickerSnapshot(
        symbol=symbol,
        last_price=Decimal(price),
        change_24h_percent=Decimal("1"),
        volume_24h=Decimal("100"),
        high_24h=Decimal(price),
        low_24h=Decimal(price),
        source="SHARK_WS_LAST",
        received_at=datetime.now(UTC) - timedelta(seconds=seconds_ago),
    )


def test_entry_price_is_the_last_completed_candle_regardless_of_response_order() -> None:
    now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    # older: closed 9 min ago · newest_closed: closed 4 min ago · still_open: closes in 1 min
    base = int((now - timedelta(minutes=14)).timestamp() * 1000)
    # Shark's documented response ordering is not guaranteed: newest-first is supplied here.
    newest_closed = {
        "startTime": str(base + 300_000),
        "open": "0.00231",
        "high": "0.00244",
        "low": "0.00229",
        "close": "0.00239",
        "endTime": str(base + 600_000),
    }
    still_open = {
        "startTime": str(base + 600_000),
        "open": "0.00239",
        "high": "0.00250",
        "low": "0.00238",
        "close": "0.00248",
        "endTime": str(base + 900_000),
    }
    older = {
        "startTime": str(base),
        "open": "0.00230",
        "high": "0.00235",
        "low": "0.00228",
        "close": "0.00231",
        "endTime": str(base + 300_000),
    }
    for payload in (
        [newest_closed, older, still_open],
        [older, newest_closed, still_open],
        {"data": [still_open, newest_closed, older]},
        [
            {
                "k": {
                    "t": base + 300_000,
                    "o": "0.00231",
                    "h": "0.00244",
                    "l": "0.00229",
                    "c": "0.00239",
                    "T": base + 600_000,
                }
            },
            older,
        ],
        # Binance-compatible positional form, still supported by the parser.
        [[base + 300_000, "0.00231", "0.00244", "0.00229", "0.00239", "10", base + 600_000], older],
    ):
        candles = parse_candles(payload, "5m")
        closed = last_closed_candle(candles, now=now, max_age_seconds=1800)
        assert closed is not None, payload
        assert closed.close == Decimal("0.00239")
    # A candle that is still forming is never used, and a stale closed candle is refused.
    assert last_closed_candle(parse_candles([still_open], "5m"), now=now) is None
    assert last_closed_candle(parse_candles([older, newest_closed], "5m"), now=now, max_age_seconds=60) is None


def test_quantize_price_keeps_exchange_decimal_fraction() -> None:
    assert quantize_price(Decimal("0.002386666"), 5) == Decimal("0.00238")
    assert str(quantize_price(Decimal("0.00239"), 5)) == "0.00239"
    assert quantize_price(Decimal("80515.2399"), 2) == Decimal("80515.23")


def test_strategy_levels_and_partial_split_match_the_documented_plan() -> None:
    levels = compute_strategy_levels(
        Decimal("0.00239"), Decimal("0.00001"), Decimal("2"), Decimal("2")
    )
    assert levels.stop == Decimal("0.00235")
    assert levels.target1 == Decimal("0.00244")
    assert levels.target2 is None and levels.target3 is None

    with_secondary = compute_strategy_levels(
        Decimal("100"), Decimal("0.01"), Decimal("2"), Decimal("2"), Decimal("3"), Decimal("4")
    )
    assert (with_secondary.stop, with_secondary.target1, with_secondary.target2, with_secondary.target3) == (
        Decimal("98"),
        Decimal("102"),
        Decimal("103"),
        Decimal("104"),
    )

    split = split_partial_quantity(Decimal("100"), Decimal("1"), Decimal("1"), Decimal("50"))
    assert split.partial == Decimal("50")
    assert split.runner == Decimal("50")
    assert split.total == Decimal("100")

    with pytest.raises(ValidationProblem):
        split_partial_quantity(Decimal("1"), Decimal("1"), Decimal("1"), Decimal("50"))


def test_next_trailing_stop_only_moves_up_and_never_below_the_floor() -> None:
    first = next_trailing_stop(
        high_water=Decimal("102"),
        current_stop=Decimal("100"),
        floor_price=Decimal("100"),
        trailing_percent=Decimal("1"),
        price_tick=Decimal("0.01"),
        reference_price=Decimal("102"),
    )
    assert first == Decimal("100.98")
    # Every +1% on the high water lifts the stop by 1% and can never move down.
    second = next_trailing_stop(
        high_water=Decimal("103.02"),
        current_stop=first,
        floor_price=Decimal("100"),
        trailing_percent=Decimal("1"),
        price_tick=Decimal("0.01"),
        reference_price=Decimal("103.02"),
    )
    assert second == Decimal("101.98")
    unchanged = next_trailing_stop(
        high_water=Decimal("102"),
        current_stop=second,
        floor_price=Decimal("100"),
        trailing_percent=Decimal("1"),
        price_tick=Decimal("0.01"),
        reference_price=Decimal("102"),
    )
    assert unchanged == second
    floored = next_trailing_stop(
        high_water=Decimal("100.2"),
        current_stop=None,
        floor_price=Decimal("100"),
        trailing_percent=Decimal("1"),
        price_tick=Decimal("0.01"),
        reference_price=Decimal("100.2"),
    )
    assert floored == Decimal("100")


def test_interval_normalization_covers_tradingview_spellings() -> None:
    assert normalize_interval("5") == "5m"
    assert normalize_interval("15") == "15m"
    assert normalize_interval("60") == "1h"
    assert normalize_interval("4H") == "4h"
    assert normalize_interval("D") == "1d"
    assert normalize_interval("") == "5m"


async def _paper_trade(session, *, entry: str = "0.00239", quantity: str = "100") -> tuple[Trade, SignalEvent]:
    event = SignalEvent(
        id="event-trailing-1",
        idempotency_key="trailing-key-1",
        supplied_event_id="trailing-event-1",
        strategy="SHASHA",
        tradingview_symbol="PEPEUSDT.P",
        symbol="PEPEUSDT",
        direction="LONG",
        timeframe="5",
        bar_time=datetime.now(UTC),
        signal_close=Decimal("0.00245"),
        entry_price=Decimal(entry),
        raw_payload={},
        raw_body_hash="c" * 64,
        status="OPEN",
        status_reason="",
        mode="PAPER",
    )
    initial_stop = (Decimal(entry) * Decimal("0.98")).quantize(Decimal("0.00001"))
    target1 = (Decimal(entry) * Decimal("1.02")).quantize(Decimal("0.00001"))
    trade = Trade(
        id="trade-trailing-1",
        event_id=event.id,
        mode="PAPER",
        tradingview_symbol="PEPEUSDT.P",
        symbol="PEPEUSDT",
        direction="LONG",
        status="OPEN",
        status_reason="test",
        planned_entry=Decimal(entry),
        signal_close=Decimal("0.00245"),
        actual_entry=Decimal(entry),
        quantity=Decimal(quantity),
        remaining_quantity=Decimal(quantity),
        target1_quantity=Decimal("50"),
        target2_quantity=None,
        target3_quantity=None,
        margin=Decimal("10"),
        notional=Decimal("100"),
        leverage=10,
        stop_price=initial_stop,
        initial_stop_price=initial_stop,
        target1_price=target1,
        target2_price=None,
        target3_price=None,
        price_tick=Decimal("0.00001"),
        trailing_enabled=True,
        secondary_targets_enabled=False,
        trailing_stop_percent=Decimal("1"),
        partial_close_percent=Decimal("50"),
        protection_state="SIMULATED_ACTIVE",
    )
    session.add(event)
    session.add(trade)
    await session.commit()
    return trade, event


@pytest.mark.asyncio
async def test_paper_strategy_closes_half_at_target_one_then_trails(tmp_path) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'trailing.sqlite3'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(_env_file=None, environment="test")
    adapter = PaperExecutionAdapter(settings)

    async with sessions() as session:
        trade, event = await _paper_trade(session)
        await adapter._ensure_paper_protection_orders(session, trade, datetime.now(UTC))
        await session.commit()

    # Target 1 at +2% closes exactly half and moves the stop to the entry (break-even).
    async with sessions() as session:
        trade = await session.get(Trade, "trade-trailing-1")
        event = await session.get(SignalEvent, "event-trailing-1")
        await adapter.process_ticker(session, trade, _ticker("PEPEUSDT", "0.00244"), event)
        await session.commit()
    async with sessions() as session:
        trade = await session.get(Trade, "trade-trailing-1")
        assert trade.partial_closed is True
        assert trade.remaining_quantity == Decimal("50")
        # Break-even is the floor; the +2% high already trails 1% above it, never below entry.
        assert trade.stop_price >= trade.actual_entry
        assert trade.stop_stage == "TRAILING"
        assert trade.trailing_active is True
        assert trade.status == "PARTIALLY_CLOSED"

    # A further +1% high lifts the protective stop by 1% of the high water.
    async with sessions() as session:
        trade = await session.get(Trade, "trade-trailing-1")
        await adapter.process_ticker(
            session, trade, _ticker("PEPEUSDT", "0.00247"), None
        )
        await session.commit()
    async with sessions() as session:
        trade = await session.get(Trade, "trade-trailing-1")
        assert trade.stop_stage == "TRAILING"
        # high water 0.00247 - 1% = 0.00244 (rounded down to the 0.00001 tick)
        assert trade.stop_price == Decimal("0.00244")
        stop_order = await session.scalar(
            select(Order).where(Order.client_order_id == trade.protective_stop_order_id)
        )
        assert stop_order is not None
        assert stop_order.price == Decimal("0.00244")
        assert stop_order.quantity == Decimal("50")

    # Price falling back to the trailed stop closes the runner at the stop price.
    async with sessions() as session:
        trade = await session.get(Trade, "trade-trailing-1")
        await adapter.process_ticker(session, trade, _ticker("PEPEUSDT", "0.00244"), None)
        await session.commit()
    async with sessions() as session:
        trade = await session.get(Trade, "trade-trailing-1")
        assert trade.status == "CLOSED"
        assert trade.remaining_quantity == Decimal("0")
        assert trade.exit_price == Decimal("0.00244")
        assert trade.gross_realized_pnl > 0
        assert "TRAILING" in trade.status_reason


@pytest.mark.asyncio
async def test_paper_stop_before_target_one_closes_the_full_position(tmp_path) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'stop.sqlite3'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    adapter = PaperExecutionAdapter(Settings(_env_file=None, environment="test"))
    async with sessions() as session:
        trade, _ = await _paper_trade(session, entry="100", quantity="3")
        trade.price_tick = Decimal("0.01")
        trade.stop_price = Decimal("98.00")
        trade.initial_stop_price = Decimal("98.00")
        trade.target1_price = Decimal("102.00")
        trade.target1_quantity = Decimal("2")
        await adapter._ensure_paper_protection_orders(session, trade, datetime.now(UTC))
        await session.commit()
    async with sessions() as session:
        trade = await session.get(Trade, "trade-trailing-1")
        await adapter.process_ticker(session, trade, _ticker("PEPEUSDT", "97.90"), None)
        await session.commit()
    async with sessions() as session:
        trade = await session.get(Trade, "trade-trailing-1")
        assert trade.status == "CLOSED"
        assert trade.remaining_quantity == Decimal("0")
        assert trade.exit_price == Decimal("98.00")
        assert trade.partial_closed is False
        assert trade.gross_realized_pnl < 0
        assert "SIMULATED_STOP" in trade.status_reason


@pytest.mark.asyncio
async def test_risk_control_defaults_express_the_requested_strategy(tmp_path) -> None:
    from app.services.risk import ensure_risk_control

    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'defaults.sqlite3'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(_env_file=None, environment="test")
    async with sessions() as session:
        control = await ensure_risk_control(session, settings)
        await session.commit()
        assert control.stop_loss_percent == Decimal("2")
        assert control.target1_percent == Decimal("2")
        assert control.target1_close_percent == Decimal("50")
        assert control.trailing_stop_percent == Decimal("1")
        assert control.trailing_enabled is True
        assert control.enable_secondary_targets is False
        assert control.entry_candle_interval == "5m"
