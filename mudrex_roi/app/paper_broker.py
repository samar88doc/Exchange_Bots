"""PAPER-mode simulation engine.

Nothing here touches Mudrex. Prices come from the caller (live public asset
price read through the read-only client). The simulation is deliberately
conservative:

* LIMIT entry fills only when the observed price reaches or crosses the limit
  (LONG: price <= entry, SHORT: price >= entry), filled at the limit price.
* Target 1 can only trigger on a tick observed AFTER the fill tick.
* Limit entries expire after LIMIT_ORDER_TTL_MINUTES if never filled.
* Liquidation uses an approximate isolated-margin formula and a maintenance
  margin rate. It is an approximation, not Mudrex's liquidation engine.
* Fees are a flat rate on notional. Funding is not modelled.

Limitations: no order book, no queue position, no latency, no slippage on
exits, no partial fills. Real execution can differ materially.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from app.models import Direction, Mode, TradeRecord, TradeStatus
from app.storage import Storage

logger = logging.getLogger("paper.broker")
ZERO = Decimal(0)


@dataclass(frozen=True)
class TickResult:
    new_status: TradeStatus | None
    updates: dict[str, Any] = field(default_factory=dict)
    event: str | None = None


def _sign(direction: str) -> int:
    return 1 if direction == Direction.LONG.value else -1


def liquidation_price(direction: str, entry: Decimal, leverage: Decimal, maintenance_rate: Decimal) -> Decimal:
    """Approximate isolated-margin liquidation price."""
    inv = Decimal(1) / leverage
    if direction == Direction.LONG.value:
        return entry * (Decimal(1) - inv + maintenance_rate)
    return entry * (Decimal(1) + inv - maintenance_rate)


def _entry_crossed(direction: str, price: Decimal, entry: Decimal) -> bool:
    return price <= entry if direction == Direction.LONG.value else price >= entry


def _tp_reached(direction: str, price: Decimal, target: Decimal) -> bool:
    return price >= target if direction == Direction.LONG.value else price <= target


def _liquidated(direction: str, price: Decimal, liq: Decimal) -> bool:
    return price <= liq if direction == Direction.LONG.value else price >= liq


def evaluate_paper_tick(
    trade: TradeRecord,
    price: Decimal,
    now: datetime,
    *,
    ttl_minutes: int,
    fee_rate: Decimal,
    maintenance_margin_rate: Decimal,
) -> TickResult:
    """Pure state transition for one simulated trade given one observed price."""
    if price <= 0:
        return TickResult(None)

    if trade.status == TradeStatus.ENTRY_PENDING.value:
        if ttl_minutes > 0 and now - trade.created_at >= timedelta(minutes=ttl_minutes):
            return TickResult(
                TradeStatus.EXPIRED,
                {"closed_at": now, "status_reason": "entry limit not filled within LIMIT_ORDER_TTL_MINUTES"},
                "paper.entry_expired",
            )
        if _entry_crossed(trade.direction, price, trade.entry_price):
            entry_fee = trade.entry_price * trade.quantity * fee_rate
            liq = liquidation_price(trade.direction, trade.entry_price, trade.leverage, maintenance_margin_rate)
            return TickResult(
                TradeStatus.ENTRY_FILLED,
                {
                    "filled_at": now,
                    "fill_price": trade.entry_price,
                    "filled_quantity": trade.quantity,
                    "entry_fee": entry_fee,
                    "liquidation_price": liq,
                    "status_reason": "simulated limit entry filled",
                },
                "paper.entry_filled",
            )
        return TickResult(None)

    if trade.status == TradeStatus.ENTRY_FILLED.value:
        entry_fee = trade.entry_fee or ZERO
        if trade.liquidation_price is not None and _liquidated(trade.direction, price, trade.liquidation_price):
            realized = -trade.est_margin - entry_fee
            return TickResult(
                TradeStatus.LIQUIDATED,
                {
                    "closed_at": now,
                    "exit_price": price,
                    "exit_reason": "LIQUIDATION (approximate)",
                    "realized_pnl": realized,
                    "status_reason": "simulated liquidation; loss capped at allocated margin",
                },
                "paper.liquidated",
            )
        if _tp_reached(trade.direction, price, trade.target1):
            exit_price = trade.target1
            gross = trade.quantity * (exit_price - trade.entry_price) * _sign(trade.direction)
            exit_fee = exit_price * trade.quantity * fee_rate
            return TickResult(
                TradeStatus.CLOSED,
                {
                    "closed_at": now,
                    "exit_price": exit_price,
                    "exit_reason": "TAKE_PROFIT_1",
                    "exit_fee": exit_fee,
                    "realized_pnl": gross - entry_fee - exit_fee,
                    "status_reason": "simulated Target 1 take-profit",
                },
                "paper.take_profit_hit",
            )
    return TickResult(None)


class PaperBroker:
    def __init__(
        self,
        storage: Storage,
        *,
        initial_balance: Decimal,
        fee_rate: Decimal,
        maintenance_margin_rate: Decimal,
        ttl_minutes: int,
    ) -> None:
        self._storage = storage
        self.initial_balance = initial_balance
        self.fee_rate = fee_rate
        self.maintenance_margin_rate = maintenance_margin_rate
        self.ttl_minutes = ttl_minutes

    def ledger(self) -> dict[str, Decimal]:
        """Simulated balance breakdown. Distinct from the real futures wallet."""
        trades = self._storage.trades(mode=Mode.PAPER.value)
        realized = ZERO
        open_fees = ZERO
        locked = ZERO
        for trade in trades:
            status = trade.trade_status
            if status in (TradeStatus.CLOSED, TradeStatus.LIQUIDATED):
                realized += trade.realized_pnl or ZERO
            elif status == TradeStatus.ENTRY_FILLED:
                open_fees += trade.entry_fee or ZERO
                locked += trade.est_margin
            elif status == TradeStatus.ENTRY_PENDING:
                locked += trade.est_margin
        available = self.initial_balance + realized - open_fees - locked
        return {
            "initial": self.initial_balance,
            "realized_pnl": realized,
            "open_entry_fees": open_fees,
            "locked_margin": locked,
            "available": available,
        }

    def available_balance(self) -> Decimal:
        return self.ledger()["available"]

    def apply_price(self, symbol: str, price: Decimal, now: datetime) -> list[tuple[TradeRecord, TickResult]]:
        """Apply one observed price to every active PAPER trade on ``symbol``."""
        transitions: list[tuple[TradeRecord, TickResult]] = []
        for trade in self._storage.active_trades(Mode.PAPER.value):
            if trade.symbol != symbol:
                continue
            result = evaluate_paper_tick(
                trade,
                price,
                now,
                ttl_minutes=self.ttl_minutes,
                fee_rate=self.fee_rate,
                maintenance_margin_rate=self.maintenance_margin_rate,
            )
            if result.new_status is None:
                continue
            updates = dict(result.updates)
            updates["status"] = result.new_status.value
            self._storage.update_trade(trade.id, now, **updates)
            detail = {k: v for k, v in updates.items() if k != "status"}
            detail.update({"observed_price": price, "trade_id": trade.id})
            self._storage.add_event(
                now,
                result.event or "paper.transition",
                trade_id=trade.id,
                signal_id=trade.signal_id,
                detail=detail,
            )
            logger.info(
                "event=%s symbol=%s trade_id=%s price=%s status=%s",
                result.event, symbol, trade.id, price, result.new_status.value,
            )
            transitions.append((trade, result))
        return transitions
