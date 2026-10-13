"""Position manager: open/close lifecycle, trade records, explainability timeline."""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import TradeEvent, log_event
from app.models.entities import Order, Position, PositionEvent, Trade
from app.models.enums import PositionState
from app.services.pnl import PnlBreakdown, compute_pnl, target_price_for_roi

log = logging.getLogger("trading.positions")


def add_position_event(
    session: AsyncSession,
    position_id: str,
    event: str,
    from_state: str = "",
    to_state: str = "",
    detail: dict | None = None,
    correlation_id: str = "",
) -> PositionEvent:
    row = PositionEvent(
        position_id=position_id,
        event=event,
        from_state=from_state,
        to_state=to_state,
        detail=detail or {},
        correlation_id=correlation_id,
    )
    session.add(row)
    return row


class PositionManager:
    async def transition(
        self,
        session: AsyncSession,
        position: Position,
        to_state: PositionState | str,
        event: str,
        detail: dict | None = None,
        correlation_id: str = "",
    ) -> None:
        from_state = position.state
        position.state = to_state.value if isinstance(to_state, PositionState) else str(to_state)
        add_position_event(session, position.position_id, event, from_state, position.state, detail, correlation_id)

    async def mark_open(
        self,
        session: AsyncSession,
        position: Position,
        entry_price: Decimal,
        quantity: Decimal,
        entry_fee: Decimal,
        order: Order,
        correlation_id: str = "",
    ) -> None:
        position.entry_price = entry_price
        position.quantity = quantity
        position.margin = self.margin_for(entry_price, quantity, position.leverage)
        position.notional = entry_price * quantity
        position.entry_fee = entry_fee
        position.entry_order_id = order.order_id
        if position.target_price is None:
            position.target_price = target_price_for_roi(
                entry_price, position.leverage, position.target_roi_percent, position.direction
            )
        position.open_time = position.open_time or datetime.now(UTC)
        position.rejection_reason = ""
        await self.transition(session, position, PositionState.OPEN, "POSITION_OPENED",
                              {"entry_price": str(entry_price), "quantity": str(quantity)}, correlation_id)
        log_event(
            log, TradeEvent.POSITION_OPENED, "position opened",
            position_id=position.position_id, canonical_symbol=position.canonical_symbol,
            tradingview_raw_symbol=position.tradingview_raw_symbol, shark_symbol=position.shark_symbol,
            entry_price=str(entry_price), quantity=str(quantity),
            margin=str(position.margin), leverage=position.leverage,
        )

    @staticmethod
    def margin_for(entry_price: Decimal, quantity: Decimal, leverage: int) -> Decimal:
        return (entry_price * quantity) / Decimal(leverage)

    def pnl_for(
        self,
        position: Position,
        mark_price: Decimal,
        exit_fee: Decimal = Decimal("0"),
        include_fees: bool = True,
    ) -> PnlBreakdown:
        assert position.entry_price is not None
        return compute_pnl(
            entry_price=position.entry_price,
            exit_price=mark_price,
            quantity=position.quantity,
            margin=position.margin,
            leverage=position.leverage,
            direction=position.direction,
            entry_fee=position.entry_fee,
            exit_fee=exit_fee,
            funding=position.funding,
            include_fees=include_fees,
        )

    async def mark_closed(
        self,
        session: AsyncSession,
        position: Position,
        exit_price: Decimal,
        exit_fee: Decimal,
        close_reason: str,
        exit_order: Order | None = None,
        correlation_id: str = "",
    ) -> Trade:

        expected_exit_fee = exit_fee
        breakdown = self.pnl_for(position, exit_price, exit_fee=expected_exit_fee, include_fees=True)
        position.exit_price = exit_price
        position.exit_fee = exit_fee
        position.realized_pnl = breakdown.net_pnl
        position.roi_percent = breakdown.roi_percent
        position.unrealized_pnl = Decimal("0")
        position.close_time = datetime.now(UTC)
        position.close_reason = close_reason
        if exit_order is not None:
            position.exit_order_id = exit_order.order_id
        await self.transition(session, position, PositionState.CLOSED, "POSITION_CLOSED",
                              {"exit_price": str(exit_price), "reason": close_reason,
                               "net_pnl": str(breakdown.net_pnl), "roi_percent": str(breakdown.roi_percent)},
                              correlation_id)
        log_event(
            log, TradeEvent.POSITION_CLOSED, f"position closed: {close_reason}",
            position_id=position.position_id, canonical_symbol=position.canonical_symbol,
            exit_price=str(exit_price), net_pnl=str(breakdown.net_pnl),
            roi_percent=str(breakdown.roi_percent), price_change_percent=str(breakdown.price_change_percent),
        )

        duration = 0
        if position.open_time and position.close_time:
            open_time = position.open_time
            close_time = position.close_time
            if open_time.tzinfo is not None:
                open_time = open_time
            if close_time.tzinfo is not None:
                close_time = close_time
            duration = int((close_time - open_time).total_seconds())
        trade = Trade(
            trade_id=f"trd-{uuid.uuid4().hex[:20]}",
            position_id=position.position_id,
            strategy_id=position.strategy_id,
            mode=position.mode,
            status="CLOSED",
            result="WIN" if breakdown.net_pnl > 0 else ("LOSS" if breakdown.net_pnl < 0 else "FLAT"),
            tradingview_raw_symbol=position.tradingview_raw_symbol,
            canonical_symbol=position.canonical_symbol,
            shark_symbol=position.shark_symbol,
            side=position.direction,
            entry_price=position.entry_price or Decimal("0"),
            exit_price=exit_price,
            quantity=position.quantity,
            margin=position.margin,
            leverage=position.leverage,
            entry_fee=position.entry_fee,
            exit_fee=exit_fee,
            funding=position.funding,
            gross_pnl=breakdown.gross_pnl,
            net_pnl=breakdown.net_pnl,
            roi_percent=breakdown.roi_percent,
            price_change_percent=breakdown.price_change_percent,
            duration_seconds=duration,
            opened_at=position.open_time,
            closed_at=position.close_time,
            close_reason=close_reason,
            event_id=position.event_id,
            entry_order_id=position.entry_order_id or "",
            exit_order_id=position.exit_order_id or "",
        )
        session.add(trade)
        return trade

    async def mark_partial_closed(
        self,
        session: AsyncSession,
        position: Position,
        exit_price: Decimal,
        exit_fee: Decimal,
        closed_quantity: Decimal,
        close_reason: str,
        exit_order: Order | None = None,
        correlation_id: str = "",
    ) -> Trade:
        assert position.entry_price is not None
        partial_margin = self.margin_for(position.entry_price, closed_quantity, position.leverage)
        partial_pnl = compute_pnl(
            entry_price=position.entry_price,
            exit_price=exit_price,
            quantity=closed_quantity,
            margin=partial_margin,
            leverage=position.leverage,
            direction=position.direction,
            entry_fee=position.entry_fee / Decimal("2"),
            exit_fee=exit_fee,
            include_fees=True,
        )
        position.quantity -= closed_quantity
        position.margin = max(Decimal("0"), position.margin - partial_margin)
        position.notional = position.entry_price * position.quantity
        position.realized_pnl += partial_pnl.net_pnl
        if exit_order is not None:
            position.exit_order_id = exit_order.order_id

        await self.transition(
            session, position, PositionState.PARTIALLY_FILLED, "PARTIAL_CLOSED",
            {
                "closed_quantity": str(closed_quantity),
                "remaining_quantity": str(position.quantity),
                "exit_price": str(exit_price),
                "net_pnl": str(partial_pnl.net_pnl),
                "reason": close_reason,
            },
            correlation_id,
        )
        trade = Trade(
            trade_id=f"trd-{uuid.uuid4().hex[:20]}",
            position_id=position.position_id,
            strategy_id=position.strategy_id,
            mode=position.mode,
            status="PARTIAL_CLOSED",
            result="WIN" if partial_pnl.net_pnl > 0 else ("LOSS" if partial_pnl.net_pnl < 0 else "FLAT"),
            tradingview_raw_symbol=position.tradingview_raw_symbol,
            canonical_symbol=position.canonical_symbol,
            shark_symbol=position.shark_symbol,
            side=position.direction,
            entry_price=position.entry_price or Decimal("0"),
            exit_price=exit_price,
            quantity=closed_quantity,
            margin=partial_margin,
            leverage=position.leverage,
            entry_fee=position.entry_fee / Decimal("2"),
            exit_fee=exit_fee,
            funding=Decimal("0"),
            gross_pnl=partial_pnl.gross_pnl,
            net_pnl=partial_pnl.net_pnl,
            roi_percent=partial_pnl.roi_percent,
            price_change_percent=partial_pnl.price_change_percent,
            duration_seconds=0,
            opened_at=position.open_time,
            closed_at=datetime.now(UTC),
            close_reason=close_reason,
            event_id=position.event_id,
            entry_order_id=position.entry_order_id or "",
            exit_order_id=exit_order.order_id if exit_order else "",
        )
        session.add(trade)
        return trade


    async def get_active(self, session: AsyncSession) -> list[Position]:
        from app.models.enums import SLOT_CONSUMING_STATES

        states = [s.value for s in SLOT_CONSUMING_STATES]
        result = await session.execute(select(Position).where(Position.state.in_(states)))
        return list(result.scalars())

    async def get_by_canonical(self, session: AsyncSession, canonical_symbol: str) -> Position | None:
        from app.models.enums import SLOT_CONSUMING_STATES

        states = [s.value for s in SLOT_CONSUMING_STATES]
        result = await session.execute(
            select(Position).where(
                Position.canonical_symbol == canonical_symbol, Position.state.in_(states)
            ).limit(1)
        )
        return result.scalar_one_or_none()
