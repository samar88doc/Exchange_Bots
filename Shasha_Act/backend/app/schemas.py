"""Validated API request DTOs; secret-bearing fields are never response DTOs."""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    margin_per_trade: Decimal | None = Field(default=None, gt=0, le=Decimal("1000000"))
    leverage: int | None = Field(default=None, ge=1, le=200)
    max_open_positions: int | None = Field(default=None, ge=1, le=20)
    target1_percent: Decimal | None = Field(default=None, gt=0, le=Decimal("100"))
    target2_percent: Decimal | None = Field(default=None, gt=0, le=Decimal("100"))
    target3_percent: Decimal | None = Field(default=None, gt=0, le=Decimal("100"))
    stop_loss_percent: Decimal | None = Field(default=None, gt=0, le=Decimal("99"))
    max_notional_per_trade: Decimal | None = Field(default=None, gt=0, le=Decimal("100000000"))
    max_daily_loss: Decimal | None = Field(default=None, gt=0, le=Decimal("100000000"))
    limit_entry_expiry_seconds: int | None = Field(default=None, ge=60, le=86_400)
    cancel_stale_entry_orders: bool | None = None
    allow_symbols: list[str] | None = None
    deny_symbols: list[str] | None = None

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

