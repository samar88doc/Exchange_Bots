"""Background reconciliation and ROI take-profit management.

PAPER: fetch the live public ticker price per symbol and advance simulated trades.
REAL : reconcile every active trade with Pi42:
       * Uncertain submissions are looked up by ``refId`` (then by a strict
         order-history fingerprint), never blindly resubmitted.
       * LIMIT entries that stay unfilled past LIMIT_ORDER_TTL_MINUTES are
         cancelled only after re-confirming the order is still unfilled.
       * The ROI take-profit attached at entry is confirmed from the order
         details; a missing take-profit is reported, never faked.
       * When the market reaches the ROI target before Pi42 closes the position,
         the bot squares the position off with a reduce-only MARKET order
         (``placeType=POSITION``) — the only way Pi42 closes a single position.
       * NO stop-loss is ever placed.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable

from app.models import Mode, OrderInfo, PositionInfo, TradeRecord, TradeStatus, utc_now
from app.notifications import NullNotifier
from app.paper_broker import PaperBroker
from app.pi42_client import (
    AmbiguousMutationError,
    Pi42Error,
    Pi42NotFoundError,
    is_duplicate_ref_error,
)
from app.storage import Storage

logger = logging.getLogger("monitor")

# Pi42 order status -> our trade status. Unknown statuses keep the trade pending
# and are logged so an operator notices a schema change at Pi42.
ORDER_STATUS_MAP = {
    "NEW": TradeStatus.ENTRY_PENDING,
    "OPEN": TradeStatus.ENTRY_PENDING,
    "PENDING": TradeStatus.ENTRY_PENDING,
    "PARTIALLY_FILLED": TradeStatus.ENTRY_PENDING,
    "PARTIALLYFILLED": TradeStatus.ENTRY_PENDING,
    "FILLED": TradeStatus.ENTRY_FILLED,
    "COMPLETED": TradeStatus.ENTRY_FILLED,
    "CANCELED": TradeStatus.CANCELLED,
    "CANCELLED": TradeStatus.CANCELLED,
    "EXPIRED": TradeStatus.EXPIRED,
    "REJECTED": TradeStatus.REJECTED,
    "FAILED": TradeStatus.REJECTED,
}

# Closed trade statuses that end monitoring.
TERMINAL_STATUSES = frozenset(
    {TradeStatus.CLOSED, TradeStatus.LIQUIDATED, TradeStatus.CANCELLED,
     TradeStatus.EXPIRED, TradeStatus.REJECTED}
)


def map_order_status(status: str) -> TradeStatus | None:
    return ORDER_STATUS_MAP.get((status or "").upper())


def _adopt_order(storage: Storage, trade: TradeRecord, order: OrderInfo, now: datetime, source: str) -> TradeStatus:
    new_status = map_order_status(order.status)
    updates: dict = {
        "client_order_id": order.client_order_id,
        "order_id": order.order_id,
        "status_reason": f"adopted Pi42 order status {order.status} ({source})",
    }
    if new_status is None:
        updates["status"] = TradeStatus.NEEDS_REVIEW.value
        updates["status_reason"] = f"unrecognised Pi42 order status {order.status}; needs review"
    else:
        updates["status"] = new_status.value
        if new_status is TradeStatus.ENTRY_FILLED:
            updates.update(
                filled_at=now,
                fill_price=order.average_price or order.price,
                filled_quantity=order.filled_quantity,
            )
        if new_status in (TradeStatus.CANCELLED, TradeStatus.EXPIRED, TradeStatus.REJECTED):
            updates["closed_at"] = now
    storage.update_trade(trade.id, now, **updates)
    storage.add_event(now, "real.reconciled", trade_id=trade.id, signal_id=trade.signal_id,
                      detail={"client_order_id": order.client_order_id, "order_status": order.status,
                              "source": source})
    logger.info("event=real.reconciled trade_id=%s client_order_id=%s order_status=%s source=%s",
                trade.id, order.client_order_id, order.status, source)
    return TradeStatus(updates["status"])


async def reconcile_uncertain_trade(
    *, storage: Storage, client, trade: TradeRecord, now: datetime, notifier=None
) -> TradeStatus | None:
    """Resolve SUBMITTING / SUBMIT_UNCERTAIN trades without ever resubmitting.

    1. ``GET /v1/order/ref-id`` on our deterministic refId (when one was sent).
    2. ``GET /v1/order?clientOrderId=`` when Pi42 already gave us an id.
    3. A strict order-history fingerprint: same symbol, side, LIMIT type and
       quantity created after this trade was persisted, and exactly one match.
    4. Otherwise the trade is parked as NEEDS_REVIEW for a human.
    """
    not_found = False
    if trade.ref_id:
        try:
            order = await client.get_order_by_ref_id(trade.ref_id)
            return _adopt_order(storage, trade, order, now, "refId")
        except Pi42NotFoundError:
            not_found = True
        except Pi42Error as exc:
            logger.warning("event=real.reconcile_retry_later trade_id=%s error=%s", trade.id, exc)
            return None

    if trade.client_order_id:
        try:
            order = await client.get_order(trade.client_order_id)
            return _adopt_order(storage, trade, order, now, "clientOrderId")
        except Pi42NotFoundError:
            not_found = True
        except Pi42Error as exc:
            logger.warning("event=real.reconcile_retry_later trade_id=%s error=%s", trade.id, exc)
            return None

    candidates = await _fingerprint_candidates(client, trade)
    if len(candidates) == 1:
        return _adopt_order(storage, trade, candidates[0], now, "order-history fingerprint")
    if len(candidates) > 1:
        reason = (
            f"submission outcome unknown and {len(candidates)} Pi42 orders match this trade; "
            "NOT resubmitted automatically. Review, then run `resolve-trade`."
        )
    elif not_found:
        reason = (
            "submission outcome unknown and Pi42 has no matching order; "
            "NOT resubmitted automatically. Review, then run `resolve-trade`."
        )
    else:
        reason = (
            "submission outcome unknown; Pi42 could not be queried for this trade. "
            "NOT resubmitted automatically."
        )
    storage.update_trade(trade.id, now, status=TradeStatus.NEEDS_REVIEW.value, status_reason=reason)
    storage.add_event(now, "real.reconcile_not_found", level="WARNING", trade_id=trade.id,
                      signal_id=trade.signal_id, detail={"ref_id": trade.ref_id})
    logger.error("event=real.reconcile_not_found trade_id=%s ref_id=%s matches=%s",
                 trade.id, trade.ref_id, len(candidates))
    return TradeStatus.NEEDS_REVIEW


async def _fingerprint_candidates(client, trade: TradeRecord) -> list[OrderInfo]:
    """Strict matches from order history for a submission whose response was lost."""
    expected_side = "BUY" if trade.direction == "LONG" else "SELL"
    try:
        history = await client.list_order_history(trade.symbol)
    except Pi42Error as exc:
        logger.warning("event=real.fingerprint_unavailable trade_id=%s error=%s", trade.id, exc)
        return []
    matches: list[OrderInfo] = []
    for order in history:
        if order.symbol != trade.symbol:
            continue
        if order.side != expected_side or order.order_type != "LIMIT":
            continue
        if order.quantity != trade.quantity:
            continue
        if order.created_time and not _created_after(order.created_time, trade.created_at):
            continue
        matches.append(order)
    return matches


def _created_after(created_time: str, reference: datetime) -> bool:
    try:
        stamp = datetime.fromisoformat(created_time.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return True  # unparseable timestamp: keep the candidate, it already matches quantity/side
    if stamp.tzinfo is None or reference.tzinfo is None:
        return True
    return stamp >= reference - timedelta(minutes=2)


def _split_quantity(total: Decimal, n: int, step: Decimal) -> list[Decimal]:
    """Split total quantity into n equal lots, snapped to step. Last lot gets the remainder."""
    if n <= 0:
        return []
    from decimal import ROUND_DOWN

    unit = (total / n).quantize(step, rounding=ROUND_DOWN)
    lots = [unit] * n
    remainder = total - unit * n
    if remainder > 0:
        lots[-1] += remainder
    return lots


class PositionMonitor:
    def __init__(
        self,
        *,
        storage: Storage,
        client,
        mode: Mode,
        market: str,
        margin_asset: str,
        paper_broker: PaperBroker | None,
        ttl_minutes: int,
        target_order_type: str = "LIMIT",
        target_roi_pct: Decimal = Decimal("8.0"),
        notifier=None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._storage = storage
        self._client = client
        self._mode = mode
        self._market = market.upper()
        self._margin_asset = margin_asset.upper()
        self._paper = paper_broker
        self._ttl = ttl_minutes
        self._target_order_type = target_order_type.upper()
        self._target_roi_pct = target_roi_pct
        self._notifier = notifier or NullNotifier()
        self._now = now

    async def monitor_once(self) -> None:
        if self._mode is Mode.PAPER:
            await self._monitor_paper()
        else:
            await self._monitor_real()

    # ------------------------------------------------------------- PAPER
    async def _monitor_paper(self) -> None:
        assert self._paper is not None
        symbols = sorted({t.symbol for t in self._storage.active_trades(Mode.PAPER.value)})
        for symbol in symbols:
            try:
                price = await self._client.get_ticker_price(symbol)
            except Pi42Error as exc:
                logger.warning("event=paper.price_unavailable symbol=%s error=%s", symbol, exc)
                continue
            if price is None or price <= 0:
                continue
            now = self._now()
            for trade, result in self._paper.apply_price(symbol, price, now):
                await self._notifier.send(
                    f"[PAPER] {symbol} trade #{trade.id}: {result.new_status.value} "
                    f"(observed {price}). Simulated only."
                )

    # -------------------------------------------------------------- REAL
    async def _monitor_real(self) -> None:
        trades = self._storage.active_trades(Mode.REAL.value)
        if not trades:
            return
        positions_by_id, positions_by_symbol = await self._position_snapshot()
        open_orders = await self._open_order_snapshot()

        for trade in trades:
            try:
                await self._reconcile_real_trade(trade, positions_by_id, positions_by_symbol, open_orders)
            except Pi42Error as exc:
                logger.warning("event=real.monitor_api_error trade_id=%s error=%s", trade.id, exc)
            except Exception:  # noqa: BLE001 - one bad trade must not stop monitoring others
                logger.exception("event=real.monitor_unexpected_error trade_id=%s", trade.id)

    async def _position_snapshot(self):
        try:
            positions = await self._client.list_open_positions()
        except Pi42Error as exc:
            logger.warning("event=real.positions_unavailable error=%s", exc)
            return None, None
        by_id = {p.position_id: p for p in positions}
        by_symbol: dict[str, PositionInfo] = {}
        for position in positions:
            by_symbol.setdefault(position.symbol, position)
        return by_id, by_symbol

    async def _open_order_snapshot(self) -> dict[str, OrderInfo] | None:
        try:
            orders = await self._client.list_open_orders()
        except Pi42Error as exc:
            logger.warning("event=real.orders_unavailable error=%s", exc)
            return None
        snapshot: dict[str, OrderInfo] = {}
        for order in orders:
            snapshot[order.client_order_id] = order
        return snapshot

    async def _reconcile_real_trade(
        self,
        trade: TradeRecord,
        positions_by_id: dict[str, PositionInfo] | None,
        positions_by_symbol: dict[str, PositionInfo] | None,
        open_orders: dict[str, OrderInfo] | None,
    ) -> None:
        now = self._now()
        status = trade.trade_status
        if status in (TradeStatus.SUBMITTING, TradeStatus.SUBMIT_UNCERTAIN):
            await reconcile_uncertain_trade(storage=self._storage, client=self._client, trade=trade, now=now)
            return
        if status is TradeStatus.NEEDS_REVIEW:
            return  # operator decision required
        if status is TradeStatus.ENTRY_PENDING:
            await self._reconcile_pending_entry(trade, open_orders, now)
            return
        if status is TradeStatus.ENTRY_FILLED:
            await self._reconcile_filled(trade, positions_by_id, positions_by_symbol, now)

    async def _reconcile_pending_entry(
        self, trade: TradeRecord, open_orders: dict[str, OrderInfo] | None, now: datetime
    ) -> None:
        order: OrderInfo | None = None
        if open_orders is not None and trade.client_order_id:
            order = open_orders.get(trade.client_order_id)
        if order is None:
            order = await self._fetch_order(trade)
        if order is None:
            # Nothing open and nothing found in history: leave the TTL logic to decide.
            if self._ttl > 0 and now - trade.created_at >= timedelta(minutes=self._ttl):
                self._storage.update_trade(
                    trade.id, now, status=TradeStatus.NEEDS_REVIEW.value,
                    status_reason="entry order disappeared from Pi42 without a known fill; review manually",
                )
                self._storage.add_event(now, "real.entry_missing", level="WARNING", trade_id=trade.id,
                                        signal_id=trade.signal_id)
            return

        new_status = map_order_status(order.status)
        if new_status is TradeStatus.ENTRY_FILLED:
            self._mark_filled(trade, order, now)
            return
        if new_status in (TradeStatus.CANCELLED, TradeStatus.EXPIRED, TradeStatus.REJECTED):
            self._storage.update_trade(
                trade.id, now, status=new_status.value, closed_at=now,
                status_reason=f"Pi42 order {order.status}",
            )
            self._storage.add_event(now, "real.entry_terminated", trade_id=trade.id, signal_id=trade.signal_id,
                                    detail={"status": order.status})
            await self._notify(f"[REAL] {trade.symbol} entry order #{trade.id} is {order.status}.")
            return
        if new_status is None:
            logger.warning("event=real.unknown_order_status trade_id=%s status=%s", trade.id, order.status)
            return

        # Still open. Apply the TTL only to completely unfilled orders.
        if self._ttl > 0 and now - trade.created_at >= timedelta(minutes=self._ttl):
            if order.filled_quantity > 0:
                logger.warning("event=real.ttl_partial_fill_kept trade_id=%s filled=%s", trade.id,
                               order.filled_quantity)
                return
            # Re-confirm immediately before cancelling so a just-filled order is never cancelled.
            fresh = await self._fetch_order(trade)
            if fresh is not None and (
                fresh.filled_quantity > 0 or map_order_status(fresh.status) is TradeStatus.ENTRY_FILLED
            ):
                self._mark_filled(trade, fresh, now)
                return
            logger.info("event=real.ttl_cancel_requested trade_id=%s client_order_id=%s", trade.id,
                        order.client_order_id)
            try:
                await self._client.cancel_order(order.client_order_id)
            except Pi42Error as exc:
                logger.error("event=real.ttl_cancel_failed trade_id=%s error=%s", trade.id, exc)
                return
            confirmed = await self._fetch_order(trade)
            if confirmed is not None and (
                confirmed.filled_quantity > 0 or map_order_status(confirmed.status) is TradeStatus.ENTRY_FILLED
            ):
                self._mark_filled(trade, confirmed, now)
            elif confirmed is None or map_order_status(confirmed.status) in (
                TradeStatus.CANCELLED, TradeStatus.EXPIRED
            ):
                self._storage.update_trade(trade.id, now, status=TradeStatus.CANCELLED.value,
                                           status_reason="cancelled after LIMIT_ORDER_TTL_MINUTES",
                                           closed_at=now)
                self._storage.add_event(now, "real.ttl_cancelled", trade_id=trade.id, signal_id=trade.signal_id)
                await self._notify(f"[REAL] {trade.symbol} unfilled entry #{trade.id} cancelled after TTL.")
            else:
                logger.error("event=real.ttl_cancel_unconfirmed trade_id=%s status=%s", trade.id,
                             confirmed.status)

    def _mark_filled(self, trade: TradeRecord, order: OrderInfo, now: datetime) -> None:
        fill_price = order.average_price or order.price
        self._storage.update_trade(
            trade.id,
            now,
            status=TradeStatus.ENTRY_FILLED.value,
            fill_price=fill_price,
            filled_quantity=order.filled_quantity or trade.quantity,
            filled_at=now,
            status_reason="entry filled on Pi42",
        )
        self._storage.add_event(now, "real.entry_filled", trade_id=trade.id, signal_id=trade.signal_id,
                                detail={"fill_price": fill_price, "filled_quantity": order.filled_quantity})
        logger.info("event=real.entry_filled trade_id=%s fill_price=%s", trade.id, fill_price)

    async def _reconcile_filled(
        self,
        trade: TradeRecord,
        positions_by_id: dict[str, PositionInfo] | None,
        positions_by_symbol: dict[str, PositionInfo] | None,
        now: datetime,
    ) -> None:
        """For a filled trade: confirm the ROI take-profit and square off on target."""
        position = self._find_position(trade, positions_by_id, positions_by_symbol)
        if position is None and positions_by_id is not None:
            # Position no longer open on Pi42 (take-profit, liquidation or manual close).
            await self._handle_position_closed(trade, now)
            return

        order = await self._fetch_order(trade)
        if order is not None and not trade.tp_confirmed:
            expected = trade.target1
            if order.take_profit_price is not None and expected is not None and (
                abs(order.take_profit_price - expected) <= (self._price_tolerance(expected))
            ):
                self._storage.update_trade(
                    trade.id, now, tp_confirmed=True,
                    tp_order_id=order.link_id or order.client_order_id,
                    status_reason=f"{self._roi_label()} take-profit confirmed by Pi42",
                )
                self._storage.add_event(
                    now, "real.tp_confirmed", trade_id=trade.id, signal_id=trade.signal_id,
                    detail={"tp_order_id": order.link_id or order.client_order_id, "target": str(expected)},
                )
                logger.info("event=real.tp_confirmed trade_id=%s target=%s", trade.id, expected)
            elif order.take_profit_price is None and trade.status_reason != "TAKE-PROFIT NOT CONFIRMED":
                self._storage.update_trade(trade.id, now, status_reason="TAKE-PROFIT NOT CONFIRMED")
                self._storage.add_event(
                    now, "real.tp_not_confirmed", level="WARNING", trade_id=trade.id,
                    signal_id=trade.signal_id,
                    detail={"expected_target1": str(expected), "order_tp": None},
                )
                logger.warning("event=real.tp_not_confirmed trade_id=%s expected=%s", trade.id, expected)

        target_price = trade.target1
        if target_price is None or target_price <= 0:
            return
        try:
            current_price = await self._client.get_ticker_price(trade.symbol)
        except Pi42Error as exc:
            logger.warning("event=real.price_fetch_failed trade_id=%s error=%s", trade.id, exc)
            return
        if current_price is None or current_price <= 0:
            return

        is_hit = (
            (trade.direction == "LONG" and current_price >= target_price)
            or (trade.direction == "SHORT" and current_price <= target_price)
        )
        if not is_hit:
            return

        # Pi42 normally closes the position itself via the attached take-profit;
        # if it has not yet, square off at market with a reduce-only order.
        if position is None:
            await self._handle_position_closed(trade, now, current_price)
            return
        await self._square_off(trade, position, current_price, now)

    def _find_position(self, trade: TradeRecord, by_id, by_symbol) -> PositionInfo | None:
        if by_id is None:
            return None
        if trade.position_id and trade.position_id in by_id:
            return by_id[trade.position_id]
        return by_symbol.get(trade.symbol) if by_symbol else None

    async def _square_off(self, trade: TradeRecord, position: PositionInfo, price: Decimal, now: datetime) -> None:
        quantity = position.quantity if position.quantity and position.quantity > 0 else trade.quantity
        ref_id = f"CLOSE-{trade.id}"
        try:
            await self._client.close_position(
                symbol=trade.symbol,
                position_id=position.position_id,
                side_of_position=position.side or trade.direction,
                quantity=quantity,
                margin_asset=self._margin_asset,
                ref_id=ref_id,
            )
        except AmbiguousMutationError as exc:
            logger.error("event=real.close_uncertain trade_id=%s error=%s (will re-check next cycle)",
                         trade.id, exc)
            self._storage.add_event(now, "real.close_uncertain", level="WARNING", trade_id=trade.id,
                                    signal_id=trade.signal_id, detail={"error": str(exc)})
            return
        except Pi42Error as exc:
            if is_duplicate_ref_error(exc):
                logger.info("event=real.close_already_submitted trade_id=%s ref_id=%s", trade.id, ref_id)
            else:
                logger.error("event=real.roi_close_failed trade_id=%s error=%s", trade.id, exc)
                return

        self._storage.update_trade(
            trade.id,
            now,
            status=TradeStatus.CLOSED.value,
            closed_at=now,
            position_id=position.position_id,
            exit_price=price,
            exit_reason="TARGET_1_HIT",
            targets_hit=json.dumps([1]),
            status_reason=f"{self._roi_label()} target hit at {price} (target {trade.target1}); "
                          "position squared off with a reduce-only MARKET order",
        )
        self._storage.add_event(
            now, "real.roi_target_hit", trade_id=trade.id, signal_id=trade.signal_id,
            detail={"target_price": str(trade.target1), "current_price": str(price)},
        )
        logger.info("event=real.roi_target_hit trade_id=%s price=%s target=%s", trade.id, price, trade.target1)
        await self._notify(
            f"[REAL] {trade.symbol} ROI target {trade.target1} reached at {price}; position squared off."
        )

    async def _handle_position_closed(self, trade: TradeRecord, now: datetime, price: Decimal | None = None) -> None:
        """Position no longer open on Pi42 - mark as closed and require verification."""
        if price is None:
            try:
                price = await self._client.get_ticker_price(trade.symbol)
            except Pi42Error:
                price = None
        reached_target = (
            price is not None
            and trade.target1 is not None
            and (
                (trade.direction == "LONG" and price >= trade.target1)
                or (trade.direction == "SHORT" and price <= trade.target1)
            )
        )
        self._storage.update_trade(
            trade.id, now, status=TradeStatus.CLOSED.value, closed_at=now,
            exit_price=price,
            exit_reason="TARGET_1_HIT" if reached_target else "NO_LONGER_OPEN",
            targets_hit=json.dumps([1]) if reached_target else trade.targets_hit,
            status_reason=(
                "position no longer open on Pi42 (take-profit, liquidation or manual close). "
                "Exit price and PnL must be verified in the Pi42 order/trade history."
            ),
        )
        self._storage.add_event(now, "real.position_closed_unverified", trade_id=trade.id,
                                signal_id=trade.signal_id,
                                detail={"observed_price": str(price) if price is not None else None})
        logger.warning("event=real.position_closed_unverified trade_id=%s", trade.id)

    async def _fetch_order(self, trade: TradeRecord) -> OrderInfo | None:
        """Fetch the order by clientOrderId, falling back to refId, then history."""
        if trade.client_order_id:
            try:
                return await self._client.get_order(trade.client_order_id)
            except Pi42NotFoundError:
                pass
            except Pi42Error as exc:
                logger.warning("event=real.order_lookup_failed trade_id=%s error=%s", trade.id, exc)
                return None
        if trade.ref_id:
            try:
                return await self._client.get_order_by_ref_id(trade.ref_id)
            except Pi42NotFoundError:
                pass
            except Pi42Error as exc:
                logger.warning("event=real.order_lookup_failed trade_id=%s error=%s", trade.id, exc)
                return None
        return None

    def _price_tolerance(self, target: Decimal) -> Decimal:
        """Tiny tolerance for float-rounding in Pi42's JSON price echo."""
        return abs(target) * Decimal("0.000001") + Decimal("0.0000001")

    def _roi_label(self) -> str:
        return f"{self._target_roi_pct}% ROI"

    async def _notify(self, text: str) -> None:
        await self._notifier.send(text)
