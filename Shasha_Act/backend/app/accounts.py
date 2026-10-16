"""Operator accounts, authenticated sessions, service subscription and disclaimer state.

The browser signs in with a username and password. The resulting opaque session credential is
stored only as a hash on the server and is sent by the first-party console in an explicit
header. `DASHBOARD_API_TOKEN` remains an optional non-browser automation credential; it is not
exposed as a dashboard sign-in method.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import AuditEvent, DashboardSession, DashboardUser, ServiceSubscription
from app.security import (
    constant_time_equals,
    hash_password,
    hash_token,
    new_session_token,
    verify_password,
)

DEFAULT_DISCLAIMER = (
    "Shasha_Act executes a rules-based SHASHA strategy on crypto perpetual futures through "
    "Shark Exchange. Leveraged derivatives carry a high risk of losing your entire margin, and "
    "paper results are simulated. Signals can be delayed, duplicated, mis-parsed or rejected; "
    "exchange outages, funding, slippage, fees and liquidation are outside this software's "
    "control. Nothing here is investment advice, a performance guarantee, or a promise of "
    "execution. Trading is permitted only where it is legal for you, and you are solely "
    "responsible for your account, your keys, your risk limits and any tax consequences. "
    "Never trade more than you can afford to lose."
)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
ROLE_RANK = {"VIEWER": 0, "OPERATOR": 1, "ADMIN": 2, "OWNER": 3}
SUBSCRIPTION_PLANS = ("TRIAL", "BASIC", "PRO", "ENTERPRISE", "LIFETIME")


def money(value: Decimal | None) -> str | None:
    """Format a Decimal amount without storage artefacts (999.000000000000 → 999)."""
    if value is None:
        return None
    parsed = value if value.is_finite() else Decimal("0")
    normalized = parsed.normalize()
    if normalized == normalized.to_integral_value():
        normalized = normalized.quantize(Decimal(1))
    return format(normalized, "f")


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def iso(value: datetime | None) -> str | None:
    normalized = as_utc(value)
    return normalized.isoformat() if normalized else None


async def ensure_subscription(session: AsyncSession, settings: Settings) -> ServiceSubscription:
    subscription = await session.get(ServiceSubscription, 1)
    if subscription is None:
        subscription = ServiceSubscription(
            id=1,
            plan_name=settings.subscription_plan_name,
            fee=settings.subscription_fee,
            currency=settings.subscription_currency,
            billing_cycle=settings.subscription_billing_cycle,
            status="ACTIVE",
            started_at=_parse_setting_datetime(settings.subscription_started_at) or datetime.now(UTC),
            expires_at=_parse_setting_datetime(settings.subscription_expires_at),
        )
        session.add(subscription)
        await session.flush()
    return subscription


def _parse_setting_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def subscription_state(
    subscription: ServiceSubscription | None, *, now: datetime | None = None
) -> dict[str, Any]:
    """Describe the subscription exactly as persisted; expiry is never inferred as active."""
    moment = now or datetime.now(UTC)
    if subscription is None:
        return {
            "plan_name": "UNCONFIGURED",
            "fee": None,
            "currency": None,
            "billing_cycle": None,
            "status": "UNKNOWN",
            "started_at": None,
            "expires_at": None,
            "days_remaining": None,
            "expired": None,
            "contact": "",
            "notes": "",
            "source": "DATABASE",
        }
    expires = as_utc(subscription.expires_at)
    days_remaining: int | None = None
    expired: bool | None = None
    if expires is not None:
        delta = expires - moment
        expired = delta.total_seconds() <= 0
        days_remaining = max(int(delta.days), 0) if not expired else 0
    effective_status = subscription.status
    if expired:
        effective_status = "EXPIRED"
    elif subscription.status == "ACTIVE" and days_remaining is not None and days_remaining <= 3:
        effective_status = "EXPIRING_SOON"
    return {
        "plan_name": subscription.plan_name,
        "fee": money(subscription.fee),
        "currency": subscription.currency,
        "billing_cycle": subscription.billing_cycle,
        "status": effective_status,
        "stored_status": subscription.status,
        "started_at": iso(subscription.started_at),
        "expires_at": expires.isoformat() if expires else None,
        "days_remaining": days_remaining,
        "expired": expired,
        "contact": subscription.contact,
        "notes": subscription.notes,
        "source": "DATABASE",
    }


def user_subscription_state(user: DashboardUser, *, now: datetime | None = None) -> dict[str, Any]:
    moment = now or datetime.now(UTC)
    expires = as_utc(user.subscription_expires_at)
    expired: bool | None = None
    days_remaining: int | None = None
    if expires is not None:
        delta = expires - moment
        expired = delta.total_seconds() <= 0
        days_remaining = 0 if expired else max(int(delta.days), 0)
    status_value = "NOT_SET"
    if expired is True:
        status_value = "EXPIRED"
    elif expired is False:
        status_value = "EXPIRING_SOON" if (days_remaining or 0) <= 3 else "ACTIVE"
    return {
        "plan": user.subscription_plan or "",
        "fee": money(user.subscription_fee),
        "currency": user.subscription_currency,
        "cycle": user.subscription_cycle,
        "started_at": iso(user.subscription_started_at),
        "expires_at": expires.isoformat() if expires else None,
        "days_remaining": days_remaining,
        "status": status_value,
        "expired": expired,
    }


async def ensure_bootstrap_user(session: AsyncSession, settings: Settings) -> DashboardUser | None:
    """Create the first OWNER account from the environment when none exists yet.

    Password must come from the environment (`DASHBOARD_PASSWORD`); the plain value is never
    stored and never logged. When no password is configured no account is created; an explicitly
    enabled one-time initial-owner sign-up may then create the first account.
    """
    existing = int(await session.scalar(select(func.count()).select_from(DashboardUser)) or 0)
    if existing:
        return None
    username = (settings.dashboard_username or "").strip().lower()
    if not settings.dashboard_password or not username:
        return None
    user = DashboardUser(
        id=str(uuid4()),
        username=username,
        display_name=settings.dashboard_username,
        password_hash=hash_password(settings.dashboard_password, settings.password_hash_iterations),
        role="OWNER",
        is_active=True,
        subscription_plan=settings.subscription_plan_name,
        subscription_fee=settings.subscription_fee,
        subscription_currency=settings.subscription_currency,
        subscription_cycle=settings.subscription_billing_cycle,
        subscription_started_at=_parse_setting_datetime(settings.subscription_started_at)
        or datetime.now(UTC),
        subscription_expires_at=_parse_setting_datetime(settings.subscription_expires_at),
    )
    session.add(user)
    await session.flush()
    return user


async def create_initial_owner(
    session: AsyncSession,
    settings: Settings,
    *,
    username: str,
    password: str,
    display_name: str | None = None,
) -> DashboardUser:
    """Create the first owner only when setup was explicitly enabled by the deployer.

    This is intentionally not general public registration. All later operator accounts are
    created by an authenticated administrator, because every account reaches one shared
    trading-control plane.
    """
    if not settings.initial_owner_signup_enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Initial owner signup is disabled")
    normalized = (username or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{3,64}", normalized):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Username must be 3-64 characters [a-z0-9._-]")

    # Lock a singleton row so two first-run requests cannot both become OWNER on PostgreSQL.
    await ensure_subscription(session, settings)
    await session.scalar(select(ServiceSubscription).where(ServiceSubscription.id == 1).with_for_update())
    existing = int(await session.scalar(select(func.count()).select_from(DashboardUser)) or 0)
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An owner account already exists; ask an administrator to create an operator account")
    owner = DashboardUser(
        id=str(uuid4()),
        username=normalized,
        display_name=(display_name or username).strip()[:96] or normalized,
        password_hash=hash_password(password, settings.password_hash_iterations),
        role="OWNER",
        is_active=True,
        subscription_plan=settings.subscription_plan_name,
        subscription_fee=settings.subscription_fee,
        subscription_currency=settings.subscription_currency,
        subscription_cycle=settings.subscription_billing_cycle,
        subscription_started_at=datetime.now(UTC),
    )
    session.add(owner)
    session.add(
        AuditEvent(
            actor=normalized,
            action="INITIAL_OWNER_CREATED",
            entity_type="user",
            entity_id=owner.id,
            details={"role": "OWNER"},
        )
    )
    await session.flush()
    return owner


async def get_user_by_username(session: AsyncSession, username: str) -> DashboardUser | None:
    normalized = (username or "").strip().lower()
    if not normalized:
        return None
    return await session.scalar(select(DashboardUser).where(DashboardUser.username == normalized))


async def authenticate_user(
    session: AsyncSession, settings: Settings, username: str, password: str
) -> DashboardUser:
    """Verify credentials with lockout on repeated failures."""
    user = await get_user_by_username(session, username)
    now = datetime.now(UTC)
    if user is None:
        # Hash anyway so a missing username and a wrong password take comparable time.
        verify_password(password, hash_password("placeholder-password", 50_000))
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
    locked_until = as_utc(user.locked_until)
    if locked_until is not None and locked_until > now:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=f"Account temporarily locked. Try again after {locked_until.isoformat()}",
        )
    if not verify_password(password, user.password_hash):
        user.failed_login_count = int(user.failed_login_count or 0) + 1
        if user.failed_login_count >= max(settings.login_max_failures, 1):
            user.locked_until = now + timedelta(minutes=max(settings.login_lockout_minutes, 1))
            user.failed_login_count = 0
        session.add(
            AuditEvent(
                actor=user.username,
                action="DASHBOARD_LOGIN_FAILED",
                entity_type="user",
                entity_id=user.id,
                details={"locked_until": iso(user.locked_until)},
            )
        )
        await session.flush()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This account is disabled")
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    session.add(
        AuditEvent(
            actor=user.username,
            action="DASHBOARD_LOGIN",
            entity_type="user",
            entity_id=user.id,
            details={"role": user.role},
        )
    )
    await session.flush()
    return user


async def issue_session(
    session: AsyncSession,
    settings: Settings,
    user: DashboardUser,
    *,
    user_agent: str = "",
    remember: bool = True,
) -> tuple[str, DashboardSession]:
    """Issue a revocable session with a short or remembered lifetime.

    The browser never asks an operator to copy a server token. A remembered session is still
    finite, hashed at rest and invalidated immediately when it is revoked or the account is
    disabled.
    """
    token = new_session_token()
    if remember:
        hours = max(min(int(settings.dashboard_remember_session_days), 365), 1) * 24
    else:
        hours = max(min(int(settings.dashboard_session_hours), 24), 1)
    record = DashboardSession(
        id=str(uuid4()),
        user_id=user.id,
        token_hash=hash_token(token),
        csrf_token_hash="",
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=hours),
        last_seen_at=datetime.now(UTC),
        user_agent=(user_agent or "")[:160],
    )
    session.add(record)
    await session.flush()
    return token, record


async def resolve_session(
    session: AsyncSession, token: str
) -> tuple[DashboardUser, DashboardSession] | None:
    if not token:
        return None
    record = await session.scalar(
        select(DashboardSession).where(DashboardSession.token_hash == hash_token(token))
    )
    if record is None or record.revoked_at is not None:
        return None
    expires = as_utc(record.expires_at)
    if expires is None or expires <= datetime.now(UTC):
        return None
    user = await session.get(DashboardUser, record.user_id)
    if user is None or not user.is_active:
        return None
    now = datetime.now(UTC)
    last_seen = as_utc(record.last_seen_at)
    if last_seen is None or (now - last_seen).total_seconds() > 60:
        record.last_seen_at = now
    return user, record


async def revoke_session(session: AsyncSession, token: str, *, actor: str = "dashboard") -> bool:
    record = await session.scalar(
        select(DashboardSession).where(DashboardSession.token_hash == hash_token(token))
    )
    if record is None or record.revoked_at is not None:
        return False
    record.revoked_at = datetime.now(UTC)
    session.add(
        AuditEvent(
            actor=actor,
            action="DASHBOARD_LOGOUT",
            entity_type="session",
            entity_id=record.id,
            details={},
        )
    )
    await session.flush()
    return True


async def revoke_user_sessions(
    session: AsyncSession,
    user_id: str,
    *,
    actor: str,
    reason: str,
) -> int:
    """Invalidate every active session after a password or account-access change."""
    rows = list(
        (
            await session.scalars(
                select(DashboardSession).where(
                    DashboardSession.user_id == user_id,
                    DashboardSession.revoked_at.is_(None),
                )
            )
        ).all()
    )
    if not rows:
        return 0
    now = datetime.now(UTC)
    for record in rows:
        record.revoked_at = now
    session.add(
        AuditEvent(
            actor=actor,
            action="DASHBOARD_SESSIONS_REVOKED",
            entity_type="user",
            entity_id=user_id,
            details={"count": len(rows), "reason": reason[:120]},
        )
    )
    await session.flush()
    return len(rows)


def user_payload(user: DashboardUser) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name or user.username,
        "role": user.role,
        "is_active": user.is_active,
        "last_login_at": iso(user.last_login_at),
        "disclaimer_accepted_at": iso(user.disclaimer_accepted_at),
        "created_at": iso(user.created_at),
        "subscription": user_subscription_state(user),
    }


def role_at_least(role: str | None, minimum: str) -> bool:
    return ROLE_RANK.get((role or "").upper(), -1) >= ROLE_RANK.get(minimum.upper(), 99)


async def current_user(request: Request) -> DashboardUser | None:
    """Return the signed-in operator for this request (read-only snapshot)."""
    user = getattr(request.state, "user", None)
    if isinstance(user, DashboardUser):
        return user
    return None


async def writable_user(session: AsyncSession, request: Request) -> DashboardUser | None:
    """Re-load the signed-in operator inside the handler's own session before mutation.

    The authentication dependency resolves the user in its own short-lived session, so the
    instance attached to the request is detached. Handlers that change account fields must
    work with a session-bound instance or the change would never be flushed.
    """
    snapshot = await current_user(request)
    if snapshot is None:
        return None
    return await session.get(DashboardUser, snapshot.id)


async def require_dashboard_auth(request: Request) -> None:
    """Primary authentication dependency for every dashboard route.

    Order of acceptance:
    1. `X-Session-Token` — an operator session issued by username/password sign-in.
    2. `X-Dashboard-Token` — an optional server-side automation credential.

    Dashboard authentication cannot be disabled at runtime. Leaving a shared trading console
    unauthenticated would allow any same-origin caller to operate the bot.
    """
    settings: Settings = request.app.state.settings
    session_token = request.headers.get("x-session-token", "")
    if session_token:
        factory = request.app.state.session_factory
        async with factory() as session:
            resolved = await resolve_session(session, session_token)
            if resolved is not None:
                user, record = resolved
                await session.commit()
                request.state.user = user
                request.state.session = record
                request.state.auth_method = "SESSION"
                return None
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or revoked. Sign in again.",
        )

    configured = settings.dashboard_api_token
    if configured and constant_time_equals(request.headers.get("x-dashboard-token"), configured):
        request.state.auth_method = "SERVER_TOKEN"
        return None

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Dashboard authentication required",
    )


async def require_mutation_allowed(request: Request) -> None:
    """Block state-changing calls while the operator must accept terms or renew a plan.

    Read-only endpoints stay available so an expired subscription never hides account,
    subscription or audit information from the operator.
    """
    if request.method.upper() in SAFE_METHODS:
        return
    if request.url.path.startswith("/api/auth/"):
        # Sign-in, sign-out and the terms acknowledgement must stay reachable while the
        # disclaimer gate is closed, otherwise the operator could never clear it.
        return
    settings: Settings = request.app.state.settings
    if getattr(request.state, "auth_method", "") != "SESSION":
        return
    user = getattr(request.state, "user", None)
    if not isinstance(user, DashboardUser):
        return
    if settings.require_disclaimer_ack and user.disclaimer_accepted_at is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="DISCLAIMER_NOT_ACCEPTED: acknowledge the risk disclaimer to continue",
        )
    if settings.enforce_subscription and settings.subscription_expires_at:
        state_value = user_subscription_state(user)
        if state_value.get("expired") is True:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="SUBSCRIPTION_EXPIRED: renew the plan to make changes",
            )


def decimal_or_zero(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception:
        return Decimal("0")
    return parsed if parsed.is_finite() and parsed >= 0 else Decimal("0")
