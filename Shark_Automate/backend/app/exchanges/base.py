"""Exchange abstraction. The trading core ONLY talks to this interface.

Implemented exactly for capabilities verified in the official Shark API docs
(docs.sharkexchange.in, change log 29-01-2026). Anything not verified is NOT
part of this interface — see docs/SHARK_INTEGRATION.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class Capability(StrEnum):
    GET_ACCOUNT = "get_account"
    GET_BALANCE = "get_balance"
    GET_MARK_PRICE = "get_mark_price"
    GET_CONTRACT = "get_contract"
    GET_OPEN_POSITIONS = "get_open_positions"
    GET_OPEN_ORDERS = "get_open_orders"
    PLACE_MARKET_ORDER = "place_market_order"
    PLACE_LIMIT_ORDER = "place_limit_order"
    CLOSE_POSITION = "close_position"          # via reduceOnly market order (verified fields)
    CLOSE_ALL_POSITIONS = "close_all_positions"
    PLACE_TAKE_PROFIT = "place_take_profit"    # takeProfitPrice on place-order / split-tp-sl
    CANCEL_ORDER = "cancel_order"
    GET_ORDER = "get_order"
    GET_POSITION = "get_position"
    SET_LEVERAGE = "set_leverage"


@dataclass
class ContractSpec:
    symbol: str
    contract_type: str = "PERPETUAL"
    status: str = "ACTIVE"
    base_asset: str = ""
    quote_asset: str = ""
    quantity_precision: int = 8
    price_precision: int = 2
    min_quantity: Decimal = Decimal("0")
    max_quantity: Decimal | None = None
    min_notional: Decimal = Decimal("0")
    quantity_step: Decimal = Decimal("0")
    price_tick_size: Decimal = Decimal("0")
    max_leverage: int | None = None
    margin_assets: list[str] = field(default_factory=list)
    maker_fee_bps: Decimal | None = None
    taker_fee_bps: Decimal | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Ticker:
    symbol: str
    last_price: Decimal
    mark_price: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    ts: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderRequest:
    symbol: str                       # MUST be shark_symbol, never raw TradingView symbol
    side: str                         # BUY / SELL
    quantity: Decimal                 # base-asset quantity
    order_type: str = "MARKET"
    price: Decimal | None = None
    reduce_only: bool = False
    margin_asset: str = "INR"
    take_profit_price: Decimal | None = None
    stop_loss_price: Decimal | None = None
    stop_price: Decimal | None = None
    client_order_id: str | None = None
    entry_exit: str = "ENTRY"
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderResult:
    order_id: str
    status: str
    symbol: str
    side: str
    quantity: Decimal
    filled_quantity: Decimal = Decimal("0")
    avg_fill_price: Decimal | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False
    error_code: str = ""
    error_message: str = ""


@dataclass
class PositionSnapshot:
    position_id: str
    symbol: str
    side: str
    entry_price: Decimal
    quantity: Decimal
    margin: Decimal = Decimal("0")
    leverage: int = 1
    unrealized_pnl: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    status: str = "OPEN"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class BalanceSnapshot:
    asset: str
    wallet_balance: Decimal = Decimal("0")
    available_balance: Decimal = Decimal("0")
    locked_balance: Decimal = Decimal("0")
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class AccountSnapshot:
    balances: list[BalanceSnapshot] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ExchangeAdapter(Protocol):
    """Only operations verified against official documentation."""

    name: str

    def supports(self, capability: Capability) -> bool: ...

    async def get_account(self) -> AccountSnapshot: ...
    async def get_balance(self) -> BalanceSnapshot: ...
    async def get_mark_price(self, symbol: str) -> Ticker: ...
    async def get_contract(self, symbol: str) -> ContractSpec | None: ...
    async def get_open_positions(self) -> list[PositionSnapshot]: ...
    async def get_open_orders(self) -> list[OrderResult]: ...
    async def place_market_order(self, request: OrderRequest) -> OrderResult: ...
    async def place_limit_order(self, request: OrderRequest) -> OrderResult: ...
    async def close_position(self, request: OrderRequest) -> OrderResult: ...
    async def close_all_positions(self) -> dict[str, Any]: ...
    async def place_take_profit(self, request: OrderRequest) -> OrderResult: ...
    async def cancel_order(self, order_id: str) -> dict[str, Any]: ...
    async def get_order(self, order_id: str) -> OrderResult | None: ...
    async def get_position(self, position_id: str) -> PositionSnapshot | None: ...
    async def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]: ...
    async def health(self) -> str: ...
