"""Idempotency + duplicate detection for TradingView webhook events.

Layers of protection (all persistent — never in-memory only):
1. event_id uniqueness           (webhook_events.event_id UNIQUE)
2. request fingerprint           (raw body hash stored)
3. event fingerprint             (deterministic hash of immutable fields, UNIQUE)
4. normalized symbol fingerprint (canonical identity for position dupes)
5. action check + time-window checks (stale events rejected)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppSettings
from app.core.security import event_fingerprint, request_hash
from app.models.entities import WebhookEvent


@dataclass(frozen=True)
class EventIdentity:
    event_id: str
    fingerprint: str
    body_hash: str
    provided_event_id: bool


class IdempotencyService:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def identify(self, payload: dict, raw_body: bytes) -> EventIdentity:
        provided = str(payload.get("event_id") or payload.get("eventId") or "").strip()
        fingerprint = event_fingerprint(payload)
        event_id = provided or f"auto-{fingerprint[:32]}"
        return EventIdentity(
            event_id=event_id,
            fingerprint=fingerprint,
            body_hash=request_hash(raw_body),
            provided_event_id=bool(provided),
        )

    def is_stale(self, payload: dict) -> bool:
        ts_raw = payload.get("timestamp")
        if not ts_raw:
            return False
        try:
            if isinstance(ts_raw, (int, float)):
                ts = datetime.fromtimestamp(float(ts_raw) / (1000 if float(ts_raw) > 1e12 else 1), tz=UTC)
            else:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        except (ValueError, OSError, OverflowError):
            return False
        now = datetime.now(UTC)
        age = abs((now - ts).total_seconds())
        return age > self.settings.webhook_replay_window_seconds

    async def exists(self, session: AsyncSession, identity: EventIdentity) -> bool:
        result = await session.execute(
            select(WebhookEvent.id).where(
                (WebhookEvent.event_id == identity.event_id)
                | (WebhookEvent.fingerprint == identity.fingerprint)
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def try_reserve_identity(self, session: AsyncSession, event: WebhookEvent) -> bool:
        """INSERT with UNIQUE constraints as the atomicity guarantee.

        Returns False when the event was already processed (duplicate).
        """
        session.add(event)
        try:
            await session.flush()
            return True
        except IntegrityError:
            await session.rollback()
            return False
