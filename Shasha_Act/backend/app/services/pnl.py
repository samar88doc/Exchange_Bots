"""P&L calculations with explicit timezone and data-source semantics."""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import TypedDict
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import directional_pnl
from app.models import Fill


class DayPnlBreakdown(TypedDict):
    net: Decimal
    gross: Decimal
    fees_paid: Decimal
    funding_paid: Decimal
    fill_count: int
    funding_record_count: int


MARKET_DAY_ROLLOVER = time(5, 30)


def market_day_bounds(
    timezone_name: str = "Asia/Kolkata",
    on_date: date | None = None,
    *,
    rollover_time: time = MARKET_DAY_ROLLOVER,
) -> tuple[date, datetime, datetime]:
    """Calculate 24-hour market day bounds (05:30:00 AM to Next Day 05:29:59.999999 AM IST).

    Crypto market day rolls over at 00:00:00 UTC, which corresponds to 05:30:00 AM IST.
    Any timestamp before 05:30:00 AM IST belongs to the previous calendar day's market day.
    """
    zone = ZoneInfo(timezone_name)
    now_local = datetime.now(zone)
    if on_date is not None:
        local_date = on_date
    elif now_local.time() < rollover_time:
        local_date = now_local.date() - timedelta(days=1)
    else:
        local_date = now_local.date()
    start_local = datetime.combine(local_date, rollover_time, tzinfo=zone)
    end_local = start_local + timedelta(days=1)
    return local_date, start_local.astimezone(UTC), end_local.astimezone(UTC)


async def realized_pnl_for_day(
    session: AsyncSession,
    timezone_name: str = "Asia/Kolkata",
    on_date: date | None = None,
    mode: str | None = None,
) -> Decimal:
    """Return net realized P&L for the configured local calendar day (backwards-compat)."""
    breakdown = await realized_pnl_breakdown_for_day(session, timezone_name, on_date, mode)
    return breakdown["net"]


async def realized_pnl_breakdown_for_day(
    session: AsyncSession,
    timezone_name: str = "Asia/Kolkata",
    on_date: date | None = None,
    mode: str | None = None,
) -> DayPnlBreakdown:
    """Return net P&L, gross P&L, trading fees and funding fees for the 24-hour market day (05:30 AM to 05:29 AM).

    Gross realized values and exchange/paper fees are read from persisted fills. Funding
    is reported separately because Shark's documented trade-history endpoint does not
    include funding; transaction-history synchronization is not silently assumed here.
    """
    local_date, start_utc, end_utc = market_day_bounds(timezone_name, on_date)
    criteria = [Fill.fill_time >= start_utc, Fill.fill_time < end_utc]
    if mode is not None:
        criteria.append(Fill.mode == mode.upper())
    rows = await session.execute(
        select(Fill.gross_realized_pnl, Fill.fee, Fill.funding).where(*criteria)
    )
    gross_total = Decimal("0")
    fees_total = Decimal("0")
    funding_total = Decimal("0")
    fill_count = 0
    funding_record_count = 0
    for gross, fee, funding in rows.all():
        fill_count += 1
        gross_total += gross or Decimal("0")
        fees_total += fee or Decimal("0")
        if funding is not None:
            funding_record_count += 1
            funding_total += funding
    return DayPnlBreakdown(
        net=gross_total - fees_total - funding_total,
        gross=gross_total,
        fees_paid=fees_total,
        funding_paid=funding_total,
        fill_count=fill_count,
        funding_record_count=funding_record_count,
    )


def position_unrealized_pnl(
    direction: str, entry_price: Decimal, mark_price: Decimal, remaining_quantity: Decimal
) -> Decimal:
    """Unrealized price P&L for a LONG (gains on rise) or SHORT (gains on fall) position."""
    return directional_pnl(direction, entry_price, mark_price, remaining_quantity)


def long_unrealized_pnl(entry_price: Decimal, mark_price: Decimal, remaining_quantity: Decimal) -> Decimal:
    """Backwards-compatible LONG-only helper; prefer `position_unrealized_pnl`."""
    return position_unrealized_pnl("LONG", entry_price, mark_price, remaining_quantity)
