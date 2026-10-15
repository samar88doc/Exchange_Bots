"""Database-backed PAPER order/fill simulator; it has no Shark private API reference."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.market_data import TickerSnapshot
from app.models import AuditEvent, Fill, Order, SignalEvent, Trade
from app.services.outbox import enqueue_unique_job


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

        # A long limit buy can only simulate a match at/below its limit. A ticker touch
        # is a simulation criterion, not proof that a real exchange order would fill.
        if ticker.last_price > trade.planned_entry:
            return
        entry_price = trade.planned_entry
        quantity = trade.quantity or Decimal("0")
        if quantity <= 0:
            raise RuntimeError("paper entry quantity is invalid")
        order.status = "FILLED"
        order.filled_quantity = quantity
        order.average_fill_price = entry_price
        order.filled_at = now
        trade.actual_entry = entry_price
        trade.remaining_quantity = quantity
        trade.status = "OPEN"
        trade.status_reason = "PAPER simulated limit fill; no exchange order was sent."
        trade.protection_state = "SIMULATED_ACTIVE"
        trade.opened_at = now
        trade.stop_stage = "INITIAL"
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
                order_type="STOP_MARKET",
                status="NEW",
                quantity=trade.remaining_quantity or Decimal("0"),
                price=trade.stop_price,
                reduce_only=True,
                request_summary={"paper": True, "protective": True},
                submitted_at=now,
            )
            session.add(stop_order)
            await session.flush()
            trade.protective_stop_order_id = stop_order.client_order_id
        if not trade.target_order_ids:
            target_data = (
                ("TARGET_1", trade.target1_quantity, trade.target1_price),
                ("TARGET_2", trade.target2_quantity, trade.target2_price),
                ("TARGET_3", trade.target3_quantity, trade.target3_price),
            )
            ids: list[str] = []
            for kind, quantity, price in target_data:
                target = Order(
                    id=str(uuid4()),
                    trade_id=trade.id,
                    client_order_id=f"paper-{uuid4().hex}",
                    kind=kind,
                    side="SELL",
                    order_type="LIMIT",
                    status="NEW",
                    quantity=quantity or Decimal("0"),
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
            await self._mark_closed(session, trade, event, now, trade.stop_price, "SIMULATED_STOP")
            return

        target_prices = (trade.target1_price, trade.target2_price, trade.target3_price)
        target_quantities = (trade.target1_quantity, trade.target2_quantity, trade.target3_quantity)
        while trade.target_progress < 3:
            index = trade.target_progress
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
            if trade.target_progress == 1:
                trade.stop_price = trade.actual_entry
                trade.stop_stage = "BREAK_EVEN_BEFORE_FEES"
                trade.status = "PARTIALLY_CLOSED"
            elif trade.target_progress == 2:
                trade.stop_price = trade.target1_price
                trade.stop_stage = "TARGET_1"
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

        if trade.status in {"OPEN", "PARTIALLY_CLOSED"} and trade.actual_entry is not None:
            trade.unrealized_pnl = (ticker.last_price - trade.actual_entry) * (
                trade.remaining_quantity or Decimal("0")
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
                order_type="STOP_MARKET" if taker else "LIMIT",
                status="NEW",
                quantity=quantity,
                price=price,
                reduce_only=True,
                request_summary={"paper": True, "reason": reason},
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
