"""Transactional PostgreSQL outbox and lease-based worker."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.config import Settings
from app.models import OutboxJob, SignalEvent, Trade
from app.telegram import TelegramPermanentError, TelegramRetryableError, format_signal_message

log = logging.getLogger("shasha_act.outbox")


def enqueue_job(
    session: AsyncSession,
    *,
    kind: str,
    idempotency_key: str,
    event_id: str | None = None,
    payload: dict[str, Any] | None = None,
    priority: int = 100,
    depends_on_id: str | None = None,
) -> OutboxJob:
    """Add one idempotent outbox row inside the caller's transaction."""
    existing = session.sync_session.identity_map  # no query here; callers use deterministic keys
    del existing
    return OutboxJob(
        id=str(uuid4()),
        event_id=event_id,
        depends_on_id=depends_on_id,
        kind=kind,
        idempotency_key=idempotency_key,
        status="PENDING",
        priority=priority,
        payload=payload or {},
    )


async def enqueue_unique_job(
    session: AsyncSession,
    *,
    kind: str,
    idempotency_key: str,
    event_id: str | None = None,
    payload: dict[str, Any] | None = None,
    priority: int = 100,
    depends_on_id: str | None = None,
) -> OutboxJob | None:
    found = await session.scalar(select(OutboxJob).where(OutboxJob.idempotency_key == idempotency_key))
    if found is not None:
        return None
    job = enqueue_job(
        session,
        kind=kind,
        idempotency_key=idempotency_key,
        event_id=event_id,
        payload=payload,
        priority=priority,
        depends_on_id=depends_on_id,
    )
    session.add(job)
    await session.flush()
    return job


class OutboxWorker:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], context: Any, settings: Settings) -> None:
        self.session_factory = session_factory
        self.context = context
        self.settings = settings
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._active = False
        self.last_success: datetime | None = None
        self.last_error: str = ""

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self.run(), name="shasha-act-outbox")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                handled = await self.run_once()
                if not handled:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.settings.worker_poll_seconds)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                continue
            except Exception as exc:
                self.last_error = type(exc).__name__
                log.exception("outbox loop failed (%s)", type(exc).__name__)
                await asyncio.sleep(min(2.0, self.settings.worker_poll_seconds * 4))

    async def run_once(self) -> bool:
        job_data: tuple[str, str, str | None, dict[str, Any]] | None = None
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            dependency = aliased(OutboxJob)
            dependency_ready = or_(
                OutboxJob.depends_on_id.is_(None),
                select(dependency.id)
                .where(
                    dependency.id == OutboxJob.depends_on_id,
                    dependency.status.in_(("DELIVERED", "FAILED")),
                )
                .exists(),
            )
            ready = and_(
                OutboxJob.available_at <= now,
                dependency_ready,
                or_(
                    OutboxJob.status == "PENDING",
                    and_(OutboxJob.status == "PROCESSING", OutboxJob.locked_until < now),
                ),
            )
            job = await session.scalar(
                select(OutboxJob)
                .where(ready)
                .order_by(OutboxJob.priority, OutboxJob.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return False
            job.status = "PROCESSING"
            job.attempts += 1
            job.locked_until = now + timedelta(seconds=self.settings.outbox_lease_seconds)
            job_data = (job.id, job.kind, job.event_id, dict(job.payload or {}))
            await session.commit()

        assert job_data is not None
        job_id, kind, event_id, payload = job_data
        try:
            await self._dispatch(kind, event_id, payload)
        except TelegramRetryableError as exc:
            await self._retry(job_id, str(exc), exc.retry_after)
        except TelegramPermanentError as exc:
            await self._fail(job_id, str(exc))
        except Exception as exc:
            # TradeExecutor records ambiguous order submissions as UNKNOWN and returns
            # normally. Unexpected failures can be retried; its state guard prevents
            # duplicate order submissions on a reclaimed lease.
            await self._retry(job_id, type(exc).__name__, None)
            log.warning("outbox job %s failed (%s)", job_id, type(exc).__name__)
        else:
            await self._delivered(job_id)
        return True

    async def _dispatch(self, kind: str, event_id: str | None, payload: dict[str, Any]) -> None:
        if kind == "EXECUTE_SIGNAL":
            if event_id is None:
                raise ValueError("outbox event is missing")
            await self.context.executor.process_signal(event_id)
            return
        if kind == "TELEGRAM_SIGNAL":
            if event_id is None:
                raise ValueError("outbox event is missing")
            async with self.session_factory() as session:
                event = await session.get(SignalEvent, event_id)
                trade = await session.scalar(select(Trade).where(Trade.event_id == event_id))
                if event is None:
                    raise ValueError("signal event not found")
                message = format_signal_message(event, trade)
            await self.context.notifier.send_message(message)
            return
        if kind == "RECONCILE_TRADE":
            if event_id is None:
                raise ValueError("reconciliation job is missing an event")
            await self.context.reconciler.reconcile_event(event_id)
            return
        if kind == "CANCEL_PENDING":
            await self.context.executor.cancel_pending_entries(payload.get("trade_ids"))
            return
        raise ValueError(f"unsupported outbox job kind: {kind}")

    async def _delivered(self, job_id: str) -> None:
        async with self.session_factory() as session:
            job = await session.get(OutboxJob, job_id)
            if job is not None:
                job.status = "DELIVERED"
                job.locked_until = None
                job.delivered_at = datetime.now(UTC)
                job.last_error = ""
                await session.commit()
        self.last_success = datetime.now(UTC)
        self.last_error = ""

    async def _retry(self, job_id: str, error: str, retry_after: int | None) -> None:
        async with self.session_factory() as session:
            job = await session.get(OutboxJob, job_id)
            if job is None:
                return
            job.locked_until = None
            job.last_error = error[:500]
            if job.attempts >= self.settings.outbox_max_attempts:
                job.status = "FAILED"
            else:
                job.status = "PENDING"
                backoff = retry_after or min(2 ** min(job.attempts, 8), 300)
                job.available_at = datetime.now(UTC) + timedelta(seconds=backoff)
            await session.commit()
        self.last_error = error[:120]

    async def _fail(self, job_id: str, error: str) -> None:
        async with self.session_factory() as session:
            job = await session.get(OutboxJob, job_id)
            if job is not None:
                job.status = "FAILED"
                job.locked_until = None
                job.last_error = error[:500]
                await session.commit()
        self.last_error = error[:120]
