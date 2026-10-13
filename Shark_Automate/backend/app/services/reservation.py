"""Atomic position-slot reservation.

Concurrency algorithm (spec-mandated):

    START TRANSACTION / take risk lock
        normalize symbol (done upstream, re-validated here)
        resolve canonical/shark symbol (done upstream)
        reserve position slot            <- INSERT reservation (UNIQUE canonical)
        re-check active positions        <- count with lock held
        re-check reservations            <- count with lock held
        re-check canonical symbol        <- UNIQUE constraint is the final word
        create order reservation
    COMMIT

Only after COMMIT is an external order ever submitted. A single UNIQUE
constraint on active reservations' canonical_symbol is the ultimate race-proof
guard: even if two transactions interleave, exactly one INSERT can succeed.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
import weakref
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppSettings
from app.core.errors import RejectReason, RiskRejection
from app.core.logging import TradeEvent, log_event
from app.models.entities import Position, PositionReservation
from app.models.enums import ReservationStatus
from app.services.risk import RiskManager

log = logging.getLogger("trading.reservation")

# Per-event-loop lock: guarantees in-process serialization. Must be per-loop
# because each test/request context may use a different running loop. Cross-process
# safety comes from the DB row lock (Postgres SELECT FOR UPDATE) + UNIQUE constraints.
_locks_by_loop: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()


def _get_reservation_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _locks_by_loop.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _locks_by_loop[loop] = lock
    return lock


class ReservationManager:
    def __init__(self, settings: AppSettings, risk: RiskManager) -> None:
        self.settings = settings
        self.risk = risk

    async def _acquire_db_lock(self, session: AsyncSession) -> None:
        """Advisory-style row lock on Postgres (no-op semantics on SQLite).

        Uses a raw SELECT ... FOR UPDATE — no ORM identity map, no autoflush —
        so the locking query can never trigger incidental writes.
        """
        from sqlalchemy import text

        bind = session.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            await session.execute(text("SELECT id FROM state_locks WHERE name = 'risk' FOR UPDATE"))

    async def reserve(
        self,
        session: AsyncSession,
        *,
        canonical_symbol: str,
        tradingview_raw_symbol: str,
        shark_symbol: str,
        direction: str = "LONG",
        alert_price: Decimal | None = None,
        margin: Decimal | None = None,
        strategy_id: str = "",
        event_id: str = "",
        correlation_id: str = "",
    ) -> Position:
        """Reserve a slot + create RESERVED position atomically. Raises on reject."""
        async with _get_reservation_lock():
            await self._acquire_db_lock(session)

            decision = await self.risk.check_entry(
                session, canonical_symbol, direction=direction, alert_price=alert_price, margin=margin
            )
            if not decision.accepted:
                self.risk.record_risk_event(
                    session, "SIGNAL_REJECTED", decision.reason,
                    {"message": decision.message}, correlation_id, event_id, canonical_symbol,
                )
                await session.commit()
                raise RiskRejection(RejectReason(decision.reason), decision.message, canonical_symbol=canonical_symbol)

            position_id = f"pos-{uuid.uuid4().hex[:20]}"
            reservation = PositionReservation(
                reservation_id=f"rsv-{uuid.uuid4().hex[:20]}",
                canonical_symbol=canonical_symbol,
                position_id=position_id,
                strategy_id=strategy_id,
                event_id=event_id,
                status=ReservationStatus.ACTIVE.value,
                expires_at=datetime.now(UTC)
                + timedelta(seconds=self.settings.stale_reservation_seconds),
            )
            position = Position(
                position_id=position_id,
                canonical_symbol=canonical_symbol,
                tradingview_raw_symbol=tradingview_raw_symbol,
                shark_symbol=shark_symbol,
                direction=direction,
                state="RESERVED",
                mode=self.settings.trading_mode,
                strategy_id=strategy_id,
                event_id=event_id,
                exchange="paper" if self.settings.trading_mode == "PAPER" else "shark",
            )

            session.add(reservation)
            session.add(position)
            try:
                await session.flush()
                # COMMIT INSIDE THE LOCK: another contender must not pass the
                # count checks on the basis of uncommitted reservations.
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                self.risk.record_risk_event(
                    session, "SIGNAL_REJECTED", RejectReason.SYMBOL_ALREADY_OPEN.value,
                    {"message": "unique canonical symbol constraint"}, correlation_id, event_id, canonical_symbol,
                )
                await session.commit()
                raise RiskRejection(
                    RejectReason.SYMBOL_ALREADY_OPEN,
                    "duplicate canonical symbol reservation (unique constraint)",
                    canonical_symbol=canonical_symbol,
                ) from exc

            log_event(
                log, TradeEvent.POSITION_SLOT_RESERVED, "position slot reserved",
                canonical_symbol=canonical_symbol, position_id=position_id,
                correlation_id=correlation_id, event_id=event_id,
            )
            return position

    async def release(self, session: AsyncSession, canonical_symbol: str, position_id: str | None = None) -> None:
        result = await session.execute(
            select(PositionReservation).where(
                PositionReservation.canonical_symbol == canonical_symbol,
                PositionReservation.status == ReservationStatus.ACTIVE.value,
            )
        )
        for reservation in result.scalars():
            reservation.status = ReservationStatus.RELEASED.value
        log_event(log, TradeEvent.POSITION_RESERVATION_RELEASED, "reservation released",
                  canonical_symbol=canonical_symbol, position_id=position_id or "")

    async def convert(self, session: AsyncSession, canonical_symbol: str, position_id: str) -> None:
        result = await session.execute(
            select(PositionReservation).where(
                PositionReservation.canonical_symbol == canonical_symbol,
                PositionReservation.status == ReservationStatus.ACTIVE.value,
            )
        )
        for reservation in result.scalars():
            reservation.status = ReservationStatus.CONVERTED.value

    async def expire_stale(self, session: AsyncSession) -> int:
        now = datetime.now(UTC)
        result = await session.execute(
            select(PositionReservation).where(
                PositionReservation.status == ReservationStatus.ACTIVE.value,
                PositionReservation.expires_at.is_not(None),
                PositionReservation.expires_at < now,
            )
        )
        count = 0
        for reservation in result.scalars():
            reservation.status = ReservationStatus.EXPIRED.value
            # paired RESERVED position must release its slot too
            pos = (
                await session.execute(
                    select(Position).where(
                        Position.position_id == reservation.position_id,
                        Position.state.in_(["RESERVED", "POSITION_SLOT_RESERVED"]),
                    )
                )
            ).scalar_one_or_none()
            if pos is not None:
                pos.state = "CANCELLED"
                pos.rejection_reason = "RESERVATION_EXPIRED"
            count += 1
        return count
