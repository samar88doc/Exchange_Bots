"""Order execution lifecycle with strict no-blind-retry safety.

Verified Shark fact: place-order accepts NO client idempotency key. Therefore:

    * every submission stores a submission_fingerprint BEFORE the HTTP call
    * if the HTTP outcome is uncertain (timeout / transport drop) the order is
      marked UNKNOWN and the position enters UNKNOWN — it is NEVER retried
      blindly. Reconciliation must determine the true exchange state first.
    * retry is only permitted after get_order/get_open_orders proves that the
      exchange never received the order.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppSettings
from app.core.errors import ExchangeError, OrderStateUncertain
from app.core.logging import TradeEvent, log_event
from app.core.security import canonical_json
from app.exchanges.base import ExchangeAdapter, OrderRequest, OrderResult
from app.models.entities import Fill, Order
from app.models.enums import OrderStatus
from app.services.pnl import fee_from_notional

log = logging.getLogger("trading.execution")


class ExecutionEngine:
    def __init__(self, settings: AppSettings, adapters: dict[str, ExchangeAdapter]) -> None:
        self.settings = settings
        self.adapters = adapters

    def adapter_for(self, mode: str | None = None) -> ExchangeAdapter:
        mode = mode or self.settings.trading_mode
        return self.adapters["paper" if mode == "PAPER" else "shark"]

    async def submit(
        self,
        session: AsyncSession,
        *,
        order: Order,
        request: OrderRequest,
    ) -> OrderResult:
        """Submit an order exactly once. Order row must already exist (PENDING)."""
        adapter = self.adapter_for(order.mode)
        if order.mode == "LIVE" and not self.settings.is_live_capable():
            order.status = OrderStatus.REJECTED.value
            order.error_code = "LIVE_NOT_ENABLED"
            order.error_message = "LIVE trading safety gates not satisfied — order refused before any network call"
            log_event(log, TradeEvent.ORDER_FAILED, order.error_message,
                      level=logging.ERROR, order_id=order.order_id)
            return OrderResult(
                order_id=order.order_id, status="REJECTED", symbol=request.symbol,
                side=request.side, quantity=request.quantity,
                error_code="LIVE_NOT_ENABLED", error_message=order.error_message,
            )
        payload_preview = {
            "symbol": request.symbol,  # shark_symbol — hard requirement
            "side": request.side,
            "quantity": str(request.quantity),
            "type": request.order_type,
            "reduceOnly": request.reduce_only,
            "entry_exit": request.entry_exit,
        }
        order.submission_fingerprint = canonical_json(payload_preview)
        order.submitted_payload = payload_preview
        order.status = OrderStatus.SUBMITTED.value
        order.submitted_at = datetime.now(UTC)
        await session.flush()

        log_event(
            log, TradeEvent.ORDER_SUBMITTED, "submitting order",
            order_id=order.order_id, symbol=request.symbol,
            shark_symbol=request.symbol, mode=order.mode,
            payload=payload_preview,
        )

        try:
            if request.order_type == "LIMIT":
                result = await adapter.place_limit_order(request)
            elif request.reduce_only:
                result = await adapter.close_position(request)
            else:
                result = await adapter.place_market_order(request)
        except OrderStateUncertain as exc:
            order.status = OrderStatus.UNKNOWN.value
            order.error_code = "ORDER_SUBMISSION_UNCERTAIN"
            order.error_message = exc.message
            log_event(log, TradeEvent.ORDER_FAILED, "submission outcome UNKNOWN — no retry without reconciliation",
                      level=logging.ERROR, order_id=order.order_id)
            raise
        except ExchangeError as exc:
            order.status = OrderStatus.REJECTED.value
            order.error_code = str(exc.reason)
            order.error_message = exc.message
            log_event(log, TradeEvent.ORDER_FAILED, exc.message, level=logging.ERROR,
                      order_id=order.order_id, error_code=str(exc.reason))
            return OrderResult(
                order_id=order.order_id, status="REJECTED", symbol=request.symbol,
                side=request.side, quantity=request.quantity,
                error_code=str(exc.reason), error_message=exc.message,
            )

        self._apply_result(order, result)
        if result.dry_run:
            log_event(log, TradeEvent.DRY_RUN_ORDER, "DRY RUN payload (not submitted)",
                      order_id=order.order_id, payload=result.raw.get("payload"))
        return result

    def _apply_result(self, order: Order, result: OrderResult) -> None:
        order.exchange_order_id = result.order_id if not result.dry_run else order.exchange_order_id
        order.response_payload = result.raw
        status = result.status.upper()
        mapping = {
            "FILLED": OrderStatus.FILLED.value,
            "NEW": OrderStatus.NEW.value,
            "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED.value,
            "CANCELED": OrderStatus.CANCELED.value,
            "CANCELLED": OrderStatus.CANCELLED.value,
            "REJECTED": OrderStatus.REJECTED.value,
            "DRY_RUN": OrderStatus.DRY_RUN.value,
        }
        order.status = mapping.get(status, OrderStatus.UNKNOWN.value)
        if result.avg_fill_price is not None:
            order.avg_fill_price = result.avg_fill_price
        if result.filled_quantity:
            order.filled_quantity = result.filled_quantity
        if order.status in (OrderStatus.FILLED.value, OrderStatus.DRY_RUN.value):
            if order.avg_fill_price is None and result.raw.get("mid_price"):
                order.avg_fill_price = Decimal(str(result.raw["mid_price"]))
            order.filled_at = datetime.now(UTC)

    async def record_fill(
        self,
        session: AsyncSession,
        *,
        order: Order,
        price: Decimal,
        quantity: Decimal,
        entry_exit: str,
        fee_bps: Decimal | None = None,
    ) -> Fill:
        if order.mode == "PAPER":
            adapter = self.adapter_for("PAPER")
            fee = adapter.fee_for(order.exchange_order_id or order.order_id)  # type: ignore[attr-defined]
            detail = adapter.fill_detail(order.exchange_order_id or order.order_id)  # type: ignore[attr-defined]
            slippage = Decimal(detail.get("slippage", "0") or 0)
        else:
            bps = fee_bps if fee_bps is not None else self.settings.paper_taker_fee_bps
            fee = fee_from_notional(price * quantity, bps)
            slippage = Decimal("0")
        fill = Fill(
            order_id=order.order_id,
            position_id=order.position_id,
            mode=order.mode,
            side=order.side,
            entry_exit=entry_exit,
            price=price,
            quantity=quantity,
            fee=fee,
            slippage=slippage,
        )
        session.add(fill)
        log_event(log, TradeEvent.ORDER_FILLED, "fill recorded",
                  order_id=order.order_id, price=str(price), quantity=str(quantity), fee=str(fee))
        return fill

    async def find_order_by_fingerprint(
        self, session: AsyncSession, fingerprint: str
    ) -> Order | None:
        """Used by reconciliation to locate an uncertain submission."""
        result = await session.execute(
            select(Order).where(Order.submission_fingerprint == fingerprint).limit(1)
        )
        return result.scalar_one_or_none()


def new_order_id() -> str:
    return f"ord-{uuid.uuid4().hex[:20]}"
