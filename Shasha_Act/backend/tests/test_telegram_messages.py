from __future__ import annotations

from decimal import Decimal
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models import SignalEvent, Trade
from app.services.outbox import OutboxWorker
from app.telegram import format_signal_message


def test_format_signal_message_accepted_vs_open() -> None:
    event = MagicMock()
    event.symbol = "STRKUSDT"
    event.tradingview_symbol = "STRKUSDT.P"

    trade = MagicMock()
    trade.symbol = "STRKUSDT"
    trade.direction = "LONG"
    trade.mode = "PAPER"
    trade.status = "OPEN"
    trade.planned_entry = Decimal("0.3950")
    trade.actual_entry = Decimal("0.3950")
    trade.quantity = Decimal("250")
    trade.remaining_quantity = Decimal("250")
    trade.target1_price = Decimal("0.4029")
    trade.target2_price = Decimal("0.4068")
    trade.target3_price = Decimal("0.4108")
    trade.stop_price = Decimal("0.3871")
    trade.initial_stop_price = Decimal("0.3871")

    # 1. Accepted signal notification
    signal_msg = format_signal_message(event, trade, action="accepted")
    assert "SHASHA — LONG SIGNAL" in signal_msg
    assert "STRKUSDT" in signal_msg
    assert "0.395" in signal_msg

    # 2. Position open notification
    open_msg = format_signal_message(event, trade, action="OPEN")
    assert "SHASHA — POSITION OPEN (LONG)" in open_msg
    assert "Fill Price" in open_msg
    assert "Quantity" in open_msg
    assert "0.395" in open_msg

    # Verify they are completely distinct messages
    assert signal_msg != open_msg


def test_format_signal_message_lifecycle_events() -> None:
    event = MagicMock()
    event.symbol = "BTCUSDT"
    event.tradingview_symbol = "BTCUSDT.P"

    trade = MagicMock()
    trade.symbol = "BTCUSDT"
    trade.direction = "SHORT"
    trade.mode = "LIVE"
    trade.status = "OPEN"
    trade.stop_price = Decimal("65000")
    trade.initial_stop_price = Decimal("65000")
    trade.target1_price = Decimal("63000")
    trade.target2_price = Decimal("62000")
    trade.target3_price = Decimal("61000")
    trade.net_realized_pnl = Decimal("150.50")

    # Target hit
    t1_msg = format_signal_message(event, trade, action="TARGET_HIT:0")
    assert "TARGET 1 HIT" in t1_msg

    # Stop loss
    sl_msg = format_signal_message(event, trade, action="STOP_LOSS")
    assert "STOP LOSS HIT" in sl_msg

    # Trailing stop
    trail_msg = format_signal_message(event, trade, action="TRAILING:64000")
    assert "TRAILING STOP UPDATED" in trail_msg

    # Closed
    closed_msg = format_signal_message(event, trade, action="CLOSED")
    assert "TRADE CLOSED" in closed_msg
    assert "+150.5 USDT" in closed_msg


@pytest.mark.asyncio
async def test_outbox_dispatch_extracts_action_from_idempotency_key() -> None:
    session = AsyncMock()
    event = SignalEvent(id="e1", idempotency_key="k1", symbol="BTCUSDT", direction="LONG")
    trade = Trade(id="t1", event_id="e1", symbol="BTCUSDT", direction="LONG", mode="PAPER", planned_entry=Decimal("50000"), actual_entry=Decimal("50000"))
    
    session.get.return_value = event
    session.scalar.return_value = trade
    
    class FakeSessionFactory:
        async def __aenter__(self):
            return session
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
        def __call__(self):
            return self

    context = MagicMock()
    context.notifier.send_message = AsyncMock()
    
    worker = OutboxWorker(cast(Any, FakeSessionFactory()), context, MagicMock())
    
    # 1. Dispatch with idempotency_key notify:e1:OPEN without payload
    await worker._dispatch("TELEGRAM_SIGNAL", "e1", {}, idempotency_key="notify:e1:OPEN")
    assert context.notifier.send_message.called
    sent_text = context.notifier.send_message.call_args[0][0]
    assert "SHASHA — POSITION OPEN (LONG)" in sent_text
    
    # 2. Dispatch with idempotency_key notify:e1:accepted
    context.notifier.send_message.reset_mock()
    await worker._dispatch("TELEGRAM_SIGNAL", "e1", {}, idempotency_key="notify:e1:accepted")
    assert context.notifier.send_message.called
    sent_text2 = context.notifier.send_message.call_args[0][0]
    assert "SHASHA — LONG SIGNAL" in sent_text2

