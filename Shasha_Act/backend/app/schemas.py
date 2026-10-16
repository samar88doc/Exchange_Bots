"""Validated API request DTOs; secret-bearing fields are never response DTOs."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: Annotated[str, Field(min_length=1, max_length=64)]
    password: Annotated[str, Field(min_length=8, max_length=256)]
    remember: bool = True


class InitialOwnerSignupRequest(BaseModel):
    """One-time first-owner setup, available only when explicitly enabled on the server."""

    model_config = ConfigDict(extra="forbid")

    username: Annotated[str, Field(min_length=3, max_length=64)]
    password: Annotated[str, Field(min_length=12, max_length=256)]
    display_name: Annotated[str, Field(max_length=96)] | None = None
    remember: bool = True


class SubscriptionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_name: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    fee: Decimal | None = Field(default=None, ge=0, le=Decimal("100000000"))
    currency: Annotated[str, Field(min_length=2, max_length=8)] | None = None
    billing_cycle: Annotated[str, Field(min_length=2, max_length=16)] | None = None
    status: Annotated[str, Field(min_length=2, max_length=16)] | None = None
    started_at: datetime | None = None
    expires_at: datetime | None = None
    contact: Annotated[str, Field(max_length=160)] | None = None
    notes: Annotated[str, Field(max_length=2000)] | None = None


class UserCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: Annotated[str, Field(min_length=3, max_length=64)]
    password: Annotated[str, Field(min_length=8, max_length=256)]
    role: Literal["VIEWER", "OPERATOR", "ADMIN", "OWNER"] = "OPERATOR"
    display_name: Annotated[str, Field(max_length=96)] | None = None
    is_active: bool = True
    subscription_plan: Annotated[str, Field(max_length=64)] | None = None
    subscription_fee: Decimal | None = Field(default=None, ge=0, le=Decimal("100000000"))
    subscription_currency: Annotated[str, Field(min_length=2, max_length=8)] | None = None
    subscription_cycle: Literal["MONTHLY", "QUARTERLY", "YEARLY", "LIFETIME"] = "MONTHLY"
    subscription_expires_at: datetime | None = None


class UserUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: Annotated[str, Field(min_length=8, max_length=256)] | None = None
    role: Literal["VIEWER", "OPERATOR", "ADMIN", "OWNER"] | None = None
    display_name: Annotated[str, Field(max_length=96)] | None = None
    is_active: bool | None = None
    subscription_plan: Annotated[str, Field(max_length=64)] | None = None
    subscription_fee: Decimal | None = Field(default=None, ge=0, le=Decimal("100000000"))
    subscription_currency: Annotated[str, Field(min_length=2, max_length=8)] | None = None
    subscription_cycle: Literal["MONTHLY", "QUARTERLY", "YEARLY", "LIFETIME"] | None = None
    subscription_expires_at: datetime | None = None


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    margin_per_trade: Decimal | None = Field(default=None, gt=0, le=Decimal("1000000"))
    leverage: int | None = Field(default=None, ge=1, le=200)
    max_open_positions: int | None = Field(default=None, ge=1, le=20)
    target1_percent: Decimal | None = Field(default=None, gt=0, le=Decimal("100"))
    target2_percent: Decimal | None = Field(default=None, gt=0, le=Decimal("100"))
    target3_percent: Decimal | None = Field(default=None, gt=0, le=Decimal("100"))
    stop_loss_percent: Decimal | None = Field(default=None, gt=0, le=Decimal("99"))
    target1_close_percent: Decimal | None = Field(default=None, gt=0, lt=Decimal("100"))
    trailing_stop_percent: Decimal | None = Field(default=None, gt=0, le=Decimal("50"))
    trailing_enabled: bool | None = None
    enable_secondary_targets: bool | None = None
    entry_candle_interval: Annotated[str, Field(min_length=1, max_length=8)] | None = None
    max_notional_per_trade: Decimal | None = Field(default=None, gt=0, le=Decimal("100000000"))
    max_daily_loss: Decimal | None = Field(default=None, gt=0, le=Decimal("100000000"))
    limit_entry_expiry_seconds: int | None = Field(default=None, ge=60, le=86_400)
    cancel_stale_entry_orders: bool | None = None
    allow_symbols: list[str] | None = None
    deny_symbols: list[str] | None = None
    confirmation_phrase: Annotated[str, Field(min_length=1, max_length=64)] | None = None

    @field_validator("allow_symbols", "deny_symbols")
    @classmethod
    def normalize_lists(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        cleaned: list[str] = []
        for value in values:
            item = value.strip().upper()
            if not item or len(item) > 64 or not item.isalnum():
                raise ValueError("symbol lists must contain normalized alphanumeric Shark symbols")
            cleaned.append(item)
        return sorted(set(cleaned))


class LiveActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_phrase: Annotated[str, Field(min_length=1, max_length=64)]
    acknowledge_exchange_risk: bool


class TradingPauseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paused: bool
    confirmation_phrase: str | None = Field(default=None, max_length=64)


class CancelPendingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_phrase: Annotated[str, Field(min_length=1, max_length=64)]


class CloseTradeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_phrase: Annotated[str, Field(min_length=1, max_length=64)]


class TradeRiskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stop_price: Decimal | None = Field(default=None, gt=0)
    target1_price: Decimal | None = Field(default=None, gt=0)
    target2_price: Decimal | None = Field(default=None, gt=0)
    target3_price: Decimal | None = Field(default=None, gt=0)
    confirmation_phrase: Annotated[str, Field(min_length=1, max_length=64)]


class DashboardAuthToggleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool

