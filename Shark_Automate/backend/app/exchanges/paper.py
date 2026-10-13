"""Paper trading engine — realistic simulation, real state, zero real orders.

Simulation knobs (visible in Settings UI): slippage bps, maker/taker fee bps,
funding rate per 8h, artificial latency. Market prices come from the configured
market-data service (live Shark public ticker when available).
"""
from __future__ import annotations

import asyncio
import time
import uuid
from decimal import Decimal

from app.core.config import AppSettings
from app.exchanges.base import (
    AccountSnapshot,
    BalanceSnapshot,
    Capability,
    ContractSpec,
    OrderRequest,
    OrderResult,
    PositionSnapshot,
    Ticker,
)

_CAPS = {
    Capability.GET_ACCOUNT,
    Capability.GET_BALANCE,
    Capability.GET_MARK_PRICE,
    Capability.GET_CONTRACT,
    Capability.GET_OPEN_POSITIONS,
    Capability.GET_OPEN_ORDERS,
    Capability.PLACE_MARKET_ORDER,
    Capability.PLACE_LIMIT_ORDER,
    Capability.CLOSE_POSITION,
    Capability.CLOSE_ALL_POSITIONS,
    Capability.PLACE_TAKE_PROFIT,
    Capability.CANCEL_ORDER,
    Capability.GET_ORDER,
    Capability.GET_POSITION,
    Capability.SET_LEVERAGE,
}

BPS = Decimal("10000")


def apply_slippage(price: Decimal, side: str, slippage_bps: Decimal) -> Decimal:
    slip = (price * slippage_bps) / BPS
    return price + slip if side == "BUY" else price - slip


class PaperExchangeAdapter:
    """In-process simulated exchange with persisted balances/positions/orders."""

    name = "paper"

    def __init__(self, settings: AppSettings, market_data, contract_registry) -> None:
        self.settings = settings
        self.market_data = market_data
        self.contracts = contract_registry
        self.balance = settings.paper_initial_balance
        self.locked_margin = Decimal("0")
        self.realized_pnl = Decimal("0")
        self._orders: dict[str, OrderResult] = {}
        self._positions: dict[str, PositionSnapshot] = {}
        self._fills: dict[str, list[dict]] = {}
        self._fees: dict[str, Decimal] = {}

    def supports(self, capability: Capability) -> bool:
        return capability in _CAPS

    async def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(balances=[await self.get_balance()])

    async def get_balance(self) -> BalanceSnapshot:
        return BalanceSnapshot(
            asset=self.settings.account_unit,
            wallet_balance=self.balance + self.realized_pnl,
            available_balance=self.balance - self.locked_margin,
            locked_balance=self.locked_margin,
        )

    async def get_mark_price(self, symbol: str) -> Ticker:
        return await self.market_data.get_ticker(symbol)

    async def get_contract(self, symbol: str) -> ContractSpec | None:
        return await self.contracts.get_contract(symbol)

    async def get_open_positions(self) -> list[PositionSnapshot]:
        return [p for p in self._positions.values() if p.status == "OPEN"]

    async def get_open_orders(self) -> list[OrderResult]:
        return [o for o in self._orders.values() if o.status in ("NEW", "PARTIALLY_FILLED")]

    def _fee_for(self, notional: Decimal, order_type: str) -> Decimal:
        bps = self.settings.paper_maker_fee_bps if order_type == "LIMIT" else self.settings.paper_taker_fee_bps
        return (notional * bps) / BPS

    def _simulate_fill(self, req: OrderRequest, mid: Decimal) -> tuple[Decimal, Decimal]:
        fill_price = apply_slippage(mid, req.side, self.settings.paper_slippage_bps)
        return fill_price, req.quantity

    async def place_market_order(self, request: OrderRequest) -> OrderResult:
        return await self._place(request, "MARKET")

    async def place_limit_order(self, request: OrderRequest) -> OrderResult:
        return await self._place(request, "LIMIT")

    async def _place(self, req: OrderRequest, order_type: str) -> OrderResult:
        if self.settings.paper_latency_ms > 0:
            await asyncio.sleep(self.settings.paper_latency_ms / 1000)
        order_id = f"paper-{uuid.uuid4().hex[:16]}"
        ticker = await self.market_data.get_ticker(req.symbol)
        mid = ticker.mark_price or ticker.last_price
        assert req.quantity > 0

        if order_type == "LIMIT" and req.price is not None:
            fill_price = req.price
            filled = req.quantity
        else:
            fill_price, filled = self._simulate_fill(req, mid)

        notional = fill_price * filled
        fee = self._fee_for(notional, order_type)
        result = OrderResult(
            order_id=order_id,
            status="FILLED",
            symbol=req.symbol,
            side=req.side,
            quantity=req.quantity,
            filled_quantity=filled,
            avg_fill_price=fill_price,
            dry_run=False,
            raw={"paper": True, "mid_price": str(mid), "slippage_bps": str(self.settings.paper_slippage_bps)},
        )
        self._orders[order_id] = result
        self._fills[order_id] = [
            {"price": str(fill_price), "quantity": str(filled), "fee": str(fee), "ts": time.time()}
        ]
        self._fees[order_id] = fee

        # maintain paper position ledger
        if not req.reduce_only:
            self._open_or_add(req, fill_price, filled)
            margin = notional / Decimal(self.settings.leverage)
            self.locked_margin += margin
        else:
            self._reduce(req, fill_price, filled)
        self.balance -= fee
        self.realized_pnl -= fee
        return result

    def _open_or_add(self, req: OrderRequest, price: Decimal, qty: Decimal) -> None:
        side = "LONG" if req.side == "BUY" else "SHORT"
        pos = self._positions.get(req.symbol)
        if pos is None or pos.status != "OPEN":
            self._positions[req.symbol] = PositionSnapshot(
                position_id=f"paper-pos-{uuid.uuid4().hex[:12]}",
                symbol=req.symbol,
                side=side,
                entry_price=price,
                quantity=qty,
                leverage=self.settings.leverage,
                status="OPEN",
            )
        else:
            total = pos.quantity + qty
            pos.entry_price = ((pos.entry_price * pos.quantity) + (price * qty)) / total
            pos.quantity = total

    def _reduce(self, req: OrderRequest, price: Decimal, qty: Decimal) -> None:
        pos = self._positions.get(req.symbol)
        if pos is None or pos.status != "OPEN":
            return
        closed = min(qty, pos.quantity)
        direction = Decimal("1") if pos.side == "LONG" else Decimal("-1")
        pnl = (price - pos.entry_price) * closed * direction
        pos.realized_pnl += pnl
        self.realized_pnl += pnl
        margin = (pos.entry_price * closed) / Decimal(pos.leverage or self.settings.leverage)
        self.locked_margin = max(Decimal("0"), self.locked_margin - margin)
        self.balance += margin + pnl
        pos.quantity -= closed
        if pos.quantity <= 0:
            pos.status = "CLOSED"

    def fill_detail(self, order_id: str) -> dict:
        entries = self._fills.get(order_id) or [{}]
        return entries[0]

    def fee_for(self, order_id: str) -> Decimal:
        return self._fees.get(order_id, Decimal("0"))

    async def close_position(self, request: OrderRequest) -> OrderResult:
        request.reduce_only = True
        return await self._place(request, "MARKET")

    async def close_all_positions(self) -> dict:
        closed = []
        for pos in list(self._positions.values()):
            if pos.status == "OPEN":
                side = "SELL" if pos.side == "LONG" else "BUY"
                await self._place(
                    OrderRequest(symbol=pos.symbol, side=side, quantity=pos.quantity, reduce_only=True),
                    "MARKET",
                )
                closed.append({"positionId": pos.position_id, "status": "CLOSED"})
        return {"success": True, "data": closed}

    async def place_take_profit(self, request: OrderRequest) -> OrderResult:
        order_id = f"paper-tp-{uuid.uuid4().hex[:12]}"
        result = OrderResult(
            order_id=order_id, status="NEW", symbol=request.symbol,
            side=request.side, quantity=request.quantity,
            raw={"takeProfitPrice": str(request.take_profit_price or "")},
        )
        self._orders[order_id] = result
        return result

    async def cancel_order(self, order_id: str) -> dict:
        order = self._orders.get(order_id)
        if order:
            order.status = "CANCELED"
        return {"clientOrderId": order_id, "status": "CANCELED", "success": True}

    async def get_order(self, order_id: str) -> OrderResult | None:
        return self._orders.get(order_id)

    async def get_position(self, position_id: str) -> PositionSnapshot | None:
        for pos in self._positions.values():
            if pos.position_id == position_id:
                return pos
        return None

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        return {"updatedLeverage": leverage, "contractName": symbol}

    async def health(self) -> str:
        return "CONNECTED"

    # ---- helpers used by the ROI monitor for paper P&L --------------------
    def unrealized_pnl(self, symbol: str, mark: Decimal) -> Decimal:
        pos = self._positions.get(symbol)
        if pos is None or pos.status != "OPEN":
            return Decimal("0")
        direction = Decimal("1") if pos.side == "LONG" else Decimal("-1")
        return (mark - pos.entry_price) * pos.quantity * direction


class PaperAdapterMemory(PaperExchangeAdapter):
    """Alias kept for tests: paper adapter is already in-memory + DB-backed."""
