"""TradingView webhook endpoint — secure ingestion.

Flow: authenticate -> size/rate limits -> JSON validation -> store raw event
-> normalize symbol -> create internal signal -> enqueue async -> fast response.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppSettings, get_settings
from app.core.errors import RejectReason
from app.core.events import event_bus
from app.core.logging import TradeEvent, correlation_id_var, log_event
from app.core.security import webhook_authenticated
from app.db.session import get_db_session, get_session_factory
from app.models.entities import AuditLog, SystemEvent, WebhookEvent
from app.models.enums import ProcessingStatus
from app.schemas.dto import TradingViewAlert, WebhookAck
from app.services.runtime import AppContext

log = logging.getLogger("trading.webhook")
router = APIRouter(prefix="/api")

_rate_buckets: dict[str, deque[float]] = {}

PROCESSING_DELAY_S = 0.05  # let the HTTP response flush before heavy work


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx  # type: ignore[no-any-return]


def _client_ip(request: Request) -> str:
    """Client IP for rate limiting. Honors X-Forwarded-For's first hop when the
    app is behind a reverse proxy you control (never trust XFF from the open
    internet — without a proxy, request.client.host is authoritative)."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"


def _rate_limited(ip: str, limit_per_minute: int) -> bool:
    now = time.time()
    if len(_rate_buckets) > 10_000:  # bounded memory under source-IP floods
        while len(_rate_buckets) > 5_000:
            _rate_buckets.pop(next(iter(_rate_buckets)))
    bucket = _rate_buckets.setdefault(ip, deque())
    while bucket and now - bucket[0] > 60:
        bucket.popleft()
    if len(bucket) >= limit_per_minute:
        return True
    bucket.append(now)
    return False


async def _process_event(ctx: AppContext, event_id: str) -> None:
    """Async processing after HTTP 202 — idempotent and crash-safe."""
    from sqlalchemy import select

    from app.models.entities import Signal

    await asyncio.sleep(PROCESSING_DELAY_S)
    factory = get_session_factory()
    session = factory()
    try:
        event = (
            await session.execute(select(WebhookEvent).where(WebhookEvent.event_id == event_id))
        ).scalar_one_or_none()
        if event is None:
            return
        signal = (
            await session.execute(select(Signal).where(Signal.event_id == event_id))
        ).scalar_one_or_none()
        if signal is None:
            return
        started = time.perf_counter()
        outcome = await ctx.pipeline.process_signal(session, signal.id)
        
        # Ensure changes from process_signal (even after rollbacks) are persisted
        session.add(signal)
        signal.processing_status = outcome.status
        if outcome.reason:
            signal.rejection_reason = str(outcome.reason)[:250]
        await session.commit()
        
        # Now it is safe to refresh and extract data
        await session.refresh(signal)
        await session.refresh(event)
        
        # Extract fields after refresh
        signal_data = {
            "event_id": event_id,
            "status": outcome.status,
            "reason": outcome.reason,
            "position_id": outcome.position_id,
            "order_id": outcome.order_id,
            "tradingview_raw_symbol": signal.tradingview_raw_symbol,
            "canonical_symbol": signal.canonical_symbol,
            "shark_symbol": signal.shark_symbol,
            "strategy_id": signal.strategy_id,
            "action": signal.action,
        }
        
        await session.commit()
        latency = int((time.perf_counter() - started) * 1000)
        event.latency_ms = latency
        status_map = {
            "EXECUTED": ProcessingStatus.EXECUTED,
            "DRY_RUN": ProcessingStatus.EXECUTED,
            "DUPLICATE": ProcessingStatus.DUPLICATE,
            "ERROR": ProcessingStatus.ERROR,
        }
        event.status = status_map.get(outcome.status, ProcessingStatus.REJECTED).value
        if outcome.reason:
            event.rejection_reason = str(outcome.reason)[:250]
        await session.commit()
        
        event_bus.publish("signal.updated", signal_data)
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        log.exception("async processing failed with traceback")
        await session.rollback()
        log_event(log, TradeEvent.SYSTEM_ERROR, f"async processing failed: {exc}", level=logging.ERROR,
                  event_id=event_id)
    finally:
        await session.close()


@router.post("/webhooks/tradingview", response_model=WebhookAck, status_code=202)
async def receive_tradingview_alert(
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    settings: AppSettings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> WebhookAck:
    correlation_id = uuid.uuid4().hex[:16]
    correlation_id_var.set(correlation_id)
    client_ip = _client_ip(request)

    # ---- size limit ------------------------------------------------------
    raw_body = await request.body()
    if len(raw_body) > settings.webhook_max_body_bytes:
        raise HTTPException(status_code=413, detail="payload too large")

    # ---- rate limit ------------------------------------------------------
    if _rate_limited(client_ip, settings.webhook_rate_limit_per_minute):
        raise HTTPException(status_code=429, detail="rate limit exceeded")

    # ---- authenticate (header or payload secret) --------------------------
    header_secret = request.headers.get("x-webhook-secret") or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=RejectReason.INVALID_PAYLOAD.value) from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail=RejectReason.INVALID_PAYLOAD.value)
    payload_secret = payload.get("secret") or payload.get("webhook_secret")
    provided = header_secret or (str(payload_secret) if payload_secret else None)
    if not webhook_authenticated(provided, settings.webhook_secret):
        log_event(log, TradeEvent.WEBHOOK_REJECTED, "webhook auth failed", level=logging.WARNING,
                  ip=client_ip)
        session.add(SystemEvent(event="WEBHOOK_REJECTED", level="WARNING",
                                message="authentication failed", correlation_id=correlation_id,
                                detail={"ip": client_ip}))
        raise HTTPException(status_code=401, detail=RejectReason.INVALID_SIGNATURE.value)

    # ---- schema validation ------------------------------------------------
    try:
        alert = TradingViewAlert.model_validate(payload)
    except Exception as exc:  # noqa: BLE001
        session.add(SystemEvent(event="WEBHOOK_REJECTED", level="WARNING",
                                message=f"invalid payload: {exc}", correlation_id=correlation_id))
        raise HTTPException(status_code=400, detail=RejectReason.INVALID_PAYLOAD.value) from exc

        # strip auth material from persisted payload (never store secrets)
    stored_payload = {k: v for k, v in payload.items() if k not in ("secret", "webhook_secret")}
    identity = ctx.idempotency.identify(stored_payload, raw_body)

    if ctx.idempotency.is_stale(stored_payload):
        session.add(SystemEvent(event="WEBHOOK_REJECTED", message="stale event timestamp",
                                correlation_id=correlation_id, detail={"event_id": identity.event_id}))
        raise HTTPException(status_code=400, detail=RejectReason.STALE_EVENT.value)

    # ---- persist raw event (UNIQUE event_id + fingerprint = idempotency) ---
    event_ts = None
    ts_raw = stored_payload.get("timestamp")
    if ts_raw:
        try:
            if isinstance(ts_raw, (int, float)):
                event_ts = datetime.fromtimestamp(float(ts_raw) / (1000 if float(ts_raw) > 1e12 else 1),
                                                  tz=UTC)
            else:
                event_ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        except (ValueError, OSError, OverflowError):
            event_ts = None

    webhook_event = WebhookEvent(
        event_id=identity.event_id,
        fingerprint=identity.fingerprint,
        correlation_id=correlation_id,
        source=str(stored_payload.get("source") or "TRADINGVIEW"),
        raw_payload=stored_payload,
        raw_body_hash=identity.body_hash,
        received_at=datetime.now(UTC),
        event_timestamp=event_ts,
        status=ProcessingStatus.RECEIVED.value,
    )

    from sqlalchemy.exc import IntegrityError

    session.add(webhook_event)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        log_event(log, TradeEvent.DUPLICATE_DETECTED, "duplicate webhook event suppressed",
                  event_id=identity.event_id)
        return WebhookAck(
            status="DUPLICATE",
            event_id=identity.event_id,
            correlation_id=correlation_id,
            message="event already processed — exactly-once guaranteed",
            tradingview_raw_symbol=str(stored_payload.get("symbol") or ""),
        )

    log_event(
        log, TradeEvent.WEBHOOK_RECEIVED, "webhook accepted",
        event_id=identity.event_id,
        tradingview_raw_symbol=str(stored_payload.get("symbol") or ""),
        strategy_id=str(stored_payload.get("strategy_id") or ""),
        action=str(stored_payload.get("action") or ""),
        correlation_id=correlation_id,
    )

    # ---- normalize + create signal (failures become REJECTED signals) ------
    try:
        signal = await ctx.pipeline.create_signal(session, webhook_event, stored_payload)
        await session.flush()
        ack = WebhookAck(
            status="ACCEPTED",
            event_id=identity.event_id,
            correlation_id=correlation_id,
            message="event accepted for async processing",
            tradingview_raw_symbol=signal.tradingview_raw_symbol,
            canonical_symbol=signal.canonical_symbol,
            shark_symbol=signal.shark_symbol,
        )
    except Exception as exc:  # noqa: BLE001 — symbol resolution failures etc.
        from app.core.errors import SymbolError

        reason = exc.reason if isinstance(exc, SymbolError) else RejectReason.INVALID_PAYLOAD.value
        webhook_event.status = ProcessingStatus.REJECTED.value
        webhook_event.rejection_reason = str(reason)
        session.add(SystemEvent(event="SIGNAL_REJECTED", message=str(exc), correlation_id=correlation_id,
                                detail={"reason": str(reason)},
                                tradingview_raw_symbol=str(stored_payload.get("symbol") or "")))
        await session.commit()
        log_event(log, TradeEvent.SIGNAL_REJECTED, str(exc), reason=str(reason),
                  event_id=identity.event_id)
        return WebhookAck(
            status="REJECTED",
            event_id=identity.event_id,
            correlation_id=correlation_id,
            message=str(exc),
            tradingview_raw_symbol=str(stored_payload.get("symbol") or ""),
        )

    session.add(AuditLog(action="WEBHOOK_RECEIVED", entity="webhook_event",
                         entity_id=identity.event_id, correlation_id=correlation_id,
                         detail={"symbol": stored_payload.get("symbol")}))
    await session.commit()

    # ---- execution: inline (deterministic tests) or async after response ---
    if settings.inline_processing:
        await _process_event(ctx, identity.event_id)
    else:
        loop = asyncio.get_running_loop()
        loop.create_task(_process_event(ctx, identity.event_id))

    event_bus.publish("webhook.received", {
        "event_id": identity.event_id,
        "strategy_id": alert.strategy_id or "",
        "tradingview_raw_symbol": signal.tradingview_raw_symbol,
        "canonical_symbol": signal.canonical_symbol,
        "shark_symbol": signal.shark_symbol,
        "action": signal.action,
        "status": "RECEIVED",
    })
    return ack


@router.get("/webhooks")
async def list_webhooks(session: AsyncSession = Depends(get_db_session)) -> list[dict]:
    from sqlalchemy import select, desc
    rows = (await session.execute(
        select(WebhookEvent).order_by(desc(WebhookEvent.received_at)).limit(100)
    )).scalars()
    return [
        {
            "id": r.id,
            "event_id": r.event_id,
            "status": r.status,
            "rejection_reason": r.rejection_reason,
            "received_at": r.received_at.isoformat() if r.received_at else None,
            "raw_payload": r.raw_payload,
        }
        for r in rows
    ]


@router.post("/webhooks/{event_id}/share")
async def share_webhook(event_id: str, ctx: AppContext = Depends(get_ctx), session: AsyncSession = Depends(get_db_session)) -> dict:
    from sqlalchemy import select
    event = (await session.execute(select(WebhookEvent).where(WebhookEvent.event_id == event_id))).scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Webhook not found")
    
    import json
    payload_str = json.dumps(event.raw_payload, indent=2)
    message = f"📢 Webhook Received!\n\nEvent ID: {event.event_id}\nStatus: {event.status}\n\nPayload:\n{payload_str}"
    
    await ctx.notifications.notify("webhook_shared", message)
    return {"status": "shared"}
