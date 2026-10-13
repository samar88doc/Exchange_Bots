"""Risk engine: position limits, duplicate protection, halt/kill-switch.

Duplicate protection uses canonical_symbol ONLY (never raw TradingView symbol).
Slot accounting: OPEN + RESERVED <= MAX_OPEN_POSITIONS, enforced server-side
inside a database transaction with row/advisory locking.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppSettings
from app.core.errors import RejectReason
from app.core.logging import TradeEvent, log_event
from app.models.entities import Position, PositionReservation, RiskEvent, Setting, Signal
from app.models.enums import SLOT_CONSUMING_STATES, ReservationStatus, ProcessingStatus

log = logging.getLogger("trading.risk")

RISK_LOCK_NAME = "risk"


@dataclass(frozen=True)
class RiskDecision:
    accepted: bool
    reason: str = ""
    message: str = ""


class RiskManager:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    # ------------------------------------------------------------- halt state
    async def is_halted(self, session: AsyncSession) -> bool:
        row = await session.execute(select(Setting).where(Setting.key == "trading_halted"))
        setting = row.scalar_one_or_none()
        return setting is not None and setting.value.lower() in ("true", "1", "yes")

    async def kill_switch_active(self, session: AsyncSession) -> bool:
        row = await session.execute(select(Setting).where(Setting.key == "kill_switch"))
        setting = row.scalar_one_or_none()
        return setting is not None and setting.value.lower() in ("true", "1", "yes")

    # ------------------------------------------------------------- counting
    async def count_active_positions(self, session: AsyncSession) -> int:
        states = [s.value for s in SLOT_CONSUMING_STATES]
        result = await session.execute(
            select(func.count()).select_from(Position).where(Position.state.in_(states))
        )
        return int(result.scalar_one())

    async def count_active_reservations(self, session: AsyncSession) -> int:
        now = datetime.now(UTC)
        result = await session.execute(
            select(func.count())
            .select_from(PositionReservation)
            .where(PositionReservation.status == ReservationStatus.ACTIVE.value)
        )
        count = int(result.scalar_one())
        # stale reservations no longer count after expiry window
        stale = await session.execute(
            select(func.count())
            .select_from(PositionReservation)
            .where(
                PositionReservation.status == ReservationStatus.ACTIVE.value,
                PositionReservation.expires_at.is_not(None),
                PositionReservation.expires_at < now,
            )
        )
        return max(0, count - int(stale.scalar_one()))

    async def symbol_in_use(self, session: AsyncSession, canonical_symbol: str) -> bool:
        states = [s.value for s in SLOT_CONSUMING_STATES]
        pos = await session.execute(
            select(func.count())
            .select_from(Position)
            .where(Position.canonical_symbol == canonical_symbol, Position.state.in_(states))
        )
        if int(pos.scalar_one()) > 0:
            return True
        res = await session.execute(
            select(func.count())
            .select_from(PositionReservation)
            .where(
                PositionReservation.canonical_symbol == canonical_symbol,
                PositionReservation.status == ReservationStatus.ACTIVE.value,
            )
        )
        return int(res.scalar_one()) > 0

    async def check_daily_trigger_rule(self, session: AsyncSession, canonical_symbol: str, alert_price: Decimal | None) -> RiskDecision:
        today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        result = await session.execute(
            select(Signal)
            .where(
                Signal.canonical_symbol == canonical_symbol,
                Signal.received_at >= today_start,
                Signal.processing_status == ProcessingStatus.EXECUTED.value,
            )
            .order_by(Signal.received_at.asc())
        )
        signals_today = result.scalars().all()
        count = len(signals_today)

        if count >= 2:
            return RiskDecision(False, RejectReason.DAILY_MAX_TRIGGERS_REACHED.value, "max 2 triggers per day allowed")

        if count == 1:
            if alert_price is None:
                return RiskDecision(False, RejectReason.PRICE_NOT_BELOW_PREVIOUS.value, "alert_price missing for 2nd trigger")
            first_alert_price = signals_today[0].alert_price
            if first_alert_price is None:
                return RiskDecision(False, RejectReason.PRICE_NOT_BELOW_PREVIOUS.value, "1st trigger alert_price is unknown")
            if alert_price > first_alert_price * Decimal("0.99"):
                return RiskDecision(False, RejectReason.PRICE_NOT_BELOW_PREVIOUS.value, f"price {alert_price} not 1% below 1st trigger {first_alert_price}")

        return RiskDecision(True)

    # --------------------------------------------------------------- checks
    async def check_entry(
        self,
        session: AsyncSession,
        canonical_symbol: str,
        direction: str = "LONG",
        alert_price: Decimal | None = None,
        margin: Decimal | None = None,
    ) -> RiskDecision:
        """All entry gates, evaluated against authoritative DB state."""
        if direction == "SHORT" and not self.settings.short_enabled:
            return RiskDecision(False, RejectReason.SHORT_DISABLED.value, "SHORT trading is disabled by configuration")
        if await self.kill_switch_active(session):
            return RiskDecision(False, RejectReason.KILL_SWITCH_ACTIVE.value, "KILL SWITCH active — no new entries")
        if await self.is_halted(session):
            return RiskDecision(False, RejectReason.TRADING_HALTED.value, "TRADING HALTED — no new entries")

        if await self.symbol_in_use(session, canonical_symbol):
            log_event(log, TradeEvent.SIGNAL_REJECTED, "symbol already open",
                      canonical_symbol=canonical_symbol)
            return RiskDecision(
                False, RejectReason.SYMBOL_ALREADY_OPEN.value,
                f"active position/reservation exists for canonical symbol {canonical_symbol}",
            )

        if self.settings.trading_mode != "PAPER":
            daily_decision = await self.check_daily_trigger_rule(session, canonical_symbol, alert_price)
            if not daily_decision.accepted:
                return daily_decision

        # Check paper balance availability in PAPER mode
        if self.settings.trading_mode == "PAPER":
            states = [s.value for s in SLOT_CONSUMING_STATES]
            locked_res = await session.execute(
                select(func.coalesce(func.sum(Position.margin), Decimal("0"))).where(Position.state.in_(states))
            )
            locked_margin = Decimal(str(locked_res.scalar_one()))
            
            from app.models.entities import Trade
            trades_res = await session.execute(
                select(func.coalesce(func.sum(Trade.net_pnl), Decimal("0")))
            )
            total_realized = Decimal(str(trades_res.scalar_one()))
            
            wallet_balance = self.settings.paper_initial_balance + total_realized
            available_balance = wallet_balance - locked_margin
            trade_margin = margin if margin is not None else Decimal(str(self.settings.margin_per_trade))
            
            if available_balance < trade_margin:
                log_event(log, TradeEvent.SIGNAL_REJECTED, "insufficient paper balance",
                          available_balance=str(available_balance), required=str(trade_margin))
                return RiskDecision(
                    False, "INSUFFICIENT_PAPER_BALANCE",
                    f"Insufficient paper balance: available {available_balance:.2f} USDT < required margin {trade_margin:.2f} USDT",
                )
            
            # In PAPER mode, position capacity is strictly driven by available paper balance (no 4-slot restriction)
            return RiskDecision(True)

        # Slot accounting (enforced in LIVE mode)
        active_positions = await self.count_active_positions(session)
        active_reservations = await self.count_active_reservations(session)
        max_pos = self.settings.max_open_positions
        if active_positions + active_reservations >= max_pos:
            log_event(log, TradeEvent.MAX_POSITION_REACHED,
                      "max open positions reached",
                      canonical_symbol=canonical_symbol,
                      open_positions=active_positions, reservations=active_reservations)
            return RiskDecision(
                False, RejectReason.MAX_OPEN_POSITIONS_REACHED.value,
                f"OPEN({active_positions}) + RESERVED({active_reservations}) >= {max_pos}",
            )
        return RiskDecision(True)


    def record_risk_event(
        self,
        session: AsyncSession,
        event: str,
        reason: str = "",
        detail: dict | None = None,
        correlation_id: str = "",
        event_id: str = "",
        canonical_symbol: str = "",
    ) -> None:
        session.add(
            RiskEvent(
                event=event,
                reason=reason,
                detail=detail or {},
                correlation_id=correlation_id,
                event_id=event_id,
                canonical_symbol=canonical_symbol,
            )
        )

    async def daily_loss_exceeded(self, session: AsyncSession) -> bool:
        limit = self.settings.daily_loss_limit
        if not limit or limit <= 0:
            return False
        from app.models.entities import Trade as TradeRow

        start = datetime.now(UTC) - timedelta(hours=24)
        result = await session.execute(
            select(func.coalesce(func.sum(TradeRow.net_pnl), 0)).where(TradeRow.closed_at >= start)
        )
        total = Decimal(str(result.scalar_one()))
        return total <= -limit
