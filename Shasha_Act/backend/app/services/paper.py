"""Database-backed PAPER order/fill simulator; it has no Shark private API reference."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.domain import (
    ValidationProblem,
    align_to_tick,
    next_trailing_stop,
    round_up_to_step,
)
from app.market_data import TickerSnapshot
from app.models import AuditEvent, Fill, Order, SignalEvent, Trade
from app.services.outbox import enqueue_unique_job


def _aligned_price(value: Decimal | None, trade: Trade) -> Decimal | None:
    """Tick-align a price for comparison; stored levels can carry storage noise."""
    if value is None:
        return None
    try:
        return align_to_tick(value, trade.price_tick)
    except ValidationProblem:
        return value


class PaperExecutionAdapter:
    """Simulate LIMIT and protective-order fills from Shark public last-price updates.

    This component intentionally does not accept a SharkClient and cannot call private
    account/order endpoints. Paper fills are simulated assumptions, not exchange fills.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def create_limit_entry(self, session: AsyncSession, trade: Trade) -> Order:
        existing = await session.scalar(
            select(Order).where(Order.trade_id == trade.id, Order.kind == "ENTRY").limit(1)
        )
        if existing is not None:
            return existing
        order = Order(
            id=str(uuid4()),
            trade_id=trade.id,
            client_order_id=f"paper-{uuid4().hex}",
            kind="ENTRY",
            side="BUY",
            order_type="LIMIT",
            status="NEW",
            quantity=trade.quantity or Decimal("0"),
            price=trade.planned_entry,
            reduce_only=False,
            request_summary={"paper": True, "type": "LIMIT", "price": str(trade.planned_entry)},
        )
        session.add(order)
        trade.entry_order_id = order.client_order_id
        return order

    async def close_manually(
        self,
        session: AsyncSession,
        trade: Trade,
        ticker: TickerSnapshot,
        event: SignalEvent | None,
    ) -> None:
        if trade.mode != "PAPER" or trade.status not in {"OPEN", "PARTIALLY_CLOSED"}:
            raise ValueError("paper trade is not closable")
        quantity = trade.remaining_quantity or Decimal("0")
        if quantity <= 0 or trade.actual_entry is None:
            raise ValueError("paper position has no remaining quantity")
        await self._close_paper_quantity(
            session,
            trade,
            quantity,
            ticker.last_price,
            "MANUAL_CLOSE",
            ticker.received_at,
            taker=True,
        )
        await self._mark_closed(
            session, trade, event, ticker.received_at, ticker.last_price, "OPERATOR_CONFIRMED"
        )

    async def process_ticker(
        self,
        session: AsyncSession,
        trade: Trade,
        ticker: TickerSnapshot,
        event: SignalEvent | None = None,
    ) -> None:
        if ticker.symbol != trade.symbol:
            return
        now = ticker.received_at
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        aligned = _aligned_price(ticker.last_price, trade)
        if aligned is not None and aligned != ticker.last_price:
            ticker = replace(ticker, last_price=aligned)
        trade.last_price = ticker.last_price

        if trade.status == "ENTRY_PENDING":
            await self._process_pending_entry(session, trade, ticker, event, now)
            return
        if trade.status not in {"OPEN", "PARTIALLY_CLOSED"}:
            return
        await self._process_protective_and_targets(session, trade, ticker, event, now)

    async def _process_pending_entry(
        self,
        session: AsyncSession,
        trade: Trade,
        ticker: TickerSnapshot,
        event: SignalEvent | None,
        now: datetime,
    ) -> None:
        order = await session.scalar(
            select(Order).where(Order.trade_id == trade.id, Order.kind == "ENTRY").limit(1)
        )
        if order is None:
            raise RuntimeError("paper entry order is missing")
        if trade.entry_expires_at is not None:
            expiry = trade.entry_expires_at
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            if now >= expiry and self.settings.cancel_stale_entry_orders:
                order.status = "CANCELED"
                trade.status = "CANCELLED"
                trade.status_reason = "Limit entry expired before simulated fill."
                trade.closed_at = now
                if event is not None:
                    event.status = "CANCELLED"
                    event.status_reason = trade.status_reason
                session.add(
                    AuditEvent(
                        action="PAPER_ENTRY_EXPIRED",
                        entity_type="trade",
                        entity_id=trade.id,
                        details={"order_id": order.client_order_id},
                    )
                )
                await enqueue_unique_job(
                    session,
                    kind="TELEGRAM_SIGNAL",
                    event_id=trade.event_id,
                    idempotency_key=f"notify:{trade.event_id}:CANCELLED",
                    priority=20,
                )
                return

        limit_price = _aligned_price(trade.planned_entry, trade) or trade.planned_entry
        # A long limit buy can only simulate a match at/below its limit. A ticker touch
        # is a simulation criterion, not proof that a real exchange order would fill.
        if ticker.last_price > limit_price:
            return
        entry_price = limit_price
        quantity = trade.quantity or Decimal("0")
        if quantity <= 0:
            raise RuntimeError("paper entry quantity is invalid")
        order.status = "FILLED"
        order.filled_quantity = quantity
        order.average_fill_price = entry_price
        order.filled_at = now
        trade.actual_entry = entry_price
        trade.remaining_quantity = quantity
        if trade.leverage and trade.leverage > 0:
            lev = Decimal(str(trade.leverage))
            # Simplified isolated-margin estimate: maintenance buffer approximated, never
            # presented as an exchange liquidation price in PAPER mode.
            trade.liquidation_price = max(
                entry_price * (Decimal("1") - (Decimal("1") / lev)) * Decimal("0.995"), Decimal("0")
            )
        trade.status = "OPEN"
        trade.status_reason = "PAPER simulated limit fill; no exchange order was sent."
        trade.protection_state = "SIMULATED_ACTIVE"
        trade.opened_at = now
        trade.stop_stage = "INITIAL"
        trade.partial_closed = False
        trade.target_progress = 0
        trade.trailing_active = False
        trade.trailing_high_price = entry_price if trade.trailing_enabled else None
        if event is not None:
            event.status = "OPEN"
            event.status_reason = "PAPER simulated entry fill."
        await self._ensure_paper_protection_orders(session, trade, now)

        fee = (entry_price * quantity) * self.settings.paper_maker_fee_bps / Decimal("10000")
        session.add(
            Fill(
                id=str(uuid4()),
                trade_id=trade.id,
                order_id=order.id,
                mode="PAPER",
                entry_exit="ENTRY",
                side="BUY",
                price=entry_price,
                quantity=quantity,
                fee=fee,
                gross_realized_pnl=Decimal("0"),
                fill_time=now,
            )
        )
        trade.fees_paid += fee
        trade.net_realized_pnl -= fee
        trade.fee_data_available = True
        session.add(
            AuditEvent(
                action="PAPER_ENTRY_FILLED",
                entity_type="trade",
                entity_id=trade.id,
                details={"price": str(entry_price), "quantity": str(quantity), "fee": str(fee)},
            )
        )
        await enqueue_unique_job(
            session,
            kind="TELEGRAM_SIGNAL",
            event_id=trade.event_id,
            idempotency_key=f"notify:{trade.event_id}:OPEN",
            priority=20,
        )

    async def _ensure_paper_protection_orders(
        self, session: AsyncSession, trade: Trade, now: datetime
    ) -> None:
        if trade.protective_stop_order_id is None:
            stop_order = Order(
                id=str(uuid4()),
                trade_id=trade.id,
                client_order_id=f"paper-{uuid4().hex}",
                kind="STOP_LOSS",
                side="SELL",
                order_type="LIMIT",
                status="NEW",
                quantity=trade.remaining_quantity or Decimal("0"),
                price=trade.stop_price,
                reduce_only=True,
                request_summary={"paper": True, "protective": True, "type": "LIMIT"},
                submitted_at=now,
            )
            session.add(stop_order)
            await session.flush()
            trade.protective_stop_order_id = stop_order.client_order_id
        if not trade.target_order_ids:
            target_data = [("TARGET_1", trade.target1_quantity, trade.target1_price)]
            if trade.secondary_targets_enabled:
                target_data.extend(
                    [
                        ("TARGET_2", trade.target2_quantity, trade.target2_price),
                        ("TARGET_3", trade.target3_quantity, trade.target3_price),
                    ]
                )
            ids: list[str] = []
            for kind, quantity, price in target_data:
                if price is None or quantity is None or quantity <= 0:
                    continue
                target = Order(
                    id=str(uuid4()),
                    trade_id=trade.id,
                    client_order_id=f"paper-{uuid4().hex}",
                    kind=kind,
                    side="SELL",
                    order_type="LIMIT",
                    status="NEW",
                    quantity=quantity,
                    price=price,
                    reduce_only=True,
                    request_summary={"paper": True, "take_profit": kind},
                    submitted_at=now,
                )
                session.add(target)
                ids.append(target.client_order_id)
            trade.target_order_ids = ids

    async def _process_protective_and_targets(
        self,
        session: AsyncSession,
        trade: Trade,
        ticker: TickerSnapshot,
        event: SignalEvent | None,
        now: datetime,
    ) -> None:
        entry = trade.actual_entry
        remaining = trade.remaining_quantity or Decimal("0")
        if entry is None or remaining <= 0:
            return

        # A last-price gap below the active stop is treated as a simulated market stop.
        # Target fills advance only by creating persisted PAPER fill records below.
        if trade.stop_price is not None and ticker.last_price <= trade.stop_price:
            await self._close_paper_quantity(
                session,
                trade,
                remaining,
                trade.stop_price,
                "STOP_LOSS",
                now,
                taker=True,
            )
            stop_reason = {
                "TRAILING": "SIMULATED_TRAILING_STOP",
                "BREAK_EVEN": "SIMULATED_BREAK_EVEN_STOP",
            }.get(trade.stop_stage, "SIMULATED_STOP")
            await self._mark_closed(session, trade, event, now, trade.stop_price, stop_reason)
            return

        await self._process_target1(session, trade, ticker, now)

        if trade.secondary_targets_enabled:
            await self._process_secondary_targets(session, trade, ticker, event, now)
            if trade.status not in {"OPEN", "PARTIALLY_CLOSED"}:
                return

        if trade.trailing_enabled and trade.partial_closed:
            await self._process_trailing_stop(session, trade, ticker, now)
            if trade.status not in {"OPEN", "PARTIALLY_CLOSED"}:
                return

        if trade.status in {"OPEN", "PARTIALLY_CLOSED"} and trade.actual_entry is not None:
            trade.unrealized_pnl = (ticker.last_price - trade.actual_entry) * (
                trade.remaining_quantity or Decimal("0")
            )

    async def _process_target1(
        self,
        session: AsyncSession,
        trade: Trade,
        ticker: TickerSnapshot,
        now: datetime,
    ) -> None:
        """Target 1 closes the configured slice and moves the stop to break-even."""
        if trade.partial_closed or trade.target1_price is None:
            return
        slice_quantity = trade.target1_quantity or Decimal("0")
        if slice_quantity <= 0 or ticker.last_price < trade.target1_price:
            return
        if slice_quantity > (trade.remaining_quantity or Decimal("0")):
            raise RuntimeError("target 1 slice exceeds remaining paper position")
        await self._close_paper_quantity(
            session,
            trade,
            slice_quantity,
            trade.target1_price,
            "TARGET_1",
            now,
            taker=False,
        )
        trade.partial_closed = True
        trade.target_progress = max(trade.target_progress, 1)
        trade.status = "PARTIALLY_CLOSED"
        stop_floor = trade.actual_entry or trade.planned_entry
        tick = trade.price_tick
        break_even = round_up_to_step(stop_floor, tick) if tick and tick > 0 else stop_floor
        if trade.stop_price is None or trade.stop_price < break_even:
            trade.stop_price = break_even
        trade.stop_stage = "BREAK_EVEN"
        trade.trailing_active = bool(trade.trailing_enabled)
        trade.trailing_high_price = max(trade.trailing_high_price or break_even, ticker.last_price)
        await self._amend_paper_stop(session, trade)
        session.add(
            AuditEvent(
                action="PAPER_TARGET_1_FILLED",
                entity_type="trade",
                entity_id=trade.id,
                details={
                    "price": str(trade.target1_price),
                    "closed_quantity": str(slice_quantity),
                    "remaining_quantity": str(trade.remaining_quantity or Decimal("0")),
                    "next_stop": str(trade.stop_price),
                    "trailing_percent": str(trade.trailing_stop_percent),
                },
            )
        )
        await enqueue_unique_job(
            session,
            kind="TELEGRAM_SIGNAL",
            event_id=trade.event_id,
            idempotency_key=f"notify:{trade.event_id}:TARGET_1",
            priority=20,
        )

    async def _process_secondary_targets(
        self,
        session: AsyncSession,
        trade: Trade,
        ticker: TickerSnapshot,
        event: SignalEvent | None,
        now: datetime,
    ) -> None:
        """Optional fixed Target 2/3 exits. Disabled by default: the runner trails instead."""
        target_prices = (trade.target1_price, trade.target2_price, trade.target3_price)
        target_quantities = (trade.target1_quantity, trade.target2_quantity, trade.target3_quantity)
        while trade.target_progress < 3:
            index = trade.target_progress
            if index == 0:
                break
            target_price = target_prices[index]
            slice_quantity = target_quantities[index]
            if target_price is None or slice_quantity is None or ticker.last_price < target_price:
                break
            if slice_quantity > (trade.remaining_quantity or Decimal("0")):
                raise RuntimeError("target slice exceeds remaining paper position")
            await self._close_paper_quantity(
                session,
                trade,
                slice_quantity,
                target_price,
                f"TARGET_{index + 1}",
                now,
                taker=False,
            )
            trade.target_progress = index + 1
            trade.status = "PARTIALLY_CLOSED"
            if trade.target_progress < 3:
                await self._amend_paper_stop(session, trade)
            session.add(
                AuditEvent(
                    action=f"PAPER_TARGET_{trade.target_progress}_FILLED",
                    entity_type="trade",
                    entity_id=trade.id,
                    details={
                        "price": str(target_price),
                        "closed_quantity": str(slice_quantity),
                        "remaining_quantity": str(trade.remaining_quantity or Decimal("0")),
                        "next_stop": str(trade.stop_price),
                    },
                )
            )
            await enqueue_unique_job(
                session,
                kind="TELEGRAM_SIGNAL",
                event_id=trade.event_id,
                idempotency_key=f"notify:{trade.event_id}:TARGET_{trade.target_progress}",
                priority=20,
            )
            if trade.target_progress == 3 or (trade.remaining_quantity or Decimal("0")) == 0:
                await self._mark_closed(session, trade, event, now, target_price, "TARGET_3")
                break

    async def _process_trailing_stop(
        self,
        session: AsyncSession,
        trade: Trade,
        ticker: TickerSnapshot,
        now: datetime,
    ) -> None:
        """Ratchet the protective stop: every 1% upward move lifts the stop by 1%."""
        percent = trade.trailing_stop_percent
        tick = trade.price_tick
        floor_price = trade.actual_entry or trade.planned_entry
        if percent is None or percent <= 0 or tick is None or tick <= 0:
            return
        high_water = max(trade.trailing_high_price or floor_price, ticker.last_price)
        trade.trailing_high_price = high_water
        try:
            candidate = next_trailing_stop(
                high_water=high_water,
                current_stop=trade.stop_price,
                floor_price=floor_price,
                trailing_percent=percent,
                price_tick=tick,
                reference_price=ticker.last_price,
            )
        except ValidationProblem:
            return
        if trade.stop_price is not None and candidate <= trade.stop_price:
            return
        previous_stop = trade.stop_price
        trade.stop_price = candidate
        trade.stop_stage = "TRAILING"
        trade.trailing_active = True
        await self._amend_paper_stop(session, trade)
        session.add(
            AuditEvent(
                action="PAPER_TRAILING_STOP_UPDATED",
                entity_type="trade",
                entity_id=trade.id,
                details={
                    "previous_stop": str(previous_stop) if previous_stop is not None else None,
                    "new_stop": str(candidate),
                    "high_water": str(high_water),
                    "trailing_percent": str(percent),
                    "remaining_quantity": str(trade.remaining_quantity or Decimal("0")),
                },
            )
        )
        await enqueue_unique_job(
            session,
            kind="TELEGRAM_SIGNAL",
            event_id=trade.event_id,
            idempotency_key=f"notify:{trade.event_id}:TRAILING:{candidate}",
            priority=30,
        )

    async def _amend_paper_stop(self, session: AsyncSession, trade: Trade) -> None:
        if not trade.protective_stop_order_id:
            raise RuntimeError("paper protective stop is missing")
        stop_order = await session.scalar(
            select(Order).where(Order.client_order_id == trade.protective_stop_order_id).limit(1)
        )
        if stop_order is None or stop_order.status not in {"NEW", "OPEN"}:
            raise RuntimeError("paper protective stop is not active")
        stop_order.price = trade.stop_price
        stop_order.quantity = trade.remaining_quantity or Decimal("0")
        stop_order.request_summary = {
            "paper": True,
            "protective": True,
            "stop_stage": trade.stop_stage,
        }

    async def _close_paper_quantity(
        self,
        session: AsyncSession,
        trade: Trade,
        quantity: Decimal,
        price: Decimal,
        reason: str,
        now: datetime,
        *,
        taker: bool,
    ) -> None:
        entry = trade.actual_entry
        if entry is None or quantity <= 0:
            return
        remaining = trade.remaining_quantity or Decimal("0")
        if quantity > remaining:
            raise RuntimeError("paper exit would over-close position")
        fee_bps = self.settings.paper_taker_fee_bps if taker else self.settings.paper_maker_fee_bps
        fee = price * quantity * fee_bps / Decimal("10000")
        gross = (price - entry) * quantity
        exit_order = await session.scalar(
            select(Order).where(
                Order.trade_id == trade.id,
                Order.kind == reason,
                Order.status.in_(("NEW", "OPEN")),
            ).limit(1)
        )
        if exit_order is None:
            exit_order = Order(
                id=str(uuid4()),
                trade_id=trade.id,
                client_order_id=f"paper-{uuid4().hex}",
                kind=reason,
                side="SELL",
                order_type="LIMIT",
                status="NEW",
                quantity=quantity,
                price=price,
                reduce_only=True,
                request_summary={"paper": True, "reason": reason, "type": "LIMIT"},
                submitted_at=now,
            )
            session.add(exit_order)
            await session.flush()
        exit_order.status = "FILLED"
        exit_order.quantity = quantity
        exit_order.filled_quantity = quantity
        exit_order.price = price
        exit_order.average_fill_price = price
        exit_order.filled_at = now
        session.add(
            Fill(
                id=str(uuid4()),
                trade_id=trade.id,
                order_id=exit_order.id,
                mode="PAPER",
                entry_exit="EXIT",
                side="SELL",
                price=price,
                quantity=quantity,
                fee=fee,
                gross_realized_pnl=gross,
                fill_time=now,
            )
        )
        trade.remaining_quantity = remaining - quantity
        trade.gross_realized_pnl += gross
        trade.fees_paid += fee
        trade.net_realized_pnl += gross - fee
        trade.exit_price = price
        trade.unrealized_pnl = Decimal("0")

    async def _mark_closed(
        self,
        session: AsyncSession,
        trade: Trade,
        event: SignalEvent | None,
        now: datetime,
        exit_price: Decimal,
        reason: str,
    ) -> None:
        trade.remaining_quantity = Decimal("0")
        trade.status = "CLOSED"
        trade.status_reason = f"PAPER simulated close: {reason}."
        trade.protection_state = "SIMULATED_COMPLETE"
        trade.closed_at = now
        trade.exit_price = exit_price
        trade.unrealized_pnl = Decimal("0")
        pending_orders = list(
            (await session.scalars(
                select(Order).where(
                    Order.trade_id == trade.id,
                    Order.status.in_(("NEW", "OPEN")),
                )
            )).all()
        )
        for pending in pending_orders:
            pending.status = "CANCELED"
        if event is not None:
            event.status = "CLOSED"
            event.status_reason = trade.status_reason
        session.add(
            AuditEvent(
                action="PAPER_TRADE_CLOSED",
                entity_type="trade",
                entity_id=trade.id,
                details={
                    "exit_price": str(exit_price),
                    "gross_realized_pnl": str(trade.gross_realized_pnl),
                    "net_realized_pnl": str(trade.net_realized_pnl),
                    "fees": str(trade.fees_paid),
                    "reason": reason,
                },
            )
        )
        await enqueue_unique_job(
            session,
            kind="TELEGRAM_SIGNAL",
            event_id=trade.event_id,
            idempotency_key=f"notify:{trade.event_id}:CLOSED",
            priority=20,
        )
