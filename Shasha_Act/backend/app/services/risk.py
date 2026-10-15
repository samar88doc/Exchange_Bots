"""Server-side admission, slot reservation, and hard risk gates."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.domain import ParsedSignal
from app.market_data import MarketDataService
from app.models import AuditEvent, RiskControl, SignalEvent, Trade
from app.services.pnl import long_unrealized_pnl, realized_pnl_for_day

ACTIVE_TRADE_STATES = (
    "QUEUED",
    "SUBMITTING",
    "ENTRY_PENDING",
    "OPEN",
    "PARTIALLY_CLOSED",
    "PROTECTION_ERROR",
    "UNKNOWN",
    "CLOSING",
    "ENTRY_PARTIAL",
)


class RiskRejected(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


async def ensure_risk_control(session: AsyncSession, settings: Settings) -> RiskControl:
    control = await session.get(RiskControl, 1)
    if control is None:
        control = RiskControl(
            id=1,
            trading_mode="PAPER",
            max_open_positions=settings.max_open_positions,
            margin_per_trade=settings.margin_per_trade,
            leverage=settings.leverage,
            target1_percent=settings.target1_percent,
            target2_percent=settings.target2_percent,
            target3_percent=settings.target3_percent,
            stop_loss_percent=settings.stop_loss_percent,
            max_notional_per_trade=settings.max_notional_per_trade,
            max_daily_loss=settings.max_daily_loss,
            limit_entry_expiry_seconds=settings.limit_entry_expiry_seconds,
            cancel_stale_entry_orders=settings.cancel_stale_entry_orders,
            kill_switch=settings.emergency_trading_disabled,
            allow_symbols=settings.allow_symbols_list,
            deny_symbols=settings.deny_symbols_list,
        )
        session.add(control)
        await session.flush()
    return control


async def reserve_trade_slot(
    session: AsyncSession,
    settings: Settings,
    event: SignalEvent,
    parsed: ParsedSignal,
    local_lock: asyncio.Lock,
    market: MarketDataService,
) -> Trade | None:
    """Serialize ingress reservations and atomically enforce cap + symbol uniqueness.

    PostgreSQL `FOR UPDATE` on the singleton risk row is the cross-process lock. The
    local asyncio lock also makes the SQLite concurrency tests representative; SQLite
    deployments are not recommended for production.
    """
    if not parsed.symbol or not parsed.entry_price or not parsed.signal_close:
        return None
    async with local_lock:
        control = await session.scalar(select(RiskControl).where(RiskControl.id == 1).with_for_update())
        if control is None:
            control = await ensure_risk_control(session, settings)
        if settings.emergency_trading_disabled or control.kill_switch:
            raise RiskRejected("EMERGENCY_TRADING_DISABLED")
        if control.allow_symbols and parsed.symbol not in control.allow_symbols:
            raise RiskRejected("SYMBOL_NOT_ALLOWLISTED")
        if parsed.symbol in (control.deny_symbols or []):
            raise RiskRejected("SYMBOL_DENYLISTED")

        effective_mode = "LIVE" if (
            control.trading_mode == "LIVE"
            and settings.live_server_gate_open
            and control.live_confirmed_at is not None
        ) else "PAPER"
        if control.max_daily_loss <= 0:
            raise RiskRejected("INVALID_DAILY_LOSS_LIMIT")
        current_daily = await realized_pnl_for_day(session, settings.timezone, mode=effective_mode)
        unresolved = await session.scalar(
            select(Trade.id).where(
                Trade.mode == effective_mode,
                Trade.status.in_(("SUBMITTING", "UNKNOWN", "PROTECTION_ERROR", "CLOSING")),
            ).limit(1)
        )
        if unresolved is not None:
            raise RiskRejected("UNRESOLVED_TRADE_REQUIRES_RECONCILIATION")
        open_trades = list(
            (await session.scalars(
                select(Trade).where(
                    Trade.mode == effective_mode,
                    Trade.status.in_(("OPEN", "PARTIALLY_CLOSED", "ENTRY_PARTIAL")),
                )
            )).all()
        )
        for open_trade in open_trades:
            ticker = market.tickers.get(open_trade.symbol)
            if (
                ticker is None
                or ticker.is_stale(settings.market_data_stale_seconds)
                or open_trade.actual_entry is None
                or open_trade.remaining_quantity is None
            ):
                raise RiskRejected("DAILY_LOSS_CHECK_MARKET_DATA_UNAVAILABLE")
            current_daily += long_unrealized_pnl(
                open_trade.actual_entry, ticker.last_price, open_trade.remaining_quantity
            )
        if current_daily <= -control.max_daily_loss:
            raise RiskRejected("MAX_DAILY_LOSS_REACHED")

        active_count = int(
            await session.scalar(
                select(func.count()).select_from(Trade).where(Trade.status.in_(ACTIVE_TRADE_STATES))
            )
            or 0
        )
        if active_count >= control.max_open_positions:
            raise RiskRejected("MAX_OPEN_POSITIONS_REACHED")
        duplicate_symbol = await session.scalar(
            select(Trade.id).where(
                Trade.symbol == parsed.symbol,
                Trade.status.in_(ACTIVE_TRADE_STATES),
            ).limit(1)
        )
        if duplicate_symbol:
            raise RiskRejected("EQUIVALENT_SYMBOL_EXPOSURE_EXISTS")

        if control.margin_per_trade <= 0 or control.leverage <= 0:
            raise RiskRejected("INVALID_RISK_CONFIGURATION")
        notional = control.margin_per_trade * Decimal(control.leverage)
        if notional > control.max_notional_per_trade:
            raise RiskRejected("MAX_NOTIONAL_PER_TRADE_EXCEEDED")

        trade = Trade(
            id=str(uuid4()),
            event_id=event.id,
            mode=effective_mode,
            tradingview_symbol=parsed.tradingview_symbol,
            symbol=parsed.symbol,
            direction="LONG",
            status="QUEUED",
            planned_entry=parsed.entry_price,
            signal_close=parsed.signal_close,
            margin=control.margin_per_trade,
            notional=notional,
            leverage=control.leverage,
        )
        event.status = "QUEUED"
        event.mode = effective_mode
        try:
            async with session.begin_nested():
                session.add(trade)
                await session.flush()
        except IntegrityError as exc:
            raise RiskRejected("EQUIVALENT_SYMBOL_EXPOSURE_EXISTS") from exc
        session.add(
            AuditEvent(
                actor="webhook",
                action="TRADE_SLOT_RESERVED",
                entity_type="trade",
                entity_id=trade.id,
                details={"symbol": trade.symbol, "mode": effective_mode},
            )
        )
        return trade
