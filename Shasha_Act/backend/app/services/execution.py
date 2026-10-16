"""Signal-to-order orchestration and guarded Shark live adapter."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain import (
    Candle,
    ValidationProblem,
    compute_strategy_levels,
    entry_side,
    exit_side,
    last_closed_candle,
    normalize_interval,
    parse_candles,
    quantity_from_notional,
    quantize_price,
    round_down_to_step,
    round_up_to_step,
    split_partial_quantity,
    split_quantity,
)
from app.market_data import MarketDataService
from app.models import AuditEvent, Order, RiskControl, SignalEvent, Trade
from app.services.outbox import enqueue_unique_job
from app.services.paper import PaperExecutionAdapter
from app.shark import SharkApiError, SharkClient, SharkOutcomeUnknown
from app.symbols import ContractSpec, ContractUnavailable


def _normalize_shark_interval(timeframe: str | None) -> str:
    """Kept for backwards compatibility; interval spellings normalize through the domain."""
    return normalize_interval(timeframe, "15m")


class EntryPriceUnavailable(RuntimeError):
    """Shark could not supply a fresh closed-candle entry price.

    Deliberately *not* a `ValidationProblem`: the durable outbox retries the signal instead of
    trading the TradingView/Binance price or rejecting the signal permanently.
    """


class TradeExecutor:
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
        self.last_entry_price_error: str = ""

    async def _resolve_entry_price(
        self,
        symbol: str,
        timeframe: str | None,
        fallback_price: Decimal,
        *,
        interval: str | None = None,
    ) -> tuple[Decimal, str, Candle | None]:
        """Resolve the SHASHA entry price from Shark Exchange data.

        TradingView prices come from Binance, so the alert price is only a trigger. The entry
        is the close of the last *completed* Shark candle (5m by default) for that symbol.
        Ordering of the kline response is never trusted: the newest fully-closed candle is
        selected from parsed timestamps. If Shark cannot supply a fresh closed candle the
        function raises `EntryPriceUnavailable` instead of silently trading a Binance price.
        """
        source_mode = str(
            getattr(self.settings, "entry_price_source", "SHARK_CLOSED_CANDLE") or ""
        ).upper()
        if source_mode == "SIGNAL_PAYLOAD":
            return fallback_price, "TradingView signal close (SIGNAL_PAYLOAD mode)", None

        candle_interval = normalize_interval(
            interval or getattr(self.settings, "entry_candle_interval", "5m")
            or timeframe,
            "5m",
        )
        max_age = int(getattr(self.settings, "entry_candle_max_age_seconds", 1800) or 0)
        try:
            response = await self.shark.klines(symbol, interval=candle_interval, limit=12)
            candles = parse_candles(response, candle_interval)
        except Exception as exc:
            self.last_entry_price_error = f"SHARK_KLINES_UNAVAILABLE:{type(exc).__name__}"
            candles = []
        if candles:
            closed = last_closed_candle(candles, max_age_seconds=max_age or None)
            if closed is not None and closed.close > 0:
                return (
                    closed.close,
                    f"Shark {candle_interval} closed-candle close",
                    closed,
                )
            self.last_entry_price_error = "SHARK_CLOSED_CANDLE_STALE_OR_UNAVAILABLE"

        if getattr(self.settings, "entry_allow_live_ticker_fallback", True):
            ticker = self.market.tickers.get(symbol)
            if (
                ticker is not None
                and ticker.last_price > 0
                and not ticker.is_stale(self.settings.market_data_stale_seconds)
            ):
                return ticker.last_price, "Shark live last price (candle feed unavailable)", None

        raise EntryPriceUnavailable(
            self.last_entry_price_error
            or "SHARK_ENTRY_PRICE_UNAVAILABLE_NO_FALLBACK_TO_TRADINGVIEW_PRICE"
        )

    async def process_signal(self, event_id: str) -> None:
        async with self.session_factory() as session:
            event = await session.get(SignalEvent, event_id)
            trade = await session.scalar(select(Trade).where(Trade.event_id == event_id))
            if event is None or trade is None:
                return
            if trade.status == "SUBMITTING":
                # A worker may have died after Shark accepted the request but before the
                # response was persisted. Never send a second non-idempotent place-order.
                await self._set_unknown(session, event, trade, "SUBMISSION_RECOVERY_REQUIRED")
                await session.commit()
                return
            if trade.status != "QUEUED":
                return
            control = await session.get(RiskControl, 1)
            if control is None:
                raise RuntimeError("risk control row is not initialized")
            if control.kill_switch or self.settings.emergency_trading_disabled:
                await self._reject(session, event, trade, "EMERGENCY_TRADING_DISABLED")
                await session.commit()
                return
            margin = control.margin_per_trade
            leverage = control.leverage
            notional = margin * Decimal(leverage)
            max_notional = control.max_notional_per_trade
            mode = trade.mode

        if notional > max_notional:
            await self._reject_by_id(event_id, "MAX_NOTIONAL_PER_TRADE_EXCEEDED")
            return

        try:
            if not self.market.resolver_contracts:
                await self.market.refresh_contracts(force=True)
            spec = self.market.resolver.resolve(trade.symbol, self.settings.shark_margin_asset)
        except ContractUnavailable as exc:
            await self._reject_by_id(event_id, str(exc))
            return
        except Exception:
            # A public metadata timeout is retried by the durable outbox; it is not
            # converted into an assumed contract or a false rejection.
            raise

        if spec.maximum_leverage is not None and leverage > spec.maximum_leverage:
            await self._reject_by_id(event_id, "LEVERAGE_EXCEEDS_CONTRACT_MAXIMUM")
            return
        if spec.quote_asset != "USDT":
            await self._reject_by_id(event_id, "CONTRACT_QUOTE_ASSET_IS_NOT_USDT")
            return
        if mode == "LIVE" and not self.settings.live_execution_rules_verified:
            await self._reject_by_id(
                event_id,
                "LIVE_DISABLED: Shark contract tick/quantity-step and split TP/SL rules are not verified",
            )
            return
        if mode == "LIVE" and not spec.live_safe:
            await self._reject_by_id(
                event_id,
                "LIVE_CONTRACT_METADATA_UNVERIFIED: exact tick, quantity step, and tradable status are not documented",
            )
            return

        try:
            tick = spec.price_tick if mode == "LIVE" else spec.paper_price_tick
            step = spec.quantity_step if mode == "LIVE" else spec.paper_quantity_step
            if tick is None or step is None:
                await self._reject_by_id(event_id, "LIVE_CONTRACT_METADATA_UNVERIFIED")
                return
            async with self.session_factory() as session:
                event = await session.get(SignalEvent, event_id)
                trade = await session.scalar(select(Trade).where(Trade.event_id == event_id))
                control = await session.get(RiskControl, 1)
                if event is None or trade is None or control is None or trade.status != "QUEUED":
                    return

                # Resolve entry price from the last completed Shark candle (5m default).
                # TradingView/Binance prices are never used as the execution price.
                resolved_entry, entry_source, entry_candle = await self._resolve_entry_price(
                    trade.symbol,
                    event.timeframe,
                    trade.planned_entry,
                    interval=control.entry_candle_interval,
                )

                # A LONG limit buy is rounded down so it never pays above the resolved candle
                # close; a SHORT limit sell is rounded up so it never sells below that close.
                # Either way the price carries the contract's exact decimal fraction.
                is_short = trade.direction == "SHORT"
                trade.planned_entry = quantize_price(
                    resolved_entry, spec.price_precision, rounding=ROUND_UP if is_short else ROUND_DOWN
                )
                trade.planned_entry = (
                    round_up_to_step(trade.planned_entry, tick)
                    if is_short
                    else round_down_to_step(trade.planned_entry, tick)
                )
                trade.entry_price_source = entry_source
                trade.entry_candle_time = entry_candle.close_time if entry_candle else None
                trade.price_tick = tick
                event.entry_price = trade.planned_entry
                trade.margin = control.margin_per_trade
                trade.leverage = control.leverage
                trade.notional = control.margin_per_trade * Decimal(control.leverage)
                quantity = quantity_from_notional(trade.notional, trade.planned_entry, step)
                if quantity < spec.minimum_quantity or quantity <= 0:
                    await self._reject(session, event, trade, "ORDER_QUANTITY_BELOW_CONTRACT_MINIMUM")
                    await session.commit()
                    return
                if quantity * trade.planned_entry < spec.minimum_notional:
                    await self._reject(session, event, trade, "ORDER_NOTIONAL_BELOW_CONTRACT_MINIMUM")
                    await session.commit()
                    return
                secondary_targets = bool(control.enable_secondary_targets)
                levels = compute_strategy_levels(
                    trade.planned_entry,
                    tick,
                    control.stop_loss_percent,
                    control.target1_percent,
                    control.target2_percent if secondary_targets else None,
                    control.target3_percent if secondary_targets else None,
                    direction=trade.direction,
                )
                target1_quantity: Decimal | None
                target2_quantity: Decimal | None
                target3_quantity: Decimal | None
                slice_levels: tuple[Decimal | None, ...]
                if secondary_targets:
                    split = split_quantity(quantity, step, spec.minimum_quantity)
                    target1_quantity = split.target1
                    target2_quantity = split.target2
                    target3_quantity = split.target3
                    slice_levels = (levels.target1, levels.target2, levels.target3)
                else:
                    partial = split_partial_quantity(
                        quantity, step, spec.minimum_quantity, control.target1_close_percent
                    )
                    target1_quantity = partial.partial
                    target2_quantity = None
                    target3_quantity = None
                    slice_levels = (levels.target1,)
                for piece, target in zip(
                    [target1_quantity, target2_quantity, target3_quantity],
                    slice_levels,
                    strict=False,
                ):
                    if piece is not None and target is not None and piece * target < spec.minimum_notional:
                        raise ValidationProblem("TARGET_SLICE_BELOW_MINIMUM_NOTIONAL")
                trade.quantity = quantity
                trade.remaining_quantity = quantity
                trade.target1_quantity = target1_quantity
                trade.target2_quantity = target2_quantity
                trade.target3_quantity = target3_quantity
                trade.stop_price = levels.stop
                trade.initial_stop_price = levels.stop
                trade.target1_price = levels.target1
                trade.target2_price = levels.target2
                trade.target3_price = levels.target3
                trade.trailing_enabled = bool(control.trailing_enabled)
                trade.secondary_targets_enabled = secondary_targets
                trade.trailing_stop_percent = control.trailing_stop_percent
                trade.partial_close_percent = control.target1_close_percent
                expiry = control.limit_entry_expiry_seconds
                trade.entry_expires_at = datetime.now(UTC) + timedelta(seconds=expiry)
                trade.status_reason = ""
                if mode == "PAPER":
                    await self.paper.create_limit_entry(session, trade)
                    trade.status = "ENTRY_PENDING"
                    trade.protection_state = "WAITING_FOR_SIMULATED_FILL"
                    event.status = "ENTRY_PENDING"
                    event.status_reason = "PAPER LIMIT entry queued; awaiting a Shark public last-price match."
                    session.add(
                        AuditEvent(
                            action="PAPER_LIMIT_QUEUED",
                            entity_type="trade",
                            entity_id=trade.id,
                            details={
                                "entry": str(trade.planned_entry),
                                "quantity": str(quantity),
                                "signal_close": str(trade.signal_close),
                                "entry_price_source": entry_source,
                            },
                        )
                    )
                    await session.commit()
                else:
                    await self._prepare_live_submission(session, event, trade, control, entry_source=entry_source)
                    await session.commit()
        except ValidationProblem as exc:
            await self._reject_by_id(event_id, str(exc))
            return

        if mode == "LIVE":
            await self._submit_live_order(event_id, spec)

    async def _prepare_live_submission(
        self,
        session: AsyncSession,
        event: SignalEvent,
        trade: Trade,
        control: RiskControl,
        *,
        entry_source: str = "Shark limit entry",
    ) -> None:
        if not self.settings.live_server_gate_open or control.live_confirmed_at is None:
            await self._reject(session, event, trade, "LIVE_REQUIRES_ENV_GATE_AND_DASHBOARD_CONFIRMATION")
            return
        if trade.mode != "LIVE":
            await self._reject(session, event, trade, "LIVE_MODE_NOT_CONFIRMED")
            return
        wallet = await self.shark.futures_wallet(self.settings.shark_margin_asset)
        data = _unwrap(wallet)
        if not isinstance(data, dict):
            await self._reject(session, event, trade, "SHARK_WALLET_RESPONSE_UNAVAILABLE")
            return
        if str(data.get("marginAsset") or "").upper() != self.settings.shark_margin_asset.upper():
            await self._reject(session, event, trade, "SHARK_WALLET_MARGIN_ASSET_MISMATCH")
            return
        available = _as_decimal(data.get("withdrawableBalance"))
        if available is None or available < trade.margin:
            await self._reject(session, event, trade, "INSUFFICIENT_OR_UNKNOWN_SHARK_AVAILABLE_MARGIN")
            return

        positions = _as_list_checked(await self.shark.positions("OPEN", trade.symbol))
        orders = _as_list_checked(await self.shark.open_orders(trade.symbol))
        if positions is None or orders is None:
            raise SharkApiError(200, "SHARK_POSITION_OR_ORDER_RESPONSE_UNAVAILABLE")
        if any(_response_symbol(item) == trade.symbol for item in positions):
            await self._reject(session, event, trade, "EXCHANGE_POSITION_ALREADY_EXISTS_FOR_SYMBOL")
            return
        if any(_response_symbol(item) == trade.symbol for item in orders):
            await self._reject(session, event, trade, "EXCHANGE_OPEN_ORDER_ALREADY_EXISTS_FOR_SYMBOL")
            return
        try:
            leverage_result = _unwrap(await self.shark.update_leverage(trade.symbol, trade.leverage))
        except SharkApiError as exc:
            if exc.exchange_code in {"2305", "2306"}:
                await self._reject(
                    session,
                    event,
                    trade,
                    "SHARK_REJECTED_LEVERAGE_CHANGE_WHILE_POSITION_OR_ORDER_EXISTS",
                )
                return
            raise
        if (
            not isinstance(leverage_result, dict)
            or leverage_result.get("updatedLeverage") is None
            or int(leverage_result["updatedLeverage"]) != trade.leverage
            or str(leverage_result.get("contractName") or "").upper() != trade.symbol
        ):
            await self._reject(session, event, trade, "SHARK_LEVERAGE_CONFIRMATION_MISSING_OR_MISMATCH")
            return

        order = Order(
            id=str(uuid4()),
            trade_id=trade.id,
            client_order_id=f"shasha-{trade.id}",
            kind="ENTRY",
            side=entry_side(trade.direction),
            order_type="LIMIT",
            status="SUBMITTING",
            quantity=trade.quantity or Decimal("0"),
            price=trade.planned_entry,
            reduce_only=False,
            request_summary={
                "type": "LIMIT",
                "side": entry_side(trade.direction),
                "symbol": trade.symbol,
                "direction": trade.direction,
            },
            submitted_at=datetime.now(UTC),
        )
        session.add(order)
        trade.entry_order_id = order.client_order_id
        trade.status = "SUBMITTING"
        trade.submission_started_at = datetime.now(UTC)
        event.status = "SUBMITTING"
        session.add(
            AuditEvent(
                action="LIVE_ORDER_SUBMISSION_ARMED",
                entity_type="trade",
                entity_id=trade.id,
                details={
                    "symbol": trade.symbol,
                    "quantity": str(trade.quantity),
                    "price": str(trade.planned_entry),
                    "entry_price_source": entry_source,
                },
            )
        )

    async def _submit_live_order(self, event_id: str, spec: ContractSpec) -> None:
        async with self.session_factory() as session:
            trade = await session.scalar(select(Trade).where(Trade.event_id == event_id))
            if trade is None or trade.status != "SUBMITTING":
                return
            symbol = trade.symbol
            quantity = trade.quantity or Decimal("0")
            price = trade.planned_entry
            side = entry_side(trade.direction)
        try:
            response = _unwrap(await self.shark.place_limit_order(symbol, quantity, price, side=side))
        except SharkOutcomeUnknown:
            async with self.session_factory() as session:
                event = await session.get(SignalEvent, event_id)
                trade = await session.scalar(select(Trade).where(Trade.event_id == event_id))
                if event is not None and trade is not None:
                    await self._set_unknown(session, event, trade, "SHARK_ORDER_SUBMISSION_OUTCOME_UNKNOWN")
                    await enqueue_unique_job(
                        session,
                        kind="RECONCILE_TRADE",
                        event_id=event_id,
                        idempotency_key=f"reconcile:{event_id}:initial-unknown",
                        priority=5,
                    )
                    await session.commit()
            return
        except SharkApiError as exc:
            async with self.session_factory() as session:
                event = await session.get(SignalEvent, event_id)
                trade = await session.scalar(select(Trade).where(Trade.event_id == event_id))
                if event is not None and trade is not None:
                    await self._reject(session, event, trade, f"SHARK_ORDER_REJECTED_{exc.exchange_code or exc.status_code}")
                    await session.commit()
            return

        if not isinstance(response, dict) or not response.get("clientOrderId"):
            async with self.session_factory() as session:
                event = await session.get(SignalEvent, event_id)
                trade = await session.scalar(select(Trade).where(Trade.event_id == event_id))
                if event is not None and trade is not None:
                    await self._set_unknown(session, event, trade, "SHARK_RESPONSE_MISSING_CLIENT_ORDER_ID")
                    await enqueue_unique_job(
                        session,
                        kind="RECONCILE_TRADE",
                        event_id=event_id,
                        idempotency_key=f"reconcile:{event_id}:missing-order-id",
                        priority=5,
                    )
                    await session.commit()
            return

        client_order_id = str(response["clientOrderId"])
        async with self.session_factory() as session:
            event = await session.get(SignalEvent, event_id)
            trade = await session.scalar(select(Trade).where(Trade.event_id == event_id))
            order = await session.scalar(select(Order).where(Order.trade_id == trade.id, Order.kind == "ENTRY")) if trade else None
            if event is None or trade is None or order is None:
                return
            order.exchange_order_id = client_order_id
            order.status = "NEW"
            trade.entry_order_id = client_order_id
            trade.status = "ENTRY_PENDING"
            trade.protection_state = "WAITING_FOR_FILL"
            event.status = "ENTRY_PENDING"
            event.status_reason = "Shark accepted a LIMIT order; position management begins after confirmed fill."
            await session.commit()

        try:
            order_state = _unwrap(await self.shark.order(client_order_id))
        except Exception:
            async with self.session_factory() as session:
                event = await session.get(SignalEvent, event_id)
                trade = await session.scalar(select(Trade).where(Trade.event_id == event_id))
                if event is not None and trade is not None:
                    await self._set_unknown(session, event, trade, "SHARK_ORDER_STATUS_UNAVAILABLE_AFTER_SUBMISSION")
                    await enqueue_unique_job(
                        session,
                        kind="RECONCILE_TRADE",
                        event_id=event_id,
                        idempotency_key=f"reconcile:{event_id}:status-unavailable",
                        priority=5,
                    )
                    await session.commit()
            return

        status = str(order_state.get("status") or "").upper() if isinstance(order_state, dict) else ""
        filled = _as_decimal(
            order_state.get("filledQty", order_state.get("filledAmount", "0"))
            if isinstance(order_state, dict) else "0"
        ) or Decimal("0")
        if status in {"FILLED", "PARTIALLY_FILLED"} or filled > 0:
            await self._handle_live_fill(event_id, spec, order_state)
        elif status in {"REJECTED", "FAILED"}:
            await self._reject_by_id(event_id, "SHARK_ORDER_REJECTED")

    async def _handle_live_fill(self, event_id: str, spec: ContractSpec, order_state: Any) -> None:
        if not isinstance(order_state, dict):
            await self._reject_by_id(event_id, "SHARK_FILL_RESPONSE_INVALID")
            return
        async with self.session_factory() as session:
            event = await session.get(SignalEvent, event_id)
            trade = await session.scalar(select(Trade).where(Trade.event_id == event_id))
            if event is None or trade is None or not trade.entry_order_id:
                return
            if trade.protection_state in {
                "ESTABLISHING", "STOP_SUBMITTED", "STOP_CONFIRMED", "TARGETS_SUBMITTING", "ACTIVE", "ALARM"
            }:
                # An earlier attempt may have reached Shark. Avoid duplicating linked
                # protection orders; reconciliation must first establish exchange state.
                return
        positions = _as_list_checked(await self.shark.positions("OPEN", trade.symbol))
        if positions is None:
            await self._protection_alarm(event_id, "SHARK_POSITION_RESPONSE_UNAVAILABLE_AFTER_FILL")
            return
        matching = [
            item for item in positions
            if isinstance(item, dict)
            and str(item.get("contractPair") or item.get("symbol") or "").upper() == trade.symbol
            and str(item.get("positionType") or trade.direction).upper() == trade.direction
        ]
        if len(matching) != 1:
            await self._protection_alarm(event_id, "SHARK_POSITION_ID_NOT_UNAMBIGUOUS_AFTER_FILL")
            return
        position = matching[0]
        position_id = str(position.get("positionId") or "")
        actual_entry = _as_decimal(position.get("entryPrice") or order_state.get("avgPrice"))
        filled_qty = _as_decimal(
            position.get("quantity") or position.get("positionAmount")
            or order_state.get("filledQty") or order_state.get("filledAmount")
        )
        if not position_id or actual_entry is None or filled_qty is None or filled_qty <= 0:
            await self._protection_alarm(event_id, "SHARK_POSITION_FILL_FIELDS_UNAVAILABLE")
            return
        if spec.price_tick is None or spec.quantity_step is None:
            await self._protection_alarm(event_id, "LIVE_CONTRACT_TICK_OR_STEP_UNAVAILABLE")
            return
        async with self.session_factory() as session:
            control = await session.get(RiskControl, 1)
            event = await session.get(SignalEvent, event_id)
            trade = await session.scalar(select(Trade).where(Trade.event_id == event_id))
            if control is None or event is None or trade is None:
                return
            try:
                secondary = bool(trade.secondary_targets_enabled or control.enable_secondary_targets)
                levels = compute_strategy_levels(
                    actual_entry,
                    spec.price_tick,
                    control.stop_loss_percent,
                    control.target1_percent,
                    control.target2_percent if secondary else None,
                    control.target3_percent if secondary else None,
                    direction=trade.direction,
                )
                t1_qty: Decimal | None
                t2_qty: Decimal | None
                t3_qty: Decimal | None
                if secondary:
                    split = split_quantity(filled_qty, spec.quantity_step, spec.minimum_quantity)
                    t1_qty = split.target1
                    t2_qty = split.target2
                    t3_qty = split.target3
                else:
                    partial = split_partial_quantity(
                        filled_qty,
                        spec.quantity_step,
                        spec.minimum_quantity,
                        control.target1_close_percent,
                    )
                    t1_qty = partial.partial
                    t2_qty = None
                    t3_qty = None
            except ValidationProblem as exc:
                await self._protection_alarm_in_session(session, event, trade, str(exc))
                await session.commit()
                return
            trade.actual_entry = actual_entry
            trade.quantity = filled_qty
            trade.remaining_quantity = filled_qty
            trade.exchange_position_id = position_id
            trade.liquidation_price = _as_decimal(position.get("liquidationPrice"))
            trade.last_price = _as_decimal(position.get("markPrice") or position.get("lastPrice"))
            trade.target1_quantity = t1_qty
            trade.target2_quantity = t2_qty
            trade.target3_quantity = t3_qty
            trade.stop_price = levels.stop
            trade.initial_stop_price = levels.stop
            trade.target1_price = levels.target1
            trade.target2_price = levels.target2
            trade.target3_price = levels.target3
            trade.trailing_enabled = bool(control.trailing_enabled)
            trade.secondary_targets_enabled = secondary
            trade.trailing_stop_percent = control.trailing_stop_percent
            trade.partial_close_percent = control.target1_close_percent
            trade.price_tick = spec.price_tick
            trade.status = "OPEN" if str(order_state.get("status")).upper() == "FILLED" else "ENTRY_PARTIAL"
            trade.opened_at = trade.opened_at or datetime.now(UTC)
            trade.protection_state = "ESTABLISHING"
            await session.commit()

        stop_id: str | None = None
        try:
            stop_response = _unwrap(
                await self.shark.split_take_profit_stop_loss(
                    position_id,
                    stop_losses=[{"quantity": filled_qty, "price": levels.stop}],
                )
            )
            stop_id = await self._accepted_order_id(stop_response)
            if not stop_id:
                raise RuntimeError("SHARK_STOP_NOT_ACCEPTED")
            async with self.session_factory() as session:
                current_trade = await session.scalar(select(Trade).where(Trade.event_id == event_id))
                if current_trade is None:
                    return
                current_trade.protective_stop_order_id = stop_id
                current_trade.protection_state = "STOP_SUBMITTED"
                session.add(
                    Order(
                        id=str(uuid4()),
                        trade_id=current_trade.id,
                        client_order_id=stop_id,
                        exchange_order_id=stop_id,
                        kind="STOP_LOSS",
                        side=exit_side(trade.direction),
                        order_type="LIMIT",
                        status="PENDING_CONFIRMATION",
                        quantity=filled_qty,
                        price=levels.stop,
                        reduce_only=True,
                        request_summary={"exchange": "shark", "protection": "STOP_LOSS", "type": "LIMIT"},
                        submitted_at=datetime.now(UTC),
                    )
                )
                await session.commit()
            confirmed_stop = _unwrap(await self.shark.order(stop_id))
            if not _order_matches(confirmed_stop, levels.stop, filled_qty, stop=True):
                raise RuntimeError("SHARK_STOP_NOT_CONFIRMED")
            async with self.session_factory() as session:
                current_trade = await session.scalar(select(Trade).where(Trade.event_id == event_id))
                stop_order = await session.scalar(select(Order).where(Order.client_order_id == stop_id))
                if current_trade is not None:
                    current_trade.protection_state = "STOP_CONFIRMED"
                if stop_order is not None:
                    stop_order.status = "NEW"
                await session.commit()
        except Exception as exc:
            await self._protection_alarm(
                event_id, str(exc)[:120] or "SHARK_STOP_SETUP_FAILED", keep_stop_id=stop_id
            )
            return

        async with self.session_factory() as session:
            current_trade = await session.scalar(select(Trade).where(Trade.event_id == event_id))
            if current_trade is None:
                return
            current_trade.protection_state = "TARGETS_SUBMITTING"
            await session.commit()
        target_plan = [
            (kind, quantity, price)
            for kind, quantity, price in (
                ("TARGET_1", t1_qty, levels.target1),
                ("TARGET_2", t2_qty, levels.target2),
                ("TARGET_3", t3_qty, levels.target3),
            )
            if quantity is not None and price is not None and quantity > 0
        ]
        if not target_plan:
            await self._protection_alarm(event_id, "NO_ACTIVE_TARGET_SLICE_DEFINED", keep_stop_id=stop_id)
            return
        try:
            tp_response = _unwrap(
                await self.shark.split_take_profit_stop_loss(
                    position_id,
                    take_profits=[
                        {"quantity": quantity, "price": price} for _, quantity, price in target_plan
                    ],
                )
            )
            tp_ids = self._collect_accepted_order_ids(tp_response)
            expected_targets = len(target_plan)
            accepted_count = int(tp_response.get("acceptedOrderCount") or 0) if isinstance(tp_response, dict) else 0
            rejected_count = int(tp_response.get("rejectedOrderCount") or 0) if isinstance(tp_response, dict) else 0
            if len(tp_ids) != expected_targets or accepted_count != expected_targets or rejected_count != 0:
                for partial_id in tp_ids:
                    try:
                        await self.shark.cancel_order(partial_id)
                        await self.shark.order(partial_id)
                    except Exception:
                        pass
                raise RuntimeError("SHARK_SPLIT_TARGETS_NOT_ALL_ACCEPTED")
            async with self.session_factory() as session:
                current_trade = await session.scalar(select(Trade).where(Trade.event_id == event_id))
                if current_trade is None:
                    return
                current_trade.target_order_ids = tp_ids
                for (kind, quantity, price), order_id in zip(target_plan, tp_ids, strict=True):
                    session.add(
                        Order(
                            id=str(uuid4()),
                            trade_id=current_trade.id,
                            client_order_id=order_id,
                            exchange_order_id=order_id,
                            kind=kind,
                            side=exit_side(trade.direction),
                            order_type="LIMIT",
                            status="PENDING_CONFIRMATION",
                            quantity=quantity,
                            price=price,
                            reduce_only=True,
                            request_summary={"exchange": "shark", "protection": kind, "type": "LIMIT"},
                            submitted_at=datetime.now(UTC),
                        )
                    )
                await session.commit()
            for order_id, (_, quantity, target_price) in zip(tp_ids, target_plan, strict=True):
                confirmed = _unwrap(await self.shark.order(order_id))
                if not _order_matches(confirmed, target_price, quantity, stop=False):
                    raise RuntimeError("SHARK_TAKE_PROFIT_NOT_CONFIRMED")
            # Submitting the profit targets must not have silently replaced or cancelled the
            # protective stop. Re-confirm it before the trade is ever reported as protected.
            stop_after_targets = _unwrap(await self.shark.order(stop_id))
            if not _order_matches(stop_after_targets, levels.stop, filled_qty, stop=True):
                raise RuntimeError("SHARK_STOP_LOST_AFTER_TARGET_SUBMISSION")
        except Exception as exc:
            await self._protection_alarm(event_id, str(exc)[:120] or "SHARK_TARGET_SETUP_FAILED", keep_stop_id=stop_id)
            return

        async with self.session_factory() as session:
            event = await session.get(SignalEvent, event_id)
            trade = await session.scalar(select(Trade).where(Trade.event_id == event_id))
            if event is None or trade is None:
                return
            trade.protective_stop_order_id = stop_id
            trade.target_order_ids = tp_ids
            trade.protection_state = "ACTIVE"
            protection_rows = [("STOP_LOSS", stop_id, filled_qty, levels.stop)]
            protection_rows.extend(
                (kind, order_id, quantity, price)
                for (kind, quantity, price), order_id in zip(target_plan, tp_ids, strict=True)
            )
            for kind, client_id, quantity, price in protection_rows:
                existing_order = await session.scalar(
                    select(Order).where(Order.client_order_id == client_id).limit(1)
                )
                if existing_order is None:
                    session.add(
                        Order(
                            id=str(uuid4()),
                            trade_id=trade.id,
                            client_order_id=client_id,
                            exchange_order_id=client_id,
                            kind=kind,
                            side=exit_side(trade.direction),
                            order_type="LIMIT",
                            status="NEW",
                            quantity=quantity,
                            price=price,
                            reduce_only=True,
                            request_summary={"exchange": "shark", "protection": kind, "type": "LIMIT"},
                            submitted_at=datetime.now(UTC),
                        )
                    )
                else:
                    existing_order.status = "NEW"
                    existing_order.quantity = quantity
                    existing_order.price = price
            event.status = trade.status
            event.status_reason = "Shark confirmed the entry and exchange-side protective orders."
            session.add(
                AuditEvent(
                    action="SHARK_PROTECTION_CONFIRMED",
                    entity_type="trade",
                    entity_id=trade.id,
                    details={"position_id": position_id, "stop_order_id": stop_id, "target_order_ids": tp_ids},
                )
            )
            if trade.planned_entry > 0 and abs(actual_entry - trade.planned_entry) / trade.planned_entry >= Decimal("0.001"):
                await enqueue_unique_job(
                    session,
                    kind="TELEGRAM_SIGNAL",
                    event_id=event_id,
                    idempotency_key=f"notify:{event_id}:MATERIAL_FILL_DIFFERENCE",
                    priority=20,
                )
            await session.commit()

    async def _accepted_order_id(self, response: Any) -> str | None:
        ids = await self._accepted_order_ids(response, expected=1)
        return ids[0] if len(ids) == 1 else None

    async def _accepted_order_ids(self, response: Any, expected: int) -> list[str]:
        if not isinstance(response, dict):
            return []
        rejected_count = int(response.get("rejectedOrderCount") or 0)
        accepted_count = int(response.get("acceptedOrderCount") or 0)
        ids = self._collect_accepted_order_ids(response)
        if rejected_count > 0 or accepted_count != expected:
            return []
        return ids if len(ids) == expected else []

    @staticmethod
    def _collect_accepted_order_ids(response: Any) -> list[str]:
        if not isinstance(response, dict) or not isinstance(response.get("acceptedOrders"), list):
            return []
        return [
            str(item.get("clientOrderId"))
            for item in response["acceptedOrders"]
            if isinstance(item, dict) and item.get("clientOrderId")
        ]

    async def close_trade(self, trade_id: str) -> None:
        async with self.session_factory() as session:
            trade = await session.get(Trade, trade_id)
            if trade is None:
                raise ValueError("TRADE_NOT_FOUND")
            mode = trade.mode
            symbol = trade.symbol
            status = trade.status
        if status not in {"OPEN", "PARTIALLY_CLOSED"}:
            raise ValueError("TRADE_NOT_OPEN")
        if mode == "PAPER":
            ticker = await self.market.get_ticker(symbol, refresh=True)
            if ticker is None or ticker.is_stale(self.settings.market_data_stale_seconds):
                raise ValueError("FRESH_SHARK_LAST_PRICE_UNAVAILABLE")
            async with self.session_factory() as session:
                trade = await session.scalar(select(Trade).where(Trade.id == trade_id).with_for_update())
                if trade is None or trade.status not in {"OPEN", "PARTIALLY_CLOSED"}:
                    raise ValueError("TRADE_NOT_OPEN")
                event = await session.get(SignalEvent, trade.event_id)
                await self.paper.close_manually(session, trade, ticker, event)
                await session.commit()
            return

        if not self.settings.live_server_gate_open:
            raise ValueError("LIVE_SERVER_GATE_CLOSED")
        if not self.settings.live_execution_rules_verified:
            raise ValueError("LIVE_CLOSE_DISABLED_EXCHANGE_RULES_UNVERIFIED")
        async with self.session_factory() as session:
            trade = await session.scalar(select(Trade).where(Trade.id == trade_id).with_for_update())
            if (
                trade is None
                or trade.status not in {"OPEN", "PARTIALLY_CLOSED"}
                or trade.protection_state != "ACTIVE"
                or not trade.exchange_position_id
            ):
                raise ValueError("LIVE_POSITION_NOT_CONFIRMED_OR_PROTECTED")
            quantity = trade.remaining_quantity or Decimal("0")
            if quantity <= 0:
                raise ValueError("NO_REMAINING_POSITION_QUANTITY")
            close_side = exit_side(trade.direction)
            close_order = Order(
                id=str(uuid4()),
                trade_id=trade.id,
                client_order_id=f"local-close-{uuid4().hex}",
                kind="MANUAL_CLOSE",
                side=exit_side(trade.direction),
                order_type="MARKET",
                status="SUBMITTING",
                quantity=quantity,
                reduce_only=True,
                request_summary={
                    "type": "MARKET",
                    "side": exit_side(trade.direction),
                    "symbol": trade.symbol,
                },
                submitted_at=datetime.now(UTC),
            )
            session.add(close_order)
            trade.status = "CLOSING"
            trade.status_reason = "Operator-confirmed reduce-only close submitted; awaiting Shark confirmation."
            event = await session.get(SignalEvent, trade.event_id)
            if event is not None:
                event.status = "CLOSING"
                event.status_reason = trade.status_reason
            session.add(
                AuditEvent(
                    actor="dashboard",
                    action="LIVE_MANUAL_CLOSE_SUBMITTED",
                    entity_type="trade",
                    entity_id=trade.id,
                    details={"quantity": str(quantity)},
                )
            )
            await session.commit()
        try:
            response = _unwrap(
                await self.shark.place_reduce_only_market(symbol, quantity, side=close_side)
            )
        except SharkOutcomeUnknown:
            async with self.session_factory() as session:
                trade = await session.get(Trade, trade_id)
                if trade is not None:
                    trade.status = "CLOSING"
                    trade.status_reason = "Manual close outcome unknown; reconciliation required, no retry sent."
                    event = await session.get(SignalEvent, trade.event_id)
                    if event is not None:
                        event.status = "CLOSING"
                        event.status_reason = trade.status_reason
                    await session.commit()
            return
        except SharkApiError as exc:
            async with self.session_factory() as session:
                trade = await session.get(Trade, trade_id)
                if trade is not None:
                    trade.status = "OPEN"
                    trade.status_reason = f"Shark rejected manual close ({exc.exchange_code or exc.status_code})."
                    event = await session.get(SignalEvent, trade.event_id)
                    if event is not None:
                        event.status = "OPEN"
                        event.status_reason = trade.status_reason
                    order = await session.scalar(
                        select(Order).where(Order.trade_id == trade_id, Order.kind == "MANUAL_CLOSE")
                    )
                    if order is not None:
                        order.status = "REJECTED"
                        order.error_code = exc.exchange_code
                        order.error_message = "Shark rejected the reduce-only close request."
                    await session.commit()
            return
        if not isinstance(response, dict) or not response.get("clientOrderId"):
            async with self.session_factory() as session:
                trade = await session.get(Trade, trade_id)
                if trade is not None:
                    trade.status = "CLOSING"
                    trade.status_reason = "Manual close response missing order ID; reconciliation required, no retry sent."
                    event = await session.get(SignalEvent, trade.event_id)
                    if event is not None:
                        event.status = "CLOSING"
                        event.status_reason = trade.status_reason
                    await session.commit()
            return
        exchange_id = str(response["clientOrderId"])
        async with self.session_factory() as session:
            trade = await session.get(Trade, trade_id)
            order = await session.scalar(
                select(Order).where(Order.trade_id == trade_id, Order.kind == "MANUAL_CLOSE")
            )
            if order is not None:
                order.exchange_order_id = exchange_id
                order.client_order_id = exchange_id
                order.status = "NEW"
            if trade is not None:
                trade.status = "CLOSING"
                trade.status_reason = "Shark accepted manual reduce-only close; awaiting fill confirmation."
                event = await session.get(SignalEvent, trade.event_id)
                if event is not None:
                    event.status = "CLOSING"
                    event.status_reason = trade.status_reason
                await enqueue_unique_job(
                    session,
                    kind="RECONCILE_TRADE",
                    event_id=trade.event_id,
                    idempotency_key=f"reconcile:{trade.event_id}:manual-close:{exchange_id}",
                    priority=2,
                )
                await session.commit()

    async def _reject_by_id(self, event_id: str, reason: str) -> None:
        async with self.session_factory() as session:
            event = await session.get(SignalEvent, event_id)
            trade = await session.scalar(select(Trade).where(Trade.event_id == event_id))
            if event is not None and trade is not None:
                await self._reject(session, event, trade, reason)
                await session.commit()

    async def _reject(self, session: AsyncSession, event: SignalEvent, trade: Trade, reason: str) -> None:
        trade.status = "REJECTED"
        trade.status_reason = reason[:500]
        trade.protection_state = "NOT_APPLICABLE"
        trade.closed_at = datetime.now(UTC)
        event.status = "REJECTED"
        event.status_reason = reason[:500]
        session.add(
            AuditEvent(
                action="TRADE_REJECTED",
                entity_type="trade",
                entity_id=trade.id,
                details={"reason": reason[:250]},
            )
        )

    async def _set_unknown(self, session: AsyncSession, event: SignalEvent, trade: Trade, reason: str) -> None:
        trade.status = "UNKNOWN"
        trade.status_reason = reason[:500]
        trade.protection_state = "UNKNOWN"
        event.status = "UNKNOWN"
        event.status_reason = reason[:500]
        session.add(
            AuditEvent(
                action="TRADE_REQUIRES_RECONCILIATION",
                entity_type="trade",
                entity_id=trade.id,
                details={"reason": reason[:250]},
            )
        )

    async def _protection_alarm(self, event_id: str, reason: str, keep_stop_id: str | None = None) -> None:
        async with self.session_factory() as session:
            event = await session.get(SignalEvent, event_id)
            trade = await session.scalar(select(Trade).where(Trade.event_id == event_id))
            if event is not None and trade is not None:
                await self._protection_alarm_in_session(session, event, trade, reason, keep_stop_id)
                await session.commit()

    async def _protection_alarm_in_session(
        self,
        session: AsyncSession,
        event: SignalEvent,
        trade: Trade,
        reason: str,
        keep_stop_id: str | None = None,
    ) -> None:
        trade.status = "PROTECTION_ERROR"
        trade.status_reason = reason[:500]
        trade.protection_state = "ALARM"
        trade.protective_stop_order_id = keep_stop_id or trade.protective_stop_order_id
        event.status = "PROTECTION_ERROR"
        event.status_reason = reason[:500]
        session.add(
            AuditEvent(
                action="URGENT_PROTECTION_ALARM",
                entity_type="trade",
                entity_id=trade.id,
                details={"reason": reason[:250], "stop_order_id": keep_stop_id},
            )
        )
        await enqueue_unique_job(
            session,
            kind="TELEGRAM_SIGNAL",
            event_id=event.id,
            idempotency_key=f"notify:{event.id}:PROTECTION_ERROR:{trade.target_progress}",
            priority=1,
        )
        await enqueue_unique_job(
            session,
            kind="RECONCILE_TRADE",
            event_id=event.id,
            idempotency_key=f"reconcile:{event.id}:protection:{trade.target_progress}",
            priority=2,
        )

    async def cancel_pending_entries(self, trade_ids: list[str] | None = None) -> int:
        async with self.session_factory() as session:
            query = select(Trade).where(Trade.status.in_(("ENTRY_PENDING", "ENTRY_PARTIAL")))
            if trade_ids:
                query = query.where(Trade.id.in_(trade_ids))
            trades = list((await session.scalars(query)).all())
        cancelled = 0
        for snapshot in trades:
            if snapshot.mode == "PAPER":
                async with self.session_factory() as session:
                    trade = await session.get(Trade, snapshot.id)
                    event = await session.get(SignalEvent, snapshot.event_id)
                    if trade is None or trade.status not in {"ENTRY_PENDING", "ENTRY_PARTIAL"}:
                        continue
                    entry_order = await session.scalar(
                        select(Order).where(Order.trade_id == trade.id, Order.kind == "ENTRY").limit(1)
                    )
                    if entry_order is not None and entry_order.status == "NEW":
                        entry_order.status = "CANCELED"
                    trade.status = "CANCELLED"
                    trade.status_reason = "Pending paper entry cancelled by operator."
                    trade.closed_at = datetime.now(UTC)
                    if event is not None:
                        event.status = "CANCELLED"
                        event.status_reason = trade.status_reason
                    await session.commit()
                    cancelled += 1
            elif snapshot.entry_order_id:
                try:
                    await self.shark.cancel_order(snapshot.entry_order_id)
                    state = _unwrap(await self.shark.order(snapshot.entry_order_id))
                    if isinstance(state, dict) and str(state.get("status") or "").upper() in {"CANCELED", "CANCELLED"}:
                        async with self.session_factory() as session:
                            trade = await session.get(Trade, snapshot.id)
                            event = await session.get(SignalEvent, snapshot.event_id)
                            entry_order = await session.scalar(
                                select(Order).where(Order.trade_id == snapshot.id, Order.kind == "ENTRY")
                            )
                            if trade is not None and trade.status in {"ENTRY_PENDING", "ENTRY_PARTIAL"}:
                                if entry_order is not None:
                                    entry_order.status = "CANCELED"
                                if trade.status == "ENTRY_PARTIAL" and trade.protection_state == "ACTIVE":
                                    trade.status = "OPEN"
                                    trade.status_reason = "Shark confirmed the unfilled remainder was canceled; protected filled quantity remains open."
                                    if event is not None:
                                        event.status = "OPEN"
                                        event.status_reason = trade.status_reason
                                else:
                                    trade.status = "CANCELLED"
                                    trade.status_reason = "Shark confirmed pending entry cancellation."
                                    trade.closed_at = datetime.now(UTC)
                                    if event is not None:
                                        event.status = "CANCELLED"
                                        event.status_reason = trade.status_reason
                                await session.commit()
                                cancelled += 1
                except Exception:
                    continue
        return cancelled


def _unwrap(response: Any) -> Any:
    if isinstance(response, dict) and "data" in response:
        return response["data"]
    return response


def _as_list_checked(response: Any) -> list[dict[str, Any]] | None:
    data = _unwrap(response)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return [item for item in data["data"] if isinstance(item, dict)]
    return None


def _as_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    return parsed if parsed.is_finite() else None


def _response_symbol(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("contractPair") or item.get("symbol") or "").upper()


def _order_matches(response: Any, price: Decimal, quantity: Decimal, *, stop: bool) -> bool:
    if not isinstance(response, dict):
        return False
    status = str(response.get("status") or "").upper()
    if status not in {"NEW", "OPEN", "PENDING", "SUBMITTED"}:
        return False
    price_value = response.get("stopPrice") if stop else None
    if price_value is None:
        price_value = response.get("price")
    actual_price = _as_decimal(price_value)
    actual_quantity = _as_decimal(
        response.get("quantity") or response.get("orderAmount") or response.get("origQty")
    )
    return actual_price == price and actual_quantity == quantity
