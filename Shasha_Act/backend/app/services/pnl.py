"""P&L calculations with explicit timezone and data-source semantics."""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Fill


async def realized_pnl_for_day(
    session: AsyncSession,
    timezone_name: str = "Asia/Kolkata",
    on_date: date | None = None,
    mode: str | None = None,
) -> Decimal:
    """Return net realized P&L and incurred fees for the configured local calendar day.

    Gross realized values and exchange/paper fees are read from persisted fills. Funding
    is reported separately because Shark's documented trade-history endpoint does not
    include funding; transaction-history synchronization is not silently assumed here.
    """
    zone = ZoneInfo(timezone_name)
    local_date = on_date or datetime.now(zone).date()
    start_local = datetime.combine(local_date, time.min, tzinfo=zone)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(UTC)
    end_utc = end_local.astimezone(UTC)
    criteria = [Fill.fill_time >= start_utc, Fill.fill_time < end_utc]
    if mode is not None:
        criteria.append(Fill.mode == mode.upper())
    rows = await session.execute(
        select(Fill.gross_realized_pnl, Fill.fee).where(*criteria)
    )
    return sum((gross - fee for gross, fee in rows.all()), Decimal("0"))


def long_unrealized_pnl(entry_price: Decimal, mark_price: Decimal, remaining_quantity: Decimal) -> Decimal:
    return (mark_price - entry_price) * remaining_quantity
