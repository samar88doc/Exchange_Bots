"""Signal -> decision -> (PAPER simulation | guarded REAL order) pipeline.

Every message is first persisted (deduplicated on channel_id + message_id),
then parsed, then checked (stale, kill switch, duplicates, concurrency, exact
contract, price precision, deviation, leverage, sizing, wallet balance). Only a
signal that passes every check reaches an execution path. Rejections are
recorded with their precise reason and are never queued for later.

Pi42 specifics:
* The signal's quote must match ``PI42_MARKET`` exactly (no currency conversion).
* Leverage **and** margin mode are configured for the contract before the entry
  order (Pi42 refuses both while an order/position is open: errors 2305/2306,
  2312/2313), and the response must confirm the requested values.
* The entry is a LIMIT order carrying the ROI take-profit price
  (``takeProfitPrice``). A stop-loss is never sent.
* Every order carries a deterministic ``refId`` so a lost response can be
  reconciled by ``GET /v1/order/ref-id`` instead of resubmitting
  (Pi42 answers error 3019 "Ref id already exists" for a duplicate).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal

from app.config import Settings
from app.leverage_manager import LeverageDecision, LeverageError, select_leverage
from app.logging_config import log_event
from app.models import (
    ContractInfo,
    Direction,
    Mode,
    OrderInfo,
    ParsedSignal,
    SignalStatus,
    TradeStatus,
    utc_now,
)
from app.notifications import NullNotifier
from app.numeric import format_decimal
from app.paper_broker import PaperBroker
from app.pi42_client import AmbiguousMutationError, Pi42Error, is_duplicate_ref_error
from app.position_monitor import reconcile_uncertain_trade
from app.risk_manager import (
    RiskRejection,
    SizingResult,
    check_concurrency,
    check_entry_deviation,
    check_funds,
    check_target_not_instant,
    compute_budget,
    size_position,
    snap_to_price_grid,
)
from app.signal_parser import SignalParseError, parse_signal
from app.storage import Storage
from app.symbol_mapper import SymbolMapper, UnsupportedSymbolError

logger = logging.getLogger("engine")

DUPLICATE_CONTENT_WINDOW = timedelta(hours=24)
MAX_FUTURE_SKEW = timedelta(minutes=1)


class SkipSignal(Exception):
    """A business rule rejected the signal. The reason is stored verbatim."""


class LeverageNotConfirmed(Exception):
    """The Pi42 leverage/margin-mode change was not confirmed by the response."""


@dataclass
class PreparedOrder:
    signal: ParsedSignal
    contract: ContractInfo
    leverage: LeverageDecision
    sizing: SizingResult
    market_price: Decimal
    deviation_pct: Decimal
    available_balance: Decimal
    ref_id: str
    target_price: Decimal


def make_ref_id(channel_id: int, message_id: int, symbol: str) -> str:
    """Deterministic reference id: letters, digits and hyphens only."""
    digest = hashlib.sha256(f"{channel_id}:{message_id}:{symbol}".encode()).hexdigest()[:20]
    return f"TG-{message_id}-{digest}"


def make_tp_ref_id(trade_id: int, tag: str) -> str:
    digest = hashlib.sha256(f"{tag}:{trade_id}".encode()).hexdigest()[:16]
    return f"TP-{trade_id}-{tag}-{digest}"


class ExecutionEngine:
    def __init__(
        self,
        *,
        settings: Settings,
        storage: Storage,
        client,
        mapper: SymbolMapper,
        mode: Mode,
        paper_broker: PaperBroker | None = None,
        notifier=None,
        now=utc_now,
    ) -> None:
        if mode is Mode.PAPER and paper_broker is None:
            raise ValueError("PAPER mode requires a PaperBroker")
        self._settings = settings
        self._storage = storage
        self._client = client
        self._mapper = mapper
        self._mode = mode
        self._paper = paper_broker
        self._notifier = notifier or NullNotifier()
        self._now = now
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ entry point
    async def handle_message(self, *, channel_id: int, message_id: int, text: str,
                             signal_time: datetime) -> str:
        async with self._lock:
            return await self._handle(channel_id=channel_id, message_id=message_id, text=text,
                                      signal_time=signal_time)

    async def _handle(self, *, channel_id: int, message_id: int, text: str, signal_time: datetime) -> str:
        now = self._now()
        sig_id = self._storage.insert_signal(
            channel_id=channel_id,
            message_id=message_id,
            received_at=now,
            signal_time=signal_time,
            raw_text=text or "",
            status=SignalStatus.RECEIVED.value,
        )
        if sig_id is None:
            log_event(logger, "signal.duplicate_message", channel_id=channel_id, message_id=message_id)
            return SignalStatus.DUPLICATE.value

        try:
            signal = parse_signal(text or "", channel_id=channel_id, message_id=message_id,
                                  signal_time=signal_time)
        except SignalParseError as exc:
            status = SignalStatus.IGNORED if exc.ignorable else SignalStatus.REJECTED
            self._finish_signal(sig_id, status, exc.reason, now)
            log_event(logger, "signal.ignored" if exc.ignorable else "signal.rejected",
                      level=logging.DEBUG if exc.ignorable else logging.WARNING,
                      message_id=message_id, reason=exc.reason)
            return status.value

        self._storage.update_signal(
            sig_id, now,
            symbol=signal.symbol,
            direction=signal.direction.value,
            entry=format_decimal(signal.entry),
            stop_loss=format_decimal(signal.stop_loss) if signal.stop_loss else None,
            target1=format_decimal(signal.target1),
            target2=format_decimal(signal.target2) if signal.target2 else None,
            target3=format_decimal(signal.target3) if signal.target3 else None,
            target4=format_decimal(signal.target4) if signal.target4 else None,
            target5=format_decimal(signal.target5) if signal.target5 else None,
            telegram_leverage=format_decimal(signal.telegram_leverage),
        )
        log_event(
            logger, "signal.parsed", message_id=message_id, symbol=signal.symbol,
            direction=signal.direction.value, entry=signal.entry, target1=signal.target1,
            telegram_leverage=signal.telegram_leverage, stop_loss_reference=signal.stop_loss,
        )

        try:
            await self._evaluate_and_execute(sig_id, signal, now)
        except SkipSignal as exc:
            self._finish_signal(sig_id, SignalStatus.REJECTED, str(exc), self._now())
            log_event(logger, "signal.rejected", level=logging.WARNING, symbol=signal.symbol,
                      message_id=message_id, reason=str(exc))
            return SignalStatus.REJECTED.value
        except Exception as exc:  # noqa: BLE001 - recorded, never silently dropped
            logger.exception("event=signal.failed message_id=%s", message_id)
            self._finish_signal(sig_id, SignalStatus.FAILED, f"internal error: {type(exc).__name__}", self._now())
            return SignalStatus.FAILED.value
        return self._storage.get_signal(sig_id)["status"]  # type: ignore[index]

    # --------------------------------------------------------- checks + flow
    async def _evaluate_and_execute(self, sig_id: int, signal: ParsedSignal, now: datetime) -> None:
        age = now - signal.signal_time
        if age > timedelta(minutes=self._settings.max_signal_age_minutes):
            raise SkipSignal(
                f"stale signal: {int(age.total_seconds())}s old, MAX_SIGNAL_AGE_MINUTES="
                f"{self._settings.max_signal_age_minutes}"
            )
        if age < -MAX_FUTURE_SKEW:
            raise SkipSignal("signal timestamp is in the future")
        if not self._trading_enabled():
            raise SkipSignal("new trades disabled (TRADING_ENABLED=false or KILL_SWITCH file present)")
        if self._storage.active_trade_for_symbol(signal.symbol, self._mode.value) is not None:
            raise SkipSignal(f"an active trade already exists for {signal.symbol}")
        if self._storage.accepted_duplicate_exists(
            signal.symbol, signal.direction.value, format_decimal(signal.entry), now - DUPLICATE_CONTENT_WINDOW
        ):
            raise SkipSignal("duplicate signal content already accepted within 24h")

        try:
            prepared = await self.prepare(signal)
        except UnsupportedSymbolError as exc:
            raise SkipSignal(str(exc)) from exc
        except LeverageError as exc:
            raise SkipSignal(f"leverage rejected: {exc}") from exc
        except RiskRejection as exc:
            raise SkipSignal(f"risk check failed: {exc}") from exc
        except Pi42Error as exc:
            raise SkipSignal(f"Pi42 metadata/balance unavailable, signal skipped: {exc}") from exc

        log_event(
            logger, "signal.validated", symbol=prepared.signal.symbol,
            contract=prepared.contract.symbol, market=prepared.contract.market,
            leverage=prepared.leverage.selected, leverage_detail=prepared.leverage.describe(),
            quantity=prepared.sizing.quantity, notional=prepared.sizing.notional,
            est_margin=prepared.sizing.estimated_margin, market_price=prepared.market_price,
            deviation_pct=f"{prepared.deviation_pct:.4f}", target=prepared.target_price,
            ref_id=prepared.ref_id, mode=self._mode.value,
        )
        if self._mode is Mode.PAPER:
            self._open_paper(sig_id, prepared, now)
        else:
            await self._open_real(sig_id, prepared, now)

    async def prepare(self, signal: ParsedSignal) -> PreparedOrder:
        """All read-only validation. Raises SkipSignal/RiskRejection/LeverageError/Pi42Error."""
        s = self._settings
        contract = await self._mapper.resolve(signal.symbol, quote=signal.quote_asset)
        if contract.quote_asset and signal.quote_asset and contract.quote_asset != signal.quote_asset:
            raise SkipSignal(
                f"quote mismatch: signal {signal.quote_asset} vs contract {contract.quote_asset}"
            )
        if not contract.supports_order_type("LIMIT"):
            raise SkipSignal(f"contract {contract.symbol} does not support LIMIT orders")

        try:
            market_price = await self._client.get_ticker_price(contract.symbol)
        except Pi42Error as exc:
            raise SkipSignal(f"live price unavailable for {contract.symbol}: {exc}") from exc

        entry = snap_to_price_grid("Entry", signal.entry, contract)
        target1 = snap_to_price_grid("Target 1", signal.target1, contract)
        target2 = snap_to_price_grid("Target 2", signal.target2, contract) if signal.target2 else None
        target3 = snap_to_price_grid("Target 3", signal.target3, contract) if signal.target3 else None
        target4 = snap_to_price_grid("Target 4", signal.target4, contract) if signal.target4 else None
        target5 = snap_to_price_grid("Target 5", signal.target5, contract) if signal.target5 else None

        if signal.direction == Direction.LONG and target1 <= entry:
            raise RiskRejection(f"Target 1 ({target1}) must be strictly greater than Entry ({entry})")
        if signal.direction == Direction.SHORT and target1 >= entry:
            raise RiskRejection(f"Target 1 ({target1}) must be strictly less than Entry ({entry})")

        if entry != signal.entry or target1 != signal.target1:
            logger.info("event=price.snapped symbol=%s entry=%s->%s target1=%s->%s",
                        signal.symbol, signal.entry, entry, signal.target1, target1)
        signal = replace(signal, entry=entry, target1=target1, target2=target2, target3=target3,
                         target4=target4, target5=target5)

        deviation = check_entry_deviation(signal.entry, market_price, s.max_entry_deviation_pct)

        leverage = select_leverage(signal.telegram_leverage, contract, margin_mode=s.margin_mode)
        budget = compute_budget(s.max_margin_per_trade, s.margin_reserve)
        sizing = size_position(entry=signal.entry, leverage=leverage.selected, budget=budget,
                               contract=contract)

        # The ROI take-profit must not be triggered instantly (Pi42 error 3011).
        target_price = self._roi_target(signal, contract)
        check_target_not_instant(direction=signal.direction.value, target=target_price,
                                 market_price=market_price, entry=signal.entry)

        if self._mode is Mode.PAPER:
            assert self._paper is not None
            available = self._paper.available_balance()
            concurrent = len(self._storage.active_trades(Mode.PAPER.value))
        else:
            wallet = await self._client.get_futures_wallet(s.margin_asset)
            available = wallet.free
            open_positions = await self._client.list_open_positions(contract.symbol)
            if any(p.symbol == contract.symbol for p in open_positions):
                raise SkipSignal(f"an open position already exists on Pi42 for {contract.symbol}")
            open_orders = await self._client.list_open_orders(contract.symbol)
            if any(o.symbol == contract.symbol for o in open_orders):
                raise SkipSignal(f"an open order already exists on Pi42 for {contract.symbol}")
            concurrent = self._storage.pending_like_count(Mode.REAL.value)

        check_concurrency(concurrent, s.max_concurrent_positions)
        check_funds(available, sizing.estimated_margin, s.margin_reserve)

        return PreparedOrder(
            signal=signal,
            contract=contract,
            leverage=leverage,
            sizing=sizing,
            market_price=market_price,
            deviation_pct=deviation,
            available_balance=available,
            ref_id=make_ref_id(signal.channel_id, signal.message_id, contract.symbol),
            target_price=target_price,
        )

    # ----------------------------------------------------------- persistence
    def _roi_target(self, signal: ParsedSignal, contract: ContractInfo) -> Decimal:
        """Take-profit price: entry +/- TARGET_ROI_PCT, snapped to the price step."""
        roi_ratio = self._settings.target_roi_pct / Decimal("100")
        if signal.direction == Direction.LONG:
            raw_target = signal.entry * (Decimal("1") + roi_ratio)
        else:
            raw_target = signal.entry * (Decimal("1") - roi_ratio)
        return snap_to_price_grid("ROI target", raw_target, contract)

    def _trade_fields(self, sig_id: int, prepared: PreparedOrder, status: TradeStatus, reason: str) -> dict:
        signal = prepared.signal
        return dict(
            signal_id=sig_id,
            mode=self._mode.value,
            ref_id=prepared.ref_id,
            client_order_id=None,
            order_id=None,
            symbol=prepared.contract.symbol,
            market=prepared.contract.market,
            margin_asset=self._settings.margin_asset,
            direction=signal.direction.value,
            entry_price=signal.entry,
            target1=prepared.target_price,
            target2=None,
            target3=None,
            target4=None,
            target5=None,
            # The signal's stop-loss is recorded for reference only: this bot
            # never sends one to Pi42 (there is no stop-loss parameter anywhere
            # in the client API, so it cannot be sent by accident).
            stop_loss_reference=signal.stop_loss,
            telegram_leverage=signal.telegram_leverage,
            leverage=prepared.leverage.selected,
            contract_max_leverage=prepared.leverage.contract_max_leverage,
            quantity=prepared.sizing.quantity,
            notional=prepared.sizing.notional,
            est_margin=prepared.sizing.estimated_margin,
            budget=prepared.sizing.budget,
            status=status.value,
            status_reason=reason,
            tp_confirmed=False,
        )

    def _finish_signal(self, sig_id: int, status: SignalStatus, reason: str, now: datetime) -> None:
        self._storage.update_signal(sig_id, now, status=status.value, decision_reason=reason)
        self._storage.add_event(now, f"signal.{status.value.lower()}", signal_id=sig_id,
                                level="WARNING" if status is SignalStatus.REJECTED else "INFO",
                                detail={"reason": reason})

    def _open_paper(self, sig_id: int, prepared: PreparedOrder, now: datetime) -> None:
        reason = "simulated LIMIT entry pending (PAPER mode, no live order)"
        with self._storage.transaction():
            trade_id = self._storage.create_trade(
                now, **self._trade_fields(sig_id, prepared, TradeStatus.ENTRY_PENDING, reason)
            )
            self._storage.update_signal(sig_id, now, status=SignalStatus.ACCEPTED.value,
                                        decision_reason=reason, pi42_symbol=prepared.contract.symbol,
                                        market=prepared.contract.market)
            self._storage.add_event(now, "paper.entry_pending", trade_id=trade_id, signal_id=sig_id,
                                    detail={"limit": prepared.signal.entry,
                                            "quantity": prepared.sizing.quantity})
        log_event(logger, "paper.entry_pending", trade_id=trade_id, symbol=prepared.contract.symbol,
                  side=prepared.signal.direction.value, limit=prepared.signal.entry,
                  quantity=prepared.sizing.quantity, est_margin=prepared.sizing.estimated_margin)

    async def _open_real(self, sig_id: int, prepared: PreparedOrder, now: datetime) -> None:
        signal = prepared.signal
        contract = prepared.contract
        settings = self._settings
        trade_fields = self._trade_fields(sig_id, prepared, TradeStatus.SUBMITTING,
                                          "persisted before Pi42 submission")
        trade_id = self._storage.create_trade(now, **trade_fields)
        self._storage.update_signal(sig_id, now, status=SignalStatus.ACCEPTED.value,
                                    decision_reason="validated; submitting to Pi42",
                                    pi42_symbol=contract.symbol, market=contract.market)

        try:
            await self._ensure_leverage(contract, prepared.leverage.selected, settings.margin_mode)
        except (Pi42Error, LeverageNotConfirmed) as exc:
            self._reject_trade(trade_id, sig_id, now, f"leverage not confirmed; no order placed: {exc}")
            return

        try:
            accepted = await self._client.place_limit_order(
                symbol=contract.symbol,
                side="BUY" if signal.direction == Direction.LONG else "SELL",
                quantity=prepared.sizing.quantity,
                price=signal.entry,
                take_profit_price=prepared.target_price,
                margin_asset=settings.margin_asset,
                ref_id=prepared.ref_id if settings.send_ref_id else None,
                device_type=settings.device_type,
                user_category=settings.user_category,
            )
        except AmbiguousMutationError as exc:
            self._storage.update_trade(trade_id, now, status=TradeStatus.SUBMIT_UNCERTAIN.value,
                                       status_reason=f"outcome unknown: {exc}")
            self._storage.add_event(now, "real.submit_uncertain", level="WARNING", trade_id=trade_id,
                                    signal_id=sig_id)
            log_event(logger, "real.submit_uncertain", level=logging.WARNING, trade_id=trade_id,
                      ref_id=prepared.ref_id)
            trade = self._storage.get_trade(trade_id)
            assert trade is not None
            await reconcile_uncertain_trade(storage=self._storage, client=self._client, trade=trade, now=now)
            return
        except Pi42Error as exc:
            if is_duplicate_ref_error(exc):
                # The order already exists (an earlier attempt lost its response):
                # adopt it rather than sending a second one.
                log_event(logger, "real.ref_id_already_exists", level=logging.WARNING, trade_id=trade_id,
                          ref_id=prepared.ref_id)
                trade = self._storage.get_trade(trade_id)
                assert trade is not None
                await reconcile_uncertain_trade(storage=self._storage, client=self._client, trade=trade, now=now)
                return
            self._reject_trade(trade_id, sig_id, now, f"Pi42 rejected the order: {exc}")
            return

        reason = f"accepted by Pi42 (status {accepted.status or 'NEW'}); awaiting fill"
        self._storage.update_trade(
            trade_id, now, status=TradeStatus.ENTRY_PENDING.value,
            client_order_id=accepted.client_order_id, order_id=accepted.order_id,
            status_reason=reason,
        )
        self._storage.update_signal(sig_id, now, decision_reason=reason)
        self._storage.add_event(
            now, "real.order_accepted", trade_id=trade_id, signal_id=sig_id,
            detail={"client_order_id": accepted.client_order_id, "order_id": accepted.order_id,
                    "ref_id": accepted.ref_id or prepared.ref_id, "status": accepted.status},
        )
        log_event(logger, "real.order_accepted", trade_id=trade_id, symbol=contract.symbol,
                  side=signal.direction.value, limit=signal.entry, target=prepared.target_price,
                  quantity=prepared.sizing.quantity, leverage=prepared.leverage.selected,
                  client_order_id=accepted.client_order_id)
        await self._notifier.send(
            f"[REAL] {contract.symbol} {signal.direction.value} LIMIT {signal.entry} qty "
            f"{prepared.sizing.quantity} x{prepared.leverage.selected} "
            f"ROI target {prepared.target_price} ({settings.target_roi_pct}%). "
            "No stop-loss. Emergency SL: " +
            (str(signal.stop_loss) if signal.stop_loss else "none from signal") + "."
        )

    def _reject_trade(self, trade_id: int, sig_id: int, now: datetime, reason: str) -> None:
        self._storage.update_trade(trade_id, now, status=TradeStatus.REJECTED.value, status_reason=reason)
        self._storage.update_signal(sig_id, now, status=SignalStatus.REJECTED.value, decision_reason=reason)
        self._storage.add_event(now, "real.order_rejected", level="WARNING", trade_id=trade_id,
                                signal_id=sig_id, detail={"reason": reason})
        log_event(logger, "real.order_rejected", level=logging.WARNING, trade_id=trade_id, reason=reason)

    async def _ensure_leverage(self, contract: ContractInfo, desired: Decimal, margin_mode: str) -> None:
        """Configure leverage + margin mode and require the response to confirm it.

        Pi42 exposes no leverage read endpoint: ``POST /v1/exchange/update/preference``
        is the only way to know, and it refuses to change anything while an order
        or position is open on the contract (errors 2305/2306, 2312/2313).
        """
        try:
            confirmed = await self._client.set_preference(contract.symbol, desired, margin_mode)
        except Pi42Error as exc:
            raise LeverageNotConfirmed(f"Pi42 refused the leverage/margin-mode change: {exc}") from exc
        if confirmed.leverage != desired:
            raise LeverageNotConfirmed(
                f"Pi42 reported leverage {confirmed.leverage} for {contract.symbol}, expected {desired}"
            )
        if confirmed.margin_mode and confirmed.margin_mode != margin_mode.upper():
            raise LeverageNotConfirmed(
                f"Pi42 reported margin mode {confirmed.margin_mode} for {contract.symbol}, "
                f"expected {margin_mode.upper()}"
            )

    def _trading_enabled(self) -> bool:
        return self._settings.trading_enabled and not self._settings.kill_switch_file.exists()

    def _order_client_id(self, trade) -> str | None:
        return trade.client_order_id

    @staticmethod
    def describe_order(order: OrderInfo | None) -> str:
        if order is None:
            return "no order"
        return f"{order.client_order_id} {order.status} filled={order.filled_quantity}/{order.quantity}"
