"""Signal replay tool: re-run stored TradingView events through the engine (PAPER).

Filters: start/end timestamp, strategy, symbol (raw/canonical/shark), speed.
Useful for reproducing bugs and validating logic changes safely.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Signal, WebhookEvent
from app.services.pipeline import SignalPipeline

log = logging.getLogger("trading.replay")


@dataclass
class ReplayFilter:
    start: datetime | None = None
    end: datetime | None = None
    strategy_id: str | None = None
    symbol: str | None = None  # matches raw OR canonical OR shark (case-insensitive)
    speed: float = 1.0  # 0 = as fast as possible, 1 = realtime spacing


@dataclass
class ReplayReport:
    total: int = 0
    executed: int = 0
    rejected: int = 0
    details: list[dict] = field(default_factory=list)


class ReplayService:
    def __init__(self, pipeline: SignalPipeline) -> None:
        self.pipeline = pipeline

    async def select_events(self, session: AsyncSession, flt: ReplayFilter) -> list[WebhookEvent]:
        stmt = select(WebhookEvent).order_by(WebhookEvent.received_at.asc())
        if flt.start:
            stmt = stmt.where(WebhookEvent.received_at >= flt.start)
        if flt.end:
            stmt = stmt.where(WebhookEvent.received_at <= flt.end)
        events = list((await session.execute(stmt)).scalars())
        if flt.strategy_id or flt.symbol:
            signals = {
                s.event_id: s
                for s in (await session.execute(select(Signal))).scalars()
            }
            filtered = []
            for ev in events:
                sig = signals.get(ev.event_id)
                if flt.strategy_id and (not sig or sig.strategy_id != flt.strategy_id):
                    continue
                if flt.symbol:
                    needle = flt.symbol.upper()
                    raw = (ev.raw_payload or {}).get("symbol", "")
                    candidates = {str(raw).upper()}
                    if sig:
                        candidates |= {sig.canonical_symbol.upper(), sig.shark_symbol.upper()}
                    if needle not in candidates:
                        continue
                filtered.append(ev)
            events = filtered
        return events

    async def replay(self, session: AsyncSession, flt: ReplayFilter) -> ReplayReport:
        events = await self.select_events(session, flt)
        report = ReplayReport(total=len(events))
        prev_ts: datetime | None = None
        for ev in events:
            if flt.speed > 0 and prev_ts is not None and ev.received_at and prev_ts:
                gap = (ev.received_at - prev_ts).total_seconds() / flt.speed
                if 0 < gap < 5:
                    await asyncio.sleep(gap)
            prev_ts = ev.received_at

            payload = dict(ev.raw_payload or {})
            payload["_replay"] = True
            signal = await self.pipeline.create_signal(session, ev, payload)
            signal.is_replay = True
            await session.commit()
            if signal.validation_status == "INVALID":
                report.rejected += 1
                report.details.append({"event_id": ev.event_id, "status": signal.status_display,
                                       "reason": signal.rejection_reason})
                continue
            outcome = await self.pipeline.process_signal(session, signal.id)
            await session.commit()
            if outcome.status == "EXECUTED":
                report.executed += 1
            else:
                report.rejected += 1
            report.details.append({
                "event_id": ev.event_id,
                "status": outcome.status,
                "reason": outcome.reason,
                "position_id": outcome.position_id,
            })
        return report
