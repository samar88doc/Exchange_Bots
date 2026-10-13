"""SQLAlchemy ORM entities — the database is the application's source of truth.

Tables (per spec):
settings, webhook_events, signals, orders, fills, positions, position_events,
trades, strategies, symbols, symbol_mappings, risk_events, system_events,
audit_logs, paper_balances, market_snapshots, reconciliation_events,
position_reservations, state_locks.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base
from app.models.enums import (
    ContractStatus,
    Direction,
    EntryExit,
    OrderSide,
    OrderStatus,
    OrderType,
    ProcessingStatus,
    ReservationStatus,
    SignalListStatus,
    TradingMode,
    ValidationStatus,
)

TS = DateTime(timezone=True)


from datetime import UTC

def utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow, server_default=func.now())


class Setting(Base, TimestampMixin):
    __tablename__ = "settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    value_type: Mapped[str] = mapped_column(String(16), default="string")
    description: Mapped[str] = mapped_column(Text, default="")


class Strategy(Base, TimestampMixin):
    __tablename__ = "strategies"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    webhook_identifier: Mapped[str] = mapped_column(String(128), default="")
    allowed_symbols: Mapped[list | None] = mapped_column(JSON, default=list)  # empty = all
    alert_count: Mapped[int] = mapped_column(Integer, default=0)
    executed_trades: Mapped[int] = mapped_column(Integer, default=0)
    rejected_trades: Mapped[int] = mapped_column(Integer, default=0)


class Symbol(Base, TimestampMixin):
    """Canonical symbol catalog (internal identity of a contract)."""

    __tablename__ = "symbols"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_symbol: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    base_asset: Mapped[str] = mapped_column(String(32), default="")
    quote_asset: Mapped[str] = mapped_column(String(32), default="")
    market_type: Mapped[str] = mapped_column(String(32), default="PERPETUAL")
    description: Mapped[str] = mapped_column(String(256), default="")


class SymbolMapping(Base, TimestampMixin):
    """TradingView raw -> canonical -> Shark symbol, with contract rules."""

    __tablename__ = "symbol_mappings"
    __table_args__ = (
        UniqueConstraint("tradingview_raw_symbol", name="uq_symbol_mapping_tv_raw"),
        UniqueConstraint("canonical_symbol", name="uq_symbol_mapping_canonical"),
        Index("ix_symbol_mappings_canonical", "canonical_symbol"),
        Index("ix_symbol_mappings_shark", "shark_symbol"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tradingview_raw_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    shark_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), default="shark")
    market_type: Mapped[str] = mapped_column(String(32), default="PERPETUAL")
    contract_type: Mapped[str] = mapped_column(String(32), default="PERPETUAL")
    contract_status: Mapped[str] = mapped_column(String(16), default=ContractStatus.ACTIVE.value)
    base_asset: Mapped[str] = mapped_column(String(32), default="")
    quote_asset: Mapped[str] = mapped_column(String(32), default="")
    quantity_precision: Mapped[int] = mapped_column(Integer, default=3)
    price_precision: Mapped[int] = mapped_column(Integer, default=2)
    minimum_quantity: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    minimum_notional: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    quantity_step: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    price_tick_size: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    max_leverage: Mapped[int | None] = mapped_column(Integer, nullable=True)
    leverage_constraints: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    verified_against_exchange: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_contract_meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class WebhookEvent(Base, TimestampMixin):
    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_webhook_events_event_id"),
        UniqueConstraint("fingerprint", name="uq_webhook_events_fingerprint"),
        Index("ix_webhook_events_received_at", "received_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), default="")
    source: Mapped[str] = mapped_column(String(32), default="TRADINGVIEW")
    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=False)
    raw_body_hash: Mapped[str] = mapped_column(String(64), default="")
    received_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    event_timestamp: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=ProcessingStatus.RECEIVED.value)
    rejection_reason: Mapped[str] = mapped_column(String(256), default="")
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    replay_of: Mapped[str | None] = mapped_column(String(128), nullable=True)


class Signal(Base, TimestampMixin):
    """Normalized internal signal — preserves raw/canonical/shark symbol triple."""

    __tablename__ = "signals"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_signals_event_id"),
        UniqueConstraint("fingerprint", name="uq_signals_fingerprint"),
        Index("ix_signals_canonical_symbol", "canonical_symbol"),
        Index("ix_signals_received_at", "received_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), default="")
    received_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    event_timestamp: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    strategy_id: Mapped[str] = mapped_column(String(64), default="")
    strategy_name: Mapped[str] = mapped_column(String(128), default="")
    source: Mapped[str] = mapped_column(String(32), default="TRADINGVIEW")
    tradingview_raw_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    shark_symbol: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), default=Direction.LONG.value)
    timeframe: Mapped[str] = mapped_column(String(16), default="")
    alert_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    raw_alert_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
    validation_status: Mapped[str] = mapped_column(String(16), default=ValidationStatus.RECEIVED.value)
    processing_status: Mapped[str] = mapped_column(String(32), default=ProcessingStatus.RECEIVED.value)
    rejection_reason: Mapped[str] = mapped_column(String(256), default="")
    status_display: Mapped[str] = mapped_column(String(32), default=SignalListStatus.RECEIVED.value)
    position_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mode: Mapped[str] = mapped_column(String(8), default=TradingMode.PAPER.value)
    is_replay: Mapped[bool] = mapped_column(Boolean, default=False)


class Position(Base, TimestampMixin):
    """Position identity is canonical_symbol. Also used for slot reservations."""

    __tablename__ = "positions"
    __table_args__ = (
        Index("ix_positions_canonical_symbol", "canonical_symbol"),
        Index("ix_positions_state", "state"),
        UniqueConstraint("position_id", name="uq_positions_position_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    tradingview_raw_symbol: Mapped[str] = mapped_column(String(64), default="")
    shark_symbol: Mapped[str] = mapped_column(String(64), default="")
    exchange: Mapped[str] = mapped_column(String(32), default="paper")
    direction: Mapped[str] = mapped_column(String(8), default=Direction.LONG.value)
    state: Mapped[str] = mapped_column(String(32), default="RESERVED")
    mode: Mapped[str] = mapped_column(String(8), default=TradingMode.PAPER.value)
    strategy_id: Mapped[str] = mapped_column(String(64), default="")
    signal_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("signals.id"), nullable=True)
    event_id: Mapped[str] = mapped_column(String(128), default="")
    # sizing
    margin: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    leverage: Mapped[int] = mapped_column(Integer, default=1)
    notional: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    quantity: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    # prices / pnl
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    mark_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    target_roi_percent: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("10"))
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    roi_percent: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    entry_fee: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    exit_fee: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    funding: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    # exchange refs
    entry_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exit_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exchange_position_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # lifecycle
    open_time: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    close_time: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    close_reason: Mapped[str] = mapped_column(String(64), default="")
    rejection_reason: Mapped[str] = mapped_column(String(64), default="")
    is_dry_run: Mapped[bool] = mapped_column(Boolean, default=False)


class Order(Base, TimestampMixin):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_orders_order_id"),
        Index("ix_orders_position_id", "position_id"),
        Index("ix_orders_shark_order_id", "exchange_order_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    position_id: Mapped[str] = mapped_column(String(64), default="")
    signal_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("signals.id"), nullable=True)
    mode: Mapped[str] = mapped_column(String(8), default=TradingMode.PAPER.value)
    exchange: Mapped[str] = mapped_column(String(32), default="paper")
    tradingview_raw_symbol: Mapped[str] = mapped_column(String(64), default="")
    canonical_symbol: Mapped[str] = mapped_column(String(64), default="")
    shark_symbol: Mapped[str] = mapped_column(String(64), default="")
    side: Mapped[str] = mapped_column(String(8), default=OrderSide.BUY.value)
    order_type: Mapped[str] = mapped_column(String(16), default=OrderType.MARKET.value)
    entry_exit: Mapped[str] = mapped_column(String(8), default=EntryExit.ENTRY.value)
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    requested_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    avg_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    filled_quantity: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(16), default=OrderStatus.PENDING.value)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    client_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    submitted_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str] = mapped_column(String(128), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    submission_fingerprint: Mapped[str] = mapped_column(Text, default="")
    submitted_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    filled_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    is_dry_run: Mapped[bool] = mapped_column(Boolean, default=False)


class Fill(Base, TimestampMixin):
    __tablename__ = "fills"
    __table_args__ = (Index("ix_fills_order_id", "order_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    position_id: Mapped[str] = mapped_column(String(64), default="")
    mode: Mapped[str] = mapped_column(String(8), default=TradingMode.PAPER.value)
    side: Mapped[str] = mapped_column(String(8), default=OrderSide.BUY.value)
    entry_exit: Mapped[str] = mapped_column(String(8), default=EntryExit.ENTRY.value)
    price: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    quantity: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    fee: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    slippage: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    fill_time: Mapped[datetime] = mapped_column(TS, default=utcnow)


class PositionEvent(Base, TimestampMixin):
    """Complete chronological event timeline for a position (explainability)."""

    __table_args__ = (Index("ix_position_events_position_id", "position_id"),)
    __tablename__ = "position_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    from_state: Mapped[str] = mapped_column(String(32), default="")
    to_state: Mapped[str] = mapped_column(String(32), default="")
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class Trade(Base, TimestampMixin):
    """Completed round-trip trade record (professional trade history)."""

    __tablename__ = "trades"
    __table_args__ = (
        Index("ix_trades_closed_at", "closed_at"),
        Index("ix_trades_canonical_symbol", "canonical_symbol"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    position_id: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(64), default="")
    strategy_name: Mapped[str] = mapped_column(String(128), default="")
    mode: Mapped[str] = mapped_column(String(8), default=TradingMode.PAPER.value)
    status: Mapped[str] = mapped_column(String(16), default="CLOSED")
    result: Mapped[str] = mapped_column(String(8), default="")  # WIN / LOSS
    tradingview_raw_symbol: Mapped[str] = mapped_column(String(64), default="")
    canonical_symbol: Mapped[str] = mapped_column(String(64), default="")
    shark_symbol: Mapped[str] = mapped_column(String(64), default="")
    side: Mapped[str] = mapped_column(String(8), default=Direction.LONG.value)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    exit_price: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    quantity: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    margin: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    leverage: Mapped[int] = mapped_column(Integer, default=1)
    entry_fee: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    exit_fee: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    funding: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    gross_pnl: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    net_pnl: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    roi_percent: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    price_change_percent: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    opened_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    close_reason: Mapped[str] = mapped_column(String(64), default="")
    event_id: Mapped[str] = mapped_column(String(128), default="")
    entry_order_id: Mapped[str] = mapped_column(String(64), default="")
    exit_order_id: Mapped[str] = mapped_column(String(64), default="")


class PositionReservation(Base, TimestampMixin):
    """Atomic slot reservation. Counts toward OPEN + RESERVED <= MAX."""

    __tablename__ = "position_reservations"
    __table_args__ = (
        UniqueConstraint("canonical_symbol", name="uq_reservations_canonical_symbol"),
        Index("ix_reservations_status", "status"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reservation_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    canonical_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    position_id: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(64), default="")
    event_id: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(16), default=ReservationStatus.ACTIVE.value)
    expires_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)


class RiskEvent(Base, TimestampMixin):
    __tablename__ = "risk_events"
    __table_args__ = (Index("ix_risk_events_created_at", "created_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(64), default="")
    event_id: Mapped[str] = mapped_column(String(128), default="")
    canonical_symbol: Mapped[str] = mapped_column(String(64), default="")


class SystemEvent(Base, TimestampMixin):
    __tablename__ = "system_events"
    __table_args__ = (Index("ix_system_events_created_at", "created_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[str] = mapped_column(String(16), default="INFO")
    message: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(64), default="")
    event_id: Mapped[str] = mapped_column(String(128), default="")
    tradingview_raw_symbol: Mapped[str] = mapped_column(String(64), default="")
    canonical_symbol: Mapped[str] = mapped_column(String(64), default="")
    shark_symbol: Mapped[str] = mapped_column(String(64), default="")
    strategy_id: Mapped[str] = mapped_column(String(64), default="")
    mode: Mapped[str] = mapped_column(String(8), default=TradingMode.PAPER.value)


class AuditLog(Base, TimestampMixin):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_created_at", "created_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    entity: Mapped[str] = mapped_column(String(64), default="")
    entity_id: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(64), default="")


class PaperBalance(Base, TimestampMixin):
    __tablename__ = "paper_balances"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset: Mapped[str] = mapped_column(String(32), default="USDT")
    balance: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    initial_balance: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    locked_margin: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal("0"))


class MarketSnapshot(Base, TimestampMixin):
    __tablename__ = "market_snapshots"
    __table_args__ = (
        Index("ix_market_snapshots_symbol_ts", "canonical_symbol", "ts"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    shark_symbol: Mapped[str] = mapped_column(String(64), default="")
    source: Mapped[str] = mapped_column(String(32), default="shark_public")
    last_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    mark_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    bid: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    ask: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    ts: Mapped[datetime] = mapped_column(TS, default=utcnow)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ReconciliationEvent(Base, TimestampMixin):
    __tablename__ = "reconciliation_events"
    __table_args__ = (Index("ix_reconciliation_events_created_at", "created_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    severity: Mapped[str] = mapped_column(String(64), default="CRITICAL_RECONCILIATION_EVENT")
    kind: Mapped[str] = mapped_column(String(64), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    internal_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    exchange_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)


class StateLock(Base):
    """Single-row lock table used for cross-process advisory locking on Postgres."""

    __tablename__ = "state_locks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)


class SentimentHistory(Base):
    """Persisted market sentiment transitions — survives page refresh and server restarts."""

    __tablename__ = "sentiment_history"
    __table_args__ = (Index("ix_sentiment_history_occurred_at", "occurred_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(TS, default=utcnow, nullable=False)
    from_sentiment: Mapped[str] = mapped_column(String(16), nullable=False)
    to_sentiment: Mapped[str] = mapped_column(String(16), nullable=False)
