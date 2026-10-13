"""Domain models and enums shared across the application."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from app.numeric import opt_decimal, req_decimal


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class Mode(str, Enum):
    PAPER = "PAPER"
    REAL = "REAL"


class SignalStatus(str, Enum):
    RECEIVED = "RECEIVED"
    IGNORED = "IGNORED"  # ordinary chat message, not signal format
    REJECTED = "REJECTED"
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    FAILED = "FAILED"


class TradeStatus(str, Enum):
    SUBMITTING = "SUBMITTING"  # persisted before the Mudrex call
    SUBMIT_UNCERTAIN = "SUBMIT_UNCERTAIN"  # timeout/5xx: outcome unknown, reconcile
    ENTRY_PENDING = "ENTRY_PENDING"
    ENTRY_FILLED = "ENTRY_FILLED"
    CLOSED = "CLOSED"
    LIQUIDATED = "LIQUIDATED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


ACTIVE_TRADE_STATUSES = frozenset(
    {
        TradeStatus.SUBMITTING,
        TradeStatus.SUBMIT_UNCERTAIN,
        TradeStatus.ENTRY_PENDING,
        TradeStatus.ENTRY_FILLED,
        TradeStatus.NEEDS_REVIEW,
    }
)
# Trades whose entry order may still be working or whose position may exist.
PENDING_LIKE_STATUSES = frozenset(
    {
        TradeStatus.SUBMITTING,
        TradeStatus.SUBMIT_UNCERTAIN,
        TradeStatus.ENTRY_PENDING,
        TradeStatus.NEEDS_REVIEW,
    }
)


@dataclass(frozen=True)
class ParsedSignal:
    channel_id: int
    message_id: int
    signal_time: datetime
    symbol: str  # Telegram-normalised, e.g. CRVUSDT
    base_asset: str
    quote_asset: str
    direction: Direction
    entry: Decimal
    target1: Decimal
    telegram_leverage: Decimal
    stop_loss: Decimal | None = None
    target2: Decimal | None = None
    target3: Decimal | None = None
    target4: Decimal | None = None
    target5: Decimal | None = None

    def all_targets(self) -> list[Decimal]:
        """Return all non-None targets in order."""
        result = [self.target1]
        for t in (self.target2, self.target3, self.target4, self.target5):
            if t is not None:
                result.append(t)
        return result


@dataclass(frozen=True)
class AssetInfo:
    asset_id: str
    symbol: str
    min_contract: Decimal
    max_contract: Decimal
    quantity_step: Decimal
    min_notional_value: Decimal | None
    min_leverage: Decimal
    max_leverage: Decimal
    price: Decimal | None
    price_step: Decimal | None = None
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    max_market_contract: Decimal | None = None
    leverage_step: Decimal | None = None

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "AssetInfo":
        asset_id = data.get("id")
        symbol = data.get("symbol")
        if not asset_id or not symbol:
            raise ValueError("asset payload missing id or symbol")
        return cls(
            asset_id=str(asset_id),
            symbol=str(symbol).strip().upper(),
            min_contract=req_decimal(data, "min_contract"),
            max_contract=req_decimal(data, "max_contract"),
            quantity_step=req_decimal(data, "quantity_step"),
            min_notional_value=opt_decimal(data, "min_notional_value"),
            min_leverage=req_decimal(data, "min_leverage"),
            max_leverage=req_decimal(data, "max_leverage"),
            price=opt_decimal(data, "price"),
            price_step=opt_decimal(data, "price_step"),
            min_price=opt_decimal(data, "min_price"),
            max_price=opt_decimal(data, "max_price"),
            max_market_contract=opt_decimal(data, "max_market_contract"),
            leverage_step=opt_decimal(data, "leverage_step"),
        )


@dataclass(frozen=True)
class FundsInfo:
    balance: Decimal
    locked: Decimal

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "FundsInfo":
        return cls(balance=req_decimal(data, "balance"), locked=opt_decimal(data, "locked_amount") or Decimal(0))


@dataclass(frozen=True)
class LeverageInfo:
    leverage: Decimal
    margin_type: str

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "LeverageInfo":
        return cls(leverage=req_decimal(data, "leverage"), margin_type=str(data.get("margin_type", "")).upper())


@dataclass(frozen=True)
class OrderAccepted:
    order_id: str
    position_id: str | None
    client_order_id: str | None
    status: str

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "OrderAccepted":
        if not data.get("order_id"):
            raise ValueError("order response missing order_id")
        return cls(
            order_id=str(data["order_id"]),
            position_id=(str(data["position_id"]) if data.get("position_id") else None),
            client_order_id=(str(data["client_order_id"]) if data.get("client_order_id") else None),
            status=str(data.get("status", "")),
        )


@dataclass(frozen=True)
class OrderInfo:
    order_id: str
    client_order_id: str | None
    position_id: str | None
    symbol: str
    status: str
    order_type: str  # LONG / SHORT / STOPLOSS / TAKEPROFIT
    trigger_type: str
    quantity: Decimal
    filled_quantity: Decimal
    price: Decimal | None
    filled_price: Decimal | None
    leverage: Decimal | None

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "OrderInfo":
        order_id = data.get("id") or data.get("order_id")
        if not order_id:
            raise ValueError("order payload missing id")
        position_id = data.get("position_id") or data.get("future_position_uuid")
        return cls(
            order_id=str(order_id),
            client_order_id=(str(data["client_order_id"]) if data.get("client_order_id") else None),
            position_id=(str(position_id) if position_id else None),
            symbol=str(data.get("symbol", "")).upper(),
            status=str(data.get("status", "")).upper(),
            order_type=str(data.get("order_type", "")).upper(),
            trigger_type=str(data.get("trigger_type", "")).upper(),
            quantity=opt_decimal(data, "quantity") or Decimal(0),
            filled_quantity=opt_decimal(data, "filled_quantity") or Decimal(0),
            price=opt_decimal(data, "price"),
            filled_price=opt_decimal(data, "filled_price"),
            leverage=opt_decimal(data, "leverage"),
        )


@dataclass(frozen=True)
class PositionInfo:
    position_id: str
    symbol: str
    side: str
    status: str
    entry_price: Decimal | None
    quantity: Decimal
    leverage: Decimal | None
    take_profit_price: Decimal | None
    take_profit_order_id: str | None
    stop_loss_order_id: str | None

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "PositionInfo":
        position_id = data.get("position_id") or data.get("id")
        if not position_id:
            raise ValueError("position payload missing id")
        tp = data.get("takeprofit") or {}
        sl = data.get("stoploss") or {}
        return cls(
            position_id=str(position_id),
            symbol=str(data.get("symbol", "")).upper(),
            side=str(data.get("order_type", "")).upper(),
            status=str(data.get("status", "")).upper(),
            entry_price=opt_decimal(data, "entry_price"),
            quantity=opt_decimal(data, "quantity") or Decimal(0),
            leverage=opt_decimal(data, "leverage"),
            take_profit_price=opt_decimal(tp, "price") if tp else None,
            take_profit_order_id=(str(tp["order_id"]) if tp.get("order_id") else None),
            stop_loss_order_id=(str(sl["order_id"]) if sl.get("order_id") else None),
        )


@dataclass
class TradeRecord:
    """A row of the ``trades`` table with typed fields."""

    id: int
    signal_id: int
    mode: str
    client_order_id: str
    symbol: str
    mudrex_asset_id: str | None
    direction: str
    entry_price: Decimal
    target1: Decimal
    stop_loss_reference: Decimal | None  # SL from telegram signal (for emergency SL use)
    telegram_leverage: Decimal
    leverage: Decimal
    asset_min_leverage: Decimal | None
    asset_max_leverage: Decimal | None
    leverage_step: Decimal | None
    quantity: Decimal
    notional: Decimal
    est_margin: Decimal
    budget: Decimal
    status: str
    status_reason: str | None
    order_id: str | None
    position_id: str | None
    fill_price: Decimal | None
    filled_quantity: Decimal | None
    exit_price: Decimal | None
    exit_reason: str | None
    entry_fee: Decimal | None
    exit_fee: Decimal | None
    realized_pnl: Decimal | None
    liquidation_price: Decimal | None
    tp_confirmed: bool
    tp_order_id: str | None
    created_at: datetime
    updated_at: datetime
    filled_at: datetime | None
    closed_at: datetime | None
    # Multi-target fields
    target2: Decimal | None = None
    target3: Decimal | None = None
    target4: Decimal | None = None
    target5: Decimal | None = None
    tp_order_ids: str | None = None   # JSON: {"1": "order-id", "2": "order-id", ...}
    targets_hit: str | None = None    # JSON list e.g. [1, 2]
    sl_order_id: str | None = None    # current active SL order id on Mudrex
    sl_price: Decimal | None = None   # current SL price placed on Mudrex

    @property
    def trade_status(self) -> TradeStatus:
        return TradeStatus(self.status)

    @property
    def is_active(self) -> bool:
        return self.trade_status in ACTIVE_TRADE_STATUSES

    def all_targets(self) -> list[Decimal]:
        """Return all non-None targets in order (target1 first)."""
        result = [self.target1]
        for t in (self.target2, self.target3, self.target4, self.target5):
            if t is not None:
                result.append(t)
        return result

    def targets_hit_set(self) -> set[int]:
        """Return set of 1-based target indices that have been hit."""
        if not self.targets_hit:
            return set()
        import json
        try:
            return set(json.loads(self.targets_hit))
        except Exception:
            return set()

    def tp_order_ids_dict(self) -> dict[str, str]:
        """Return dict mapping target index (str) -> tp order id."""
        if not self.tp_order_ids:
            return {}
        import json
        try:
            return dict(json.loads(self.tp_order_ids))
        except Exception:
            return {}


_DECIMAL_FIELDS = {
    "entry_price", "target1", "target2", "target3", "target4", "target5",
    "stop_loss_reference", "telegram_leverage", "leverage",
    "asset_min_leverage", "asset_max_leverage", "leverage_step", "quantity", "notional",
    "est_margin", "budget", "fill_price", "filled_quantity", "exit_price", "entry_fee",
    "exit_fee", "realized_pnl", "liquidation_price", "sl_price",
}
_DATETIME_FIELDS = {"created_at", "updated_at", "filled_at", "closed_at"}


def _dec_or_none(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _dt_or_none(value: Any) -> datetime | None:
    return None if value is None else datetime.fromisoformat(str(value))


def trade_from_row(row: Mapping[str, Any]) -> TradeRecord:
    values: dict[str, Any] = {}
    for key in row.keys():
        raw = row[key]
        if key in _DECIMAL_FIELDS:
            values[key] = _dec_or_none(raw)
        elif key in _DATETIME_FIELDS:
            values[key] = _dt_or_none(raw)
        elif key == "tp_confirmed":
            values[key] = bool(raw)
        else:
            values[key] = raw
    # Required decimals that may be stored as NULL by very old rows are not expected.
    return TradeRecord(**values)
