"""LIVE reconciliation: internal state vs Shark state. Detect and flag, never guess.

Detects: internal OPEN / exchange CLOSED, internal CLOSED / exchange OPEN,
quantity mismatch, entry mismatch, unexpected position, unknown order,
symbol mismatch. Any mismatch => CRITICAL_RECONCILIATION_EVENT. We NEVER
create a new position to 'fix' a mismatch automatically.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppSettings
from app.core.logging import TradeEvent, log_event
from app.models.entities import Order, ReconciliationEvent
from app.models.enums import OrderStatus
from app.services.execution import ExecutionEngine
from app.services.position_manager import PositionManager

log = logging.getLogger("trading.recon")

TOLERANCE = Decimal("0.0001")


class ReconciliationService:
    def __init__(
        self,
        settings: AppSettings,
        execution: ExecutionEngine,
        positions: PositionManager,
    ) -> None:
        self.settings = settings
        self.execution = execution
        self.positions = positions

    async def run(self, session: AsyncSession) -> list[ReconciliationEvent]:
        events: list[ReconciliationEvent] = []
        if self.settings.trading_mode != "LIVE":
            return events
        adapter = self.execution.adapter_for("LIVE")
        try:
            exchange_positions = await adapter.get_open_positions()
        except Exception as exc:  # noqa: BLE001
            ev = await self._flag(session, "EXCHANGE_UNAVAILABLE", f"cannot reconcile: {exc}")
            return [ev]

        internal = await self.positions.get_active(session)
        internal_by_symbol = {p.canonical_symbol: p for p in internal}
        exchange_by_symbol = {p.symbol.upper(): p for p in exchange_positions}

        for symbol, ipos in internal_by_symbol.items():
            epos = exchange_by_symbol.get(symbol)
            if epos is None and ipos.state not in ("RESERVED", "ORDER_PENDING", "UNKNOWN"):
                events.append(await self._flag(
                    session, "INTERNAL_OPEN_EXCHANGE_CLOSED",
                    f"internal position {ipos.position_id} open on {symbol} but not on exchange",
                    internal_state={"position_id": ipos.position_id, "state": ipos.state},
                ))
            elif epos is not None:
                if abs((epos.quantity or Decimal("0")) - (ipos.quantity or Decimal("0"))) > TOLERANCE:
                    events.append(await self._flag(
                        session, "QUANTITY_MISMATCH", f"quantity mismatch on {symbol}",
                        internal_state={"quantity": str(ipos.quantity)},
                        exchange_state={"quantity": str(epos.quantity)},
                    ))
                if ipos.entry_price and epos.entry_price and abs(epos.entry_price - ipos.entry_price) > TOLERANCE:
                    events.append(await self._flag(
                        session, "ENTRY_MISMATCH", f"entry price mismatch on {symbol}",
                        internal_state={"entry": str(ipos.entry_price)},
                        exchange_state={"entry": str(epos.entry_price)},
                    ))
                if epos.symbol.upper() != (ipos.shark_symbol or "").upper():
                    events.append(await self._flag(
                        session, "SYMBOL_MISMATCH", f"symbol mismatch {epos.symbol} vs {ipos.shark_symbol}",
                    ))

        for symbol, epos in exchange_by_symbol.items():
            if symbol not in internal_by_symbol:
                events.append(await self._flag(
                    session, "UNEXPECTED_EXCHANGE_POSITION",
                    f"unexpected exchange position on {symbol}",
                    exchange_state=epos.raw,
                ))

        # Unknown-submission orders must be resolved before any retry
        from sqlalchemy import select

        result = await session.execute(
            select(Order).where(Order.status.in_(["UNKNOWN", OrderStatus.UNKNOWN.value]))
        )
        for order in result.scalars():
            fetched = await adapter.get_order(order.exchange_order_id or "")
            if fetched is None:
                events.append(await self._flag(
                    session, "UNKNOWN_ORDER", f"order {order.order_id} submission unresolved",
                    internal_state={"fingerprint": order.submission_fingerprint},
                ))
            else:
                order.status = fetched.status
                events.append(await self._flag(
                    session, "UNKNOWN_ORDER_RESOLVED",
                    f"order {order.order_id} resolved as {fetched.status}",
                    exchange_state=fetched.raw,
                ))
        return events

    async def _flag(
        self,
        session: AsyncSession,
        kind: str,
        message: str,
        internal_state: dict | None = None,
        exchange_state: dict | None = None,
    ) -> ReconciliationEvent:
        ev = ReconciliationEvent(
            severity="CRITICAL_RECONCILIATION_EVENT",
            kind=kind,
            message=message,
            internal_state=internal_state or {},
            exchange_state=exchange_state or {},
        )
        session.add(ev)
        log_event(log, TradeEvent.RECONCILIATION_ERROR, message, level=logging.ERROR,
                  kind=kind, internal=internal_state or {}, exchange=exchange_state or {})
        return ev
