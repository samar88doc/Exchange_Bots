"""ROI monitor: watches open positions and closes at +10% ROI on margin.

ROI is margin-based (P&L / margin), computed from ACTUAL filled entry price.
10% price increase is NOT 10% ROI — at 10x, +1% price = +10% ROI.
"""
from __future__ import annotations

import logging
from decimal import Decimal, ROUND_DOWN

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppSettings
from app.core.logging import TradeEvent, log_event
from app.exchanges.base import OrderRequest
from app.models.entities import Order, Signal
from app.models.enums import EntryExit, OrderSide, OrderStatus, PositionState
from app.services.execution import ExecutionEngine, new_order_id
from app.services.pnl import fee_from_notional, roi_reached
from app.services.position_manager import PositionManager
from app.services.reservation import ReservationManager

log = logging.getLogger("trading.roi")


class RoiMonitor:
    def __init__(
        self,
        settings: AppSettings,
        execution: ExecutionEngine,
        positions: PositionManager,
        reservations: ReservationManager,
    ) -> None:
        self.settings = settings
        self.execution = execution
        self.positions = positions
        self.reservations = reservations

    async def tick(self, session: AsyncSession) -> list[str]:
        """One monitoring pass. Returns ids of positions fully closed this tick."""
        closed: list[str] = []
        active = await self.positions.get_active(session)
        monitor_states = {
            PositionState.OPEN.value,
            PositionState.MONITORING.value,
            PositionState.PARTIALLY_FILLED.value,
        }
        for position in active:
            if position.state not in monitor_states or position.entry_price is None:
                continue
            adapter = self.execution.adapter_for(position.mode)
            ticker = await adapter.get_mark_price(position.shark_symbol or position.canonical_symbol)
            mark = ticker.mark_price or ticker.last_price
            if mark <= 0:
                continue

            expected_exit_fee = fee_from_notional(mark * position.quantity, self.settings.paper_taker_fee_bps)
            pnl = self.positions.pnl_for(
                position, mark, exit_fee=expected_exit_fee,
                include_fees=self.settings.roi_includes_fees,
            )
            position.mark_price = mark
            position.unrealized_pnl = pnl.net_pnl if self.settings.roi_includes_fees else pnl.gross_pnl
            position.roi_percent = pnl.roi_percent
            if position.state == PositionState.OPEN.value:
                await self.positions.transition(
                    session, position, PositionState.MONITORING, "ROI_UPDATED",
                    {"mark": str(mark), "roi_percent": str(pnl.roi_percent),
                     "price_change_percent": str(pnl.price_change_percent)},
                )

            # Retrieve signal metadata for VIP targets (Target 1: 50%, Target 2: 50%)
            signal = None
            if position.signal_id:
                signal = (await session.execute(select(Signal).where(Signal.id == position.signal_id))).scalar_one_or_none()
            meta = (signal.metadata_ if signal else None) or {}
            target_1 = Decimal(str(meta["target_1"])) if meta.get("target_1") else None
            target_2 = Decimal(str(meta["target_2"])) if meta.get("target_2") else None

            # 1. Liquidation Check (Isolated Margin — no auto topup, liquidation price is SL)
            if pnl.roi_percent <= Decimal("-95"):
                log_event(
                    log, TradeEvent.POSITION_CLOSED, "Liquidation price reached — position liquidated",
                    position_id=position.position_id, canonical_symbol=position.canonical_symbol,
                    roi_percent=str(pnl.roi_percent), mark_price=str(mark),
                )
                await self.close_position(session, position, reason="LIQUIDATED", mark_price=mark)
                closed.append(position.position_id)
                continue

            # 2. Target 1 Handling — Close 100% on Target 1 reached (revised: only T1 100%)
            target_1 = Decimal(str(meta["target_1"])) if meta.get("target_1") else position.target_price
            if target_1:
                t1_hit = (mark >= target_1) if position.direction == "LONG" else (mark <= target_1)
                if t1_hit:
                    log_event(
                        log, TradeEvent.TP_TRIGGERED, "Target 1 reached — closing 100%",
                        position_id=position.position_id, canonical_symbol=position.canonical_symbol,
                        target=str(target_1), mark_price=str(mark),
                    )
                    await self.close_position(session, position, reason="TP1_100_PERCENT", mark_price=mark)
                    closed.append(position.position_id)
                    continue

            # 3. Clean up any existing partially filled positions (e.g. from prior runs)
            if position.state == PositionState.PARTIALLY_FILLED.value:
                target_2 = Decimal(str(meta["target_2"])) if meta.get("target_2") else None
                t2_hit = ((mark >= target_2) if position.direction == "LONG" else (mark <= target_2)) if target_2 else False
                if t2_hit or roi_reached(pnl.roi_percent, position.target_roi_percent):
                    log_event(
                        log, TradeEvent.TP_TRIGGERED, "Closing remaining partially filled position",
                        position_id=position.position_id, canonical_symbol=position.canonical_symbol,
                        mark_price=str(mark),
                    )
                    await self.close_position(session, position, reason="TP1_100_PERCENT", mark_price=mark)
                    closed.append(position.position_id)
                    continue

            # 4. Standard ROI Target Fallback (if no custom VIP targets)
            if not target_1 and roi_reached(pnl.roi_percent, position.target_roi_percent):
                log_event(
                    log, TradeEvent.TP_TRIGGERED, "target ROI reached",
                    position_id=position.position_id, canonical_symbol=position.canonical_symbol,
                    roi_percent=str(pnl.roi_percent), target=str(position.target_roi_percent),
                    mark_price=str(mark),
                )
                await self.positions.transition(
                    session, position, PositionState.TP_TRIGGERED, "TP_TRIGGERED",
                    {"roi_percent": str(pnl.roi_percent), "mark": str(mark)},
                )
                await self.close_position(session, position, reason="TP_10_ROI", mark_price=mark)
                closed.append(position.position_id)

        return closed

    async def close_position(
        self,
        session: AsyncSession,
        position,
        reason: str = "MANUAL",
        mark_price: Decimal | None = None,
        quantity: Decimal | None = None,
    ) -> None:
        """Close via verified reduceOnly MARKET order; settle trade record."""
        if position.state in (PositionState.CLOSED.value, PositionState.CLOSE_PENDING.value):
            return
        adapter = self.execution.adapter_for(position.mode)

        close_qty = quantity if (quantity is not None and quantity < position.quantity) else position.quantity
        is_partial = close_qty < position.quantity

        if not is_partial:
            await self.positions.transition(session, position, PositionState.CLOSE_PENDING, "CLOSE_SUBMITTED",
                                            {"reason": reason})

        side = OrderSide.SELL.value if position.direction == "LONG" else OrderSide.BUY.value
        exit_order = Order(
            order_id=new_order_id(),
            position_id=position.position_id,
            mode=position.mode,
            exchange=adapter.name,
            tradingview_raw_symbol=position.tradingview_raw_symbol,
            canonical_symbol=position.canonical_symbol,
            shark_symbol=position.shark_symbol,
            side=side,
            order_type="MARKET",
            entry_exit=EntryExit.EXIT.value,
            reduce_only=True,
            quantity=close_qty,
            status=OrderStatus.PENDING.value,
            is_dry_run=position.is_dry_run,
        )
        session.add(exit_order)
        await session.flush()
        position.exit_order_id = exit_order.order_id

        request = OrderRequest(
            symbol=position.shark_symbol or position.canonical_symbol,
            side=side,
            quantity=close_qty,
            order_type="MARKET",
            reduce_only=True,
            margin_asset=self.settings.shark_margin_asset,
            entry_exit=EntryExit.EXIT.value,
        )
        await self.execution.submit(session, order=exit_order, request=request)
        exit_price = exit_order.avg_fill_price or mark_price or position.mark_price or position.entry_price or Decimal("0")
        fill = await self.execution.record_fill(
            session, order=exit_order, price=exit_price,
            quantity=exit_order.filled_quantity or close_qty,
            entry_exit=EntryExit.EXIT.value,
        )
        if is_partial:
            await self.positions.mark_partial_closed(
                session, position, exit_price=fill.price, exit_fee=fill.fee,
                closed_quantity=fill.quantity, close_reason=reason, exit_order=exit_order,
            )
        else:
            await self.positions.mark_closed(
                session, position, exit_price=fill.price, exit_fee=fill.fee,
                close_reason=reason, exit_order=exit_order,
            )
            await self.reservations.release(session, position.canonical_symbol, position.position_id)

