"""LIVE protection assembly must fail closed.

The executor submits the protective stop, then the profit targets through the Shark split
TP/SL endpoint. Because that endpoint's replacement semantics are not verified, the stop is
re-confirmed *after* the targets are accepted: a stop that silently disappeared must raise a
protection alarm instead of the trade ever being reported as protected.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db import Base, make_engine
from app.models import Order, RiskControl, SignalEvent, Trade
from app.services.execution import TradeExecutor
from app.services.paper import PaperExecutionAdapter
from app.shark import SharkClient
from app.symbols import ContractSpec


def _spec() -> ContractSpec:
    return ContractSpec(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        contract_type="PERPETUAL",
        margin_assets=("USDT",),
        order_types=("LIMIT", "MARKET"),
        minimum_quantity=Decimal("0.001"),
        minimum_notional=Decimal("5"),
        quantity_precision=3,
        price_precision=1,
        maximum_leverage=100,
        price_tick=Decimal("0.1"),
        quantity_step=Decimal("0.001"),
    )


def _stop_order(state: str = "NEW", price: str = "78904.9", quantity: str = "0.012") -> dict:
    return {"clientOrderId": "stop-1", "status": state, "stopPrice": price, "quantity": quantity}


def _target_order(order_id: str, price: str, quantity: str) -> dict:
    return {"clientOrderId": order_id, "status": "NEW", "price": price, "quantity": quantity}


def _split_response(ids: list[str]) -> dict:
    return {
        "acceptedOrderCount": len(ids),
        "rejectedOrderCount": 0,
        "acceptedOrders": [{"clientOrderId": order_id} for order_id in ids],
    }


async def _seed(tmp_path) -> tuple[async_sessionmaker, TradeExecutor, MagicMock]:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'live.sqlite3'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(_env_file=None, environment="test")

    shark = MagicMock(spec=SharkClient)
    shark.positions = AsyncMock(
        return_value=[
            {
                "positionId": "position-77",
                "contractPair": "BTCUSDT",
                "positionType": "LONG",
                "entryPrice": "80515.2",
                "quantity": "0.012",
                "liquidationPrice": "73000",
                "markPrice": "80520.5",
            }
        ]
    )
    paper = PaperExecutionAdapter(settings)
    market = MagicMock()
    market.tickers = {}
    executor = TradeExecutor(settings, sessions, market, shark, paper)

    async with sessions() as session:
        session.add(
            RiskControl(
                id=1,
                trading_mode="LIVE",
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
                max_notional_per_trade=Decimal("1000"),
                max_daily_loss=Decimal("200"),
                limit_entry_expiry_seconds=900,
                cancel_stale_entry_orders=True,
            )
        )
        session.add(
            SignalEvent(
                id="event-live-1",
                idempotency_key="live-key-1",
                supplied_event_id="tv-1",
                strategy="SHASHA",
                tradingview_symbol="BTCUSDT.P",
                symbol="BTCUSDT",
                direction="LONG",
                timeframe="5",
                bar_time=datetime.now(UTC),
                signal_close=Decimal("80515.2"),
                entry_price=Decimal("80515.2"),
                status="ENTRY_PENDING",
                mode="LIVE",
                raw_payload={},
                raw_body_hash="c" * 64,
            )
        )
        session.add(
            Trade(
                id="trade-live-1",
                event_id="event-live-1",
                mode="LIVE",
                symbol="BTCUSDT",
                tradingview_symbol="BTCUSDT.P",
                direction="LONG",
                status="ENTRY_PENDING",
                planned_entry=Decimal("80515.2"),
                signal_close=Decimal("80515.2"),
                quantity=Decimal("0.012"),
                remaining_quantity=Decimal("0.012"),
                margin=Decimal("100"),
                notional=Decimal("1000"),
                leverage=10,
                entry_order_id="entry-1",
            )
        )
        session.add(
            Order(
                id="order-entry-1",
                trade_id="trade-live-1",
                client_order_id="entry-1",
                kind="ENTRY",
                side="BUY",
                order_type="LIMIT",
                status="NEW",
                quantity=Decimal("0.012"),
                price=Decimal("80515.2"),
            )
        )
        await session.commit()
    return sessions, executor, shark


@pytest.mark.asyncio
async def test_live_fill_establishes_stop_and_targets_then_reports_active(tmp_path) -> None:
    sessions, executor, shark = await _seed(tmp_path)
    shark.split_take_profit_stop_loss = AsyncMock(
        side_effect=[
            _split_response(["stop-1"]),
            _split_response(["tp-1"]),
        ]
    )
    shark.order = AsyncMock(
        side_effect=[
            _stop_order(),  # stop confirmation
            _target_order("tp-1", "82125.6", "0.006"),  # target confirmation
            _stop_order(),  # stop re-confirmation after the targets were accepted
        ]
    )

    await executor._handle_live_fill(
        "event-live-1", _spec(), {"status": "FILLED", "avgPrice": "80515.2", "filledQty": "0.012"}
    )

    async with sessions() as session:
        trade = await session.scalar(select(Trade).where(Trade.id == "trade-live-1"))
        event = await session.scalar(select(SignalEvent).where(SignalEvent.id == "event-live-1"))
        assert trade is not None and event is not None
        assert trade.protection_state == "ACTIVE"
        assert trade.status == "OPEN"
        assert trade.actual_entry == Decimal("80515.2")
        assert trade.price_tick == Decimal("0.1")
        assert trade.stop_price == Decimal("78904.9")  # 2% below the Shark close
        assert trade.target1_price == Decimal("82125.6")  # 2% above, aligned to the tick
        assert trade.target1_quantity == Decimal("0.006")  # 50% of the position
        assert trade.remaining_quantity == Decimal("0.012")
        assert trade.trailing_enabled is True
        assert trade.trailing_stop_percent == Decimal("1")
        assert trade.secondary_targets_enabled is False
        assert trade.target_order_ids == ["tp-1"]
        assert trade.protective_stop_order_id == "stop-1"
        assert event.status == "OPEN"


@pytest.mark.asyncio
async def test_live_fill_alarms_when_the_stop_disappears_after_target_submission(tmp_path) -> None:
    sessions, executor, shark = await _seed(tmp_path)
    shark.split_take_profit_stop_loss = AsyncMock(
        side_effect=[
            _split_response(["stop-1"]),
            _split_response(["tp-1"]),
        ]
    )
    shark.order = AsyncMock(
        side_effect=[
            _stop_order(),
            _target_order("tp-1", "82125.6", "0.006"),
            _stop_order(state="CANCELED"),  # the split call silently cleared the stop
        ]
    )

    await executor._handle_live_fill(
        "event-live-1", _spec(), {"status": "FILLED", "avgPrice": "80515.2", "filledQty": "0.012"}
    )

    async with sessions() as session:
        trade = await session.scalar(select(Trade).where(Trade.id == "trade-live-1"))
        event = await session.scalar(select(SignalEvent).where(SignalEvent.id == "event-live-1"))
        assert trade is not None and event is not None
        assert trade.status == "PROTECTION_ERROR"
        assert trade.protection_state == "ALARM"
        assert "SHARK_STOP_LOST_AFTER_TARGET_SUBMISSION" in (trade.status_reason or "")
        assert trade.protective_stop_order_id == "stop-1"
        assert event.status == "PROTECTION_ERROR"


@pytest.mark.asyncio
async def test_live_fill_alarms_when_a_target_slice_is_rejected(tmp_path) -> None:
    sessions, executor, shark = await _seed(tmp_path)
    shark.split_take_profit_stop_loss = AsyncMock(
        side_effect=[
            _split_response(["stop-1"]),
            {"acceptedOrderCount": 0, "rejectedOrderCount": 1, "acceptedOrders": []},
        ]
    )
    shark.order = AsyncMock(side_effect=[_stop_order()])
    shark.cancel_order = AsyncMock()

    await executor._handle_live_fill(
        "event-live-1", _spec(), {"status": "FILLED", "avgPrice": "80515.2", "filledQty": "0.012"}
    )

    async with sessions() as session:
        trade = await session.scalar(select(Trade).where(Trade.id == "trade-live-1"))
        assert trade is not None
        assert trade.status == "PROTECTION_ERROR"
        assert "SHARK_SPLIT_TARGETS_NOT_ALL_ACCEPTED" in (trade.status_reason or "")
        assert trade.protective_stop_order_id == "stop-1"
