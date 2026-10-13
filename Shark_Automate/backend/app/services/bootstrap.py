"""Bootstrap: seed reference data + restart recovery. DB is the source of truth."""
from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppSettings
from app.core.logging import TradeEvent, log_event
from app.models.entities import (
    PaperBalance,
    Setting,
    StateLock,
    Strategy,
    Symbol,
    SymbolMapping,
    SystemEvent,
)
from app.models.enums import SLOT_CONSUMING_STATES

log = logging.getLogger("trading.bootstrap")

# Starter mappings ONLY (examples from the spec). All other symbols are synced
# dynamically from verified Shark exchangeInfo — never hard-coded by the hundred.
STARTER_MAPPINGS = [
    # (tv_raw, canonical, shark, base, quote)
    ("BASUSDT.P", "BASUSDT", "BASUSDT", "BAS", "USDT"),
    ("BTCUSDT.P", "BTCUSDT", "BTCUSDT", "BTC", "USDT"),
    ("ETHUSDT.P", "ETHUSDT", "ETHUSDT", "ETH", "USDT"),
    ("SOLUSDT.P", "SOLUSDT", "SOLUSDT", "SOL", "USDT"),
    ("DOGEUSDT.P", "DOGEUSDT", "DOGEUSDT", "DOGE", "USDT"),
]


async def seed_reference_data(session: AsyncSession, settings: AppSettings) -> None:
    # singleton lock row for cross-process reservation locking
    if (await session.execute(select(StateLock).where(StateLock.name == "risk"))).scalar_one_or_none() is None:
        try:
            async with session.begin_nested():
                session.add(StateLock(name="risk"))
        except Exception:  # noqa: BLE001 — concurrent seed is harmless
            pass


    for key, value, description in [
        ("trading_halted", "false", "Emergency halt: reject new entries, keep monitoring"),
        ("kill_switch", "false", "Kill switch: reject new entries"),
        ("dry_run", "true" if settings.shark_dry_run else "false", "LIVE dry-run: build payloads, never submit"),
    ]:
        if (await session.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none() is None:
            session.add(Setting(key=key, value=value, value_type="bool", description=description))

    for sid, name in [("strategy_1", "Strategy 1"), ("strategy_2", "Strategy 2"), ("vip_telegram", "Vip Crypto Signal Jemal")]:
        if (await session.execute(select(Strategy).where(Strategy.strategy_id == sid))).scalar_one_or_none() is None:
            session.add(Strategy(strategy_id=sid, name=name, enabled=True, webhook_identifier=sid))


    for raw, canonical, shark, base, quote in STARTER_MAPPINGS:
        exists = (
            await session.execute(select(SymbolMapping).where(SymbolMapping.canonical_symbol == canonical))
        ).scalar_one_or_none()
        if exists is None:
            session.add(Symbol(canonical_symbol=canonical, base_asset=base, quote_asset=quote))
            session.add(
                SymbolMapping(
                    tradingview_raw_symbol=raw,
                    canonical_symbol=canonical,
                    shark_symbol=shark,
                    base_asset=base,
                    quote_asset=quote,
                    quantity_precision=3,
                    price_precision=2,
                    minimum_quantity=Decimal("0.001"),
                    minimum_notional=Decimal("5"),
                    verified_against_exchange=False,
                )
            )

    if (await session.execute(select(PaperBalance))).scalar_one_or_none() is None:
        session.add(
            PaperBalance(
                asset=settings.account_unit,
                balance=settings.paper_initial_balance,
                initial_balance=settings.paper_initial_balance,
            )
        )
    await session.flush()


async def recover_state(session: AsyncSession, settings: AppSettings) -> dict:
    """Restart recovery: reload persisted positions/reservations/pending orders.

    NEVER assumes 'application restarted = no positions'.
    """
    from app.models.entities import Order, Position, PositionReservation
    from app.models.enums import OrderStatus, ReservationStatus

    states = [s.value for s in SLOT_CONSUMING_STATES]
    positions = (await session.execute(select(Position).where(Position.state.in_(states)))).scalars().all()
    reservations = (
        await session.execute(
            select(PositionReservation).where(PositionReservation.status == ReservationStatus.ACTIVE.value)
        )
    ).scalars().all()
    pending_orders = (
        await session.execute(
            select(Order).where(Order.status.in_([OrderStatus.PENDING.value, OrderStatus.SUBMITTED.value,
                                                 OrderStatus.UNKNOWN.value, OrderStatus.NEW.value]))
        )
    ).scalars().all()

    report = {
        "recovered_positions": [p.position_id for p in positions],
        "recovered_reservations": [r.reservation_id for r in reservations],
        "pending_orders": [o.order_id for o in pending_orders],
        "unknown_orders": [o.order_id for o in pending_orders if o.status == OrderStatus.UNKNOWN.value],
    }
    session.add(SystemEvent(event="RESTART_RECOVERY", message="state reloaded from database", detail=report,
                            mode=settings.trading_mode))
    log_event(log, TradeEvent.SYSTEM_ERROR if report["unknown_orders"] else TradeEvent.WEBHOOK_RECEIVED,
              "restart recovery complete", **report)
    return report
