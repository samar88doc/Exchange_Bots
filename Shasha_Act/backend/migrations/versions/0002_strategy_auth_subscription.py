"""SHASHA entry/trailing strategy columns plus dashboard accounts, sessions and subscription.

Revision ID: 0002_strategy_auth
Revises: 0001_shasha_act
Create Date: 2026-10-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_strategy_auth"
down_revision = "0001_shasha_act"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)
PRICE = sa.Numeric(28, 12, asdecimal=True)


def upgrade() -> None:
    # ---- trades: Shark entry provenance and trailing-stop bookkeeping -------------------
    op.add_column("shasha_act_trades", sa.Column("partial_closed", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("shasha_act_trades", sa.Column("trailing_active", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("shasha_act_trades", sa.Column("trailing_high_price", PRICE))
    op.add_column("shasha_act_trades", sa.Column("trailing_stop_percent", PRICE))
    op.add_column("shasha_act_trades", sa.Column("partial_close_percent", PRICE))
    op.add_column("shasha_act_trades", sa.Column("price_tick", PRICE))
    op.add_column("shasha_act_trades", sa.Column("trailing_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("shasha_act_trades", sa.Column("secondary_targets_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("shasha_act_trades", sa.Column("entry_price_source", sa.String(64), nullable=False, server_default=""))
    op.add_column("shasha_act_trades", sa.Column("entry_candle_time", TS))

    # ---- risk control: strategy knobs ---------------------------------------------------
    op.add_column("shasha_act_risk_control", sa.Column("target1_close_percent", PRICE, nullable=False, server_default="50"))
    op.add_column("shasha_act_risk_control", sa.Column("trailing_stop_percent", PRICE, nullable=False, server_default="1"))
    op.add_column("shasha_act_risk_control", sa.Column("trailing_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("shasha_act_risk_control", sa.Column("enable_secondary_targets", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("shasha_act_risk_control", sa.Column("entry_candle_interval", sa.String(16), nullable=False, server_default="5m"))

    # ---- dashboard accounts -------------------------------------------------------------
    op.create_table(
        "shasha_act_users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(96), nullable=False, server_default=""),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="OPERATOR"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("subscription_plan", sa.String(64), nullable=False, server_default=""),
        sa.Column("subscription_fee", PRICE, nullable=False, server_default="0"),
        sa.Column("subscription_currency", sa.String(8), nullable=False, server_default="INR"),
        sa.Column("subscription_cycle", sa.String(16), nullable=False, server_default="MONTHLY"),
        sa.Column("subscription_started_at", TS),
        sa.Column("subscription_expires_at", TS),
        sa.Column("disclaimer_accepted_at", TS),
        sa.Column("last_login_at", TS),
        sa.Column("failed_login_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", TS),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("username", name="uq_shasha_act_users_username"),
    )
    op.create_index("ix_shasha_act_users_active", "shasha_act_users", ["is_active"])

    op.create_table(
        "shasha_act_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("shasha_act_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("csrf_token_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("last_seen_at", TS),
        sa.Column("revoked_at", TS),
        sa.Column("user_agent", sa.String(160), nullable=False, server_default=""),
        sa.UniqueConstraint("token_hash", name="uq_shasha_act_sessions_token"),
    )
    op.create_index("ix_shasha_act_sessions_user", "shasha_act_sessions", ["user_id"])
    op.create_index("ix_shasha_act_sessions_expiry", "shasha_act_sessions", ["expires_at"])

    # `shasha_act_sentiment_history` is referenced by the model layer and the
    # /api/market/sentiment-history endpoints but was missing from 0001, so a migrated
    # PostgreSQL database could not serve those routes.
    bind = op.get_bind()
    insp = sa.inspect(bind)
    table_names = set(insp.get_table_names())
    if "shasha_act_sentiment_history" not in table_names:
        op.create_table(
            "shasha_act_sentiment_history",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("occurred_at", TS, nullable=False, server_default=sa.func.now()),
            sa.Column("from_sentiment", sa.String(32), nullable=False),
            sa.Column("to_sentiment", sa.String(32), nullable=False),
        )
        op.create_index("ix_shasha_act_sentiment_occurred", "shasha_act_sentiment_history", ["occurred_at"])

    op.create_table(
        "shasha_act_subscription",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plan_name", sa.String(64), nullable=False, server_default="SHASHA ACT PRO"),
        sa.Column("fee", PRICE, nullable=False, server_default="0"),
        sa.Column("currency", sa.String(8), nullable=False, server_default="INR"),
        sa.Column("billing_cycle", sa.String(16), nullable=False, server_default="MONTHLY"),
        sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"),
        sa.Column("started_at", TS),
        sa.Column("expires_at", TS),
        sa.Column("contact", sa.String(160), nullable=False, server_default=""),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_index("ix_shasha_act_sentiment_occurred", table_name="shasha_act_sentiment_history")
    op.drop_table("shasha_act_sentiment_history")
    op.drop_table("shasha_act_subscription")
    op.drop_index("ix_shasha_act_sessions_expiry", table_name="shasha_act_sessions")
    op.drop_index("ix_shasha_act_sessions_user", table_name="shasha_act_sessions")
    op.drop_table("shasha_act_sessions")
    op.drop_index("ix_shasha_act_users_active", table_name="shasha_act_users")
    op.drop_table("shasha_act_users")

    op.drop_column("shasha_act_risk_control", "entry_candle_interval")
    op.drop_column("shasha_act_risk_control", "enable_secondary_targets")
    op.drop_column("shasha_act_risk_control", "trailing_enabled")
    op.drop_column("shasha_act_risk_control", "trailing_stop_percent")
    op.drop_column("shasha_act_risk_control", "target1_close_percent")

    op.drop_column("shasha_act_trades", "entry_candle_time")
    op.drop_column("shasha_act_trades", "entry_price_source")
    op.drop_column("shasha_act_trades", "secondary_targets_enabled")
    op.drop_column("shasha_act_trades", "trailing_enabled")
    op.drop_column("shasha_act_trades", "price_tick")
    op.drop_column("shasha_act_trades", "partial_close_percent")
    op.drop_column("shasha_act_trades", "trailing_stop_percent")
    op.drop_column("shasha_act_trades", "trailing_high_price")
    op.drop_column("shasha_act_trades", "trailing_active")
    op.drop_column("shasha_act_trades", "partial_closed")
