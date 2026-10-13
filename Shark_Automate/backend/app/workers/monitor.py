"""Background workers: ROI monitor, reconciliation, market polling.

Proper asyncio lifecycle — no uncontrolled threads. Started/stopped with app.
"""
from __future__ import annotations

import asyncio
import logging

from app.core.events import event_bus
from app.core.logging import TradeEvent, log_event
from app.db.session import session_scope
from app.services.runtime import AppContext

log = logging.getLogger("trading.workers")


class WorkerManager:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._tasks: list[asyncio.Task] = []
        self.status = {
            "roi_monitor": "CONNECTED",
            "reconciler": "CONNECTED",
            "market_data": "CONNECTED",
            "position_monitor": "CONNECTED",
        }

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._roi_loop(), name="roi-monitor"),
            asyncio.create_task(self._reconcile_loop(), name="reconciler"),
            asyncio.create_task(self._maintenance_loop(), name="maintenance"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks = []
        await self.ctx.market_data.aclose()
        await self.ctx.shark.aclose()

    async def _roi_loop(self) -> None:
        interval = self.ctx.settings.roi_monitor_interval_seconds
        while True:
            try:
                async with session_scope() as session:
                    closed = await self.ctx.roi_monitor.tick(session)
                if closed:
                    for position_id in closed:
                        await self.ctx.notifications.notify("roi_reached", f"position {position_id} closed at target ROI")
                        event_bus.publish("position.closed", {"position_id": position_id})
                    event_bus.publish("positions.changed", {})
                self.status["roi_monitor"] = "CONNECTED"
                self.status["position_monitor"] = "CONNECTED"
            except Exception as exc:  # noqa: BLE001
                self.status["roi_monitor"] = "ERROR"
                log_event(log, TradeEvent.SYSTEM_ERROR, f"roi loop error: {exc}", level=logging.ERROR)
            await asyncio.sleep(interval)

    async def _reconcile_loop(self) -> None:
        interval = self.ctx.settings.reconciliation_interval_seconds
        while True:
            try:
                async with session_scope() as session:
                    events = await self.ctx.reconciliation.run(session)
                for ev in events:
                    await self.ctx.notifications.notify("reconciliation_error", ev.message)
                self.status["reconciler"] = "CONNECTED"
            except Exception as exc:  # noqa: BLE001
                self.status["reconciler"] = "ERROR"
                log_event(log, TradeEvent.SYSTEM_ERROR, f"reconcile loop error: {exc}", level=logging.ERROR)
            await asyncio.sleep(interval)

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                async with session_scope() as session:
                    expired = await self.ctx.reservations.expire_stale(session)
                if expired:
                    log_event(log, TradeEvent.POSITION_RESERVATION_RELEASED,
                              f"{expired} stale reservations expired")
                self.status["market_data"] = self.ctx.market_data.health()
            except Exception as exc:  # noqa: BLE001
                log_event(log, TradeEvent.SYSTEM_ERROR, f"maintenance loop error: {exc}", level=logging.ERROR)
            await asyncio.sleep(30)
