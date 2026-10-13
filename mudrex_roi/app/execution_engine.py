"""Signal -> decision -> (PAPER simulation | guarded REAL order) pipeline.

Every message is first persisted (deduplicated on channel_id + message_id),
then parsed, then checked (stale, kill switch, duplicates, concurrency,
exact symbol, price grid, deviation, leverage, sizing, funds). Only a signal
that passes every check reaches an execution path. Rejections are recorded
with their precise reason and are never queued for later.
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
    AssetInfo,
    Direction,
    Mode,
    ParsedSignal,
    SignalStatus,
    TradeStatus,
    utc_now,
)
from app.mudrex_client import AmbiguousMutationError, MudrexError
from app.notifications import NullNotifier
from app.numeric import format_decimal
from app.paper_broker import PaperBroker
from app.position_monitor import reconcile_uncertain_trade
from app.risk_manager import (
    RiskRejection,
    SizingResult,
    check_concurrency,
    check_entry_deviation,
    check_funds,
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
    """Leverage could not be verified after the set call."""


@dataclass
class PreparedOrder:
    signal: ParsedSignal
    asset: AssetInfo
    leverage: LeverageDecision
    sizing: SizingResult
    market_price: Decimal
    deviation_pct: Decimal
    available_balance: Decimal
    client_order_id: str


def make_client_order_id(channel_id: int, message_id: int, symbol: str) -> str:
    """Deterministic, <=64 chars, letters/digits/hyphen only."""
    digest = hashlib.sha256(f"{channel_id}:{message_id}:{symbol}".encode()).hexdigest()[:20]
    return f"TG-{message_id}-{digest}"


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
        except MudrexError as exc:
            raise SkipSignal(f"Mudrex metadata/balance unavailable, signal skipped: {exc}") from exc

        log_event(
            logger, "signal.validated", symbol=signal.symbol, asset_id=prepared.asset.asset_id,
            leverage=prepared.leverage.selected, leverage_detail=prepared.leverage.describe(),
            quantity=prepared.sizing.quantity, notional=prepared.sizing.notional,
            est_margin=prepared.sizing.estimated_margin, market=prepared.market_price,
            deviation_pct=f"{prepared.deviation_pct:.4f}", mode=self._mode.value,
        )
        if self._mode is Mode.PAPER:
            self._open_paper(sig_id, prepared, now)
        else:
            await self._open_real(sig_id, prepared, now)

    async def prepare(self, signal: ParsedSignal) -> PreparedOrder:
        """All read-only validation. Raises SkipSignal/RiskRejection/LeverageError/MudrexError."""
        s = self._settings
        summary = await self._mapper.resolve(signal.symbol)
        asset = await self._client.get_asset_detail(summary.symbol)
        if asset.symbol != signal.symbol:
            raise SkipSignal(f"symbol mismatch: Mudrex returned {asset.symbol}")

        entry = snap_to_price_grid("Entry", signal.entry, asset)
        target1 = snap_to_price_grid("Target 1", signal.target1, asset)
        target2 = snap_to_price_grid("Target 2", signal.target2, asset) if signal.target2 else None
        target3 = snap_to_price_grid("Target 3", signal.target3, asset) if signal.target3 else None
        target4 = snap_to_price_grid("Target 4", signal.target4, asset) if signal.target4 else None
        target5 = snap_to_price_grid("Target 5", signal.target5, asset) if signal.target5 else None

        if signal.direction == Direction.LONG and target1 <= entry:
            raise RiskRejection(f"Target 1 ({target1}) must be strictly greater than Entry ({entry})")
        if signal.direction == Direction.SHORT and target1 >= entry:
            raise RiskRejection(f"Target 1 ({target1}) must be strictly less than Entry ({entry})")

        if entry != signal.entry or target1 != signal.target1:
            logger.info("event=price.snapped symbol=%s entry=%s->%s target1=%s->%s",
                        signal.symbol, signal.entry, entry, signal.target1, target1)
        signal = replace(signal, entry=entry, target1=target1, target2=target2, target3=target3,
                         target4=target4, target5=target5)

        if asset.price is None or asset.price <= 0:
            raise SkipSignal("no valid live market price for the entry-deviation check")
        deviation = check_entry_deviation(signal.entry, asset.price, s.max_entry_deviation_pct)

        leverage = select_leverage(signal.telegram_leverage, asset)
        budget = compute_budget(s.max_margin_usdt_per_trade, s.margin_reserve_usdt)
        sizing = size_position(entry=signal.entry, leverage=leverage.selected, budget=budget, asset=asset)

        if self._mode is Mode.PAPER:
            assert self._paper is not None
            available = self._paper.available_balance()
            concurrent = len(self._storage.active_trades(Mode.PAPER.value))
        else:
            funds = await self._client.get_futures_funds(s.trade_currency)
            available = funds.balance
            open_positions = await self._client.list_open_positions(s.trade_currency)
            if any(p.symbol == signal.symbol for p in open_positions):
                raise SkipSignal(f"an open position already exists on Mudrex for {signal.symbol}")
            open_orders = await self._client.list_open_orders(s.trade_currency)
            if any(o.symbol == signal.symbol and o.status in ("CREATED", "PARTIAL_FILLED") for o in open_orders):
                raise SkipSignal(f"an unfilled order already exists on Mudrex for {signal.symbol}")
            concurrent = len(open_positions) + self._storage.pending_like_count(Mode.REAL.value)

        check_concurrency(concurrent, s.max_concurrent_positions)
        check_funds(available, sizing.estimated_margin, s.margin_reserve_usdt)

        return PreparedOrder(
            signal=signal,
            asset=asset,
            leverage=leverage,
            sizing=sizing,
            market_price=asset.price,
            deviation_pct=deviation,
            available_balance=available,
            client_order_id=make_client_order_id(signal.channel_id, signal.message_id, signal.symbol),
        )

    # ----------------------------------------------------------- persistence
    def _trade_fields(self, sig_id: int, prepared: PreparedOrder, status: TradeStatus, reason: str) -> dict:
        signal = prepared.signal
        roi_ratio = self._settings.target_roi_pct / Decimal("100")
        if signal.direction.value == "LONG":
            raw_target = signal.entry * (Decimal("1") + roi_ratio)
        else:
            raw_target = signal.entry * (Decimal("1") - roi_ratio)

        target1 = snap_to_price_grid("target1", raw_target, prepared.asset) if prepared.asset.price_step else raw_target

        return dict(
            signal_id=sig_id,
            mode=self._mode.value,
            client_order_id=prepared.client_order_id,
            symbol=signal.symbol,
            mudrex_asset_id=prepared.asset.asset_id,
            direction=signal.direction.value,
            entry_price=signal.entry,
            target1=target1,
            target2=None,
            target3=None,
            target4=None,
            target5=None,
            stop_loss_reference=None,
            telegram_leverage=signal.telegram_leverage,
            leverage=prepared.leverage.selected,
            asset_min_leverage=prepared.leverage.min_leverage,
            asset_max_leverage=prepared.leverage.max_leverage,
            leverage_step=prepared.leverage.leverage_step,
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
                                        decision_reason=reason, mudrex_symbol=prepared.signal.symbol,
                                        mudrex_asset_id=prepared.asset.asset_id)
            self._storage.add_event(now, "paper.entry_pending", trade_id=trade_id, signal_id=sig_id,
                                    detail={"limit": prepared.signal.entry,
                                            "quantity": prepared.sizing.quantity})
        log_event(logger, "paper.entry_pending", trade_id=trade_id, symbol=prepared.signal.symbol,
                  side=prepared.signal.direction.value, limit=prepared.signal.entry,
                  quantity=prepared.sizing.quantity, est_margin=prepared.sizing.estimated_margin)

    async def _open_real(self, sig_id: int, prepared: PreparedOrder, now: datetime) -> None:
        signal = prepared.signal
        currency = self._settings.trade_currency
        trade_fields = self._trade_fields(sig_id, prepared, TradeStatus.SUBMITTING, "persisted before Mudrex submission")
        target_price = trade_fields["target1"]
        trade_id = self._storage.create_trade(now, **trade_fields)
        self._storage.update_signal(sig_id, now, status=SignalStatus.ACCEPTED.value,
                                    decision_reason="validated; submitting to Mudrex",
                                    mudrex_symbol=signal.symbol, mudrex_asset_id=prepared.asset.asset_id)

        try:
            await self._ensure_leverage(signal.symbol, prepared.leverage.selected, currency)
        except (MudrexError, LeverageNotConfirmed) as exc:
            self._reject_trade(trade_id, sig_id, now, f"leverage not confirmed; no order placed: {exc}")
            return

        try:
            accepted = await self._client.place_limit_order(
                symbol=signal.symbol,
                side=signal.direction.value,
                quantity=prepared.sizing.quantity,
                price=signal.entry,
                takeprofit_price=target_price,
                client_order_id=prepared.client_order_id,
                trade_currency=currency,
            )
        except AmbiguousMutationError as exc:
            self._storage.update_trade(trade_id, now, status=TradeStatus.SUBMIT_UNCERTAIN.value,
                                       status_reason=f"outcome unknown: {exc}")
            self._storage.add_event(now, "real.submit_uncertain", level="WARNING", trade_id=trade_id,
                                    signal_id=sig_id)
            log_event(logger, "real.submit_uncertain", level=logging.WARNING, trade_id=trade_id,
                      client_order_id=prepared.client_order_id)
            trade = self._storage.get_trade(trade_id)
            assert trade is not None
            await reconcile_uncertain_trade(storage=self._storage, client=self._client, trade=trade, now=now)
            return
        except MudrexError as exc:
            self._reject_trade(trade_id, sig_id, now, f"Mudrex rejected the order: {exc}")
            return

        reason = f"accepted by Mudrex (status {accepted.status}); awaiting fill"
        self._storage.update_trade(
            trade_id, now, status=TradeStatus.ENTRY_PENDING.value, order_id=accepted.order_id,
            position_id=accepted.position_id, status_reason=reason,
        )
        self._storage.update_signal(sig_id, now, decision_reason=reason)
        self._storage.add_event(
            now, "real.order_accepted", trade_id=trade_id, signal_id=sig_id,
            detail={"order_id": accepted.order_id, "position_id": accepted.position_id,
                    "client_order_id": prepared.client_order_id, "status": accepted.status},
        )
        targets_count = len(signal.all_targets())
        log_event(logger, "real.order_accepted", trade_id=trade_id, symbol=signal.symbol,
                  order_id=accepted.order_id, side=signal.direction.value, limit=signal.entry,
                  targets=targets_count, quantity=prepared.sizing.quantity)
        await self._notifier.send(
            f"[REAL] {signal.symbol} {signal.direction.value} LIMIT {signal.entry} qty "
            f"{prepared.sizing.quantity} x{prepared.leverage.selected} "
            f"{targets_count} target(s): " +
            ", ".join(str(t) for t in signal.all_targets()) +
            ". SL will be set after TP1 hit. Emergency SL: " +
            (str(signal.stop_loss) if signal.stop_loss else "none from signal") + "."
        )

    def _reject_trade(self, trade_id: int, sig_id: int, now: datetime, reason: str) -> None:
        self._storage.update_trade(trade_id, now, status=TradeStatus.REJECTED.value, status_reason=reason)
        self._storage.update_signal(sig_id, now, status=SignalStatus.REJECTED.value, decision_reason=reason)
        self._storage.add_event(now, "real.order_rejected", level="WARNING", trade_id=trade_id,
                                signal_id=sig_id, detail={"reason": reason})
        log_event(logger, "real.order_rejected", level=logging.WARNING, trade_id=trade_id, reason=reason)

    async def _ensure_leverage(self, symbol: str, desired: Decimal, currency: str) -> None:
        """Set ISOLATED leverage and verify it before any entry order is submitted."""
        current = await self._client.get_leverage(symbol, currency)
        if current is not None and current.leverage == desired and current.margin_type == "ISOLATED":
            return
        await self._client.set_leverage(symbol, desired, currency)
        confirmed = await self._client.get_leverage(symbol, currency)
        if confirmed is None or confirmed.leverage != desired or confirmed.margin_type != "ISOLATED":
            raise LeverageNotConfirmed(f"Mudrex leverage for {symbol} is not {desired} ISOLATED after set")

    def _trading_enabled(self) -> bool:
        return self._settings.trading_enabled and not self._settings.kill_switch_file.exists()

