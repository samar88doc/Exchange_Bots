"""Domain models and enums shared across the application (Pi42 flavour).

Field names follow the Pi42 API reference (https://docs.pi42.com):
contracts come from ``GET /v1/exchange/exchangeInfo``, orders from
``GET /v1/order`` / ``GET /v1/order/open-orders`` and positions from
``GET /v1/positions/{status}``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from app.numeric import opt_decimal, req_decimal, to_decimal


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
    SUBMITTING = "SUBMITTING"  # persisted before the Pi42 call
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


def _opt_int(data: Mapping[str, Any], key: str) -> int | None:
    value = data.get(key)
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _opt_str(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None or value == "":
        return None
    return str(value)


def _str_list(data: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key)
    if isinstance(value, (list, tuple)):
        return tuple(str(v).upper() for v in value if str(v).strip())
    if isinstance(value, str) and value.strip():
        return (value.strip().upper(),)
    return ()


@dataclass(frozen=True)
class ContractInfo:
    """One Pi42 perpetual contract from ``GET /v1/exchange/exchangeInfo``.

    ``symbol`` is the contract pair used by every order endpoint (``BTCINR``).
    Precision is expressed both as decimal places (as Pi42 sends it) and as the
    step used for snapping prices/quantities.
    """

    symbol: str
    market: str  # INR / USDT
    contract_name: str
    base_asset: str
    quote_asset: str
    contract_type: str
    max_leverage: Decimal
    min_leverage: Decimal
    leverage_step: Decimal
    price_precision: int
    quantity_precision: int
    min_qty: Decimal | None
    max_qty: Decimal | None
    min_notional: Decimal | None
    order_types: tuple[str, ...]
    margin_assets_supported: tuple[str, ...]
    maker_fee: Decimal | None = None
    taker_fee: Decimal | None = None
    funding_fee_interval: Decimal | None = None
    maintenance_margin_percentage: Decimal | None = None
    limit_price_var_allowed: Decimal | None = None
    is_default_contract: bool = False
    price: Decimal | None = None  # last price when the caller filled it in

    @property
    def price_step(self) -> Decimal:
        return Decimal(1).scaleb(-self.price_precision)

    @property
    def quantity_step(self) -> Decimal:
        return Decimal(1).scaleb(-self.quantity_precision)

    @property
    def margin_asset(self) -> str:
        """Preferred margin asset: the market currency when supported."""
        if self.market in self.margin_assets_supported or not self.margin_assets_supported:
            return self.market
        return self.margin_assets_supported[0]

    def supports_order_type(self, order_type: str) -> bool:
        return not self.order_types or order_type.upper() in self.order_types

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "ContractInfo":
        symbol = data.get("name") or data.get("contractPair") or data.get("symbol")
        if not symbol:
            raise ValueError("contract payload missing name/contractPair")
        symbol = str(symbol).strip().upper()
        market = str(data.get("market") or "").upper()
        if not market:
            for candidate in ("USDT", "INR"):
                if symbol.endswith(candidate):
                    market = candidate
                    break
        if not market:
            raise ValueError(f"contract {symbol} payload missing market")

        filters = data.get("filters") or []
        # This bot places LIMIT entries, so LIMIT_QTY_SIZE wins when Pi42 sends
        # one; otherwise the strictest quantity filter is used (the largest
        # minimum and the smallest maximum). Constraints are never loosened.
        limit_min = limit_max = None
        other_min = other_max = None
        min_notional = None
        for flt in filters:
            if not isinstance(flt, Mapping):
                continue
            filter_type = str(flt.get("filterType", "")).upper()
            if "QTY_SIZE" in filter_type or "QTY" in filter_type:
                f_min = opt_decimal(flt, "minQty")
                f_max = opt_decimal(flt, "maxQty")
                if "LIMIT" in filter_type:
                    limit_min = _stricter_min(limit_min, f_min)
                    limit_max = _stricter_max(limit_max, f_max)
                else:
                    other_min = _stricter_min(other_min, f_min)
                    other_max = _stricter_max(other_max, f_max)
            elif filter_type == "MIN_NOTIONAL":
                min_notional = opt_decimal(flt, "notional") or opt_decimal(flt, "minNotional")
        min_qty = limit_min if limit_min is not None else other_min
        max_qty = limit_max if limit_max is not None else other_max
        # Some payloads put the quantities directly on the contract object.
        min_qty = _stricter_min(min_qty, opt_decimal(data, "minQty"))
        max_qty = _stricter_max(max_qty, opt_decimal(data, "maxQty"))
        min_notional = min_notional or opt_decimal(data, "minNotional")

        price_precision = _opt_int(data, "pricePrecision")
        quantity_precision = _opt_int(data, "quantityPrecision")
        if price_precision is None or quantity_precision is None:
            raise ValueError(f"contract {symbol} payload missing price/quantity precision")

        max_leverage = opt_decimal(data, "maxLeverage")
        if max_leverage is None or max_leverage <= 0:
            raise ValueError(f"contract {symbol} payload missing a valid maxLeverage")

        return cls(
            symbol=symbol,
            market=market,
            contract_name=str(data.get("contractName") or symbol),
            base_asset=str(data.get("baseAsset") or "").upper(),
            quote_asset=str(data.get("quoteAsset") or market).upper(),
            contract_type=str(data.get("contractType") or "PERPETUAL").upper(),
            max_leverage=max_leverage,
            min_leverage=Decimal(1),
            leverage_step=Decimal(1),
            price_precision=max(0, price_precision),
            quantity_precision=max(0, quantity_precision),
            min_qty=min_qty,
            max_qty=max_qty,
            min_notional=min_notional,
            order_types=_str_list(data, "orderTypes"),
            margin_assets_supported=_str_list(data, "marginAssetsSupported"),
            maker_fee=opt_decimal(data, "makerFee"),
            taker_fee=opt_decimal(data, "takerFee"),
            funding_fee_interval=opt_decimal(data, "fundingFeeInterval"),
            maintenance_margin_percentage=opt_decimal(data, "maintenanceMarginPercentage"),
            limit_price_var_allowed=opt_decimal(data, "limitPriceVarAllowed"),
            is_default_contract=bool(data.get("isDefaultContract", False)),
        )


def _stricter_min(current: Decimal | None, candidate: Decimal | None) -> Decimal | None:
    """Combine two minimum-quantity constraints: keep the larger (stricter) one."""
    if candidate is None:
        return current
    if current is None:
        return candidate
    return max(current, candidate)


def _stricter_max(current: Decimal | None, candidate: Decimal | None) -> Decimal | None:
    """Combine two maximum-quantity constraints: keep the smaller (stricter) one."""
    if candidate is None:
        return current
    if current is None:
        return candidate
    return min(current, candidate)


@dataclass(frozen=True)
class WalletInfo:
    """Futures wallet snapshot (``GET /v1/wallet/futures-wallet/details``)."""

    margin_asset: str
    balance: Decimal  # walletBalance
    available: Decimal  # withdrawableBalance
    locked: Decimal  # lockedBalance
    margin_balance: Decimal | None = None
    unrealised_pnl: Decimal | None = None

    @property
    def free(self) -> Decimal:
        """Balance usable for a new position."""
        return min(self.available, self.balance) if self.available <= self.balance else self.balance

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "WalletInfo":
        balance = _wallet_field(data, "walletBalance", "balance", "usdtBalance", "inrBalance")
        available = _wallet_field(data, "withdrawableBalance", "availableBalance", "available") or balance
        locked = _wallet_field(data, "lockedBalance", "locked") or Decimal(0)
        return cls(
            margin_asset=str(data.get("marginAsset") or "").upper(),
            balance=balance,
            available=available,
            locked=locked,
            margin_balance=_wallet_field(data, "marginBalance"),
            unrealised_pnl=_wallet_field(data, "unrealisedPnlIsolated", "unrealisedPnlCross"),
        )


def _wallet_field(data: Mapping[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        value = data.get(key)
        if value is None or value == "":
            continue
        try:
            return to_decimal(value, key)
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class LeverageInfo:
    contract: str
    leverage: Decimal
    margin_mode: str

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "LeverageInfo":
        leverage = _first_decimal(data, "updatedLeverage", "leverage")
        if leverage is None:
            raise ValueError("leverage payload missing updatedLeverage")
        contract = str(data.get("contractName") or data.get("contract") or data.get("symbol") or "")
        return cls(
            contract=contract.upper(),
            leverage=leverage,
            margin_mode=str(data.get("marginMode") or "").upper(),
        )


def _first_decimal(data: Mapping[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        value = opt_decimal(data, key)
        if value is not None:
            return value
    return None


@dataclass(frozen=True)
class OrderAccepted:
    client_order_id: str
    order_id: str | None
    ref_id: str | None
    status: str

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "OrderAccepted":
        client_order_id = data.get("clientOrderId") or data.get("client_order_id")
        if not client_order_id:
            raise ValueError("order response missing clientOrderId")
        return cls(
            client_order_id=str(client_order_id),
            order_id=str(data["id"]) if data.get("id") is not None else _opt_str(data, "orderId"),
            ref_id=_opt_str(data, "refId"),
            status=str(data.get("status") or "").upper(),
        )


@dataclass(frozen=True)
class OrderInfo:
    """An order as returned by ``GET /v1/order``, ``/v1/order/{clientOrderId}``,
    ``/v1/order/open-orders`` and ``/v1/order/order-history``."""

    client_order_id: str
    symbol: str
    status: str
    order_type: str  # LIMIT / MARKET / STOP_MARKET / STOP_LIMIT
    side: str  # BUY / SELL
    quantity: Decimal
    filled_quantity: Decimal
    price: Decimal | None
    average_price: Decimal | None
    leverage: Decimal | None
    take_profit_price: Decimal | None
    stop_loss_price: Decimal | None
    stop_price: Decimal | None
    order_id: str | None
    ref_id: str | None
    link_id: str | None
    link_type: str | None
    reduce_only: bool
    margin_asset: str | None
    created_time: str | None

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "OrderInfo":
        client_order_id = data.get("clientOrderId") or data.get("client_order_id")
        if not client_order_id:
            raise ValueError("order payload missing clientOrderId")
        quantity = _first_decimal(data, "quantity", "orderAmount", "origQty") or Decimal(0)
        return cls(
            client_order_id=str(client_order_id),
            symbol=str(data.get("symbol") or "").upper(),
            status=str(data.get("status") or "").upper(),
            order_type=str(data.get("type") or data.get("orderType") or "").upper(),
            side=str(data.get("side") or "").upper(),
            quantity=quantity,
            filled_quantity=_first_decimal(data, "filledQty", "filledAmount", "cumQty", "executedQty")
            or Decimal(0),
            price=_first_decimal(data, "price", "limitPrice"),
            average_price=_first_decimal(data, "avgPrice"),
            leverage=_first_decimal(data, "leverage"),
            take_profit_price=_first_decimal(data, "takeProfitPrice"),
            stop_loss_price=_first_decimal(data, "stopLossPrice"),
            stop_price=_first_decimal(data, "stopPrice"),
            order_id=str(data["id"]) if data.get("id") is not None else _opt_str(data, "orderId"),
            ref_id=_opt_str(data, "refId"),
            link_id=_opt_str(data, "linkId"),
            link_type=_opt_str(data, "linkType"),
            reduce_only=bool(data.get("reduceOnly", False)),
            margin_asset=_opt_str(data, "marginAsset"),
            created_time=_opt_str(data, "createdTime") or _opt_str(data, "time"),
        )


@dataclass(frozen=True)
class PositionInfo:
    """An open/closed position from ``GET /v1/positions/{status}``."""

    position_id: str
    symbol: str
    side: str  # LONG / SHORT
    status: str
    entry_price: Decimal | None
    quantity: Decimal
    leverage: Decimal | None
    margin: Decimal | None
    margin_asset: str | None
    margin_type: str | None
    liquidation_price: Decimal | None
    take_profit_price: Decimal | None = None
    stop_loss_price: Decimal | None = None
    take_profit_order_id: str | None = None
    stop_loss_order_id: str | None = None

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "PositionInfo":
        position_id = data.get("positionId") or data.get("id")
        if position_id is None:
            raise ValueError("position payload missing positionId")
        return cls(
            position_id=str(position_id),
            symbol=str(data.get("contractPair") or data.get("symbol") or "").upper(),
            side=str(data.get("positionType") or "").upper(),
            status=str(data.get("positionStatus") or "").upper(),
            entry_price=_first_decimal(data, "entryPrice"),
            quantity=_first_decimal(data, "positionAmount", "quantity") or Decimal(0),
            leverage=_first_decimal(data, "leverage"),
            margin=_first_decimal(data, "margin", "marginInMarginAsset"),
            margin_asset=_opt_str(data, "marginAsset"),
            margin_type=_opt_str(data, "marginType"),
            liquidation_price=_first_decimal(data, "liquidationPrice"),
        )


@dataclass
class TradeRecord:
    """A row of the ``trades`` table with typed fields."""

    id: int
    signal_id: int
    mode: str
    ref_id: str
    client_order_id: str | None
    order_id: str | None
    symbol: str
    market: str
    margin_asset: str
    direction: str
    entry_price: Decimal
    target1: Decimal
    stop_loss_reference: Decimal | None  # SL from telegram signal (reference only, never sent)
    telegram_leverage: Decimal
    leverage: Decimal
    contract_max_leverage: Decimal | None
    quantity: Decimal
    notional: Decimal
    est_margin: Decimal
    budget: Decimal
    status: str
    status_reason: str | None
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
    tp_order_ids: str | None = None   # JSON: {"1": "clientOrderId", "2": "clientOrderId", ...}
    targets_hit: str | None = None    # JSON list e.g. [1, 2]
    sl_order_id: str | None = None    # never populated: this bot places no stop-loss
    sl_price: Decimal | None = None   # never populated: this bot places no stop-loss

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
        except Exception:  # noqa: BLE001 - corrupt JSON must not crash monitoring
            return set()

    def tp_order_ids_dict(self) -> dict[str, str]:
        """Return dict mapping target index (str) -> Pi42 clientOrderId."""
        if not self.tp_order_ids:
            return {}
        import json

        try:
            return dict(json.loads(self.tp_order_ids))
        except Exception:  # noqa: BLE001
            return {}


_DECIMAL_FIELDS = {
    "entry_price", "target1", "target2", "target3", "target4", "target5",
    "stop_loss_reference", "telegram_leverage", "leverage", "contract_max_leverage",
    "quantity", "notional", "est_margin", "budget", "fill_price", "filled_quantity",
    "exit_price", "entry_fee", "exit_fee", "realized_pnl", "liquidation_price", "sl_price",
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
    return TradeRecord(**values)


def require_decimal(data: Mapping[str, Any], key: str) -> Decimal:
    """Re-exported helper so callers do not import app.numeric directly."""
    return req_decimal(data, key)
