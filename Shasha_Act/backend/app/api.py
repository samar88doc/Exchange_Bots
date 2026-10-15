"""Authenticated dashboard API, control plane, history, and service health."""
from __future__ import annotations

import csv
import io
import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import (
    DEFAULT_DISCLAIMER,
    SUBSCRIPTION_PLANS,
    authenticate_user,
    current_user,
    ensure_subscription,
    get_user_by_username,
    issue_session,
    require_dashboard_auth,
    require_mutation_allowed,
    revoke_session,
    role_at_least,
    subscription_state,
    user_payload,
    user_subscription_state,
    writable_user,
)
from app.config import Settings
from app.db import session_dependency
from app.domain import normalize_interval
from app.models import (
    AuditEvent,
    DashboardUser,
    Fill,
    Order,
    OutboxJob,
    RiskControl,
    SentimentHistory,
    SignalEvent,
    Trade,
)
from app.schemas import (
    CancelPendingRequest,
    CloseTradeRequest,
    DashboardAuthToggleRequest,
    LiveActivationRequest,
    LoginRequest,
    SettingsUpdate,
    SubscriptionUpdate,
    TradeRiskUpdate,
    TradingPauseRequest,
    UserCreateRequest,
    UserUpdateRequest,
)
from app.security import hash_password, redact_text
from app.services.pnl import long_unrealized_pnl, realized_pnl_breakdown_for_day
from app.services.risk import ACTIVE_TRADE_STATES, ensure_risk_control

PROJECT_ROOT = Path(__file__).resolve().parents[2]
public_router = APIRouter(prefix="/api", tags=["Status"])
auth_router = APIRouter(prefix="/api/auth", tags=["Authentication"])

router = APIRouter(
    prefix="/api",
    tags=["Dashboard"],
    dependencies=[Depends(require_dashboard_auth), Depends(require_mutation_allowed)],
)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    return parsed if parsed.is_finite() else None


def _str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        rendered = format(value, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return rendered or "0"
    return str(value)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _local_day_bounds(timezone_name: str, requested: date | None = None) -> tuple[date, datetime, datetime]:
    zone = ZoneInfo(timezone_name)
    local_day = requested or datetime.now(zone).date()
    start_local = datetime.combine(local_day, time.min, tzinfo=zone)
    return local_day, start_local.astimezone(UTC), (start_local + timedelta(days=1)).astimezone(UTC)


def _audit(
    *,
    action: str,
    entity_type: str,
    entity_id: str,
    details: dict[str, Any] | None = None,
    actor: str = "dashboard",
) -> AuditEvent:
    return AuditEvent(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details or {},
    )


def _require_role(request: Request, minimum: str) -> None:
    """Enforce an operator role on a mutating dashboard route.

    A resolved session must satisfy the minimum role. The server automation token — and a
    deployment that has deliberately set ``DASHBOARD_AUTH_ENABLED=false`` — carries no operator
    identity and keeps owner-level access, which is the same convention the account endpoints
    already use.
    """
    user = getattr(request.state, "user", None)
    if not isinstance(user, DashboardUser):
        return
    if not role_at_least(user.role, minimum):
        raise HTTPException(
            status_code=403,
            detail=f"{minimum} role required for this action",
        )


def _actor(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if user is not None and getattr(user, "username", None):
        return str(user.username)[:64]
    method = getattr(request.state, "auth_method", "dashboard")
    return "server-token" if method == "SERVER_TOKEN" else "dashboard"


def _plain(value: Any) -> str:
    """Render a Decimal percentage/quantity without trailing storage zeros (2.000000000000 → 2)."""
    return _money(value) or ""


def _money(value: Any) -> str | None:
    """Format a Decimal money/price value without storage artefacts (999.000000000000 → 999)."""
    parsed = _decimal(value)
    if parsed is None:
        return None
    normalized = parsed.normalize()
    if normalized == normalized.to_integral_value():
        normalized = normalized.quantize(Decimal(1))
    return format(normalized, "f")


def _risk_reward(trade: Trade) -> str | None:
    """Reward:risk of the planned Target 1 against the initial stop, as a decimal string."""
    entry = trade.actual_entry or trade.planned_entry
    stop = trade.initial_stop_price or trade.stop_price
    target = trade.target1_price
    if entry is None or stop is None or target is None:
        return None
    risk = entry - stop
    reward = target - entry
    if risk <= 0 or reward <= 0:
        return None
    return _str((reward / risk).quantize(Decimal("0.01")))


def _trade_payload(trade: Trade, ticker: Any | None, stale_after: int) -> dict[str, Any]:
    fresh_ticker = ticker if ticker is not None and not ticker.is_stale(stale_after) else None
    fresh = fresh_ticker is not None
    last_price = fresh_ticker.last_price if fresh_ticker is not None else None
    unrealized = None
    roi = None
    realized = trade.net_realized_pnl or Decimal("0")
    if last_price is not None and trade.actual_entry is not None and trade.remaining_quantity is not None:
        unrealized = long_unrealized_pnl(trade.actual_entry, last_price, trade.remaining_quantity)
        if trade.margin > 0:
            roi = unrealized / trade.margin * Decimal("100")
    net_profit = (realized + unrealized) if unrealized is not None else realized
    net_roi = (net_profit / trade.margin * Decimal("100")) if trade.margin > 0 else None
    return {
        "id": trade.id,
        "event_id": trade.event_id,
        "mode": trade.mode,
        "symbol": trade.symbol,
        "tradingview_symbol": trade.tradingview_symbol,
        "direction": trade.direction,
        "status": trade.status,
        "status_reason": redact_text(trade.status_reason or "")[:500],
        "planned_entry": _str(trade.planned_entry),
        "signal_close": _str(trade.signal_close),
        "actual_entry": _str(trade.actual_entry),
        "quantity": _str(trade.quantity),
        "remaining_quantity": _str(trade.remaining_quantity),
        "target1_quantity": _str(trade.target1_quantity),
        "target2_quantity": _str(trade.target2_quantity),
        "target3_quantity": _str(trade.target3_quantity),
        "margin": _str(trade.margin),
        "notional": _str(trade.notional),
        "leverage": trade.leverage,
        "last_price": _str(last_price),
        "price_source": fresh_ticker.source if fresh_ticker is not None else None,
        "price_updated_at": _iso(fresh_ticker.received_at) if fresh_ticker is not None else None,
        "price_stale": not fresh,
        "realized_pnl": _str(trade.net_realized_pnl),
        "gross_realized_pnl": _str(trade.gross_realized_pnl),
        "net_realized_pnl": _str(trade.net_realized_pnl),
        "unrealized_pnl": _str(unrealized),
        "net_profit_pnl": _str(net_profit),
        "roi_percent_on_margin": _str(roi),
        "net_profit_roi_percent": _str(net_roi),
        "fees_paid": _str(trade.fees_paid),
        "fee_data_available": trade.fee_data_available,
        "funding_paid": _str(trade.funding_paid),
        "funding_data_available": trade.funding_data_available,
        "stop_price": _str(trade.stop_price),
        "initial_stop_price": _str(trade.initial_stop_price),
        "target1_price": _str(trade.target1_price),
        "target2_price": _str(trade.target2_price),
        "target3_price": _str(trade.target3_price),
        "target_progress": trade.target_progress,
        "partial_closed": trade.partial_closed,
        "partial_close_percent": _str(trade.partial_close_percent),
        "trailing_enabled": trade.trailing_enabled,
        "trailing_active": trade.trailing_active,
        "trailing_stop_percent": _str(trade.trailing_stop_percent),
        "trailing_high_price": _str(trade.trailing_high_price),
        "secondary_targets_enabled": trade.secondary_targets_enabled,
        "entry_price_source": trade.entry_price_source or "",
        "entry_candle_time": _iso(trade.entry_candle_time),
        "price_tick": _str(trade.price_tick),
        "stop_stage": trade.stop_stage,
        "risk_reward": _risk_reward(trade),
        "protection_state": trade.protection_state,
        "liquidation_price": _str(
            trade.liquidation_price
            if trade.liquidation_price is not None
            else (
                max(
                    trade.actual_entry * (Decimal("1") - (Decimal("1") / Decimal(str(trade.leverage))) + Decimal("0.005")),
                    Decimal("0"),
                )
                if trade.actual_entry is not None and (trade.leverage or 0) > 0
                else None
            )
        ),
        "liquidation_price_source": (
            "Shark position response"
            if trade.mode == "LIVE" and trade.liquidation_price is not None
            else ("Simulated isolated liquidation" if trade.actual_entry is not None else None)
        ),
        "entry_order_id": trade.entry_order_id,
        "exchange_position_id": trade.exchange_position_id,
        "opened_at": _iso(trade.opened_at),
        "closed_at": _iso(trade.closed_at),
        "exit_price": _str(trade.exit_price),
        "created_at": _iso(trade.created_at),
        "updated_at": _iso(trade.updated_at),
    }


async def _wallet_snapshot(request: Request, session: AsyncSession, mode: str) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    if mode == "PAPER":
        rows = await session.execute(
            select(Trade.net_realized_pnl, Trade.margin, Trade.status, Trade.unrealized_pnl).where(
                Trade.mode == "PAPER"
            )
        )
        realized = sum((row[0] or Decimal("0") for row in rows.all()), Decimal("0"))
        active_rows = await session.execute(
            select(Trade.margin, Trade.status).where(
                Trade.mode == "PAPER", Trade.status.in_(ACTIVE_TRADE_STATES)
            )
        )
        locked = sum(
            (margin or Decimal("0") for margin, _ in active_rows.all()), Decimal("0")
        )
        equity = settings.paper_initial_balance + realized
        return {
            "status": "AVAILABLE",
            "source": "PAPER_VIRTUAL_LEDGER",
            "balance": _str(equity),
            "available": _str(equity - locked),
            "locked_margin": _str(locked),
            "currency": "USDT",
            "note": "Simulated ledger estimate; not an exchange wallet balance.",
        }

    if not settings.shark_credentials_configured:
        return {
            "status": "UNAVAILABLE",
            "source": "SHARK_PRIVATE_API",
            "balance": None,
            "available": None,
            "locked_margin": None,
            "currency": settings.shark_margin_asset,
            "note": "Shark private API credentials are not configured.",
        }
    now = datetime.now(UTC)
    cached = getattr(request.app.state, "wallet_cache", None)
    if cached is not None and (now - cached[0]).total_seconds() < 10:
        return cached[1]
    try:
        response = await request.app.state.context.shark.futures_wallet(settings.shark_margin_asset)
        data = response.get("data", response) if isinstance(response, dict) else None
        if isinstance(data, list):
            data = next(
                (item for item in data if isinstance(item, dict) and str(item.get("marginAsset") or "").upper() == settings.shark_margin_asset.upper()),
                None,
            )
        if isinstance(data, dict) and isinstance(data.get("wallets"), list):
            data = next(
                (item for item in data["wallets"] if isinstance(item, dict) and str(item.get("marginAsset") or "").upper() == settings.shark_margin_asset.upper()),
                None,
            )
        if not isinstance(data, dict) or str(data.get("marginAsset") or "").upper() != settings.shark_margin_asset.upper():
            raise ValueError("wallet response shape unavailable")
        result = {
            "status": "AVAILABLE",
            "source": "SHARK_PRIVATE_API",
            "balance": _str(_decimal(data.get("walletBalance"))),
            "available": _str(_decimal(data.get("withdrawableBalance"))),
            "locked_margin": None,
            "currency": settings.shark_margin_asset,
            "note": "Values reflect the latest Shark wallet response.",
        }
        request.app.state.wallet_cache = (now, result)
        return result
    except Exception:
        return {
            "status": "UNAVAILABLE",
            "source": "SHARK_PRIVATE_API",
            "balance": None,
            "available": None,
            "locked_margin": None,
            "currency": settings.shark_margin_asset,
            "note": "Shark wallet data is unavailable; no value is inferred.",
        }


@public_router.get("/public/config")
async def public_config(request: Request) -> dict[str, Any]:
    """Unauthenticated login-screen metadata: no secrets, no internal state beyond this."""
    settings: Settings = request.app.state.settings
    return {
        "app_name": settings.app_name,
        "environment": settings.environment,
        "auth_enabled": getattr(settings, "dashboard_auth_enabled", True),
        "username_signin_available": True,
        "server_token_signin_available": bool(settings.dashboard_api_token),
        "disclaimer_required": settings.require_disclaimer_ack,
        "disclaimer": DEFAULT_DISCLAIMER,
        "subscription": {
            "plan_name": settings.subscription_plan_name,
            "fee": _money(settings.subscription_fee),
            "currency": settings.subscription_currency,
            "billing_cycle": settings.subscription_billing_cycle,
            "enforced": settings.enforce_subscription,
        },
    }


@auth_router.post("/login")
async def login(
    body: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    user = await authenticate_user(session, settings, body.username, body.password)
    token, record = await issue_session(
        session,
        settings,
        user,
        user_agent=request.headers.get("user-agent", ""),
    )
    subscription = await ensure_subscription(session, settings)
    await session.commit()
    request.state.user = user
    return {
        "authenticated": True,
        "session_token": token,
        "session_expires_at": _iso(record.expires_at),
        "user": user_payload(user),
        "subscription": subscription_state(subscription),
        "disclaimer": {
            "required": settings.require_disclaimer_ack,
            "accepted_at": user_payload(user)["disclaimer_accepted_at"],
            "text": DEFAULT_DISCLAIMER,
        },
        "permissions": {
            "can_manage_users": role_at_least(user.role, "ADMIN"),
            "can_change_settings": role_at_least(user.role, "OPERATOR"),
            "can_trade": role_at_least(user.role, "OPERATOR"),
        },
    }


@auth_router.post("/logout")
async def logout(request: Request, session: AsyncSession = Depends(session_dependency)) -> dict[str, Any]:
    token = request.headers.get("x-session-token", "")
    actor = _actor(request)
    revoked = await revoke_session(session, token, actor=actor)
    return {"revoked": revoked, "message": "Session revoked" if revoked else "No active session"}


@router.get("/auth/me")
async def auth_me(
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    user = await current_user(request)
    subscription = await ensure_subscription(session, settings)
    disclaimer_required = bool(settings.require_disclaimer_ack)
    if user is None:
        return {
            "auth_method": getattr(request.state, "auth_method", "UNKNOWN"),
            "user": None,
            "subscription": subscription_state(subscription),
            "disclaimer": {"required": False, "accepted_at": None, "text": DEFAULT_DISCLAIMER},
            "permissions": {"can_manage_users": True, "can_change_settings": True, "can_trade": True},
        }
    return {
        "auth_method": getattr(request.state, "auth_method", "SESSION"),
        "user": user_payload(user),
        "subscription": subscription_state(subscription),
        "disclaimer": {
            "required": disclaimer_required,
            "accepted_at": user_payload(user)["disclaimer_accepted_at"],
            "text": DEFAULT_DISCLAIMER,
        },
        "permissions": {
            "can_manage_users": role_at_least(user.role, "ADMIN"),
            "can_change_settings": role_at_least(user.role, "OPERATOR"),
            "can_trade": role_at_least(user.role, "OPERATOR"),
        },
    }


@router.post("/auth/disclaimer")
async def accept_disclaimer(
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    user = await writable_user(session, request)
    if user is None:
        raise HTTPException(status_code=400, detail="Disclaimer acknowledgement requires a sign-in session")
    user.disclaimer_accepted_at = datetime.now(UTC)
    session.add(
        _audit(
            action="DISCLAIMER_ACCEPTED",
            entity_type="user",
            entity_id=user.id,
            details={},
            actor=_actor(request),
        )
    )
    await session.flush()
    return {"accepted": True, "accepted_at": _iso(user.disclaimer_accepted_at)}


@router.get("/subscription")
async def read_subscription(
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    subscription = await ensure_subscription(session, settings)
    user = await current_user(request)
    return {
        "service": subscription_state(subscription),
        "user": user_subscription_state(user) if user is not None else None,
        "enforced": settings.enforce_subscription,
        "plans": list(SUBSCRIPTION_PLANS),
        "billing_cycles": ["MONTHLY", "QUARTERLY", "YEARLY", "LIFETIME"],
    }


@router.patch("/subscription")
async def update_subscription(
    body: SubscriptionUpdate,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    user = await current_user(request)
    if user is not None and not role_at_least(user.role, "ADMIN"):
        raise HTTPException(status_code=403, detail="Admin role required to change the subscription")
    subscription = await ensure_subscription(session, settings)
    values = body.model_dump(exclude_unset=True, exclude_none=True)
    if not values:
        raise HTTPException(status_code=422, detail="No subscription fields were supplied")
    previous = {key: str(getattr(subscription, key)) for key in values}
    for key, value in values.items():
        setattr(subscription, key, value)
    if subscription.status not in {"ACTIVE", "SUSPENDED", "CANCELLED", "TRIAL"}:
        raise HTTPException(status_code=422, detail="Unsupported subscription status")
    session.add(
        _audit(
            action="SUBSCRIPTION_UPDATED",
            entity_type="subscription",
            entity_id="1",
            details={"changed": {k: str(v) for k, v in values.items()}, "previous": previous},
            actor=_actor(request),
        )
    )
    await session.flush()
    return {"updated": True, "service": subscription_state(subscription)}


@router.get("/users")
async def list_users(
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    user = await current_user(request)
    if user is not None and not role_at_least(user.role, "ADMIN"):
        raise HTTPException(status_code=403, detail="Admin role required")
    rows = list((await session.scalars(select(DashboardUser).order_by(DashboardUser.created_at))).all())
    return {"items": [user_payload(item) for item in rows], "count": len(rows)}


@router.post("/users")
async def create_user(
    body: UserCreateRequest,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    actor = await writable_user(session, request)
    if actor is not None and not role_at_least(actor.role, "ADMIN"):
        raise HTTPException(status_code=403, detail="Admin role required")
    username = body.username.strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{3,64}", username):
        raise HTTPException(status_code=422, detail="Username must be 3-64 characters [a-z0-9._-]")
    if await get_user_by_username(session, username) is not None:
        raise HTTPException(status_code=409, detail="That username already exists")
    settings: Settings = request.app.state.settings
    created = DashboardUser(
        id=str(uuid4()),
        username=username,
        display_name=(body.display_name or body.username).strip()[:96],
        password_hash=hash_password(body.password, settings.password_hash_iterations),
        role=body.role,
        is_active=body.is_active,
        subscription_plan=body.subscription_plan or settings.subscription_plan_name,
        subscription_fee=body.subscription_fee if body.subscription_fee is not None else settings.subscription_fee,
        subscription_currency=body.subscription_currency or settings.subscription_currency,
        subscription_cycle=body.subscription_cycle,
        subscription_started_at=datetime.now(UTC),
        subscription_expires_at=body.subscription_expires_at,
    )
    session.add(created)
    session.add(
        _audit(
            action="USER_CREATED",
            entity_type="user",
            entity_id=created.id,
            details={"username": username, "role": body.role},
            actor=_actor(request),
        )
    )
    await session.flush()
    return {"created": True, "user": user_payload(created)}


@router.patch("/users/{user_id}")
async def update_user(
    user_id: str,
    body: UserUpdateRequest,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    actor = await writable_user(session, request)
    if actor is not None and not role_at_least(actor.role, "ADMIN"):
        raise HTTPException(status_code=403, detail="Admin role required")
    target = await session.get(DashboardUser, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    settings: Settings = request.app.state.settings
    values = body.model_dump(exclude_unset=True, exclude_none=True)
    if "password" in values:
        target.password_hash = hash_password(str(values.pop("password")), settings.password_hash_iterations)
        target.failed_login_count = 0
        target.locked_until = None
    if "is_active" in values and target.username == (actor.username if actor else ""):
        if values["is_active"] is False:
            raise HTTPException(status_code=422, detail="You cannot disable your own account")
    if "role" in values and not role_at_least(values["role"], "VIEWER"):
        raise HTTPException(status_code=422, detail="Unsupported role")
    previous = {key: str(getattr(target, key)) for key in values}
    for key, value in values.items():
        setattr(target, key, value)
    session.add(
        _audit(
            action="USER_UPDATED",
            entity_type="user",
            entity_id=target.id,
            details={"changed": sorted(values), "previous": previous},
            actor=_actor(request),
        )
    )
    await session.flush()
    return {"updated": True, "user": user_payload(target)}


@public_router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    context = request.app.state.context
    database = "OK"
    queue: dict[str, int | None] = {"pending": None, "processing": None, "failed": None}
    try:
        async with request.app.state.session_factory() as session:
            await session.scalar(select(func.count()).select_from(RiskControl))
            counts = await session.execute(
                select(OutboxJob.status, func.count()).group_by(OutboxJob.status)
            )
            totals = {str(status_value): int(count) for status_value, count in counts.all()}
        queue = {
            "pending": totals.get("PENDING", 0),
            "processing": totals.get("PROCESSING", 0),
            "failed": totals.get("FAILED", 0),
        }
    except Exception:
        database = "UNAVAILABLE"
    worker_active = bool(context.worker._task and not context.worker._task.done()) if request.app.state.settings.run_workers else False
    market_status = context.market.health()
    return {
        "status": "OK" if database == "OK" else "DEGRADED",
        "timestamp": datetime.now(UTC).isoformat(),
        "components": {
            "database": database,
            "worker": "RUNNING" if worker_active else ("DISABLED" if not request.app.state.settings.run_workers else "STOPPED"),
            "worker_last_error": redact_text(context.worker.last_error)[:120] or None,
            "market_data": market_status,
            "websocket": "CONNECTED" if context.market.websocket_connected else "DISCONNECTED",
            "reconciliation": "OK" if context.reconciler.last_error == "" else "DEGRADED",
            "private_api": (
                "NOT_CONFIGURED" if not request.app.state.settings.shark_credentials_configured
                else "OK" if context.shark.last_private_ok is True
                else "ERROR" if context.shark.last_private_ok is False
                else "NOT_CHECKED"
            ),
            "telegram": "CONFIGURED" if request.app.state.settings.telegram_configured else "NOT_CONFIGURED",
        },
        "outbox": queue,
        "market_data_note": "Health reflects observed data; a disconnected WebSocket can fall back to REST polling.",
    }


@public_router.get("/ready")
async def readiness(request: Request) -> dict[str, Any]:
    try:
        async with request.app.state.session_factory() as session:
            await session.scalar(select(func.count()).select_from(RiskControl))
        return {"ready": True}
    except Exception:
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)  # type: ignore[return-value]


@router.get("/settings")
async def read_settings(
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    control = await ensure_risk_control(session, settings)
    live_contracts = request.app.state.context.market.resolver_contracts
    return {
        "mode": control.trading_mode,
        "effective_mode": (
            control.trading_mode
            if control.trading_mode == "PAPER"
            or (settings.live_server_gate_open and control.live_confirmed_at is not None)
            else "PAPER"
        ),
        "live_confirmation_at": _iso(control.live_confirmed_at),
        "live_available": bool(
            settings.live_server_gate_open
            and settings.live_execution_rules_verified
            and live_contracts
            and all(spec.live_safe for spec in live_contracts.values() if spec.quote_asset == "USDT")
        ),
        "live_server_gate_configured": settings.live_server_gate_open,
        "live_contract_rules_verified": settings.shark_contract_rules_verified,
        "live_split_protection_verified": settings.shark_split_protection_verified,
        "shark_credentials_configured": settings.shark_credentials_configured,
        "telegram_configured": settings.telegram_configured,
        "webhook_configured": bool(settings.tradingview_webhook_token),
        "dashboard_auth_enabled": getattr(settings, "dashboard_auth_enabled", True),
        "dashboard_auth_configured": bool(settings.dashboard_api_token),
        "trading_paused": control.kill_switch or settings.emergency_trading_disabled,
        "environment_pause": settings.emergency_trading_disabled,
        "risk": {
            "margin_per_trade": _str(control.margin_per_trade),
            "leverage": control.leverage,
            "max_open_positions": control.max_open_positions,
            "target1_percent": _str(control.target1_percent),
            "target2_percent": _str(control.target2_percent),
            "target3_percent": _str(control.target3_percent),
            "stop_loss_percent": _str(control.stop_loss_percent),
            "target1_close_percent": _str(control.target1_close_percent),
            "trailing_stop_percent": _str(control.trailing_stop_percent),
            "trailing_enabled": control.trailing_enabled,
            "enable_secondary_targets": control.enable_secondary_targets,
            "entry_candle_interval": control.entry_candle_interval,
            "max_notional_per_trade": _str(control.max_notional_per_trade),
            "max_daily_loss": _str(control.max_daily_loss),
            "limit_entry_expiry_seconds": control.limit_entry_expiry_seconds,
            "cancel_stale_entry_orders": control.cancel_stale_entry_orders,
            "allow_symbols": control.allow_symbols or [],
            "deny_symbols": control.deny_symbols or [],
        },
        "strategy": {
            "entry_price_source": settings.entry_price_source,
            "entry_candle_interval": control.entry_candle_interval,
            "entry_candle_max_age_seconds": settings.entry_candle_max_age_seconds,
            "entry_allow_live_ticker_fallback": settings.entry_allow_live_ticker_fallback,
            "stop_loss_percent": _str(control.stop_loss_percent),
            "target1_percent": _str(control.target1_percent),
            "target1_close_percent": _str(control.target1_close_percent),
            "trailing_stop_percent": _str(control.trailing_stop_percent),
            "trailing_enabled": control.trailing_enabled,
            "enable_secondary_targets": control.enable_secondary_targets,
            "description": (
                "TradingView alert is only a trigger. Entry = close of the last completed Shark "
                f"{control.entry_candle_interval} candle. Stop = entry -{_plain(control.stop_loss_percent)}%. "
                f"Target 1 = entry +{_plain(control.target1_percent)}% closing "
                f"{_plain(control.target1_close_percent)}% of the position, then the stop moves to "
                "break-even and trails "
                f"{_plain(control.trailing_stop_percent)}% behind every new high."
            ),
        },
        "market_day_timezone": settings.timezone,
        "market_day": datetime.now(ZoneInfo(settings.timezone)).date().isoformat(),
        "paper_initial_balance": _str(settings.paper_initial_balance),
        "paper_maker_fee_bps": _str(settings.paper_maker_fee_bps),
        "paper_taker_fee_bps": _str(settings.paper_taker_fee_bps),
        "risk_reward_ratio": _risk_reward_ratio(control),
    }


def _risk_reward_ratio(control: RiskControl) -> str | None:
    if control.stop_loss_percent <= 0 or control.target1_percent <= 0:
        return None
    return _str((control.target1_percent / control.stop_loss_percent).quantize(Decimal("0.01")))


@router.patch("/settings")
async def update_settings(
    body: SettingsUpdate,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_role(request, "OPERATOR")
    settings: Settings = request.app.state.settings
    control = await session.scalar(select(RiskControl).where(RiskControl.id == 1).with_for_update())
    if control is None:
        control = await ensure_risk_control(session, settings)
    values = body.model_dump(exclude_unset=True, exclude_none=True)
    confirmation = values.pop("confirmation_phrase", None)
    if not values:
        raise HTTPException(status_code=422, detail="No settings were supplied")
    del confirmation
    merged = {
        "target1_percent": values.get("target1_percent", control.target1_percent),
        "target2_percent": values.get("target2_percent", control.target2_percent),
        "target3_percent": values.get("target3_percent", control.target3_percent),
        "margin_per_trade": values.get("margin_per_trade", control.margin_per_trade),
        "leverage": values.get("leverage", control.leverage),
        "max_notional_per_trade": values.get("max_notional_per_trade", control.max_notional_per_trade),
        "enable_secondary_targets": values.get(
            "enable_secondary_targets", control.enable_secondary_targets
        ),
        "entry_candle_interval": values.get("entry_candle_interval", control.entry_candle_interval),
    }
    targets_supplied = any(
        key in values for key in ("target1_percent", "target2_percent", "target3_percent")
    )
    if (targets_supplied or merged["enable_secondary_targets"]) and not (
        merged["target1_percent"] < merged["target2_percent"] < merged["target3_percent"]
    ):
        raise HTTPException(
            status_code=422,
            detail="Targets must satisfy T1 < T2 < T3 (secondary targets stay disabled by default)",
        )
    if merged["margin_per_trade"] * Decimal(merged["leverage"]) > merged["max_notional_per_trade"]:
        raise HTTPException(status_code=422, detail="Margin × leverage exceeds the per-trade notional cap")
    normalized_interval = normalize_interval(str(merged["entry_candle_interval"]), "5m")
    if normalized_interval != str(merged["entry_candle_interval"]).strip().lower():
        raise HTTPException(
            status_code=422,
            detail="Entry candle interval must be one of 1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 12h, 1d",
        )
    values["entry_candle_interval"] = normalized_interval
    previous: dict[str, Any] = {}
    for key, value in values.items():
        previous[key] = getattr(control, key, None)
        setattr(control, key, value)
    session.add(
        _audit(
            action="RISK_SETTINGS_UPDATED",
            entity_type="risk_control",
            entity_id="1",
            details={
                "changed": {key: _str(value) for key, value in values.items()},
                "previous": {key: _str(value) for key, value in previous.items()},
            },
        )
    )
    await session.flush()
    return {"updated": True, "settings": await read_settings(request, session)}


@router.get("/performance")
async def performance(
    request: Request,
    session: AsyncSession = Depends(session_dependency),
    days: int = Query(default=30, ge=7, le=90),
    mode: str | None = Query(default=None, pattern="^(PAPER|LIVE)$"),
) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    control = await ensure_risk_control(session, settings)
    selected_mode = mode or control.trading_mode
    today, _, _ = _local_day_bounds(settings.timezone)
    first_day = today - timedelta(days=days - 1)
    _, start_utc, _ = _local_day_bounds(settings.timezone, first_day)
    rows = await session.execute(
        select(Fill.fill_time, Fill.gross_realized_pnl, Fill.fee).where(
            Fill.mode == selected_mode,
            Fill.fill_time >= start_utc,
        ).order_by(Fill.fill_time)
    )
    zone = ZoneInfo(settings.timezone)
    grouped: dict[date, Decimal] = {}
    for fill_time, gross, fee in rows.all():
        moment = fill_time.replace(tzinfo=UTC) if fill_time.tzinfo is None else fill_time
        local_day = moment.astimezone(zone).date()
        grouped[local_day] = grouped.get(local_day, Decimal("0")) + (gross or Decimal("0")) - (fee or Decimal("0"))
    series = [
        {"date": (first_day + timedelta(days=index)).isoformat(), "net_realized_pnl": _str(grouped.get(first_day + timedelta(days=index), Decimal("0")))}
        for index in range(days)
    ]
    return {
        "mode": selected_mode,
        "timezone": settings.timezone,
        "days": days,
        "series": series,
        "source": "PERSISTED_FILLS",
        "fees_included": True,
        "funding_included": False,
    }


@router.get("/overview")
async def overview(
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    control = await ensure_risk_control(session, settings)
    mode = control.trading_mode
    today, start_utc, end_utc = _local_day_bounds(settings.timezone)
    pnl_breakdown = await realized_pnl_breakdown_for_day(session, settings.timezone, today, mode=mode)
    daily_pnl = pnl_breakdown["net"]

    # Include both fully closed trades and trades with exit fills today (e.g. target 1 partial closes)
    exit_fill_trade_ids = list(
        (
            await session.scalars(
                select(Fill.trade_id)
                .where(
                    Fill.mode == mode,
                    Fill.entry_exit == "EXIT",
                    Fill.fill_time >= start_utc,
                    Fill.fill_time < end_utc,
                )
                .distinct()
            )
        ).all()
    )
    closed_trade_ids = list(
        (
            await session.scalars(
                select(Trade.id)
                .where(
                    Trade.mode == mode,
                    Trade.status == "CLOSED",
                    Trade.closed_at >= start_utc,
                    Trade.closed_at < end_utc,
                )
            )
        ).all()
    )
    all_exited_ids = set(exit_fill_trade_ids) | set(closed_trade_ids)
    if all_exited_ids:
        trades_for_pnl = list(
            (
                await session.scalars(
                    select(Trade).where(Trade.id.in_(all_exited_ids))
                )
            ).all()
        )
        wins = sum(1 for t in trades_for_pnl if (t.net_realized_pnl or Decimal("0")) > 0)
        losses = sum(1 for t in trades_for_pnl if (t.net_realized_pnl or Decimal("0")) < 0)
        closed_today = len(trades_for_pnl)
    else:
        wins = 0
        losses = 0
        closed_today = 0
    trades_today = int(
        await session.scalar(
            select(func.count()).select_from(Trade).where(
                Trade.mode == mode,
                Trade.created_at >= start_utc,
                Trade.created_at < end_utc,
            )
        )
        or 0
    )
    signals_today = int(
        await session.scalar(
            select(func.count()).select_from(SignalEvent).where(
                SignalEvent.received_at >= start_utc,
                SignalEvent.received_at < end_utc,
            )
        )
        or 0
    )
    rows = await session.execute(
        select(Trade).where(Trade.mode == mode, Trade.status.in_(ACTIVE_TRADE_STATES))
    )
    active = list(rows.scalars().all())
    active_unrealized = Decimal("0")
    active_prices_available = True
    for trade in active:
        if trade.status not in {"OPEN", "PARTIALLY_CLOSED", "PROTECTION_ERROR", "CLOSING"}:
            continue
        ticker = request.app.state.context.market.tickers.get(trade.symbol)
        if ticker is None or ticker.is_stale(settings.market_data_stale_seconds):
            active_prices_available = False
        elif trade.actual_entry is not None and trade.remaining_quantity is not None:
            active_unrealized += long_unrealized_pnl(
                trade.actual_entry, ticker.last_price, trade.remaining_quantity
            )
    status_counts = await session.execute(
        select(Trade.status, func.count()).where(Trade.mode == mode).group_by(Trade.status)
    )
    statuses = {str(key): int(value) for key, value in status_counts.all()}
    wallet = await _wallet_snapshot(request, session, mode)
    funding_available = bool(active) and all(trade.funding_data_available for trade in active)
    return {
        "mode": mode,
        "market_day": today.isoformat(),
        "market_day_timezone": settings.timezone,
        "day_pnl": {
            "realized_net": _str(daily_pnl),
            "gross": _str(pnl_breakdown["gross"]),
            "fees_today": _str(pnl_breakdown["fees_paid"]),
            "funding_today": _str(pnl_breakdown["funding_paid"]),
            "unrealized": _str(active_unrealized) if active_prices_available else None,
            "unrealized_status": "AVAILABLE" if active_prices_available else "STALE_OR_UNAVAILABLE_MARKET_DATA",
            "total": _str(daily_pnl + active_unrealized) if active_prices_available else None,
            "currency": settings.shark_margin_asset,
            "source": "PERSISTED_FILLS_AND_LAST_PRICE",
            "fees_included": True,
            "funding_included": funding_available,
            "funding_status": "AVAILABLE" if funding_available else "NOT_AVAILABLE",
            "note": "Exchange fees are included where fills are available; funding is not assumed.",
        },
        "trades_today": trades_today,
        "signals_today": signals_today,
        "closed_today": closed_today,
        "wins_today": wins,
        "losses_today": losses,
        "open_positions": sum(
            value for key, value in statuses.items()
            if key in {"OPEN", "PARTIALLY_CLOSED", "PROTECTION_ERROR", "CLOSING", "ENTRY_PARTIAL"}
        ),
        "pending_entries": sum(value for key, value in statuses.items() if key in {"QUEUED", "ENTRY_PENDING", "SUBMITTING"}),
        "unknown_or_attention": sum(value for key, value in statuses.items() if key in {"UNKNOWN", "PROTECTION_ERROR"}),
        "status_counts": statuses,
        "wallet": wallet,
        "risk_limits": {
            "max_open_positions": control.max_open_positions,
            "max_daily_loss": _str(control.max_daily_loss),
            "max_notional_per_trade": _str(control.max_notional_per_trade),
        },
    }


@router.get("/pending-entries")
async def pending_entries(
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    rows = await session.scalars(
        select(Trade)
        .where(Trade.status.in_(("QUEUED", "SUBMITTING", "ENTRY_PENDING", "ENTRY_PARTIAL")))
        .order_by(Trade.created_at.desc())
    )
    items = []
    for trade in rows.all():
        ticker = request.app.state.context.market.tickers.get(trade.symbol)
        item = _trade_payload(trade, ticker, settings.market_data_stale_seconds)
        item["entry_expires_at"] = _iso(trade.entry_expires_at)
        items.append(item)
    return {"items": items, "count": len(items), "note": "Pending limits are not confirmed open positions."}


@router.get("/positions")
async def positions(
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    rows = await session.scalars(
        select(Trade)
        .where(
            Trade.status.in_(
                ("OPEN", "PARTIALLY_CLOSED", "ENTRY_PARTIAL", "PROTECTION_ERROR", "UNKNOWN", "CLOSING")
            )
        )
        .order_by(Trade.created_at.desc())
    )
    all_trades = rows.all()
    output = []
    total_realized = Decimal("0")
    total_unrealized = Decimal("0")
    total_margin = Decimal("0")
    has_unrealized = False

    for trade in all_trades:
        ticker = request.app.state.context.market.tickers.get(trade.symbol)
        item = _trade_payload(trade, ticker, settings.market_data_stale_seconds)
        output.append(item)
        if trade.margin:
            total_margin += trade.margin
        if trade.net_realized_pnl:
            total_realized += trade.net_realized_pnl
        if item.get("unrealized_pnl") is not None:
            try:
                total_unrealized += Decimal(str(item["unrealized_pnl"]))
                has_unrealized = True
            except (InvalidOperation, ValueError, TypeError):
                pass

    total_net_profit = total_realized + total_unrealized

    return {
        "items": output,
        "count": len(output),
        "summary": {
            "total_realized_pnl": _str(total_realized),
            "total_unrealized_pnl": _str(total_unrealized) if has_unrealized else "0.00",
            "total_net_profit_pnl": _str(total_net_profit),
            "total_margin": _str(total_margin),
        },
        "updated_at": datetime.now(UTC).isoformat(),
    }


@router.get("/market")
async def market_data(
    request: Request,
    q: str = Query(default="", max_length=64),
    sort: str = Query(
        default="symbol",
        pattern="^(symbol|last_price|change_24h_percent|volume_24h|day_change_pct|high_24h|low_24h|day_high|day_low)$",
    ),
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
) -> dict[str, Any]:
    state = request.app.state.context.market.state()
    query = q.strip().upper()
    items = [item for item in state["items"] if query in item["symbol"]]
    def sort_key(item: dict[str, Any]) -> Any:
        if sort == "symbol":
            return item["symbol"]
        value = _decimal(item.get(sort))
        return value if value is not None else Decimal("-Infinity")
    items.sort(key=sort_key, reverse=order == "desc")
    return {**state, "items": items, "count": len(items)}


@router.get("/market/sentiment-history")
async def get_sentiment_history(
    limit: int = Query(50, le=200),
    session: AsyncSession = Depends(session_dependency),
) -> list[dict[str, Any]]:
    """Return persisted sentiment transitions, newest first."""
    rows = list(
        (
            await session.execute(
                select(SentimentHistory)
                .order_by(SentimentHistory.occurred_at.desc())
                .limit(limit)
            )
        ).scalars()
    )
    return [
        {"id": r.id, "time": r.occurred_at.isoformat(), "from": r.from_sentiment, "to": r.to_sentiment}
        for r in rows
    ]


@router.post("/market/sentiment-history")
async def post_sentiment_history(
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Record a new sentiment transition."""
    body = await request.json()
    from_s = str(body.get("from", "")).strip()
    to_s = str(body.get("to", "")).strip()
    if not from_s or not to_s:
        raise HTTPException(400, "'from' and 'to' are required")
    row = SentimentHistory(from_sentiment=from_s, to_sentiment=to_s)
    session.add(row)
    await session.commit()
    return {"id": row.id, "time": row.occurred_at.isoformat(), "from": from_s, "to": to_s}


@router.get("/market/live-feed")
async def market_live_feed(
    request: Request,
    limit: int = Query(350, le=500),
) -> list[dict[str, Any]]:
    state = request.app.state.context.market.state()
    items = state["items"][:limit]
    res = []
    for item in items:
        last = float(item["last_price"]) if item.get("last_price") else 0.0
        pct = float(item["change_24h_percent"]) if item.get("change_24h_percent") else 0.0
        high = float(item["high_24h"]) if item.get("high_24h") else 0.0
        low = float(item["low_24h"]) if item.get("low_24h") else 0.0
        vol = float(item["volume_24h"]) if item.get("volume_24h") else 0.0
        change_abs = float(item["change_abs"]) if item.get("change_abs") else 0.0

        # Day metrics (Crypto market day from 05:30 AM IST / 00:00 UTC)
        day_open = float(item["day_open"]) if item.get("day_open") is not None else 0.0
        day_high = float(item["day_high"]) if item.get("day_high") is not None else (high if day_open > 0 else 0.0)
        day_low = float(item["day_low"]) if item.get("day_low") is not None else (low if day_open > 0 else 0.0)
        day_pct = float(item["day_change_pct"]) if item.get("day_change_pct") is not None else (
            ((last - day_open) / day_open * 100.0) if day_open > 0 else 0.0
        )
        day_abs = float(item["day_change_abs"]) if item.get("day_change_abs") is not None else (last - day_open if day_open > 0 else 0.0)

        res.append({
            "symbol": item["symbol"],
            "base_asset": item.get("base_asset") or item["symbol"],
            "quote_asset": item.get("quote_asset") or "USDT",
            "last_price": last,
            "open_price": last - change_abs,
            "high": high,
            "low": low,
            "volume": vol,
            "change_pct": pct,
            "change_abs": change_abs,
            "day_open": day_open,
            "day_high": day_high,
            "day_low": day_low,
            "day_change_pct": day_pct,
            "day_change_abs": day_abs,
            "status": "ok",
            "webhook_status": "",
        })
    return res


@router.get("/history")
async def history(
    session: AsyncSession = Depends(session_dependency),
    symbol: str | None = Query(default=None, max_length=64),
    mode: str | None = Query(default=None, pattern="^(PAPER|LIVE)$"),
    trade_status: str | None = Query(default=None, alias="status", max_length=32),
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100_000),
) -> dict[str, Any]:
    criteria = []
    if symbol:
        criteria.append(Trade.symbol == symbol.upper())
    if mode:
        criteria.append(Trade.mode == mode)
    if trade_status:
        criteria.append(Trade.status == trade_status.upper())
    if start:
        criteria.append(Trade.created_at >= start)
    if end:
        criteria.append(Trade.created_at <= end)
    total = int(await session.scalar(select(func.count()).select_from(Trade).where(*criteria)) or 0)
    trades = await session.scalars(
        select(Trade).where(*criteria).order_by(Trade.created_at.desc()).limit(limit).offset(offset)
    )
    return {
        "items": [_trade_payload(trade, None, 0) for trade in trades.all()],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/history.csv")
async def history_csv(
    session: AsyncSession = Depends(session_dependency),
    mode: str | None = Query(default=None, pattern="^(PAPER|LIVE)$"),
    symbol: str | None = Query(default=None, max_length=64),
) -> StreamingResponse:
    criteria = []
    if mode:
        criteria.append(Trade.mode == mode)
    if symbol:
        criteria.append(Trade.symbol == symbol.upper())
    trades = await session.scalars(
        select(Trade).where(*criteria).order_by(Trade.created_at.desc()).limit(10_000)
    )
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    columns = [
        "created_at", "mode", "symbol", "status", "planned_entry", "signal_close", "actual_entry",
        "quantity", "remaining_quantity", "stop_price", "target1_price", "target2_price", "target3_price",
        "gross_realized_pnl", "fees_paid", "funding_paid", "net_realized_pnl", "fee_data_available", "funding_data_available",
    ]
    writer.writerow(columns)
    for trade in trades.all():
        row = _trade_payload(trade, None, 0)
        values = [row.get(column) for column in columns]
        safe = ["'" + value if isinstance(value, str) and value[:1] in {"=", "+", "-", "@"} else value for value in values]
        writer.writerow(safe)
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=shasha-act-history.csv"},
    )


@router.get("/events")
async def events(
    session: AsyncSession = Depends(session_dependency),
    event_status: str | None = Query(default=None, alias="status", max_length=32),
    symbol: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100_000),
) -> dict[str, Any]:
    criteria = []
    if event_status:
        criteria.append(SignalEvent.status == event_status.upper())
    if symbol:
        criteria.append(SignalEvent.symbol == symbol.upper())
    total = int(await session.scalar(select(func.count()).select_from(SignalEvent).where(*criteria)) or 0)
    rows = await session.scalars(
        select(SignalEvent).where(*criteria).order_by(SignalEvent.received_at.desc()).limit(limit).offset(offset)
    )
    item_list = list(rows.all())
    event_ids = [item.id for item in item_list]
    trade_rows: list[Trade] = (
        list((await session.scalars(select(Trade).where(Trade.event_id.in_(event_ids)))).all())
        if event_ids else []
    )
    trade_by_event = {trade.event_id: trade for trade in trade_rows}
    jobs: list[OutboxJob] = (
        list((await session.scalars(
            select(OutboxJob).where(OutboxJob.event_id.in_(event_ids)).order_by(OutboxJob.created_at.desc())
        )).all())
        if event_ids else []
    )
    jobs_by_event: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        jobs_by_event.setdefault(job.event_id or "", []).append(
            {
                "kind": job.kind,
                "status": job.status,
                "attempts": job.attempts,
                "last_error": redact_text(job.last_error or "")[:250] or None,
                "created_at": _iso(job.created_at),
                "delivered_at": _iso(job.delivered_at),
            }
        )
    return {
        "items": [
            {
                "id": event.id,
                "supplied_event_id": event.supplied_event_id,
                "strategy": event.strategy,
                "tradingview_symbol": event.tradingview_symbol,
                "symbol": event.symbol,
                "direction": event.direction,
                "timeframe": event.timeframe,
                "bar_time": _iso(event.bar_time),
                "signal_close": _str(event.signal_close),
                "entry_price": _str(event.entry_price),
                "status": event.status,
                "status_reason": redact_text(event.status_reason or "")[:500],
                "mode": event.mode,
                "received_at": _iso(event.received_at),
                "body_hash": event.raw_body_hash,
                "trade_id": trade_by_event[event.id].id if event.id in trade_by_event else None,
                "outbox": jobs_by_event.get(event.id, []),
            }
            for event in item_list
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/trades/{trade_id}")
async def trade_detail(
    trade_id: str,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    trade = await session.get(Trade, trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    ticker = request.app.state.context.market.tickers.get(trade.symbol)
    return _trade_payload(trade, ticker, request.app.state.settings.market_data_stale_seconds)


@router.get("/trades/{trade_id}/orders")
async def trade_orders(
    trade_id: str,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    trade = await session.get(Trade, trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    rows = await session.scalars(select(Order).where(Order.trade_id == trade_id).order_by(Order.created_at))
    return {
        "items": [
            {
                "id": order.id,
                "client_order_id": order.client_order_id,
                "exchange_order_id": order.exchange_order_id,
                "kind": order.kind,
                "side": order.side,
                "order_type": order.order_type,
                "status": order.status,
                "quantity": _str(order.quantity),
                "filled_quantity": _str(order.filled_quantity),
                "price": _str(order.price),
                "average_fill_price": _str(order.average_fill_price),
                "reduce_only": order.reduce_only,
                "error_code": order.error_code or None,
                "error_message": redact_text(order.error_message or "")[:250] or None,
                "submitted_at": _iso(order.submitted_at),
                "filled_at": _iso(order.filled_at),
            }
            for order in rows.all()
        ]
    }


@router.get("/trades/{trade_id}/audit")
async def trade_audit(
    trade_id: str,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    trade = await session.get(Trade, trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    rows = await session.scalars(
        select(AuditEvent).where(AuditEvent.entity_id == trade_id).order_by(AuditEvent.created_at.desc()).limit(200)
    )
    return {
        "items": [
            {
                "id": row.id,
                "actor": row.actor,
                "action": row.action,
                "details": row.details or {},
                "created_at": _iso(row.created_at),
            }
            for row in rows.all()
        ]
    }


@router.patch("/trades/{trade_id}/risk")
async def update_trade_risk(
    trade_id: str,
    body: TradeRiskUpdate,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_role(request, "OPERATOR")
    if body.confirmation_phrase != "CONFIRM RISK CHANGE":
        raise HTTPException(status_code=422, detail="Type CONFIRM RISK CHANGE to apply position-level edits")
    trade = await session.scalar(select(Trade).where(Trade.id == trade_id).with_for_update())
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    if trade.mode != "PAPER":
        raise HTTPException(status_code=409, detail="LIVE risk edits remain disabled until Shark protection rules are verified")
    if trade.status not in {"OPEN", "PARTIALLY_CLOSED"} or trade.actual_entry is None:
        raise HTTPException(status_code=409, detail="Only confirmed open PAPER positions can be edited")
    values = body.model_dump(exclude={"confirmation_phrase"}, exclude_unset=True, exclude_none=True)
    if not values:
        raise HTTPException(status_code=422, detail="At least one risk level must be supplied")
    for index, field in enumerate(("target1_price", "target2_price", "target3_price"), start=1):
        old_target = getattr(trade, field)
        if index <= trade.target_progress and field in values and values[field] != old_target:
            raise HTTPException(status_code=409, detail="A target that has already filled cannot be edited")
    entry = trade.actual_entry
    new_stop = values.get("stop_price", trade.stop_price)
    t1 = values.get("target1_price", trade.target1_price)
    t2 = values.get("target2_price", trade.target2_price)
    t3 = values.get("target3_price", trade.target3_price)
    ticker = request.app.state.context.market.tickers.get(trade.symbol)
    if ticker is None or ticker.is_stale(request.app.state.settings.market_data_stale_seconds):
        raise HTTPException(status_code=409, detail="A fresh Shark last price is required; no risk edit was applied")
    if new_stop is None or new_stop >= ticker.last_price:
        raise HTTPException(status_code=422, detail="For a long position, the stop must be below the current last price")
    if t1 is None or t2 is None or t3 is None or any(value <= entry for value in (t1, t2, t3)):
        raise HTTPException(status_code=422, detail="Target prices must be above entry and satisfy T1 < T2 < T3")
    if not t1 < t2 < t3:
        raise HTTPException(status_code=422, detail="Target prices must be above entry and satisfy T1 < T2 < T3")
    old = {
        "stop_price": _str(trade.stop_price),
        "target1_price": _str(trade.target1_price),
        "target2_price": _str(trade.target2_price),
        "target3_price": _str(trade.target3_price),
    }
    trade.stop_price = new_stop
    trade.target1_price = t1
    trade.target2_price = t2
    trade.target3_price = t3
    active_orders = await session.scalars(
        select(Order).where(Order.trade_id == trade.id, Order.status.in_(("NEW", "OPEN")))
    )
    by_kind = {order.kind: order for order in active_orders.all()}
    stop_order = by_kind.get("STOP_LOSS")
    if stop_order is not None:
        stop_order.price = new_stop
        stop_order.quantity = trade.remaining_quantity or stop_order.quantity
    for kind, price in (("TARGET_1", t1), ("TARGET_2", t2), ("TARGET_3", t3)):
        target_order = by_kind.get(kind)
        if target_order is not None:
            target_order.price = price
    session.add(
        _audit(
            action="PAPER_POSITION_RISK_EDITED",
            entity_type="trade",
            entity_id=trade.id,
            details={
                "old": old,
                "new": {key: _str(value) for key, value in {
                    "stop_price": new_stop,
                    "target1_price": t1,
                    "target2_price": t2,
                    "target3_price": t3,
                }.items()},
                "confirmation": "CONFIRM RISK CHANGE",
            },
        )
    )
    return {"updated": True, "trade": _trade_payload(trade, ticker, request.app.state.settings.market_data_stale_seconds)}


@router.post("/trades/{trade_id}/close")
async def close_trade(
    trade_id: str,
    body: CloseTradeRequest,
    request: Request,
) -> dict[str, Any]:
    _require_role(request, "OPERATOR")
    if body.confirmation_phrase != "CLOSE POSITION":
        raise HTTPException(status_code=422, detail="Type CLOSE POSITION to confirm the reduce-only close")
    async with request.app.state.session_factory() as session:
        trade = await session.get(Trade, trade_id)
        mode = trade.mode if trade is not None else None
    if mode is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    try:
        await request.app.state.context.executor.close_trade(trade_id)
    except ValueError as exc:
        message = str(exc)
        raise HTTPException(status_code=409, detail=message) from exc
    if mode == "PAPER":
        return {"accepted": True, "message": "PAPER close simulated at a fresh Shark last price; no private exchange order was sent."}
    return {"accepted": True, "message": "Close request recorded; final state requires Shark confirmation."}


@router.post("/trades/{trade_id}/cancel-entry")
async def cancel_entry(
    trade_id: str,
    body: CancelPendingRequest,
    request: Request,
) -> dict[str, Any]:
    _require_role(request, "OPERATOR")
    if body.confirmation_phrase != "CANCEL PENDING ENTRIES":
        raise HTTPException(status_code=422, detail="Type CANCEL PENDING ENTRIES to confirm")
    async with request.app.state.session_factory() as session:
        trade = await session.get(Trade, trade_id)
        if trade is None:
            raise HTTPException(status_code=404, detail="Trade not found")
        if trade.status not in {"ENTRY_PENDING", "ENTRY_PARTIAL"}:
            raise HTTPException(status_code=409, detail="Trade has no confirmed pending entry to cancel yet")
    confirmed = await request.app.state.context.executor.cancel_pending_entries([trade_id])
    return {
        "cancel_requested": True,
        "confirmed_cancellations": confirmed,
        "message": "Entry cancellation was confirmed." if confirmed else "Cancellation is not confirmed; entry state remains visible for reconciliation.",
    }


@router.post("/control/cancel-pending")
async def cancel_pending(
    body: CancelPendingRequest,
    request: Request,
) -> dict[str, Any]:
    _require_role(request, "OPERATOR")
    if body.confirmation_phrase != "CANCEL PENDING ENTRIES":
        raise HTTPException(status_code=422, detail="Type CANCEL PENDING ENTRIES to confirm")
    count = await request.app.state.context.executor.cancel_pending_entries()
    return {"requested": True, "confirmed_cancellations": count, "note": "Only exchange-confirmed cancellations are counted."}


@router.post("/control/pause")
async def pause_trading(
    body: TradingPauseRequest,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_role(request, "OPERATOR")
    settings: Settings = request.app.state.settings
    control = await session.scalar(select(RiskControl).where(RiskControl.id == 1).with_for_update())
    if control is None:
        control = await ensure_risk_control(session, settings)
    if body.paused and body.confirmation_phrase != "PAUSE SHASHA ACT":
        raise HTTPException(status_code=422, detail="Type PAUSE SHASHA ACT to pause signal processing")
    if not body.paused:
        if settings.emergency_trading_disabled:
            raise HTTPException(status_code=409, detail="Server emergency pause cannot be cleared from the dashboard")
        if body.confirmation_phrase != "RESUME SHASHA ACT":
            raise HTTPException(status_code=422, detail="Type RESUME SHASHA ACT to resume signal processing")
    control.kill_switch = body.paused
    session.add(
        _audit(
            action="TRADING_PAUSED" if body.paused else "TRADING_RESUMED",
            entity_type="risk_control",
            entity_id="1",
            details={"paused": body.paused},
        )
    )
    return {"trading_paused": control.kill_switch or settings.emergency_trading_disabled}


@router.post("/control/mode/paper")
async def switch_to_paper(
    body: CloseTradeRequest,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_role(request, "OPERATOR")
    if body.confirmation_phrase != "SWITCH TO PAPER":
        raise HTTPException(status_code=422, detail="Type SWITCH TO PAPER to confirm")
    control = await session.scalar(select(RiskControl).where(RiskControl.id == 1).with_for_update())
    if control is None:
        control = await ensure_risk_control(session, request.app.state.settings)
    live_count = int(
        await session.scalar(
            select(func.count()).select_from(Trade).where(
                Trade.mode == "LIVE", Trade.status.in_(ACTIVE_TRADE_STATES)
            )
        )
        or 0
    )
    if live_count:
        raise HTTPException(status_code=409, detail="Open or unresolved LIVE trades must be reconciled before switching to PAPER")
    control.trading_mode = "PAPER"
    control.live_confirmed_at = None
    session.add(_audit(action="MODE_SWITCHED_TO_PAPER", entity_type="risk_control", entity_id="1"))
    return {"mode": "PAPER"}


@router.post("/control/mode/live")
async def activate_live(
    body: LiveActivationRequest,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_role(request, "ADMIN")
    settings: Settings = request.app.state.settings
    if body.confirmation_phrase != "ENABLE LIVE TRADING" or not body.acknowledge_exchange_risk:
        raise HTTPException(status_code=422, detail="Deliberate confirmation and exchange-risk acknowledgement are required")
    if not settings.live_server_gate_open:
        raise HTTPException(status_code=409, detail="LIVE server gate is closed; configure server-side keys and LIVE_TRADING_ENABLED")
    if not settings.live_execution_rules_verified:
        raise HTTPException(
            status_code=409,
            detail="LIVE is blocked: official Shark tick/quantity-step and split TP/SL replacement semantics remain unverified",
        )
    context = request.app.state.context
    if not context.market.resolver_contracts:
        try:
            await context.market.refresh_contracts(force=True)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Shark contract metadata is unavailable") from exc
    safe_contracts = [
        spec for spec in context.market.resolver_contracts.values() if spec.quote_asset == "USDT"
    ]
    if not safe_contracts or not all(spec.live_safe for spec in safe_contracts):
        raise HTTPException(status_code=409, detail="LIVE is blocked: Shark contract trading filters are not verified")
    control = await session.scalar(select(RiskControl).where(RiskControl.id == 1).with_for_update())
    if control is None:
        control = await ensure_risk_control(session, settings)
    active_count = int(
        await session.scalar(
            select(func.count()).select_from(Trade).where(Trade.status.in_(ACTIVE_TRADE_STATES))
        )
        or 0
    )
    if active_count:
        raise HTTPException(status_code=409, detail="Reconcile or close all active PAPER trades before enabling LIVE")
    control.trading_mode = "LIVE"
    control.live_confirmed_at = datetime.now(UTC)
    session.add(
        _audit(
            action="LIVE_MODE_CONFIRMED",
            entity_type="risk_control",
            entity_id="1",
            details={"acknowledged": True, "environment_gate": True},
        )
    )
    return {"mode": "LIVE", "confirmed_at": _iso(control.live_confirmed_at)}


@router.post("/events/{event_id}/retry-notification")
async def retry_notification(
    event_id: str,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_role(request, "OPERATOR")
    event = await session.get(SignalEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Signal event not found")
    failed = int(
        await session.scalar(
            select(func.count()).select_from(OutboxJob).where(
                OutboxJob.event_id == event_id,
                OutboxJob.kind == "TELEGRAM_SIGNAL",
                OutboxJob.status == "FAILED",
            )
        )
        or 0
    )
    if failed == 0:
        raise HTTPException(status_code=409, detail="There is no failed Telegram delivery to retry")
    key = f"notify:{event_id}:manual:{uuid4().hex}"
    job = OutboxJob(
        id=str(uuid4()),
        event_id=event_id,
        kind="TELEGRAM_SIGNAL",
        idempotency_key=key,
        status="PENDING",
        priority=15,
        payload={},
    )
    session.add(job)
    session.add(_audit(action="TELEGRAM_RETRY_REQUESTED", entity_type="event", entity_id=event_id))
    return {"queued": True, "message": "Notification retry was durably queued."}


@router.post("/control/dashboard-auth")
async def toggle_dashboard_auth(
    body: DashboardAuthToggleRequest,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_role(request, "ADMIN")
    settings: Settings = request.app.state.settings
    settings.dashboard_auth_enabled = body.enabled

    if settings.environment != "test":
        env_path = PROJECT_ROOT / "backend" / ".env"
        if env_path.exists():
            try:
                content = env_path.read_text(encoding="utf-8")
                if re.search(r"^DASHBOARD_AUTH_ENABLED=", content, flags=re.MULTILINE):
                    content = re.sub(
                        r"^DASHBOARD_AUTH_ENABLED=.*$",
                        f"DASHBOARD_AUTH_ENABLED={'true' if body.enabled else 'false'}",
                        content,
                        flags=re.MULTILINE,
                    )
                else:
                    content += f"\nDASHBOARD_AUTH_ENABLED={'true' if body.enabled else 'false'}\n"
                env_path.write_text(content, encoding="utf-8")
            except Exception:
                pass

    session.add(
        _audit(
            action="DASHBOARD_AUTH_TOGGLED",
            entity_type="dashboard_auth",
            entity_id="dashboard_auth",
            details={"enabled": body.enabled},
        )
    )
    await session.flush()
    return {
        "dashboard_auth_enabled": settings.dashboard_auth_enabled,
        "dashboard_auth_configured": bool(settings.dashboard_api_token),
    }

