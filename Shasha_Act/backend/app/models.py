"""Namespaced PostgreSQL persistence models for Shasha_Act only."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, ExactDecimal, utcnow

TS = DateTime(timezone=True)
# Exact Decimal storage: see ExactDecimal for why SQLite needs the re-quantize step.
PRICE = ExactDecimal()


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TS, default=utcnow, onupdate=utcnow, server_default=func.now()
    )


class SignalEvent(Base, TimestampMixin):
    __tablename__ = "shasha_act_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_shasha_act_events_idempotency"),
        Index("ix_shasha_act_events_received", "received_at"),
        Index("ix_shasha_act_events_status", "status"),
        Index("ix_shasha_act_events_symbol", "symbol"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    supplied_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    strategy: Mapped[str] = mapped_column(String(32), nullable=False, default="SHASHA")
    tradingview_symbol: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    direction: Mapped[str] = mapped_column(String(8), nullable=False, default="LONG")
    timeframe: Mapped[str] = mapped_column(String(24), nullable=False, default="")
    bar_time: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    signal_close: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    entry_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    raw_body_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="RECEIVED")
    status_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default="PAPER")
    received_at: Mapped[datetime] = mapped_column(TS, default=utcnow, nullable=False)


class Trade(Base, TimestampMixin):
    __tablename__ = "shasha_act_trades"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_shasha_act_trades_event"),
        Index("ix_shasha_act_trades_status", "status"),
        Index("ix_shasha_act_trades_symbol", "symbol"),
        Index("ix_shasha_act_trades_opened", "opened_at"),
        Index("ix_shasha_act_trades_closed", "closed_at"),
        Index(
            "uq_shasha_act_trades_active_symbol",
            "symbol",
            unique=True,
            postgresql_where=text(
                "status IN ('QUEUED','SUBMITTING','ENTRY_PENDING','OPEN','PARTIALLY_CLOSED',"
                "'PROTECTION_ERROR','UNKNOWN','CLOSING','ENTRY_PARTIAL')"
            ),
            sqlite_where=text(
                "status IN ('QUEUED','SUBMITTING','ENTRY_PENDING','OPEN','PARTIALLY_CLOSED',"
                "'PROTECTION_ERROR','UNKNOWN','CLOSING','ENTRY_PARTIAL')"
            ),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("shasha_act_events.id", ondelete="CASCADE"), nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default="PAPER")
    tradingview_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False, default="LONG")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="QUEUED")
    status_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    planned_entry: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    signal_close: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    actual_entry: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    quantity: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    remaining_quantity: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    target1_quantity: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    target2_quantity: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    target3_quantity: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    margin: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    notional: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    leverage: Mapped[int] = mapped_column(Integer, nullable=False)
    stop_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    initial_stop_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    target1_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    target2_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    target3_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    target_progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    partial_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    trailing_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    trailing_high_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    trailing_stop_percent: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    partial_close_percent: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    price_tick: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    trailing_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    secondary_targets_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    entry_price_source: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    entry_candle_time: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    stop_stage: Mapped[str] = mapped_column(String(24), nullable=False, default="INITIAL")
    protection_state: Mapped[str] = mapped_column(String(48), nullable=False, default="NOT_ESTABLISHED")
    entry_order_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    exchange_position_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    protective_stop_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_order_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    entry_expires_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    submission_started_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    exit_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    last_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    liquidation_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    unrealized_pnl: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=Decimal("0"))
    gross_realized_pnl: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=Decimal("0"))
    net_realized_pnl: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=Decimal("0"))
    fees_paid: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=Decimal("0"))
    funding_paid: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    fee_data_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    funding_data_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Order(Base, TimestampMixin):
    __tablename__ = "shasha_act_orders"
    __table_args__ = (
        UniqueConstraint("client_order_id", name="uq_shasha_act_orders_client_id"),
        Index("ix_shasha_act_orders_trade_status", "trade_id", "status"),
        Index("ix_shasha_act_orders_exchange_id", "exchange_order_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    trade_id: Mapped[str] = mapped_column(ForeignKey("shasha_act_trades.id", ondelete="CASCADE"), nullable=False)
    client_order_id: Mapped[str] = mapped_column(String(96), nullable=False)
    exchange_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="NEW")
    quantity: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    filled_quantity: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=Decimal("0"))
    price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    average_fill_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    request_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    submitted_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    filled_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)


class Fill(Base, TimestampMixin):
    __tablename__ = "shasha_act_fills"
    __table_args__ = (
        UniqueConstraint("source_fill_id", name="uq_shasha_act_fills_source_id"),
        Index("ix_shasha_act_fills_trade_time", "trade_id", "fill_time"),
        Index("ix_shasha_act_fills_order", "order_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_fill_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trade_id: Mapped[str] = mapped_column(ForeignKey("shasha_act_trades.id", ondelete="CASCADE"), nullable=False)
    order_id: Mapped[str] = mapped_column(ForeignKey("shasha_act_orders.id", ondelete="CASCADE"), nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
    entry_exit: Mapped[str] = mapped_column(String(8), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    fee: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=Decimal("0"))
    gross_realized_pnl: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=Decimal("0"))
    funding: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    fill_time: Mapped[datetime] = mapped_column(TS, nullable=False, default=utcnow)


class OutboxJob(Base, TimestampMixin):
    __tablename__ = "shasha_act_outbox"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_shasha_act_outbox_idempotency"),
        Index("ix_shasha_act_outbox_ready", "status", "available_at"),
        Index("ix_shasha_act_outbox_event", "event_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str | None] = mapped_column(
        ForeignKey("shasha_act_events.id", ondelete="CASCADE"), nullable=True
    )
    depends_on_id: Mapped[str | None] = mapped_column(
        ForeignKey("shasha_act_outbox.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(TS, nullable=False, default=utcnow)
    locked_until: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    delivered_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)


class RiskControl(Base, TimestampMixin):
    __tablename__ = "shasha_act_risk_control"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    trading_mode: Mapped[str] = mapped_column(String(8), nullable=False, default="PAPER")
    max_open_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    margin_per_trade: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=Decimal("100"))
    leverage: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    target1_percent: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=Decimal("2"))
    target2_percent: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=Decimal("3"))
    target3_percent: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=Decimal("4"))
    stop_loss_percent: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=Decimal("2"))
    target1_close_percent: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=Decimal("50"))
    trailing_stop_percent: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=Decimal("1"))
    trailing_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    enable_secondary_targets: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    entry_candle_interval: Mapped[str] = mapped_column(String(16), nullable=False, default="5m")
    max_notional_per_trade: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=Decimal("1000"))
    max_daily_loss: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=Decimal("200"))
    limit_entry_expiry_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=900)
    cancel_stale_entry_orders: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    kill_switch: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allow_symbols: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    deny_symbols: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    live_confirmed_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)


class AuditEvent(Base):
    __tablename__ = "shasha_act_audit_events"
    __table_args__ = (Index("ix_shasha_act_audit_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(96), nullable=False)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, default=utcnow, server_default=func.now())


class MarketSnapshot(Base):
    __tablename__ = "shasha_act_market_snapshots"
    __table_args__ = (Index("ix_shasha_act_market_updated", "updated_at"),)

    symbol: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    change_24h_percent: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    volume_24h: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    high_24h: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    low_24h: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="SHARK_LAST")
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, default=utcnow)


class DashboardUser(Base, TimestampMixin):
    """Operator account: username/password sign-in, role, and subscription window."""

    __tablename__ = "shasha_act_users"
    __table_args__ = (
        UniqueConstraint("username", name="uq_shasha_act_users_username"),
        Index("ix_shasha_act_users_active", "is_active"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(96), nullable=False, default="")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="OPERATOR")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    subscription_plan: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    subscription_fee: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=Decimal("0"))
    subscription_currency: Mapped[str] = mapped_column(String(8), nullable=False, default="INR")
    subscription_cycle: Mapped[str] = mapped_column(String(16), nullable=False, default="MONTHLY")
    subscription_started_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    subscription_expires_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    disclaimer_accepted_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(TS, nullable=True)


class DashboardSession(Base):
    """Server-issued session for cookie sign-in; only the token hash is stored."""

    __tablename__ = "shasha_act_sessions"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_shasha_act_sessions_token"),
        Index("ix_shasha_act_sessions_user", "user_id"),
        Index("ix_shasha_act_sessions_expiry", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("shasha_act_users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    csrf_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    user_agent: Mapped[str] = mapped_column(String(160), nullable=False, default="")


class ServiceSubscription(Base, TimestampMixin):
    """Single-row service plan shown in the dashboard (fee, cycle, expiry, status)."""

    __tablename__ = "shasha_act_subscription"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    plan_name: Mapped[str] = mapped_column(String(64), nullable=False, default="SHASHA ACT PRO")
    fee: Mapped[Decimal] = mapped_column(PRICE, nullable=False, default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="INR")
    billing_cycle: Mapped[str] = mapped_column(String(16), nullable=False, default="MONTHLY")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    started_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    contact: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")


class SentimentHistory(Base):
    __tablename__ = "shasha_act_sentiment_history"
    __table_args__ = (Index("ix_shasha_act_sentiment_occurred", "occurred_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(TS, nullable=False, default=utcnow, server_default=func.now())
    from_sentiment: Mapped[str] = mapped_column(String(32), nullable=False)
    to_sentiment: Mapped[str] = mapped_column(String(32), nullable=False)

