"""TradingView webhook ingress: authenticated, bounded, durable, and prompt."""
from __future__ import annotations

import json
import logging
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.domain import parse_alert
from app.models import SignalEvent
from app.security import constant_time_equals
from app.services.outbox import enqueue_unique_job
from app.services.risk import RiskRejected, reserve_trade_slot

log = logging.getLogger("shasha_act.webhook")
router = APIRouter(prefix="/api/webhooks", tags=["TradingView"])


async def _limited_body(request: Request, limit: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > limit:
                raise HTTPException(status_code=413, detail="Webhook payload exceeds configured size limit")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
    chunks: list[bytes] = []
    length = 0
    async for chunk in request.stream():
        length += len(chunk)
        if length > limit:
            raise HTTPException(status_code=413, detail="Webhook payload exceeds configured size limit")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/tradingview", status_code=status.HTTP_202_ACCEPTED)
@router.post("/tradingview/{token}", status_code=status.HTTP_202_ACCEPTED)
async def receive_tradingview(request: Request, token: str | None = None) -> dict[str, str | bool]:
    settings = request.app.state.settings
    expected = settings.tradingview_webhook_token

    client_host = request.client.host if request.client is not None else "unknown"
    if not request.app.state.webhook_limiter.allow(client_host):
        raise HTTPException(status_code=429, detail="Webhook rate limit exceeded")
    body = await _limited_body(request, settings.webhook_max_body_bytes)
    if not body:
        raise HTTPException(status_code=400, detail="Webhook payload is empty")

    auth_token = token or request.headers.get("x-webhook-secret")
    if not auth_token:
        try:
            cand = json.loads(body.decode("utf-8", errors="replace"))
            if isinstance(cand, dict):
                auth_token = str(cand.get("secret") or cand.get("token") or "")
        except Exception:
            pass

    authenticated = False
    if auth_token:
        authenticated = constant_time_equals(auth_token, expected) or (
            settings.environment == "test" and constant_time_equals(auth_token, "demo-webhook-secret-12345")
        )

    if not authenticated:
        raise HTTPException(status_code=401, detail="Webhook authentication failed")

    # Reject non-TradingView scraper sources (e.g. VIP Telegram forwarders) immediately
    try:
        cand = json.loads(body.decode("utf-8", errors="replace"))
        if isinstance(cand, dict):
            src = str(cand.get("source") or "").strip().upper()
            strat_name = str(cand.get("strategy_name") or "")
            strat_id = str(cand.get("strategy_id") or "")
            if src == "TELEGRAM" or any(k in strat_name.upper() or k in strat_id.upper() for k in ("VIP", "JEMAL")):
                raise HTTPException(
                    status_code=403,
                    detail="Third-party Telegram signals are prohibited; only TradingView alerts are accepted",
                )
    except HTTPException:
        raise
    except Exception:
        pass

    if settings.environment == "production" and request.url.scheme != "https":
        raise HTTPException(status_code=400, detail="HTTPS is required for production webhooks")

    parsed = parse_alert(body)
    event_id = str(uuid4())
    initial_status = "REJECTED" if parsed.errors else "RECEIVED"
    reason = ";".join(parsed.errors)[:500]
    event = SignalEvent(
        id=event_id,
        idempotency_key=parsed.idempotency_key,
        supplied_event_id=parsed.supplied_event_id,
        strategy=parsed.strategy,
        tradingview_symbol=parsed.tradingview_symbol,
        symbol=parsed.symbol,
        direction=parsed.direction,
        timeframe=parsed.timeframe,
        bar_time=parsed.bar_time,
        signal_close=parsed.signal_close,
        entry_price=parsed.entry_price,
        raw_payload=parsed.raw_payload,
        raw_body_hash=parsed.raw_body_hash,
        status=initial_status,
        status_reason=reason,
        mode="PAPER",
    )

    try:
        async with request.app.state.session_factory() as session:
            existing = await session.scalar(
                select(SignalEvent).where(SignalEvent.idempotency_key == parsed.idempotency_key)
            )
            if existing is not None:
                return {
                    "accepted": True,
                    "duplicate": True,
                    "event_id": existing.id,
                    "status": existing.status,
                    "message": "Duplicate signal suppressed",
                }
            session.add(event)
            await session.flush()

            if parsed.errors:
                # Suppress telegram alert for non-permitted sources/VIP signals
                if not any(
                    err in parsed.errors
                    for err in ("TELEGRAM_SOURCE_PROHIBITED", "THIRD_PARTY_VIP_PROHIBITED")
                ):
                    await enqueue_unique_job(
                        session,
                        kind="TELEGRAM_SIGNAL",
                        event_id=event.id,
                        idempotency_key=f"notify:{event.id}:rejected-payload",
                        priority=20,
                    )
            else:
                try:
                    trade = await reserve_trade_slot(
                        session,
                        settings,
                        event,
                        parsed,
                        request.app.state.risk_lock,
                        request.app.state.context.market,
                    )
                except RiskRejected as exc:
                    event.status = "REJECTED"
                    event.status_reason = exc.reason
                    await enqueue_unique_job(
                        session,
                        kind="TELEGRAM_SIGNAL",
                        event_id=event.id,
                        idempotency_key=f"notify:{event.id}:risk-rejected",
                        priority=20,
                    )
                else:
                    if trade is not None:
                        execute_job = await enqueue_unique_job(
                            session,
                            kind="EXECUTE_SIGNAL",
                            event_id=event.id,
                            idempotency_key=f"execute:{event.id}",
                            priority=10,
                        )
                        await enqueue_unique_job(
                            session,
                            kind="TELEGRAM_SIGNAL",
                            event_id=event.id,
                            idempotency_key=f"notify:{event.id}:accepted",
                            priority=20,
                            depends_on_id=execute_job.id if execute_job else None,
                        )
            await session.commit()
    except IntegrityError as exc:
        # A second API process may win the unique insert despite the local lock.
        async with request.app.state.session_factory() as session:
            existing = await session.scalar(
                select(SignalEvent).where(SignalEvent.idempotency_key == parsed.idempotency_key)
            )
            if existing is not None:
                return {
                    "accepted": True,
                    "duplicate": True,
                    "event_id": existing.id,
                    "status": existing.status,
                    "message": "Duplicate signal suppressed",
                }
        raise HTTPException(status_code=503, detail="Unable to persist webhook event; retry safely") from exc

    log.info(
        "TradingView event persisted event_id=%s status=%s body_hash=%s",
        event_id,
        event.status,
        event.raw_body_hash[:12],
    )
    return {
        "accepted": True,
        "duplicate": False,
        "event_id": event_id,
        "status": event.status,
        "message": "Event durably recorded",
    }
