"""Restart-safe paper matching and Shark order/position reconciliation."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain import (
    ValidationProblem,
    direction_sign,
    entry_side,
    favorable_extreme,
    next_trailing_stop,
)
from app.market_data import MarketDataService, TickerSnapshot
from app.models import AuditEvent, Fill, Order, SignalEvent, Trade
from app.services.outbox import enqueue_unique_job
from app.services.paper import PaperExecutionAdapter
from app.shark import SharkClient

log = logging.getLogger("shasha_act.reconcile")

ACTIVE_STATES = (
    "ENTRY_PENDING",
    "ENTRY_PARTIAL",
    "SUBMITTING",
    "UNKNOWN",
    "OPEN",
    "PARTIALLY_CLOSED",
    "PROTECTION_ERROR",
    "CLOSING",
)


def _data(value: Any) -> Any:
    return value.get("data", value) if isinstance(value, dict) and "data" in value else value


def _as_list(value: Any) -> list[dict[str, Any]]:
    root = _data(value)
    if isinstance(root, list):
        return [item for item in root if isinstance(item, dict)]
    if isinstance(root, dict) and isinstance(root.get("data"), list):
        return [item for item in root["data"] if isinstance(item, dict)]
    return []


def _as_list_checked(value: Any) -> list[dict[str, Any]] | None:
    root = _data(value)
    if isinstance(root, list):
        return [item for item in root if isinstance(item, dict)]
    if isinstance(root, dict) and isinstance(root.get("data"), list):
        return [item for item in root["data"] if isinstance(item, dict)]
    return None


def _dec(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except Exception:
        return None
    return result if result.is_finite() else None


def _parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float, Decimal)) or str(value).isdigit():
            numeric = Decimal(str(value))
            seconds = numeric / Decimal("1000") if numeric > Decimal("100000000000") else numeric
            return datetime.fromtimestamp(float(seconds), tz=UTC)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (ValueError, OverflowError, OSError, ArithmeticError):
        return None


def _order_price(order: dict[str, Any], *, stop: bool) -> Decimal | None:
    raw = order.get("stopPrice") if stop else None
    if raw in (None, "", 0, "0"):
        raw = order.get("price")
    return _dec(raw)


class ReconciliationService:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        market: MarketDataService,
        shark: SharkClient,
        paper: PaperExecutionAdapter,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.market = market
        self.shark = shark
        self.paper = paper
        self.executor: Any | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._stop: asyncio.Event | None = None
        self.last_success: datetime | None = None
        self.last_error: str = ""

    def set_executor(self, executor: Any) -> None:
        self.executor = executor

    def _lock(self, trade_id: str) -> asyncio.Lock:
        return self._locks.setdefault(trade_id, asyncio.Lock())

    async def on_ticker(self, ticker: TickerSnapshot) -> None:
        async with self.session_factory() as session:
            result = await session.scalars(
                select(Trade).where(
                    Trade.mode == "PAPER",
                    Trade.symbol == ticker.symbol,
                    Trade.status.in_(("ENTRY_PENDING", "OPEN", "PARTIALLY_CLOSED")),
                )
            )
            trade_ids = [trade.id for trade in result.all()]
        for trade_id in trade_ids:
            async with self._lock(trade_id):
                async with self.session_factory() as session:
                    trade = await session.scalar(
                        select(Trade).where(Trade.id == trade_id).with_for_update()
                    )
                    if trade is None:
                        continue
                    event = await session.get(SignalEvent, trade.event_id)
                    await self.paper.process_ticker(session, trade, ticker, event)
                    await session.commit()
        self.last_success = datetime.now(UTC)
        self.last_error = ""

    async def reconcile_event(self, event_id: str) -> None:
        async with self.session_factory() as session:
            trade = await session.scalar(select(Trade).where(Trade.event_id == event_id))
            if trade is None:
                return
            trade_id = trade.id
            mode = trade.mode
        if mode == "PAPER":
            async with self.session_factory() as session:
                trade = await session.get(Trade, trade_id)
                if trade is None or trade.status not in {"ENTRY_PENDING", "OPEN", "PARTIALLY_CLOSED"}:
                    return
                symbol = trade.symbol
            ticker = await self.market.get_ticker(symbol, refresh=True)
            if ticker is not None:
                await self.on_ticker(ticker)
            return
        await self.reconcile_live_trade(trade_id)

    async def reconcile_live_trade(self, trade_id: str) -> None:
        async with self._lock(trade_id):
            async with self.session_factory() as session:
                trade = await session.scalar(select(Trade).where(Trade.id == trade_id).with_for_update())
                if trade is None or trade.mode != "LIVE" or trade.status not in ACTIVE_STATES:
                    return
                event_id = trade.event_id
                symbol = trade.symbol
                status = trade.status
                expires_at = trade.entry_expires_at
                protection = trade.protection_state
                entry_order = await session.scalar(
                    select(Order).where(Order.trade_id == trade.id, Order.kind == "ENTRY").limit(1)
                )
                exchange_order_id = entry_order.exchange_order_id if entry_order is not None else None

                if status in {"SUBMITTING", "UNKNOWN"} and not exchange_order_id:
                    await self._resolve_unknown_submission(session, trade)
                    await session.commit()
                    return

                if status == "ENTRY_PARTIAL" and protection == "ACTIVE" and exchange_order_id:
                    # Protect the confirmed partial quantity, then cancel and verify the
                    # unfilled remainder so later fills cannot exceed the protected size.
                    try:
                        await self.shark.cancel_order(exchange_order_id)
                        cancelled = _data(await self.shark.order(exchange_order_id))
                    except Exception as exc:
                        await self._set_protection_alarm(
                            session, trade, f"PARTIAL_ENTRY_REMAINDER_CANCEL_FAILED_{type(exc).__name__}"
                        )
                        await session.commit()
                        return
                    if not isinstance(cancelled, dict) or str(cancelled.get("status") or "").upper() not in {
                        "CANCELED", "CANCELLED", "FILLED"
                    }:
                        await self._set_protection_alarm(session, trade, "PARTIAL_ENTRY_REMAINDER_NOT_CONFIRMED_CANCELLED")
                        await session.commit()
                        return
                    if entry_order is not None:
                        entry_order.status = str(cancelled.get("status") or "").upper()
                    cancellation_status = str(cancelled.get("status") or "").upper()
                    if cancellation_status in {"CANCELED", "CANCELLED"}:
                        trade.status = "OPEN"
                        trade.status_reason = "Partial entry protected; remaining limit quantity cancelled and confirmed."
                        status = "OPEN"
                    else:
                        await self._set_protection_alarm(
                            session, trade, "PARTIAL_ENTRY_REMAINDER_FILLED_AFTER_PROTECTION"
                        )
                        await session.commit()
                        return
                    await session.commit()
                    if status == "OPEN":
                        await self._reconcile_protective_orders(session, trade)
                        await session.commit()
                        return

                if status in {"ENTRY_PENDING", "ENTRY_PARTIAL", "SUBMITTING", "UNKNOWN"} and exchange_order_id:
                    try:
                        order_state = _data(await self.shark.order(exchange_order_id))
                    except Exception as exc:
                        self.last_error = type(exc).__name__
                        return
                    await self._apply_entry_order_state(session, trade, order_state, expires_at)
                    await session.commit()
                    if (
                        trade.status in {"ENTRY_PARTIAL", "ENTRY_PENDING"}
                        and trade.protection_state in {"WAITING_FOR_FILL", "NOT_ESTABLISHED"}
                        and self.executor is not None
                        and isinstance(order_state, dict)
                        and (
                            str(order_state.get("status") or "").upper() in {"PARTIALLY_FILLED", "FILLED"}
                            or (_dec(order_state.get("filledQty", order_state.get("filledAmount", 0))) or Decimal("0")) > 0
                        )
                    ):
                        await self.executor._handle_live_fill(
                            event_id, self.market.resolver.get(symbol), order_state
                        )
                    return
                if status == "CLOSING":
                    await self._reconcile_manual_close(session, trade)
                    await session.commit()
                    return
                if protection == "ACTIVE" and status in {"OPEN", "PARTIALLY_CLOSED"}:
                    await self._reconcile_protective_orders(session, trade)
                    await session.commit()
            self.last_success = datetime.now(UTC)
            self.last_error = ""

    async def _reconcile_manual_close(self, session: AsyncSession, trade: Trade) -> None:
        order = await session.scalar(
            select(Order).where(Order.trade_id == trade.id, Order.kind == "MANUAL_CLOSE").limit(1)
        )
        if order is None or not order.exchange_order_id:
            # A crash or ambiguous response occurred after the close was armed. Never
            # repeat the market order; leave the position visible for operator review.
            await self._set_protection_alarm(session, trade, "MANUAL_CLOSE_OUTCOME_AMBIGUOUS_NO_RETRY")
            return
        try:
            state = _data(await self.shark.order(order.exchange_order_id))
        except Exception as exc:
            self.last_error = type(exc).__name__
            return
        if not isinstance(state, dict):
            await self._set_protection_alarm(session, trade, "MANUAL_CLOSE_ORDER_STATE_UNAVAILABLE")
            return
        status = str(state.get("status") or "UNKNOWN").upper()
        order.status = status
        order.filled_quantity = _dec(state.get("filledQty", state.get("filledAmount", 0))) or Decimal("0")
        order.average_fill_price = _dec(state.get("avgPrice"))
        if status == "FILLED":
            order.filled_at = _parse_time(state.get("updateTime") or state.get("time")) or datetime.now(UTC)
            await self._finish_live_trade(session, trade)
        elif status == "PARTIALLY_FILLED":
            await self._set_protection_alarm(session, trade, "MANUAL_CLOSE_PARTIAL_FILL_REQUIRES_POSITION_REPROTECTION")
        elif status in {"REJECTED", "FAILED", "CANCELED", "CANCELLED", "EXPIRED"}:
            trade.status = "OPEN"
            trade.status_reason = f"Shark manual close ended with {status}; original protection remains under reconciliation."
            event = await session.get(SignalEvent, trade.event_id)
            if event is not None:
                event.status = "OPEN"
                event.status_reason = trade.status_reason
        elif status in {"NEW", "OPEN", "PENDING", "SUBMITTED"}:
            trade.status_reason = f"Manual close order is {status}; awaiting confirmed final state."
        else:
            await self._set_protection_alarm(session, trade, "MANUAL_CLOSE_ORDER_STATE_AMBIGUOUS")

    async def _apply_entry_order_state(
        self,
        session: AsyncSession,
        trade: Trade,
        state: Any,
        expires_at: datetime | None,
    ) -> None:
        if not isinstance(state, dict):
            return
        order = await session.scalar(select(Order).where(Order.trade_id == trade.id, Order.kind == "ENTRY"))
        if order is None:
            return
        status = str(state.get("status") or "UNKNOWN").upper()
        filled = _dec(state.get("filledQty", state.get("filledAmount", 0))) or Decimal("0")
        order.filled_quantity = filled
        order.average_fill_price = _dec(state.get("avgPrice"))
        order.status = status
        if status == "FILLED":
            order.filled_at = datetime.now(UTC)
            trade.status = (
                "ENTRY_PARTIAL"
                if trade.quantity is not None and filled < trade.quantity
                else "ENTRY_PENDING"
            )
        elif status == "PARTIALLY_FILLED" or filled > 0:
            trade.status = "ENTRY_PARTIAL"
            trade.status_reason = "Shark reports a partial entry fill; filled quantity is being protected."
        elif status in {"CANCELED", "CANCELLED", "REJECTED", "FAILED", "EXPIRED"}:
            order.status = "CANCELED" if status in {"CANCELED", "CANCELLED", "EXPIRED"} else status
            trade.status = "CANCELLED" if order.status == "CANCELED" else "FAILED"
            trade.status_reason = f"Shark entry order ended with status {status}."
            trade.closed_at = datetime.now(UTC)
            event = await session.get(SignalEvent, trade.event_id)
            if event is not None:
                event.status = trade.status
                event.status_reason = trade.status_reason
            await enqueue_unique_job(
                session,
                kind="TELEGRAM_SIGNAL",
                event_id=trade.event_id,
                idempotency_key=f"notify:{trade.event_id}:{trade.status}",
                priority=20,
            )
            return
        else:
            trade.status = "ENTRY_PENDING"

        event = await session.get(SignalEvent, trade.event_id)
        if event is not None:
            event.status = trade.status
            event.status_reason = trade.status_reason

        if expires_at is not None and status in {"NEW", "PARTIALLY_FILLED"}:
            now = datetime.now(UTC)
            deadline = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
            if now >= deadline and self.settings.cancel_stale_entry_orders:
                # External cancellation is performed in the caller after this state is
                # persisted. Here we only mark the requested action through the outbox.
                await enqueue_unique_job(
                    session,
                    kind="CANCEL_PENDING",
                    event_id=trade.event_id,
                    idempotency_key=f"cancel-expired:{trade.id}",
                    payload={"trade_ids": [trade.id]},
                    priority=1,
                )

    async def _resolve_unknown_submission(self, session: AsyncSession, trade: Trade) -> None:
        if trade.submission_started_at is None:
            trade.status = "UNKNOWN"
            trade.status_reason = "Order submission has no timestamp; manual reconciliation required."
            event = await session.get(SignalEvent, trade.event_id)
            if event is not None:
                event.status = "UNKNOWN"
                event.status_reason = trade.status_reason
            return
        started = trade.submission_started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        start_ms = int((started - timedelta(minutes=2)).timestamp() * 1000)
        end_ms = int((datetime.now(UTC) + timedelta(minutes=1)).timestamp() * 1000)
        try:
            open_orders = _as_list(await self.shark.open_orders(trade.symbol))
            history_response = await self.shark.order_history(
                symbol=trade.symbol, start_ms=start_ms, end_ms=end_ms
            )
            history = _as_list(history_response)
        except Exception as exc:
            trade.status = "UNKNOWN"
            trade.status_reason = f"Reconciliation query failed ({type(exc).__name__}); no resubmission attempted."
            event = await session.get(SignalEvent, trade.event_id)
            if event is not None:
                event.status = "UNKNOWN"
                event.status_reason = trade.status_reason
            return
        candidates = [item for item in [*open_orders, *history] if self._matches_uncertain_entry(item, trade, started)]
        # Deduplicate the same order returned by both history and open-orders.
        by_id = {str(item.get("clientOrderId")): item for item in candidates if item.get("clientOrderId")}
        if len(by_id) != 1:
            trade.status = "UNKNOWN"
            trade.status_reason = (
                "No unique matching Shark order found; automatic resubmission is disabled. "
                "Review exchange orders and reconcile manually."
            )
            event = await session.get(SignalEvent, trade.event_id)
            if event is not None:
                event.status = "UNKNOWN"
                event.status_reason = trade.status_reason
            session.add(
                AuditEvent(
                    action="AMBIGUOUS_ORDER_RECONCILIATION",
                    entity_type="trade",
                    entity_id=trade.id,
                    details={"candidate_count": len(by_id)},
                )
            )
            return
        client_id, candidate = next(iter(by_id.items()))
        trade.entry_order_id = client_id
        trade.status = "ENTRY_PENDING"
        trade.status_reason = "Matching Shark order found during reconciliation; status will be refreshed."
        event = await session.get(SignalEvent, trade.event_id)
        if event is not None:
            event.status = "ENTRY_PENDING"
            event.status_reason = trade.status_reason
        order = await session.scalar(select(Order).where(Order.trade_id == trade.id, Order.kind == "ENTRY"))
        if order is not None:
            order.exchange_order_id = client_id
            order.status = str(candidate.get("status") or "UNKNOWN").upper()

    @staticmethod
    def _matches_uncertain_entry(item: dict[str, Any], trade: Trade, started: datetime) -> bool:
        symbol = str(item.get("symbol") or item.get("contractPair") or "").upper()
        if symbol != trade.symbol:
            return False
        if str(item.get("side") or "").upper() != entry_side(trade.direction):
            return False
        if str(item.get("type") or "").upper() != "LIMIT":
            return False
        price = _dec(item.get("price") or item.get("limitPrice"))
        quantity = _dec(item.get("quantity") or item.get("origQty") or item.get("orderAmount"))
        if price != trade.planned_entry or quantity != trade.quantity:
            return False
        timestamp = _parse_time(item.get("time") or item.get("createdTime"))
        if timestamp is None:
            return False
        return abs((timestamp - started).total_seconds()) <= 600

    async def _reconcile_protective_orders(self, session: AsyncSession, trade: Trade) -> None:
        target_ids = list(trade.target_order_ids or [])
        target_prices = (trade.target1_price, trade.target2_price, trade.target3_price)
        target_quantities = (trade.target1_quantity, trade.target2_quantity, trade.target3_quantity)
        expected_targets = sum(
            1
            for price, quantity in zip(target_prices, target_quantities, strict=True)
            if price is not None and quantity is not None and quantity > 0
        )
        if len(target_ids) != expected_targets or not trade.protective_stop_order_id:
            await self._set_protection_alarm(session, trade, "PROTECTIVE_ORDER_IDS_INCOMPLETE")
            return
        active_statuses = {"NEW", "OPEN", "PENDING", "SUBMITTED"}
        for index in range(trade.target_progress, expected_targets):
            order_id = target_ids[index]
            try:
                state = _data(await self.shark.order(order_id))
            except Exception as exc:
                await self._set_protection_alarm(session, trade, f"TARGET_ORDER_QUERY_FAILED_{type(exc).__name__}")
                return
            if not isinstance(state, dict):
                await self._set_protection_alarm(session, trade, "TARGET_ORDER_STATE_UNAVAILABLE")
                return
            order = await session.scalar(select(Order).where(Order.client_order_id == order_id))
            target_status = str(state.get("status") or "UNKNOWN").upper()
            if order is not None:
                order.status = target_status
                order.filled_quantity = _dec(state.get("filledQty", state.get("filledAmount", 0))) or Decimal("0")
                order.average_fill_price = _dec(state.get("avgPrice"))
            expected = target_quantities[index]
            expected_price = target_prices[index]
            if target_status == "FILLED":
                filled = _dec(state.get("filledQty", state.get("filledAmount")))
                if expected is None or filled is None or filled < expected:
                    await self._set_protection_alarm(session, trade, "TARGET_FILL_QUANTITY_NOT_CONFIRMED")
                    return
                await self._sync_trade_history(session, trade)
                if index < expected_targets - 1:
                    await self._move_live_stop_after_target(session, trade, index)
                elif trade.trailing_enabled and not trade.secondary_targets_enabled:
                    # Target 1 is the partial exit; the runner is protected by the
                    # break-even stop and then by the trailing stop ratchet.
                    await self._move_live_stop_after_target(session, trade, index)
                else:
                    await self._finish_live_trade(session, trade)
                return
            if target_status not in active_statuses or expected is None or expected_price is None:
                await self._set_protection_alarm(session, trade, f"TARGET_ORDER_NOT_ACTIVE_{index + 1}")
                return
            confirmed_price = _order_price(state, stop=False)
            confirmed_quantity = _dec(state.get("quantity") or state.get("orderAmount") or state.get("origQty"))
            if confirmed_price != expected_price or confirmed_quantity != expected:
                await self._set_protection_alarm(session, trade, f"TARGET_ORDER_MISMATCH_{index + 1}")
                return

        try:
            stop_state = _data(await self.shark.order(trade.protective_stop_order_id))
        except Exception as exc:
            await self._set_protection_alarm(session, trade, f"PROTECTIVE_STOP_QUERY_FAILED_{type(exc).__name__}")
            return
        if not isinstance(stop_state, dict):
            await self._set_protection_alarm(session, trade, "PROTECTIVE_STOP_STATE_UNAVAILABLE")
            return
        stop_order = await session.scalar(
            select(Order).where(Order.client_order_id == trade.protective_stop_order_id)
        )
        stop_status = str(stop_state.get("status") or "UNKNOWN").upper()
        if stop_order is not None:
            stop_order.status = stop_status
            stop_order.filled_quantity = _dec(stop_state.get("filledQty", stop_state.get("filledAmount", 0))) or Decimal("0")
        if stop_status == "FILLED":
            await self._sync_trade_history(session, trade)
            await self._finish_live_trade(session, trade)
            return
        expected_stop_quantity = trade.remaining_quantity
        confirmed_stop_price = _order_price(stop_state, stop=True)
        confirmed_stop_quantity = _dec(
            stop_state.get("quantity") or stop_state.get("orderAmount") or stop_state.get("origQty")
        )
        if (
            stop_status not in active_statuses
            or trade.stop_price is None
            or expected_stop_quantity is None
            or confirmed_stop_price != trade.stop_price
            or confirmed_stop_quantity != expected_stop_quantity
        ):
            await self._set_protection_alarm(session, trade, "PROTECTIVE_STOP_NOT_ACTIVE_OR_MISMATCHED")
            return
        if trade.trailing_enabled and trade.partial_closed and trade.trailing_stop_percent:
            await self._apply_live_trailing_stop(session, trade, expected_stop_quantity)

    async def _apply_live_trailing_stop(
        self, session: AsyncSession, trade: Trade, protected_quantity: Decimal
    ) -> None:
        """Ratchet the exchange stop in the profitable direction after Target 1.

        LONG: each 1% up move lifts the stop 1%. SHORT: each 1% down move lowers the stop 1%.
        Only ever amends the stop toward profit and only after the exchange confirmed the
        current stop. A failed amendment raises a protection alarm instead of being retried.
        """
        percent = trade.trailing_stop_percent
        tick = trade.price_tick
        floor_price = trade.actual_entry
        if percent is None or percent <= 0 or tick is None or tick <= 0 or floor_price is None:
            return
        ticker = self.market.tickers.get(trade.symbol)
        if ticker is None or ticker.is_stale(self.settings.market_data_stale_seconds):
            return
        high_water = favorable_extreme(
            trade.direction, trade.trailing_high_price or floor_price, ticker.last_price
        )
        trade.trailing_high_price = high_water
        try:
            candidate = next_trailing_stop(
                high_water=high_water,
                current_stop=trade.stop_price,
                floor_price=floor_price,
                trailing_percent=percent,
                price_tick=tick,
                reference_price=ticker.last_price,
                direction=trade.direction,
            )
        except ValidationProblem:
            return
        if trade.stop_price is not None and (
            (candidate - trade.stop_price) * direction_sign(trade.direction) <= 0
        ):
            return
        previous_stop = trade.stop_price
        try:
            await self.shark.edit_order(
                trade.protective_stop_order_id or "",
                quantity=protected_quantity,
                stop_price=candidate,
            )
            confirmed = _data(await self.shark.order(trade.protective_stop_order_id or ""))
        except Exception as exc:
            await self._set_protection_alarm(
                session, trade, f"TRAILING_STOP_AMENDMENT_FAILED_{type(exc).__name__}"
            )
            return
        if not isinstance(confirmed, dict):
            await self._set_protection_alarm(session, trade, "TRAILING_STOP_AMENDMENT_UNCONFIRMED")
            return
        status = str(confirmed.get("status") or "").upper()
        confirmed_price = _order_price(confirmed, stop=True)
        confirmed_quantity = _dec(confirmed.get("quantity") or confirmed.get("orderAmount"))
        if (
            status not in {"NEW", "OPEN", "PENDING", "SUBMITTED"}
            or confirmed_price != candidate
            or confirmed_quantity != protected_quantity
        ):
            await self._set_protection_alarm(session, trade, "TRAILING_STOP_AMENDMENT_UNCONFIRMED")
            return
        trade.stop_price = candidate
        trade.stop_stage = "TRAILING"
        trade.trailing_active = True
        stop_order = await session.scalar(
            select(Order).where(Order.client_order_id == trade.protective_stop_order_id)
        )
        if stop_order is not None:
            stop_order.price = candidate
            stop_order.quantity = protected_quantity
            stop_order.status = "NEW"
        session.add(
            AuditEvent(
                action="SHARK_TRAILING_STOP_UPDATED",
                entity_type="trade",
                entity_id=trade.id,
                details={
                    "previous_stop": str(previous_stop) if previous_stop is not None else None,
                    "new_stop": str(candidate),
                    "high_water": str(high_water),
                    "trailing_percent": str(percent),
                },
            )
        )
        await enqueue_unique_job(
            session,
            kind="TELEGRAM_SIGNAL",
            event_id=trade.event_id,
            idempotency_key=f"notify:{trade.event_id}:TRAILING:{candidate}",
            priority=30,
        )

    async def _move_live_stop_after_target(
        self, session: AsyncSession, trade: Trade, target_index: int
    ) -> None:
        current_positions = _as_list_checked(await self.shark.positions("OPEN", trade.symbol))
        if current_positions is None:
            await self._set_protection_alarm(session, trade, "SHARK_POSITION_RESPONSE_UNAVAILABLE_AFTER_TARGET")
            return
        matching = [
            item for item in current_positions
            if str(item.get("positionId") or "") == str(trade.exchange_position_id or "")
        ]
        if len(matching) != 1:
            await self._set_protection_alarm(session, trade, "OPEN_POSITION_NOT_CONFIRMED_AFTER_TARGET_FILL")
            return
        remaining = _dec(matching[0].get("quantity") or matching[0].get("positionAmount"))
        if remaining is None or remaining <= 0:
            await self._set_protection_alarm(session, trade, "REMAINING_POSITION_QUANTITY_UNAVAILABLE")
            return
        new_stop = trade.actual_entry if target_index == 0 else trade.target1_price
        if new_stop is None:
            await self._set_protection_alarm(session, trade, "NEXT_STOP_PRICE_UNAVAILABLE")
            return
        try:
            await self.shark.edit_order(
                trade.protective_stop_order_id or "",
                quantity=remaining,
                stop_price=new_stop,
            )
            confirmed = _data(await self.shark.order(trade.protective_stop_order_id or ""))
        except Exception as exc:
            await self._set_protection_alarm(session, trade, f"STOP_AMENDMENT_FAILED_{type(exc).__name__}")
            return
        if not isinstance(confirmed, dict):
            await self._set_protection_alarm(session, trade, "STOP_AMENDMENT_UNCONFIRMED")
            return
        status = str(confirmed.get("status") or "").upper()
        confirmed_price = _order_price(confirmed, stop=True)
        confirmed_qty = _dec(confirmed.get("quantity") or confirmed.get("orderAmount"))
        if status not in {"NEW", "OPEN", "PENDING", "SUBMITTED"} or confirmed_price != new_stop or confirmed_qty != remaining:
            await self._set_protection_alarm(session, trade, "STOP_AMENDMENT_UNCONFIRMED")
            return

        trade.target_progress = target_index + 1
        trade.remaining_quantity = remaining
        trade.stop_price = new_stop
        trade.stop_stage = "BREAK_EVEN_BEFORE_FEES" if target_index == 0 else "TARGET_1"
        trade.status = "PARTIALLY_CLOSED"
        trade.protection_state = "ACTIVE"
        trade.status_reason = "Shark confirmed target fill and amended protective stop."
        event = await session.get(SignalEvent, trade.event_id)
        if event is not None:
            event.status = "PARTIALLY_CLOSED"
            event.status_reason = trade.status_reason
        stop_order = await session.scalar(
            select(Order).where(Order.client_order_id == trade.protective_stop_order_id)
        )
        if stop_order is not None:
            stop_order.price = new_stop
            stop_order.quantity = remaining
            stop_order.status = "NEW"
        session.add(
            AuditEvent(
                action=f"SHARK_TARGET_{target_index + 1}_CONFIRMED",
                entity_type="trade",
                entity_id=trade.id,
                details={"remaining_quantity": str(remaining), "confirmed_stop": str(new_stop)},
            )
        )
        await enqueue_unique_job(
            session,
            kind="TELEGRAM_SIGNAL",
            event_id=trade.event_id,
            idempotency_key=f"notify:{trade.event_id}:TARGET_{target_index + 1}",
            priority=20,
        )

    async def _finish_live_trade(self, session: AsyncSession, trade: Trade) -> None:
        positions = _as_list_checked(await self.shark.positions("OPEN", trade.symbol))
        if positions is None:
            await self._set_protection_alarm(session, trade, "SHARK_POSITION_STATUS_UNAVAILABLE_AFTER_EXIT")
            return
        active = [
            item for item in positions
            if str(item.get("positionId") or "") == str(trade.exchange_position_id or "")
            and (_dec(item.get("quantity") or item.get("positionAmount")) or Decimal("0")) > 0
        ]
        if active:
            await self._set_protection_alarm(session, trade, "TARGET_FILLED_BUT_POSITION_REMAINS_OPEN")
            return
        target_ids = list(trade.target_order_ids or [])
        if not trade.protective_stop_order_id or not target_ids:
            await self._set_protection_alarm(session, trade, "PROTECTIVE_ORDER_IDS_INCOMPLETE_AFTER_EXIT")
            return
        order_ids = [trade.protective_stop_order_id, *target_ids]
        terminal_states = {"CANCELED", "CANCELLED", "FILLED", "REJECTED", "EXPIRED"}
        active_states = {"NEW", "OPEN", "PENDING", "SUBMITTED", "PARTIALLY_FILLED"}
        for order_id in order_ids:
            try:
                state = _data(await self.shark.order(order_id))
                if not isinstance(state, dict):
                    await self._set_protection_alarm(session, trade, "ORPHAN_PROTECTIVE_ORDER_STATE_UNAVAILABLE")
                    return
                order_status = str(state.get("status") or "").upper()
                if order_status in active_states:
                    await self.shark.cancel_order(order_id)
                    confirmed = _data(await self.shark.order(order_id))
                    if not isinstance(confirmed, dict) or str(confirmed.get("status") or "").upper() not in {
                        "CANCELED", "CANCELLED"
                    }:
                        await self._set_protection_alarm(session, trade, "ORPHAN_PROTECTIVE_ORDER_NOT_CANCELLED")
                        return
                elif order_status not in terminal_states:
                    await self._set_protection_alarm(session, trade, "ORPHAN_PROTECTIVE_ORDER_STATE_AMBIGUOUS")
                    return
            except Exception as exc:
                await self._set_protection_alarm(session, trade, f"ORPHAN_ORDER_CLEANUP_FAILED_{type(exc).__name__}")
                return
        await self._sync_trade_history(session, trade)
        trade.status = "CLOSED"
        trade.status_reason = "Shark confirmed the position is closed and linked orders are reconciled."
        trade.protection_state = "CLOSED"
        trade.remaining_quantity = Decimal("0")
        trade.unrealized_pnl = Decimal("0")
        trade.closed_at = datetime.now(UTC)
        event = await session.get(SignalEvent, trade.event_id)
        if event is not None:
            event.status = "CLOSED"
            event.status_reason = trade.status_reason
        session.add(
            AuditEvent(
                action="SHARK_POSITION_CLOSED_CONFIRMED",
                entity_type="trade",
                entity_id=trade.id,
                details={"position_id": trade.exchange_position_id},
            )
        )
        await enqueue_unique_job(
            session,
            kind="TELEGRAM_SIGNAL",
            event_id=trade.event_id,
            idempotency_key=f"notify:{trade.event_id}:CLOSED",
            priority=20,
        )

    async def _sync_trade_history(self, session: AsyncSession, trade: Trade) -> None:
        start = trade.opened_at or trade.submission_started_at or trade.created_at
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        try:
            result = _as_list(
                await self.shark.trade_history(
                    symbol=trade.symbol,
                    start_ms=int((start - timedelta(minutes=2)).timestamp() * 1000),
                )
            )
        except Exception:
            trade.fee_data_available = False
            return
        ids = {trade.entry_order_id, trade.protective_stop_order_id, *(trade.target_order_ids or [])}
        for item in result:
            client_id = str(item.get("clientOrderId") or "")
            if client_id not in ids:
                continue
            source_id = str(item.get("id") or "")
            quantity = _dec(item.get("quantity"))
            price = _dec(item.get("price"))
            if not source_id or quantity is None or price is None:
                continue
            exists = await session.scalar(select(Fill.id).where(Fill.source_fill_id == source_id))
            if exists:
                continue
            order = await session.scalar(select(Order).where(Order.client_order_id == client_id).limit(1))
            if order is None:
                continue
            fee = _dec(item.get("fee")) or Decimal("0")
            gross = _dec(item.get("realizedProfit")) or Decimal("0")
            fill_time = _parse_time(item.get("time"))
            if fill_time is None:
                trade.fee_data_available = False
                continue
            session.add(
                Fill(
                    id=str(uuid4()),
                    source_fill_id=source_id,
                    trade_id=trade.id,
                    order_id=order.id,
                    mode="LIVE",
                    entry_exit="ENTRY" if order.kind == "ENTRY" else "EXIT",
                    side=str(item.get("side") or order.side).upper(),
                    price=price,
                    quantity=quantity,
                    fee=fee,
                    gross_realized_pnl=gross,
                    fill_time=fill_time,
                )
            )
            trade.fees_paid += fee
            trade.gross_realized_pnl += gross
            trade.net_realized_pnl += gross - fee
        if result:
            trade.fee_data_available = True

    async def _set_protection_alarm(self, session: AsyncSession, trade: Trade, reason: str) -> None:
        trade.status = "PROTECTION_ERROR"
        trade.status_reason = reason[:500]
        trade.protection_state = "ALARM"
        event = await session.get(SignalEvent, trade.event_id)
        if event is not None:
            event.status = "PROTECTION_ERROR"
            event.status_reason = reason[:500]
        session.add(
            AuditEvent(
                action="URGENT_PROTECTION_RECONCILIATION_ALARM",
                entity_type="trade",
                entity_id=trade.id,
                details={"reason": reason[:250]},
            )
        )
        await enqueue_unique_job(
            session,
            kind="TELEGRAM_SIGNAL",
            event_id=trade.event_id,
            idempotency_key=f"notify:{trade.event_id}:PROTECTION_ERROR:{trade.target_progress}",
            priority=1,
        )

    async def expire_stale_queued_trades(self) -> int:
        """Cancel QUEUED reservations whose entry price never resolved inside the window.

        Without this sweep a signal that repeatedly fails to resolve a Shark entry price
        would hold its position slot and the per-symbol exposure lock indefinitely.
        """
        window = max(int(self.settings.limit_entry_expiry_seconds), 60)
        now = datetime.now(UTC)
        expired = 0
        async with self.session_factory() as session:
            trades = list(
                (await session.scalars(select(Trade).where(Trade.status == "QUEUED"))).all()
            )
            for trade in trades:
                created = trade.created_at
                if created is None:
                    continue
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                if (now - created).total_seconds() < window:
                    continue
                trade.status = "CANCELLED"
                trade.status_reason = (
                    "QUEUED_ENTRY_EXPIRED_WITHOUT_CONFIRMED_SHARK_ENTRY_PRICE: the signal could "
                    "not resolve a fresh Shark closed-candle price before the entry window ended."
                )
                trade.closed_at = now
                trade.protection_state = "NOT_APPLICABLE"
                event = await session.get(SignalEvent, trade.event_id)
                if event is not None and event.status in {"QUEUED", "RECEIVED"}:
                    event.status = "CANCELLED"
                    event.status_reason = trade.status_reason
                session.add(
                    AuditEvent(
                        action="QUEUED_ENTRY_EXPIRED",
                        entity_type="trade",
                        entity_id=trade.id,
                        details={"window_seconds": window},
                    )
                )
                await enqueue_unique_job(
                    session,
                    kind="TELEGRAM_SIGNAL",
                    event_id=trade.event_id,
                    idempotency_key=f"notify:{trade.event_id}:QUEUED_ENTRY_EXPIRED",
                    priority=20,
                )
                expired += 1
            if expired:
                await session.commit()
        return expired

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.expire_stale_queued_trades()
                async with self.session_factory() as session:
                    rows = await session.scalars(
                        select(Trade.event_id).where(Trade.status.in_(ACTIVE_STATES))
                    )
                    events = list(rows.all())
                for event_id in events:
                    try:
                        await self.reconcile_event(event_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.last_error = type(exc).__name__
                        log.warning("trade reconciliation failed (%s)", type(exc).__name__)
                self.last_success = datetime.now(UTC)
                self.last_error = ""
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
                log.exception("reconciliation cycle failed (%s)", type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.settings.reconciliation_interval_seconds)
            except TimeoutError:
                pass
