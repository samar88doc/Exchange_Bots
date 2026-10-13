"""Pydantic request/response schemas with strict validation."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TradingViewAlert(BaseModel):
    """Expected conceptual TradingView alert payload (final template must match Pine)."""

    model_config = ConfigDict(extra="allow")

    event_id: str | None = None
    strategy_id: str | None = None
    strategy_name: str | None = None
    symbol: str = Field(min_length=1, max_length=64)
    action: str = Field(min_length=1, max_length=32)
    timestamp: str | float | int | None = None
    timeframe: str | None = None
    price: float | str | None = None
    alert_price: float | str | None = None
    source: str | None = "TRADINGVIEW"
    secret: str | None = None
    metadata: dict[str, Any] | None = None


class WebhookAck(BaseModel):
    status: str
    event_id: str
    correlation_id: str
    message: str = ""
    tradingview_raw_symbol: str = ""
    canonical_symbol: str = ""
    shark_symbol: str = ""


class SymbolTripleOut(BaseModel):
    tradingview_raw_symbol: str
    canonical_symbol: str
    shark_symbol: str


class SignalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_id: str
    received_at: datetime
    strategy_id: str
    strategy_name: str
    tradingview_raw_symbol: str
    canonical_symbol: str
    shark_symbol: str
    action: str
    source: str
    timeframe: str
    alert_price: Decimal | None
    processing_status: str
    validation_status: str
    status_display: str
    rejection_reason: str
    position_id: str | None
    order_id: str | None
    mode: str


class PositionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    position_id: str
    tradingview_raw_symbol: str
    canonical_symbol: str
    shark_symbol: str
    exchange: str
    direction: str
    state: str
    mode: str
    strategy_id: str
    entry_price: Decimal | None
    mark_price: Decimal | None
    exit_price: Decimal | None
    quantity: Decimal
    margin: Decimal
    notional: Decimal
    leverage: int
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    roi_percent: Decimal
    target_roi_percent: Decimal
    target_price: Decimal | None
    entry_fee: Decimal
    exit_fee: Decimal
    funding: Decimal
    open_time: datetime | None
    close_time: datetime | None
    close_reason: str
    event_id: str
    entry_order_id: str | None
    exit_order_id: str | None


class TradeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    trade_id: str
    position_id: str
    strategy_id: str
    strategy_name: str
    mode: str
    status: str
    result: str
    tradingview_raw_symbol: str
    canonical_symbol: str
    shark_symbol: str
    side: str
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    margin: Decimal
    leverage: int
    entry_fee: Decimal
    exit_fee: Decimal
    funding: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal
    roi_percent: Decimal
    price_change_percent: Decimal
    duration_seconds: int
    opened_at: datetime | None
    closed_at: datetime | None
    close_reason: str
    event_id: str
    entry_order_id: str
    exit_order_id: str


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    order_id: str
    position_id: str
    mode: str
    exchange: str
    tradingview_raw_symbol: str
    canonical_symbol: str
    shark_symbol: str
    side: str
    order_type: str
    entry_exit: str
    reduce_only: bool
    quantity: Decimal
    avg_fill_price: Decimal | None
    filled_quantity: Decimal
    status: str
    exchange_order_id: str | None
    submitted_payload: dict | None
    error_code: str
    error_message: str
    is_dry_run: bool
    created_at: datetime


class LiveActivationRequest(BaseModel):
    confirmation_phrase: str
    acknowledge_margin_per_trade: Decimal
    acknowledge_leverage: int
    acknowledge_max_positions: int
    acknowledge_target_roi: Decimal
    acknowledge_stop_loss_disabled: bool
    secret_confirmation: str = ""


class SettingUpdate(BaseModel):
    key: str
    value: str


class CloseRequest(BaseModel):
    reason: str = "MANUAL"
    confirm: bool = False


class ReplayRequest(BaseModel):
    start: datetime | None = None
    end: datetime | None = None
    strategy_id: str | None = None
    symbol: str | None = None
    speed: float = 1.0


class SymbolMappingCreate(BaseModel):
    tradingview_raw_symbol: str
    canonical_symbol: str
    shark_symbol: str
    base_asset: str = ""
    quote_asset: str = "USDT"
    quantity_precision: int = 3
    price_precision: int = 2
    minimum_quantity: Decimal = Decimal("0")
    minimum_notional: Decimal = Decimal("0")
    quantity_step: Decimal = Decimal("0")
    price_tick_size: Decimal = Decimal("0")
    max_leverage: int | None = None
    contract_status: str = "ACTIVE"
