"""Background reconciliation and multi-target TP/SL management.

PAPER: fetch the live public asset price per symbol and advance simulated trades.
REAL : reconcile every active trade with Mudrex:
       * Uncertain submissions are looked up, never blindly resubmitted.
       * LIMIT entries that stay unfilled past LIMIT_ORDER_TTL_MINUTES are cancelled
         only after re-confirming the order is still unfilled.
       * After entry fills, reduce-only LIMIT orders are placed at each target price
         for 1/N of the position quantity (N = total targets).
       * When Target 1 is hit: SL is moved to Entry price (Break-Even).
       * When Target N (>1) is hit: SL is moved to Target N-1 price.
       * Emergency SL: if unrealised PnL margin reaches -75%, the SL from the
         telegram signal is placed immediately.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Callable

from app.models import AssetInfo, Mode, OrderInfo, PositionInfo, TradeRecord, TradeStatus, utc_now
from app.mudrex_client import AmbiguousMutationError, MudrexError, MudrexNotFoundError
from app.notifications import NullNotifier
from app.paper_broker import PaperBroker
from app.risk_manager import snap_to_price_grid
from app.storage import Storage

logger = logging.getLogger("monitor")

# Mudrex order status -> our trade status. Unknown statuses keep the trade pending.
ORDER_STATUS_MAP = {
    "FILLED": TradeStatus.ENTRY_FILLED,
    "CREATED": TradeStatus.ENTRY_PENDING,
    "PARTIAL_FILLED": TradeStatus.ENTRY_PENDING,
    "CANCELLED": TradeStatus.CANCELLED,
    "SYSTEM_CANCELLED": TradeStatus.CANCELLED,
}

# If unrealised PnL as a fraction of est_margin drops below this, place emergency SL.
EMERGENCY_SL_MARGIN_THRESHOLD = Decimal("-0.75")  # -75%


def map_order_status(status: str) -> TradeStatus | None:
    return ORDER_STATUS_MAP.get(status.upper())


async def reconcile_uncertain_trade(
    *, storage: Storage, client, trade: TradeRecord, now: datetime, notifier=None
) -> TradeStatus | None:
    """Resolve SUBMITTING / SUBMIT_UNCERTAIN trades by looking up client_order_id.

    Never resubmits. A 404 means Mudrex has no order for this id, which is
    parked as NEEDS_REVIEW for an operator to resolve (no automatic retry).
    """
    try:
        order = await client.get_order_by_client_id(trade.client_order_id)
    except MudrexNotFoundError:
        reason = (
            "submission outcome unknown and Mudrex has no order for this client_order_id; "
            "NOT resubmitted automatically. Review, then run `resolve-trade`."
        )
        storage.update_trade(trade.id, now, status=TradeStatus.NEEDS_REVIEW.value, status_reason=reason)
        storage.add_event(now, "real.reconcile_not_found", level="WARNING", trade_id=trade.id,
                          signal_id=trade.signal_id, detail={"client_order_id": trade.client_order_id})
        logger.error("event=real.reconcile_not_found trade_id=%s client_order_id=%s", trade.id,
                     trade.client_order_id)
        return TradeStatus.NEEDS_REVIEW
    except MudrexError as exc:
        logger.warning("event=real.reconcile_retry_later trade_id=%s error=%s", trade.id, exc)
        return None

    new_status = map_order_status(order.status)
    updates: dict = {
        "order_id": order.order_id,
        "position_id": order.position_id,
        "status_reason": f"adopted Mudrex order status {order.status}",
    }
    if new_status is None:
        updates["status_reason"] = f"unrecognised Mudrex order status {order.status}; needs review"
        updates["status"] = TradeStatus.NEEDS_REVIEW.value
    else:
        updates["status"] = new_status.value
        if new_status is TradeStatus.ENTRY_FILLED:
            updates.update(filled_at=now, fill_price=order.filled_price, filled_quantity=order.filled_quantity)
    storage.update_trade(trade.id, now, **updates)
    storage.add_event(now, "real.reconciled", trade_id=trade.id, signal_id=trade.signal_id,
                      detail={"order_id": order.order_id, "order_status": order.status})
    logger.info("event=real.reconciled trade_id=%s order_id=%s order_status=%s", trade.id, order.order_id,
                order.status)
    return TradeStatus(updates["status"])


def _tp_client_order_id(trade_id: int, target_idx: int) -> str:
    """Deterministic client order ID for a TP leg."""
    digest = hashlib.sha256(f"tp:{trade_id}:{target_idx}".encode()).hexdigest()[:16]
    return f"TP-{trade_id}-T{target_idx}-{digest}"


def _sl_client_order_id(trade_id: int, version: int) -> str:
    """Deterministic client order ID for a SL order (version increments when SL is moved)."""
    digest = hashlib.sha256(f"sl:{trade_id}:{version}".encode()).hexdigest()[:16]
    return f"SL-{trade_id}-V{version}-{digest}"


def _split_quantity(total: Decimal, n: int, step: Decimal) -> list[Decimal]:
    """Split total quantity into n equal lots, snapped to step. Last lot gets the remainder."""
    if n <= 0:
        return []
    unit = (total / n).quantize(step, rounding=ROUND_DOWN)
    lots = [unit] * n
    # Add remainder to last lot to avoid leaving fractional qty in the position
    placed_total = unit * n
    remainder = total - placed_total
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
        currency: str,
        paper_broker: PaperBroker | None,
        ttl_minutes: int,
        target_order_type: str = "LIMIT",
        notifier=None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._storage = storage
        self._client = client
        self._mode = mode
        self._currency = currency
        self._paper = paper_broker
        self._ttl = ttl_minutes
        self._target_order_type = target_order_type.upper()
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
                asset = await self._client.get_asset_detail(symbol)
            except MudrexError as exc:
                logger.warning("event=paper.price_unavailable symbol=%s error=%s", symbol, exc)
                continue
            if asset.price is None:
                continue
            now = self._now()
            for trade, result in self._paper.apply_price(symbol, asset.price, now):
                await self._notifier.send(
                    f"[PAPER] {symbol} trade #{trade.id}: {result.new_status.value} "
                    f"(observed {asset.price}). Simulated only."
                )

    # -------------------------------------------------------------- REAL
    async def _monitor_real(self) -> None:
        trades = self._storage.active_trades(Mode.REAL.value)
        if not trades:
            return
        try:
            positions = await self._client.list_open_positions(self._currency)
        except MudrexError as exc:
            logger.warning("event=real.positions_unavailable error=%s", exc)
            positions = None
        positions_by_id = {p.position_id: p for p in positions} if positions is not None else None

        for trade in trades:
            try:
                await self._reconcile_real_trade(trade, positions_by_id)
            except MudrexError as exc:
                logger.warning("event=real.monitor_api_error trade_id=%s error=%s", trade.id, exc)
            except Exception:  # noqa: BLE001 - one bad trade must not stop monitoring others
                logger.exception("event=real.monitor_unexpected_error trade_id=%s", trade.id)

    async def _reconcile_real_trade(self, trade: TradeRecord, positions_by_id: dict[str, PositionInfo] | None) -> None:
        now = self._now()
        status = trade.trade_status
        if status in (TradeStatus.SUBMITTING, TradeStatus.SUBMIT_UNCERTAIN):
            await reconcile_uncertain_trade(storage=self._storage, client=self._client, trade=trade, now=now)
            return
        if status is TradeStatus.NEEDS_REVIEW:
            return  # operator decision required
        if status is TradeStatus.ENTRY_PENDING:
            await self._reconcile_pending_entry(trade, now)
            return
        if status is TradeStatus.ENTRY_FILLED:
            await self._reconcile_filled(trade, positions_by_id, now)

    async def _reconcile_pending_entry(self, trade: TradeRecord, now: datetime) -> None:
        order = await self._fetch_order(trade)
        new_status = map_order_status(order.status)
        if new_status is TradeStatus.ENTRY_FILLED:
            self._mark_filled(trade, order, now)
            return
        if new_status is TradeStatus.CANCELLED:
            self._storage.update_trade(trade.id, now, status=TradeStatus.CANCELLED.value,
                                       status_reason=f"Mudrex order {order.status}", closed_at=now)
            self._storage.add_event(now, "real.entry_cancelled", trade_id=trade.id, signal_id=trade.signal_id)
            await self._notify(f"[REAL] {trade.symbol} entry order #{trade.id} is {order.status}.")
            return
        if new_status is None:
            logger.warning("event=real.unknown_order_status trade_id=%s status=%s", trade.id, order.status)
            return

        # Still open. Apply the TTL only to completely unfilled orders.
        if self._ttl > 0 and now - trade.created_at >= _minutes(self._ttl):
            if order.filled_quantity > 0:
                logger.warning("event=real.ttl_partial_fill_kept trade_id=%s filled=%s", trade.id,
                               order.filled_quantity)
                return
            # Re-confirm immediately before cancelling so a just-filled order is never cancelled.
            fresh = await self._fetch_order(trade)
            if fresh.filled_quantity > 0 or map_order_status(fresh.status) is TradeStatus.ENTRY_FILLED:
                self._mark_filled(trade, fresh, now)
                return
            logger.info("event=real.ttl_cancel_requested trade_id=%s order_id=%s", trade.id, order.order_id)
            try:
                await self._client.cancel_order(order.order_id)
            except MudrexError as exc:
                logger.error("event=real.ttl_cancel_failed trade_id=%s error=%s", trade.id, exc)
                return
            confirmed = await self._fetch_order(trade)
            if map_order_status(confirmed.status) is TradeStatus.ENTRY_FILLED or confirmed.filled_quantity > 0:
                self._mark_filled(trade, confirmed, now)
            elif map_order_status(confirmed.status) is TradeStatus.CANCELLED:
                self._storage.update_trade(trade.id, now, status=TradeStatus.CANCELLED.value,
                                           status_reason="cancelled after LIMIT_ORDER_TTL_MINUTES", closed_at=now)
                self._storage.add_event(now, "real.ttl_cancelled", trade_id=trade.id, signal_id=trade.signal_id)
                await self._notify(f"[REAL] {trade.symbol} unfilled entry #{trade.id} cancelled after TTL.")
            else:
                logger.error("event=real.ttl_cancel_unconfirmed trade_id=%s status=%s", trade.id, confirmed.status)

    def _mark_filled(self, trade: TradeRecord, order: OrderInfo, now: datetime) -> None:
        self._storage.update_trade(
            trade.id,
            now,
            status=TradeStatus.ENTRY_FILLED.value,
            fill_price=order.filled_price,
            filled_quantity=order.filled_quantity,
            filled_at=now,
            position_id=order.position_id or trade.position_id,
            status_reason="entry filled on Mudrex",
        )
        self._storage.add_event(now, "real.entry_filled", trade_id=trade.id, signal_id=trade.signal_id,
                                detail={"fill_price": order.filled_price, "filled_quantity": order.filled_quantity})
        logger.info("event=real.entry_filled trade_id=%s fill_price=%s", trade.id, order.filled_price)

    async def _reconcile_filled(self, trade: TradeRecord, positions_by_id: dict[str, PositionInfo] | None,
                                now: datetime) -> None:
        """For a filled trade: manage multi-target partial exits, trailing SL, and emergency SL."""
        if positions_by_id is None or trade.position_id is None:
            return  # cannot verify this cycle

        position = positions_by_id.get(trade.position_id)
        if position is None:
            # Position no longer open on Mudrex (take-profit hit, SL hit, or manual close)
            self._handle_position_closed(trade, now)
            return

        targets = trade.all_targets()
        n_targets = len(targets)

        tp_ok = (
            position.take_profit_price is not None
            and position.take_profit_price == trade.target1
            and position.take_profit_order_id is not None
        )
        if tp_ok and not trade.tp_confirmed:
            self._storage.update_trade(trade.id, now, tp_confirmed=True, tp_order_id=position.take_profit_order_id,
                                       status_reason="Target 1 take-profit confirmed by Mudrex")
            self._storage.add_event(now, "real.tp_confirmed", trade_id=trade.id, signal_id=trade.signal_id,
                                    detail={"tp_order_id": position.take_profit_order_id})
            logger.info("event=real.tp_confirmed trade_id=%s", trade.id)
        elif not tp_ok and not trade.tp_confirmed and trade.status_reason != "TAKE-PROFIT NOT CONFIRMED":
            self._storage.update_trade(trade.id, now, status_reason="TAKE-PROFIT NOT CONFIRMED")
            self._storage.add_event(now, "real.tp_not_confirmed", level="ERROR", trade_id=trade.id,
                                    signal_id=trade.signal_id,
                                    detail={"expected_target1": trade.target1,
                                            "position_tp": position.take_profit_price})
            logger.error("event=real.tp_not_confirmed trade_id=%s expected=%s", trade.id, trade.target1)

        # For multi-target trades: if an initial 100% TP order was attached to Mudrex position on entry,
        # cancel it so Mudrex doesn't exit 100% of the position at Target 1.
        if n_targets > 1 and position.take_profit_order_id:
            try:
                await self._client.cancel_order(position.take_profit_order_id)
                logger.info("event=real.cancelled_full_tp trade_id=%s tp_order_id=%s",
                            trade.id, position.take_profit_order_id)
            except MudrexError as exc:
                logger.warning("event=real.cancel_full_tp_failed trade_id=%s error=%s", trade.id, exc)

        # Fetch current market price & asset detail for quantity_step
        try:
            asset = await self._client.get_asset_detail(trade.symbol)
        except MudrexError as exc:
            logger.warning("event=real.price_fetch_failed trade_id=%s error=%s", trade.id, exc)
            return

        current_price = asset.price
        if current_price is None or current_price <= 0:
            return

        # Check multi-target progression
        if n_targets > 1:
            await self._check_multi_targets(trade, position, asset, current_price, targets, now)

        # Emergency SL check (-75% margin loss) if Target 1 not yet hit
        hit_set = trade.targets_hit_set()
        if 1 not in hit_set:
            await self._check_emergency_sl(trade, position, current_price, now)

    async def _check_multi_targets(
        self,
        trade: TradeRecord,
        position: PositionInfo,
        asset: AssetInfo,
        current_price: Decimal,
        targets: list[Decimal],
        now: datetime,
    ) -> None:
        """Evaluate targets 1..N and execute partial closes & trailing SL."""
        n_targets = len(targets)
        hit_set = trade.targets_hit_set()
        total_qty = trade.filled_quantity or trade.quantity
        if total_qty <= 0 or position.quantity <= 0:
            return

        lots = _split_quantity(total_qty, n_targets, asset.quantity_step)
        entry_price = trade.fill_price or trade.entry_price

        for idx, target_price in enumerate(targets):
            t_num = idx + 1
            if t_num in hit_set:
                continue

            # Check if target reached
            is_hit = (
                (trade.direction == "LONG" and current_price >= target_price)
                or (trade.direction == "SHORT" and current_price <= target_price)
            )
            if not is_hit:
                break  # targets are strictly sequential

            # Target is reached!
            is_final = (t_num == n_targets)
            lot_qty = lots[idx] if idx < len(lots) else position.quantity

            if is_final:
                # Final target: square off remaining position
                try:
                    await self._client.close_position(trade.position_id)
                    hit_set.add(t_num)
                    self._storage.update_trade(
                        trade.id,
                        now,
                        targets_hit=json.dumps(sorted(list(hit_set))),
                        status=TradeStatus.CLOSED.value,
                        closed_at=now,
                        exit_price=current_price,
                        exit_reason=f"TARGET_{t_num}_HIT",
                        status_reason=f"Final Target {t_num} hit at {current_price}; position squared off",
                    )
                    self._storage.add_event(
                        now, "real.target_hit", trade_id=trade.id, signal_id=trade.signal_id,
                        detail={"target": t_num, "target_price": str(target_price),
                                "current_price": str(current_price), "action": "squared_off"}
                    )
                    logger.info("event=real.target_final_hit trade_id=%s target=%s price=%s",
                                trade.id, t_num, current_price)
                    await self._notify(
                        f"🎯 [REAL] {trade.symbol} FINAL Target {t_num} reached ({target_price})! "
                        f"Position squared off at {current_price}."
                    )
                except (AmbiguousMutationError, MudrexError) as exc:
                    logger.error("event=real.final_close_failed trade_id=%s error=%s", trade.id, exc)
                return  # trade finished
            else:
                # Partial close lot_qty
                close_qty = min(lot_qty, position.quantity)
                coid = _tp_client_order_id(trade.id, t_num)
                order_type = self._target_order_type
                limit_price = snap_to_price_grid(f"target{t_num}", target_price, asset) if (order_type == "LIMIT" and asset.price_step) else target_price
                try:
                    await self._client.partial_close_position(
                        trade.position_id,
                        quantity=close_qty,
                        order_type=order_type,
                        limit_price=limit_price if order_type == "LIMIT" else None,
                        client_order_id=coid,
                    )
                    hit_set.add(t_num)
                    logger.info("event=real.partial_closed trade_id=%s target=%s qty=%s type=%s price=%s",
                                trade.id, t_num, close_qty, order_type, limit_price if order_type == "LIMIT" else "MARKET")
                except (AmbiguousMutationError, MudrexError) as exc:
                    logger.error("event=real.partial_close_failed trade_id=%s target=%s qty=%s error=%s",
                                 trade.id, t_num, close_qty, exc)
                    return

                # Calculate new SL:
                # Target 1 hit -> SL moved to Entry Price (Break-Even)
                # Target 2 hit -> SL moved to Target 1 price
                # Target k hit -> SL moved to Target k-1 price
                if t_num == 1:
                    new_sl = entry_price
                    sl_desc = f"Break-Even ({new_sl})"
                else:
                    new_sl = targets[idx - 1]
                    sl_desc = f"Target {t_num - 1} ({new_sl})"

                sl_coid = _sl_client_order_id(trade.id, t_num)
                try:
                    await self._client.set_position_risk(
                        trade.position_id,
                        stoploss_price=new_sl,
                        is_stoploss=True,
                        stoploss_client_order_id=sl_coid,
                    )
                    self._storage.update_trade(
                        trade.id,
                        now,
                        targets_hit=json.dumps(sorted(list(hit_set))),
                        sl_price=new_sl,
                        status_reason=f"Target {t_num} hit ({target_price}); closed {close_qty}; SL moved to {sl_desc}",
                    )
                    self._storage.add_event(
                        now, "real.target_hit", trade_id=trade.id, signal_id=trade.signal_id,
                        detail={"target": t_num, "target_price": str(target_price),
                                "qty_closed": str(close_qty), "sl_moved_to": str(new_sl)}
                    )
                    logger.info("event=real.sl_updated trade_id=%s new_sl=%s", trade.id, new_sl)
                    await self._notify(
                        f"🎯 [REAL] {trade.symbol} Target {t_num} hit ({target_price})! "
                        f"Partially closed {close_qty} (1/{n_targets}). SL moved to {sl_desc}."
                    )
                except (AmbiguousMutationError, MudrexError) as exc:
                    logger.error("event=real.sl_update_failed trade_id=%s new_sl=%s error=%s",
                                 trade.id, new_sl, exc)
                    self._storage.update_trade(
                        trade.id,
                        now,
                        targets_hit=json.dumps(sorted(list(hit_set))),
                        status_reason=f"Target {t_num} hit; closed {close_qty}; SL update failed: {exc}",
                    )
                await asyncio.sleep(1.0)

    async def _check_emergency_sl(
        self, trade: TradeRecord, position: PositionInfo, current_price: Decimal, now: datetime
    ) -> None:
        """Place the telegram SL only when unrealised margin loss reaches -75%."""
        if trade.sl_price is not None:
            return  # SL is already set

        emergency_price = trade.stop_loss_reference
        if emergency_price is None:
            return  # signal had no reference SL

        fill = trade.fill_price or trade.entry_price
        if fill is None or fill <= 0 or trade.est_margin <= 0:
            return

        qty = position.quantity or trade.quantity
        if trade.direction == "LONG":
            unrealised_pnl = (current_price - fill) * qty
        else:
            unrealised_pnl = (fill - current_price) * qty

        margin_loss_pct = unrealised_pnl / trade.est_margin
        if margin_loss_pct > EMERGENCY_SL_MARGIN_THRESHOLD:
            return  # not bad enough yet

        # -75% threshold breached — place emergency SL now
        logger.warning(
            "event=real.emergency_sl_triggered trade_id=%s symbol=%s margin_loss_pct=%.2f%% sl_price=%s",
            trade.id, trade.symbol, float(margin_loss_pct) * 100, emergency_price,
        )
        self._storage.add_event(
            now, "real.emergency_sl_triggered", level="WARNING",
            trade_id=trade.id, signal_id=trade.signal_id,
            detail={"margin_loss_pct": str(margin_loss_pct),
                    "current_price": str(current_price),
                    "sl_price": str(emergency_price)},
        )
        await self._notify(
            f"⚠️ [REAL] {trade.symbol} margin loss at {float(margin_loss_pct)*100:.1f}%! "
            f"Placing Emergency SL at {emergency_price}."
        )

        sl_coid = _sl_client_order_id(trade.id, 0)
        try:
            await self._client.set_position_risk(
                trade.position_id,
                stoploss_price=emergency_price,
                is_stoploss=True,
                stoploss_client_order_id=sl_coid,
            )
            self._storage.update_trade(
                trade.id,
                now,
                sl_price=emergency_price,
                status_reason=f"Emergency SL placed at {emergency_price} (-75% margin loss)",
            )
            self._storage.add_event(
                now, "real.emergency_sl_placed", trade_id=trade.id, signal_id=trade.signal_id,
                detail={"sl_price": str(emergency_price)}
            )
            logger.info("event=real.emergency_sl_placed trade_id=%s sl_price=%s", trade.id, emergency_price)
            await self._notify(
                f"🛡️ [REAL] {trade.symbol} Emergency SL confirmed at {emergency_price}."
            )
        except (AmbiguousMutationError, MudrexError) as exc:
            logger.error("event=real.emergency_sl_failed trade_id=%s sl_price=%s error=%s",
                         trade.id, emergency_price, exc)
            self._storage.update_trade(
                trade.id, now,
                status_reason=f"Emergency SL placement failed at {emergency_price}: {exc}"
            )
            # Safety fallback: if Mudrex rejected the SL order, square off immediately to save remaining capital
            try:
                await self._client.close_position(trade.position_id)
                self._storage.update_trade(
                    trade.id, now, status=TradeStatus.CLOSED.value, closed_at=now,
                    exit_price=current_price, exit_reason="EMERGENCY_SL_SQUARE_OFF",
                    status_reason=f"Emergency SL placement rejected ({exc}); squared off at {current_price} to prevent liquidation",
                )
                await self._notify(
                    f"🛡️ [REAL] {trade.symbol} Emergency Exit: SL placement rejected ({exc}); position squared off at {current_price}."
                )
            except Exception as close_exc:
                logger.error("event=real.emergency_close_failed trade_id=%s error=%s", trade.id, close_exc)



    def _handle_position_closed(self, trade: TradeRecord, now: datetime) -> None:
        """Position no longer open on Mudrex - mark as closed."""
        self._storage.update_trade(
            trade.id, now, status=TradeStatus.CLOSED.value, closed_at=now,
            exit_reason="NO_LONGER_OPEN",
            status_reason=("position no longer open on Mudrex (take-profit, SL, liquidation or manual close). "
                           "Exit price and PnL must be verified in Mudrex position history."),
        )
        self._storage.add_event(now, "real.position_closed_unverified", trade_id=trade.id,
                                signal_id=trade.signal_id)
        logger.warning("event=real.position_closed_unverified trade_id=%s", trade.id)

    async def _fetch_order(self, trade: TradeRecord) -> OrderInfo:
        if trade.order_id:
            return await self._client.get_order(trade.order_id)
        return await self._client.get_order_by_client_id(trade.client_order_id)

    async def _notify(self, text: str) -> None:
        await self._notifier.send(text)


def _minutes(value: int) -> timedelta:
    return timedelta(minutes=value)
