"""Signal processing pipeline: webhook event -> validated signal -> position/order.

Every transition is persisted. The flow (per spec):

  authenticate -> validate payload -> store raw event -> normalize symbol
  -> create internal signal -> enqueue -> process asynchronously -> HTTP response
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppSettings
from app.core.errors import (
    OrderStateUncertain,
    RejectReason,
    RiskRejection,
    SymbolError,
    TradingError,
)
from app.core.logging import TradeEvent, log_event
from app.exchanges.base import OrderRequest
from app.models.entities import Order, Signal, WebhookEvent
from app.models.enums import (
    Direction,
    EntryExit,
    OrderSide,
    OrderStatus,
    OrderType,
    ProcessingStatus,
    SignalAction,
    SignalListStatus,
    ValidationStatus,
)
from app.services.execution import ExecutionEngine, new_order_id
from app.services.pnl import calculate_quantity, target_price_for_roi
from app.services.position_manager import PositionManager, add_position_event
from app.services.reservation import ReservationManager
from app.services.risk import RiskManager
from app.services.symbols import SymbolService

log = logging.getLogger("trading.pipeline")

ENTRY_ACTIONS = {SignalAction.BUY.value, SignalAction.OPEN_LONG.value, SignalAction.SHORT.value, "SHORT", "OPEN_SHORT"}
EXIT_ACTIONS = {SignalAction.SELL.value, SignalAction.CLOSE.value, SignalAction.CLOSE_SHORT.value}
SHORT_ACTIONS = {SignalAction.SHORT.value, SignalAction.CLOSE_SHORT.value, "SHORT", "OPEN_SHORT"}


@dataclass
class ProcessOutcome:
    status: str
    reason: str = ""
    position_id: str = ""
    order_id: str = ""


class SignalPipeline:
    def __init__(
        self,
        settings: AppSettings,
        symbols: SymbolService,
        risk: RiskManager,
        reservations: ReservationManager,
        execution: ExecutionEngine,
        positions: PositionManager,
    ) -> None:
        self.settings = settings
        self.symbols = symbols
        self.risk = risk
        self.reservations = reservations
        self.execution = execution
        self.positions = positions

    # ---------------------------------------------------------------- ingest
    async def create_signal(
        self,
        session: AsyncSession,
        event: WebhookEvent,
        payload: dict,
    ) -> Signal:
        raw_symbol = str(payload.get("symbol") or "").strip().upper()
        action = str(payload.get("action") or "").strip().upper()
        replay_suffix = f"#replay-{uuid.uuid4().hex[:8]}" if payload.get("_replay") else ""

        rejection_reason = ""
        try:
            triple = await self.symbols.resolve(session, raw_symbol)  # STEP 1-9, before anything else
            shark_symbol = triple.shark_symbol
            canonical = triple.canonical_symbol
            raw = triple.tradingview_raw_symbol
        except SymbolError as exc:
            raw = raw_symbol
            try:
                canonical = self.symbols.normalize(raw_symbol)
            except SymbolError:
                canonical = raw_symbol
            shark_symbol = ""
            rejection_reason = str(exc.reason)

        strategy_id = str(payload.get("strategy_id") or "")
        strategy_name = str(payload.get("strategy_name") or "")
        alert_price = payload.get("price") or payload.get("alert_price")
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}

        direction = Direction.SHORT.value if action in (SignalAction.SHORT.value, "SHORT", "OPEN_SHORT") else Direction.LONG.value
        rejected = bool(rejection_reason)
        signal = Signal(
            event_id=event.event_id + replay_suffix,
            fingerprint=event.fingerprint + replay_suffix,
            correlation_id=event.correlation_id,
            received_at=event.received_at,
            event_timestamp=event.event_timestamp,
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            source=str(payload.get("source") or "TRADINGVIEW"),
            tradingview_raw_symbol=raw,
            canonical_symbol=canonical,
            shark_symbol=shark_symbol,
            action=action,
            direction=direction,
            timeframe=str(payload.get("timeframe") or ""),
            alert_price=Decimal(str(alert_price)) if alert_price not in (None, "") else None,
            raw_alert_payload=payload,
            metadata_=metadata,
            validation_status=ValidationStatus.INVALID.value if rejected else ValidationStatus.VALIDATED.value,
            processing_status=ProcessingStatus.REJECTED.value if rejected else ProcessingStatus.RECEIVED.value,
            status_display=(
                SignalListStatus.SHARK_SYMBOL_NOT_FOUND.value
                if rejection_reason == RejectReason.SHARK_SYMBOL_NOT_FOUND.value
                else SignalListStatus.REJECTED.value
            ) if rejected else SignalListStatus.RECEIVED.value,
            rejection_reason=rejection_reason,
            mode=self.settings.trading_mode,
            is_replay=bool(payload.get("_replay")),
        )
        session.add(signal)
        await session.flush()
        if rejected:
            log_event(log, TradeEvent.SHARK_SYMBOL_NOT_FOUND, "symbol resolution failed",
                      event_id=event.event_id, tradingview_raw_symbol=raw, reason=rejection_reason)
        else:
            log_event(
                log, TradeEvent.SIGNAL_VALIDATED, "signal created",
                event_id=event.event_id, strategy_id=strategy_id,
                tradingview_raw_symbol=signal.tradingview_raw_symbol,
                canonical_symbol=signal.canonical_symbol, shark_symbol=signal.shark_symbol,
                action=action,
            )
        return signal

    def _reject_signal(self, signal: Signal, reason: str, display: str | None = None) -> ProcessOutcome:
        signal.processing_status = ProcessingStatus.REJECTED.value
        signal.rejection_reason = str(reason)[:250]
        display_map = {
            RejectReason.MAX_OPEN_POSITIONS_REACHED.value: SignalListStatus.MAX_POSITIONS.value,
            RejectReason.SHARK_SYMBOL_NOT_FOUND.value: SignalListStatus.SHARK_SYMBOL_NOT_FOUND.value,
            RejectReason.SYMBOL_ALREADY_OPEN.value: SignalListStatus.SYMBOL_ALREADY_OPEN.value,
            RejectReason.DUPLICATE_EVENT.value: SignalListStatus.DUPLICATE.value,
            RejectReason.DUPLICATE_SIGNAL.value: SignalListStatus.DUPLICATE.value,
            RejectReason.DAILY_MAX_TRIGGERS_REACHED.value: SignalListStatus.DAILY_MAX_TRIGGERS.value,
            RejectReason.PRICE_NOT_BELOW_PREVIOUS.value: SignalListStatus.PRICE_NOT_BELOW_PREVIOUS.value,
        }
        signal.status_display = display or display_map.get(reason, SignalListStatus.REJECTED.value)
        return ProcessOutcome(status=signal.status_display, reason=reason)

    # --------------------------------------------------------------- process
    async def process_signal(self, session: AsyncSession, signal_id: int) -> ProcessOutcome:
        """Execute the full entry path for a stored signal (async from HTTP)."""
        signal = (await session.execute(select(Signal).where(Signal.id == signal_id))).scalar_one_or_none()
        if signal is None:
            return ProcessOutcome(status="ERROR", reason="signal missing")
        if signal.processing_status in (
            ProcessingStatus.ACCEPTED.value,
            ProcessingStatus.EXECUTED.value,
            ProcessingStatus.REJECTED.value,
            ProcessingStatus.DUPLICATE.value,
        ):
            return ProcessOutcome(status=signal.status_display, reason=signal.rejection_reason)
        if signal.validation_status == ValidationStatus.INVALID.value:
            return self._reject_signal(signal, signal.rejection_reason or RejectReason.INVALID_PAYLOAD.value,
                                       signal.status_display)

        signal.processing_status = ProcessingStatus.VALIDATING.value
        # Extract variables before processing, as session.rollback() inside reserve
        # will expire the signal object, causing lazy-load errors if accessed later.
        sig_correlation = signal.correlation_id
        sig_event = signal.event_id
        sig_canonical = signal.canonical_symbol

        try:
            outcome = await self._process_entry(session, signal)
            return outcome
        except RiskRejection as exc:
            # session.rollback() inside reserve expires all objects, refresh signal
            await session.refresh(signal)
            self.risk.record_risk_event(
                session, "SIGNAL_REJECTED", exc.reason, {"message": exc.message},
                sig_correlation, sig_event, sig_canonical,
            )
            log_event(log, TradeEvent.SIGNAL_REJECTED, exc.message,
                      reason=exc.reason, event_id=signal.event_id,
                      canonical_symbol=signal.canonical_symbol)
            return self._reject_signal(signal, exc.reason)
        except SymbolError as exc:
            log_event(log, TradeEvent.SHARK_SYMBOL_NOT_FOUND, exc.message,
                      event_id=signal.event_id, tradingview_raw_symbol=signal.tradingview_raw_symbol)
            return self._reject_signal(signal, exc.reason)
        except OrderStateUncertain as exc:
            signal.processing_status = ProcessingStatus.ERROR.value
            signal.rejection_reason = RejectReason.ORDER_SUBMISSION_UNCERTAIN.value
            signal.status_display = SignalListStatus.ERROR.value
            return ProcessOutcome(status="ERROR", reason=exc.reason)
        except TradingError as exc:
            log_event(log, TradeEvent.SIGNAL_REJECTED, exc.message, reason=exc.reason, level=logging.ERROR)
            return self._reject_signal(signal, str(exc.reason))
        except Exception as exc:  # noqa: BLE001 — never swallow silently
            log_event(log, TradeEvent.SYSTEM_ERROR, f"unhandled pipeline error: {exc}",
                      level=logging.ERROR, event_id=signal.event_id)
            signal.processing_status = ProcessingStatus.ERROR.value
            signal.rejection_reason = RejectReason.INTERNAL_ERROR.value
            signal.status_display = SignalListStatus.ERROR.value
            session.add(signal)
            await session.commit()
            return ProcessOutcome(status="ERROR", reason=str(exc))

    async def _process_entry(self, session: AsyncSession, signal: Signal) -> ProcessOutcome:
        action = signal.action
        if action in SHORT_ACTIONS and not self.settings.short_enabled:
            return self._reject_signal(signal, RejectReason.SHORT_DISABLED.value)
        if action not in ENTRY_ACTIONS:
            if action in EXIT_ACTIONS:
                return self._reject_signal(signal, RejectReason.ACTION_NOT_SUPPORTED.value,
                                           SignalListStatus.REJECTED.value)
            return self._reject_signal(signal, RejectReason.ACTION_NOT_SUPPORTED.value)

        # Re-resolve + verify contract status before any risk work (STEP 3-8)
        triple = await self.symbols.resolve(session, signal.tradingview_raw_symbol)
        signal.canonical_symbol = triple.canonical_symbol
        signal.shark_symbol = triple.shark_symbol
        rules = await self.symbols.rules_for(session, triple.canonical_symbol)
        if rules.contract_status.upper() != "ACTIVE":
            raise SymbolError(RejectReason.CONTRACT_INACTIVE, f"contract {triple.shark_symbol} not active")

        if signal.strategy_id:
            from app.models.entities import Strategy

            strategy = (
                await session.execute(select(Strategy).where(Strategy.strategy_id == signal.strategy_id))
            ).scalar_one_or_none()
            if strategy is None:
                strategy = Strategy(
                    strategy_id=signal.strategy_id,
                    name=signal.strategy_name or signal.strategy_id,
                    enabled=True,
                    webhook_identifier=signal.strategy_id,
                )
                session.add(strategy)
                await session.flush()
            elif not strategy.enabled:
                return self._reject_signal(signal, RejectReason.STRATEGY_DISABLED.value)

            if strategy.allowed_symbols and triple.canonical_symbol not in strategy.allowed_symbols:
                return self._reject_signal(signal, RejectReason.SYMBOL_NOT_FOUND.value)
            strategy.alert_count = (strategy.alert_count or 0) + 1

        # ---- persist signal state before the locked critical section --------
        await session.commit()
        await session.refresh(signal)

        # ---- atomic reservation (risk re-checks happen under the lock) ------
        metadata = signal.metadata_ or {}
        sig_margin = metadata.get("margin")
        trade_margin = Decimal(str(sig_margin)) if sig_margin else self.settings.margin_per_trade

        try:
            position = await self.reservations.reserve(
                session,
                canonical_symbol=triple.canonical_symbol,
                tradingview_raw_symbol=triple.tradingview_raw_symbol,
                shark_symbol=triple.shark_symbol,
                direction=signal.direction,
                alert_price=signal.alert_price,
                margin=trade_margin,
                strategy_id=signal.strategy_id,
                event_id=signal.event_id,
                correlation_id=signal.correlation_id,
            )
        except RiskRejection:
            raise
        
        # reservation.reserve performs a commit, so we must refresh signal
        await session.refresh(signal)
        sig_lev = metadata.get("leverage")
        position.signal_id = signal.id
        requested_leverage = int(sig_lev) if sig_lev else self.settings.leverage

        # Automatically cap leverage to broker/exchange maximum if signal leverage exceeds it
        max_lev = rules.max_leverage
        if max_lev and max_lev > 0 and requested_leverage > max_lev:
            position.leverage = max_lev
            log.info(
                f"Requested leverage {requested_leverage}x exceeds broker max {max_lev}x for {position.canonical_symbol}. "
                f"Automatically capping to {max_lev}x."
            )
            add_position_event(
                session, position.position_id, "LEVERAGE_CAPPED",
                detail={"requested_leverage": requested_leverage, "capped_leverage": max_lev, "broker_max": max_lev},
                correlation_id=signal.correlation_id,
            )
        else:
            position.leverage = requested_leverage
        position.target_roi_percent = self.settings.target_roi_percent
        position.direction = signal.direction
        if metadata.get("target_1"):
            try:
                position.target_price = Decimal(str(metadata["target_1"]))
            except Exception:
                pass

        add_position_event(session, position.position_id, "SIGNAL_RECEIVED",
                           detail={"event_id": signal.event_id}, correlation_id=signal.correlation_id)
        add_position_event(session, position.position_id, "SYMBOL_NORMALIZED",
                           detail={
                               "tradingview_raw_symbol": triple.tradingview_raw_symbol,
                               "canonical_symbol": triple.canonical_symbol,
                               "shark_symbol": triple.shark_symbol,
                           },
                           correlation_id=signal.correlation_id)

        # ---- quantity from REAL market price + verified contract rules ------
        try:
            adapter = self.execution.adapter_for(self.settings.trading_mode)
            ticker = await adapter.get_mark_price(triple.shark_symbol)
            ref_price = ticker.mark_price or ticker.last_price

            sig_entry = metadata.get("entry")
            limit_price = None
            if sig_entry:
                try:
                    limit_price = Decimal(str(sig_entry))
                except Exception:
                    limit_price = None
            if not limit_price or limit_price <= 0:
                limit_price = signal.alert_price

            # Use exact signal entry price for LIMIT orders; fallback to live ticker only if no price specified
            ref_for_qty = limit_price if (limit_price and limit_price > 0) else (ref_price or Decimal("0"))

            sig_margin = metadata.get("margin")
            trade_margin = Decimal(str(sig_margin)) if sig_margin else self.settings.margin_per_trade
            trade_notional = trade_margin * Decimal(position.leverage)

            decision = calculate_quantity(
                notional_target=trade_notional,
                entry_price=ref_for_qty,
                quantity_precision=rules.quantity_precision,
                min_quantity=rules.min_quantity,
                min_notional=rules.min_notional,
                quantity_step=rules.quantity_step,
            )
        except Exception:
            await self.reservations.release(session, position.canonical_symbol, position.position_id)
            raise
        if not decision.ok:
            await self.reservations.release(session, position.canonical_symbol, position.position_id)
            await self.positions.transition(
                session, position, "REJECTED", "SIGNAL_REJECTED",
                {"reason": decision.reason, "message": decision.message},
                signal.correlation_id,
            )
            log_event(log, TradeEvent.ORDER_FAILED, decision.message,
                      reason=decision.reason, canonical_symbol=position.canonical_symbol)
            return self._reject_signal(signal, decision.reason)

        position.quantity = decision.quantity
        position.margin = trade_margin
        position.notional = decision.quantity * ref_for_qty

        # ---- create order record then submit exactly once -------------------
        entry_side = OrderSide.SELL.value if position.direction == Direction.SHORT.value else OrderSide.BUY.value
        order = Order(
            order_id=new_order_id(),
            position_id=position.position_id,
            signal_id=signal.id,
            mode=self.settings.trading_mode,
            exchange=adapter.name,
            tradingview_raw_symbol=position.tradingview_raw_symbol,
            canonical_symbol=position.canonical_symbol,
            shark_symbol=position.shark_symbol,
            side=entry_side,
            order_type=OrderType.LIMIT.value,
            entry_exit=EntryExit.ENTRY.value,
            reduce_only=False,
            quantity=decision.quantity,
            requested_price=ref_for_qty,
            status=OrderStatus.PENDING.value,
            is_dry_run=self.settings.shark_dry_run and self.settings.trading_mode == "LIVE",
        )
        session.add(order)
        await self.positions.transition(
            session, position, "ORDER_PENDING", "ORDER_CREATED",
            {"order_id": order.order_id, "quantity": str(decision.quantity)},
            signal.correlation_id,
        )
        signal.position_id = position.position_id
        signal.order_id = order.order_id
        signal.processing_status = ProcessingStatus.ACCEPTED.value
        signal.status_display = SignalListStatus.ACCEPTED.value
        await session.commit()  # durable BEFORE any external call
        await session.refresh(signal)

        request = OrderRequest(
            symbol=triple.shark_symbol,  # NEVER tradingview_raw_symbol
            side=entry_side,
            quantity=decision.quantity,
            order_type=OrderType.LIMIT.value,
            price=ref_for_qty,
            margin_asset=self.settings.shark_margin_asset,
        )
        try:
            result = await self.execution.submit(session, order=order, request=request)
        except OrderStateUncertain:
            position.state = "UNKNOWN"
            add_position_event(session, position.position_id, "ORDER_UNKNOWN", to_state="UNKNOWN",
                               correlation_id=signal.correlation_id)
            await session.commit()
            raise

        if result.dry_run:
            # DRY RUN: payload recorded + visible, NO real order, NO open position.
            await self.reservations.release(session, position.canonical_symbol, position.position_id)
            await self.positions.transition(
                session, position, "CANCELLED", "DRY_RUN_ORDER",
                {"payload": result.raw.get("payload"), "reason": "DRY RUN — not submitted"},
                signal.correlation_id,
            )
            position.is_dry_run = True
            signal.processing_status = ProcessingStatus.EXECUTED.value
            signal.status_display = SignalListStatus.ACCEPTED.value
            signal.rejection_reason = RejectReason.DRY_RUN.value
            await session.commit()
            return ProcessOutcome(status="DRY_RUN", reason=RejectReason.DRY_RUN.value,
                                  position_id=position.position_id, order_id=order.order_id)

        if result.status.upper() in ("REJECTED", "FAILED"):
            await self.reservations.release(session, position.canonical_symbol, position.position_id)
            await self.positions.transition(
                session, position, "ORDER_FAILED", "ORDER_FAILED",
                {"error": result.error_message}, signal.correlation_id,
            )
            signal.status_display = SignalListStatus.REJECTED.value
            signal.rejection_reason = result.error_code or "ORDER_FAILED"
            await session.commit()
            return ProcessOutcome(status="REJECTED", reason=signal.rejection_reason,
                                  position_id=position.position_id, order_id=order.order_id)

        fill_price = order.avg_fill_price or ref_for_qty
        fill = await self.execution.record_fill(
            session, order=order, price=fill_price,
            quantity=order.filled_quantity or decision.quantity,
            entry_exit=EntryExit.ENTRY.value,
        )
        await self.positions.mark_open(
            session, position, entry_price=fill.price, quantity=fill.quantity,
            entry_fee=fill.fee, order=order, correlation_id=signal.correlation_id,
        )
        await self.reservations.convert(session, position.canonical_symbol, position.position_id)
        signal.processing_status = ProcessingStatus.EXECUTED.value
        signal.status_display = SignalListStatus.EXECUTED.value
        from app.models.entities import Strategy  # local import avoids cycles

        if signal.strategy_id:
            strategy = (await session.execute(select(Strategy).where(Strategy.strategy_id == signal.strategy_id))).scalar_one_or_none()
            if strategy:
                strategy.executed_trades = (strategy.executed_trades or 0) + 1
                
        # Background task: wait 10s then place limit sell order for ROI (if not custom signal targets)
        if not metadata.get("target_1"):
            tp_price = target_price_for_roi(
                entry_price=fill.price,
                leverage=position.leverage,
                target_roi_percent=self.settings.target_roi_percent,
                direction=position.direction
            )
            
            bg_pos_id = position.position_id
            bg_mode = position.mode
            bg_exchange = adapter.name
            bg_raw_symbol = position.tradingview_raw_symbol
            bg_canon_symbol = position.canonical_symbol
            bg_shark_symbol = position.shark_symbol
            bg_direction = position.direction
            bg_quantity = position.quantity
            bg_is_dry_run = position.is_dry_run
            bg_margin_asset = self.settings.shark_margin_asset

            async def place_delayed_tp_order():
                await asyncio.sleep(10)
                from app.db.session import get_session_factory
                session_factory = get_session_factory()
                async with session_factory() as bg_session:
                    from app.models.entities import Position
                    pos = await bg_session.get(Position, bg_pos_id)
                    if pos and pos.state in ("OPEN", "PARTIALLY_FILLED"):
                        exit_order = Order(
                            order_id=new_order_id(),
                            position_id=bg_pos_id,
                            mode=bg_mode,
                            exchange=bg_exchange,
                            tradingview_raw_symbol=bg_raw_symbol,
                            canonical_symbol=bg_canon_symbol,
                            shark_symbol=bg_shark_symbol,
                            side=OrderSide.SELL.value if bg_direction == "LONG" else OrderSide.BUY.value,
                            order_type=OrderType.LIMIT.value,
                            entry_exit=EntryExit.EXIT.value,
                            reduce_only=True,
                            quantity=bg_quantity,
                            requested_price=tp_price,
                            status=OrderStatus.PENDING.value,
                            is_dry_run=bg_is_dry_run,
                        )
                        bg_session.add(exit_order)
                        pos.exit_order_id = exit_order.order_id
                        await bg_session.flush()
                        
                        exit_request = OrderRequest(
                            symbol=bg_shark_symbol or bg_canon_symbol,
                            side=exit_order.side,
                            quantity=bg_quantity,
                            order_type=OrderType.LIMIT.value,
                            price=tp_price,
                            reduce_only=True,
                            margin_asset=bg_margin_asset,
                            entry_exit=EntryExit.EXIT.value,
                        )
                        try:
                            await self.execution.submit(bg_session, order=exit_order, request=exit_request)
                            await bg_session.commit()
                        except Exception as e:
                            log_event(log, TradeEvent.SYSTEM_ERROR, f"Failed to place delayed TP order: {e}", position_id=bg_pos_id, level=logging.ERROR)
                            await bg_session.rollback()

            asyncio.create_task(place_delayed_tp_order())

        
        await session.commit()
        return ProcessOutcome(status="EXECUTED", position_id=position.position_id, order_id=order.order_id)
