from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db import Base, make_engine
from app.domain import compute_long_levels, split_quantity
from app.market_data import TickerSnapshot
from app.models import Fill, Order, SignalEvent, Trade
from app.services.paper import PaperExecutionAdapter


@pytest.mark.asyncio
async def test_paper_limit_and_target_fills_conserve_quantity_and_never_need_private_client(tmp_path) -> None:
    database_path = tmp_path / "paper.sqlite3"
    engine = make_engine(f"sqlite+aiosqlite:///{database_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(_env_file=None, environment="test", run_workers=False, run_market_stream=False, run_market_poll=False)
    adapter = PaperExecutionAdapter(settings)

    async with sessions() as session:
        event = SignalEvent(
            id="event-paper-1",
            idempotency_key="paper-key-1",
            supplied_event_id="paper-event-1",
            strategy="SHASHA",
            tradingview_symbol="BTCUSDT.P",
            symbol="BTCUSDT",
            direction="LONG",
            timeframe="15",
            bar_time=datetime.now(UTC),
            signal_close=Decimal("102"),
            entry_price=Decimal("100"),
            raw_payload={},
            raw_body_hash="a" * 64,
            status="ENTRY_PENDING",
            status_reason="",
            mode="PAPER",
        )
        levels = compute_long_levels(
            Decimal("100"), Decimal("1"), Decimal("2"), Decimal("1"), Decimal("2"), Decimal("3")
        )
        split = split_quantity(Decimal("3"), Decimal("1"), Decimal("1"))
        trade = Trade(
            id="trade-paper-1",
            event_id=event.id,
            mode="PAPER",
            tradingview_symbol="BTCUSDT.P",
            symbol="BTCUSDT",
            direction="LONG",
            status="ENTRY_PENDING",
            status_reason="",
            planned_entry=Decimal("100"),
            signal_close=Decimal("102"),
            quantity=Decimal("3"),
            remaining_quantity=Decimal("3"),
            target1_quantity=split.target1,
            target2_quantity=split.target2,
            target3_quantity=split.target3,
            margin=Decimal("30"),
            notional=Decimal("90"),
            leverage=3,
            stop_price=levels.stop,
            initial_stop_price=levels.stop,
            target1_price=levels.target1,
            target2_price=levels.target2,
            target3_price=levels.target3,
            # Optional legacy mode: fixed Target 2/3 exits on top of Target 1.
            secondary_targets_enabled=True,
            trailing_enabled=False,
            price_tick=Decimal("1"),
            entry_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        session.add_all([event, trade])
        await session.flush()
        await adapter.create_limit_entry(session, trade)
        await session.commit()

    now = datetime.now(UTC)
    entry_ticker = TickerSnapshot(
        symbol="BTCUSDT",
        last_price=Decimal("99"),
        change_24h_percent=None,
        volume_24h=None,
        high_24h=None,
        low_24h=None,
        source="SHARK_WS_LAST",
        received_at=now,
    )
    async with sessions() as session:
        trade = await session.get(Trade, "trade-paper-1")
        event = await session.get(SignalEvent, "event-paper-1")
        assert trade is not None
        await adapter.process_ticker(session, trade, entry_ticker, event)
        await session.commit()
        assert trade.status == "OPEN"
        assert trade.actual_entry == Decimal("100")
        assert trade.signal_close == Decimal("102")
        assert trade.protection_state == "SIMULATED_ACTIVE"

    target_ticker = TickerSnapshot(
        symbol="BTCUSDT",
        last_price=Decimal("103"),
        change_24h_percent=None,
        volume_24h=None,
        high_24h=None,
        low_24h=None,
        source="SHARK_WS_LAST",
        received_at=now + timedelta(seconds=1),
    )
    async with sessions() as session:
        trade = await session.get(Trade, "trade-paper-1")
        event = await session.get(SignalEvent, "event-paper-1")
        assert trade is not None
        await adapter.process_ticker(session, trade, target_ticker, event)
        await session.commit()
        assert trade.status == "CLOSED"
        assert trade.remaining_quantity == Decimal("0")
        assert trade.target_progress == 3
        assert trade.net_realized_pnl > 0

    async with sessions() as session:
        fills = list((await session.scalars(select(Fill).where(Fill.trade_id == "trade-paper-1"))).all())
        entry_quantity = sum((fill.quantity for fill in fills if fill.entry_exit == "ENTRY"), Decimal("0"))
        exit_quantity = sum((fill.quantity for fill in fills if fill.entry_exit == "EXIT"), Decimal("0"))
        assert entry_quantity == Decimal("3")
        assert exit_quantity == Decimal("3")
        orders = list((await session.scalars(select(Order).where(Order.trade_id == "trade-paper-1"))).all())
        assert all(order.request_summary and order.request_summary.get("paper") for order in orders)
        assert len([order for order in orders if order.kind.startswith("TARGET_") and order.status == "FILLED"]) == 3
    await engine.dispose()


@pytest.mark.asyncio
async def test_paper_manual_close_requires_fresh_ticker_and_closes_simulated_quantity(tmp_path) -> None:
    database_path = tmp_path / "paper-manual.sqlite3"
    engine = make_engine(f"sqlite+aiosqlite:///{database_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(_env_file=None, environment="test", paper_taker_fee_bps=Decimal("5"))
    adapter = PaperExecutionAdapter(settings)
    now = datetime.now(UTC)
    async with sessions() as session:
        event = SignalEvent(
            id="event-manual",
            idempotency_key="manual-key",
            strategy="SHASHA",
            tradingview_symbol="ETHUSDT.P",
            symbol="ETHUSDT",
            direction="LONG",
            timeframe="5",
            bar_time=now,
            signal_close=Decimal("2100"),
            entry_price=Decimal("2050"),
            raw_payload={},
            raw_body_hash="b" * 64,
            status="OPEN",
            mode="PAPER",
        )
        trade = Trade(
            id="trade-manual",
            event_id=event.id,
            mode="PAPER",
            tradingview_symbol="ETHUSDT.P",
            symbol="ETHUSDT",
            direction="LONG",
            status="OPEN",
            planned_entry=Decimal("2050"),
            signal_close=Decimal("2100"),
            actual_entry=Decimal("2050"),
            quantity=Decimal("1"),
            remaining_quantity=Decimal("1"),
            target1_quantity=Decimal("1"),
            target2_quantity=Decimal("0"),
            target3_quantity=Decimal("0"),
            margin=Decimal("100"),
            notional=Decimal("1000"),
            leverage=10,
            stop_price=Decimal("2010"),
            initial_stop_price=Decimal("2010"),
            target1_price=Decimal("2070"),
            target2_price=Decimal("2090"),
            target3_price=Decimal("2110"),
            protection_state="SIMULATED_ACTIVE",
        )
        session.add_all([event, trade])
        await session.flush()
        await adapter.create_limit_entry(session, trade)
        entry_order = await session.scalar(select(Order).where(Order.trade_id == trade.id, Order.kind == "ENTRY"))
        assert entry_order is not None
        entry_order.status = "FILLED"
        entry_order.filled_quantity = Decimal("1")
        session.add(Fill(id="entry-fill", trade_id=trade.id, order_id=entry_order.id, mode="PAPER", entry_exit="ENTRY", side="BUY", price=Decimal("2050"), quantity=Decimal("1"), fee=Decimal("0"), gross_realized_pnl=Decimal("0"), fill_time=now))
        await adapter._ensure_paper_protection_orders(session, trade, now)
        await session.commit()

    ticker = TickerSnapshot("ETHUSDT", Decimal("2060"), None, None, None, None, "SHARK_REST_LAST", now)
    async with sessions() as session:
        trade = await session.get(Trade, "trade-manual")
        event = await session.get(SignalEvent, "event-manual")
        assert trade is not None
        await adapter.close_manually(session, trade, ticker, event)
        await session.commit()
        assert trade.status == "CLOSED"
        assert trade.remaining_quantity == Decimal("0")
        assert trade.exit_price == Decimal("2060")
    await engine.dispose()


@pytest.mark.asyncio
async def test_realized_pnl_breakdown_deducts_trading_fees_and_funding(tmp_path) -> None:
    from app.services.pnl import realized_pnl_breakdown_for_day, realized_pnl_for_day

    database_path = tmp_path / "pnl.sqlite3"
    engine = make_engine(f"sqlite+aiosqlite:///{database_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    zone = ZoneInfo("Asia/Kolkata")
    now = datetime.now(UTC)
    target_date = now.astimezone(zone).date()

    async with sessions() as session:
        # Create an entry fill with trading fee
        session.add(
            Fill(
                id="f1",
                trade_id="t1",
                order_id="o1",
                mode="PAPER",
                entry_exit="ENTRY",
                side="BUY",
                price=Decimal("100"),
                quantity=Decimal("1"),
                fee=Decimal("0.05"),
                gross_realized_pnl=Decimal("0"),
                funding=None,
                fill_time=now,
            )
        )
        # Create an exit fill with gross profit, trading fee, and funding fee
        session.add(
            Fill(
                id="f2",
                trade_id="t1",
                order_id="o2",
                mode="PAPER",
                entry_exit="EXIT",
                side="SELL",
                price=Decimal("110"),
                quantity=Decimal("1"),
                fee=Decimal("0.06"),
                gross_realized_pnl=Decimal("10.00"),
                funding=Decimal("0.02"),
                fill_time=now,
            )
        )
        await session.commit()

        breakdown = await realized_pnl_breakdown_for_day(session, "Asia/Kolkata", target_date, mode="PAPER")
        assert breakdown["gross"] == Decimal("10.00")
        assert breakdown["fees_paid"] == Decimal("0.11")
        assert breakdown["funding_paid"] == Decimal("0.02")
        assert breakdown["fill_count"] == 2
        assert breakdown["funding_record_count"] == 1
        # Net = Gross - Fees - Funding = 10.00 - 0.11 - 0.02 = 9.87
        assert breakdown["net"] == Decimal("9.87")

        # Also backwards-compat realized_pnl_for_day matches net
        net = await realized_pnl_for_day(session, "Asia/Kolkata", target_date, mode="PAPER")
        assert net == Decimal("9.87")
    await engine.dispose()

