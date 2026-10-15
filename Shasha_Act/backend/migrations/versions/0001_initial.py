"""Create isolated Shasha_Act operational tables.

Revision ID: 0001_shasha_act
Revises:
Create Date: 2026-10-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_shasha_act"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)
PRICE = sa.Numeric(28, 12, asdecimal=True)


def upgrade() -> None:
    op.create_table(
        "shasha_act_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("supplied_event_id", sa.String(128)),
        sa.Column("strategy", sa.String(32), nullable=False, server_default="SHASHA"),
        sa.Column("tradingview_symbol", sa.String(64), nullable=False, server_default=""),
        sa.Column("symbol", sa.String(64)),
        sa.Column("direction", sa.String(8), nullable=False, server_default="LONG"),
        sa.Column("timeframe", sa.String(24), nullable=False, server_default=""),
        sa.Column("bar_time", TS),
        sa.Column("signal_close", PRICE),
        sa.Column("entry_price", PRICE),
        sa.Column("raw_payload", sa.JSON),
        sa.Column("raw_body_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False, server_default="RECEIVED"),
        sa.Column("status_reason", sa.Text, nullable=False, server_default=""),
        sa.Column("mode", sa.String(8), nullable=False, server_default="PAPER"),
        sa.Column("received_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("idempotency_key", name="uq_shasha_act_events_idempotency"),
    )
    op.create_index("ix_shasha_act_events_received", "shasha_act_events", ["received_at"])
    op.create_index("ix_shasha_act_events_status", "shasha_act_events", ["status"])
    op.create_index("ix_shasha_act_events_symbol", "shasha_act_events", ["symbol"])

    op.create_table(
        "shasha_act_trades",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event_id", sa.String(36), sa.ForeignKey("shasha_act_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False, server_default="PAPER"),
        sa.Column("tradingview_symbol", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False, server_default="LONG"),
        sa.Column("status", sa.String(32), nullable=False, server_default="QUEUED"),
        sa.Column("status_reason", sa.Text, nullable=False, server_default=""),
        sa.Column("planned_entry", PRICE, nullable=False),
        sa.Column("signal_close", PRICE, nullable=False),
        sa.Column("actual_entry", PRICE),
        sa.Column("quantity", PRICE),
        sa.Column("remaining_quantity", PRICE),
        sa.Column("target1_quantity", PRICE),
        sa.Column("target2_quantity", PRICE),
        sa.Column("target3_quantity", PRICE),
        sa.Column("margin", PRICE, nullable=False),
        sa.Column("notional", PRICE, nullable=False),
        sa.Column("leverage", sa.Integer, nullable=False),
        sa.Column("stop_price", PRICE),
        sa.Column("initial_stop_price", PRICE),
        sa.Column("target1_price", PRICE),
        sa.Column("target2_price", PRICE),
        sa.Column("target3_price", PRICE),
        sa.Column("target_progress", sa.Integer, nullable=False, server_default="0"),
        sa.Column("stop_stage", sa.String(24), nullable=False, server_default="INITIAL"),
        sa.Column("protection_state", sa.String(48), nullable=False, server_default="NOT_ESTABLISHED"),
        sa.Column("entry_order_id", sa.String(96)),
        sa.Column("exchange_position_id", sa.String(96)),
        sa.Column("protective_stop_order_id", sa.String(128)),
        sa.Column("target_order_ids", sa.JSON, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("entry_expires_at", TS),
        sa.Column("submission_started_at", TS),
        sa.Column("opened_at", TS),
        sa.Column("closed_at", TS),
        sa.Column("exit_price", PRICE),
        sa.Column("last_price", PRICE),
        sa.Column("liquidation_price", PRICE),
        sa.Column("unrealized_pnl", PRICE, nullable=False, server_default="0"),
        sa.Column("gross_realized_pnl", PRICE, nullable=False, server_default="0"),
        sa.Column("net_realized_pnl", PRICE, nullable=False, server_default="0"),
        sa.Column("fees_paid", PRICE, nullable=False, server_default="0"),
        sa.Column("funding_paid", PRICE),
        sa.Column("fee_data_available", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("funding_data_available", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("event_id", name="uq_shasha_act_trades_event"),
    )
    op.create_index("ix_shasha_act_trades_status", "shasha_act_trades", ["status"])
    op.create_index("ix_shasha_act_trades_symbol", "shasha_act_trades", ["symbol"])
    op.create_index("ix_shasha_act_trades_opened", "shasha_act_trades", ["opened_at"])
    op.create_index("ix_shasha_act_trades_closed", "shasha_act_trades", ["closed_at"])
    active_predicate = sa.text(
        "status IN ('QUEUED','SUBMITTING','ENTRY_PENDING','OPEN','PARTIALLY_CLOSED',"
        "'PROTECTION_ERROR','UNKNOWN','CLOSING','ENTRY_PARTIAL')"
    )
    op.create_index(
        "uq_shasha_act_trades_active_symbol",
        "shasha_act_trades",
        ["symbol"],
        unique=True,
        postgresql_where=active_predicate,
        sqlite_where=active_predicate,
    )

    op.create_table(
        "shasha_act_orders",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("trade_id", sa.String(36), sa.ForeignKey("shasha_act_trades.id", ondelete="CASCADE"), nullable=False),
        sa.Column("client_order_id", sa.String(96), nullable=False),
        sa.Column("exchange_order_id", sa.String(128)),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("order_type", sa.String(24), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="NEW"),
        sa.Column("quantity", PRICE, nullable=False),
        sa.Column("filled_quantity", PRICE, nullable=False, server_default="0"),
        sa.Column("price", PRICE),
        sa.Column("average_fill_price", PRICE),
        sa.Column("reduce_only", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("request_summary", sa.JSON),
        sa.Column("error_code", sa.String(64), nullable=False, server_default=""),
        sa.Column("error_message", sa.Text, nullable=False, server_default=""),
        sa.Column("submitted_at", TS),
        sa.Column("filled_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("client_order_id", name="uq_shasha_act_orders_client_id"),
    )
    op.create_index("ix_shasha_act_orders_trade_status", "shasha_act_orders", ["trade_id", "status"])
    op.create_index("ix_shasha_act_orders_exchange_id", "shasha_act_orders", ["exchange_order_id"])

    op.create_table(
        "shasha_act_fills",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_fill_id", sa.String(128)),
        sa.Column("trade_id", sa.String(36), sa.ForeignKey("shasha_act_trades.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_id", sa.String(36), sa.ForeignKey("shasha_act_orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("entry_exit", sa.String(8), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("price", PRICE, nullable=False),
        sa.Column("quantity", PRICE, nullable=False),
        sa.Column("fee", PRICE, nullable=False, server_default="0"),
        sa.Column("gross_realized_pnl", PRICE, nullable=False, server_default="0"),
        sa.Column("funding", PRICE),
        sa.Column("fill_time", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("source_fill_id", name="uq_shasha_act_fills_source_id"),
    )
    op.create_index("ix_shasha_act_fills_trade_time", "shasha_act_fills", ["trade_id", "fill_time"])
    op.create_index("ix_shasha_act_fills_order", "shasha_act_fills", ["order_id"])

    op.create_table(
        "shasha_act_outbox",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event_id", sa.String(36), sa.ForeignKey("shasha_act_events.id", ondelete="CASCADE")),
        sa.Column("depends_on_id", sa.String(36), sa.ForeignKey("shasha_act_outbox.id", ondelete="SET NULL")),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("priority", sa.Integer, nullable=False, server_default="100"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("available_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("locked_until", TS),
        sa.Column("payload", sa.JSON),
        sa.Column("last_error", sa.Text, nullable=False, server_default=""),
        sa.Column("delivered_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("idempotency_key", name="uq_shasha_act_outbox_idempotency"),
    )
    op.create_index("ix_shasha_act_outbox_ready", "shasha_act_outbox", ["status", "available_at"])
    op.create_index("ix_shasha_act_outbox_event", "shasha_act_outbox", ["event_id"])

    op.create_table(
        "shasha_act_risk_control",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("trading_mode", sa.String(8), nullable=False, server_default="PAPER"),
        sa.Column("max_open_positions", sa.Integer, nullable=False, server_default="3"),
        sa.Column("margin_per_trade", PRICE, nullable=False, server_default="100"),
        sa.Column("leverage", sa.Integer, nullable=False, server_default="10"),
        sa.Column("target1_percent", PRICE, nullable=False, server_default="1"),
        sa.Column("target2_percent", PRICE, nullable=False, server_default="2"),
        sa.Column("target3_percent", PRICE, nullable=False, server_default="3"),
        sa.Column("stop_loss_percent", PRICE, nullable=False, server_default="2"),
        sa.Column("max_notional_per_trade", PRICE, nullable=False, server_default="1000"),
        sa.Column("max_daily_loss", PRICE, nullable=False, server_default="200"),
        sa.Column("limit_entry_expiry_seconds", sa.Integer, nullable=False, server_default="900"),
        sa.Column("cancel_stale_entry_orders", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("kill_switch", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("allow_symbols", sa.JSON, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("deny_symbols", sa.JSON, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("live_confirmed_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "shasha_act_audit_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("actor", sa.String(64), nullable=False, server_default="system"),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.String(96), nullable=False),
        sa.Column("details", sa.JSON),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_shasha_act_audit_created", "shasha_act_audit_events", ["created_at"])

    op.create_table(
        "shasha_act_market_snapshots",
        sa.Column("symbol", sa.String(64), primary_key=True),
        sa.Column("last_price", PRICE, nullable=False),
        sa.Column("change_24h_percent", PRICE),
        sa.Column("volume_24h", PRICE),
        sa.Column("high_24h", PRICE),
        sa.Column("low_24h", PRICE),
        sa.Column("source", sa.String(24), nullable=False, server_default="SHARK_LAST"),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_shasha_act_market_updated", "shasha_act_market_snapshots", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_shasha_act_market_updated", table_name="shasha_act_market_snapshots")
    op.drop_table("shasha_act_market_snapshots")
    op.drop_index("ix_shasha_act_audit_created", table_name="shasha_act_audit_events")
    op.drop_table("shasha_act_audit_events")
    op.drop_table("shasha_act_risk_control")
    op.drop_index("ix_shasha_act_outbox_event", table_name="shasha_act_outbox")
    op.drop_index("ix_shasha_act_outbox_ready", table_name="shasha_act_outbox")
    op.drop_table("shasha_act_outbox")
    op.drop_index("ix_shasha_act_fills_order", table_name="shasha_act_fills")
    op.drop_index("ix_shasha_act_fills_trade_time", table_name="shasha_act_fills")
    op.drop_table("shasha_act_fills")
    op.drop_index("ix_shasha_act_orders_exchange_id", table_name="shasha_act_orders")
    op.drop_index("ix_shasha_act_orders_trade_status", table_name="shasha_act_orders")
    op.drop_table("shasha_act_orders")
    op.drop_index("uq_shasha_act_trades_active_symbol", table_name="shasha_act_trades")
    op.drop_index("ix_shasha_act_trades_closed", table_name="shasha_act_trades")
    op.drop_index("ix_shasha_act_trades_opened", table_name="shasha_act_trades")
    op.drop_index("ix_shasha_act_trades_symbol", table_name="shasha_act_trades")
    op.drop_index("ix_shasha_act_trades_status", table_name="shasha_act_trades")
    op.drop_table("shasha_act_trades")
    op.drop_index("ix_shasha_act_events_symbol", table_name="shasha_act_events")
    op.drop_index("ix_shasha_act_events_status", table_name="shasha_act_events")
    op.drop_index("ix_shasha_act_events_received", table_name="shasha_act_events")
    op.drop_table("shasha_act_events")
