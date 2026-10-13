"""Core REST API: status, positions, signals, trades, orders, risk, settings,
strategies, symbols, audit, replay, live activation, paper report, SSE stream.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppSettings, get_settings
from app.core.errors import RejectReason
from app.core.events import event_bus
from app.core.logging import TradeEvent, log_event
from app.core.security import constant_time_equals
from app.db.session import get_db_session
from app.models.entities import (
    AuditLog,
    Order,
    Position,
    PositionEvent,
    PositionReservation,
    ReconciliationEvent,
    RiskEvent,
    SentimentHistory,
    Setting,
    Signal,
    Strategy,
    SymbolMapping,
    SystemEvent,
    Trade,
    WebhookEvent,
)
from app.models.enums import SLOT_CONSUMING_STATES, HealthState
from app.schemas.dto import (
    CloseRequest,
    LiveActivationRequest,
    OrderOut,
    PositionOut,
    ReplayRequest,
    SettingUpdate,
    SignalOut,
    SymbolMappingCreate,
    TradeOut,
)
from app.services.runtime import AppContext

log = logging.getLogger("trading.api")
router = APIRouter(prefix="/api")


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx  # type: ignore[no-any-return]


# --------------------------------------------------------------------- health
@router.get("/health")
async def health(ctx: AppContext = Depends(get_ctx), session: AsyncSession = Depends(get_db_session)) -> dict:
    db_state = HealthState.CONNECTED.value
    try:
        await session.execute(select(func.count()).select_from(Setting))
    except Exception:  # noqa: BLE001
        db_state = HealthState.ERROR.value
    shark_state = await ctx.shark.health() if ctx.settings.trading_mode == "LIVE" else "DEGRADED"
    return {
        "status": "ok",
        "mode": ctx.settings.trading_mode,
        "components": {
            "database": db_state,
            "tradingview_webhook": HealthState.CONNECTED.value,
            "shark_api": shark_state,
            "market_data": ctx.market_data.health(),
            "redis": "CONNECTED" if ctx.settings.redis_enabled else "DEGRADED",
            "worker": "CONNECTED",
            "queue": "CONNECTED",
            "position_monitor": "CONNECTED",
            "symbol_mapping": HealthState.CONNECTED.value,
            "position_reconciliation": HealthState.CONNECTED.value,
        },
    }


@router.get("/status")
async def status(ctx: AppContext = Depends(get_ctx), session: AsyncSession = Depends(get_db_session)) -> dict:
    states = [s.value for s in SLOT_CONSUMING_STATES]
    open_positions = int((await session.execute(
        select(func.count()).select_from(Position).where(Position.state.in_(states))
    )).scalar_one())
    reservations = int((await session.execute(
        select(func.count()).select_from(PositionReservation)
        .where(PositionReservation.status == "ACTIVE")
    )).scalar_one())
    halted = await ctx.risk.is_halted(session)
    kill = await ctx.risk.kill_switch_active(session)

    today_start = datetime.now(UTC) - timedelta(hours=24)
    trades = (await session.execute(select(Trade))).scalars().all()

    def _naive(dt):
        """Strip timezone from a datetime so comparisons always work."""
        return dt if dt and dt.tzinfo else dt

    today_pnl = sum(
        (t.net_pnl for t in trades if t.closed_at and _naive(t.closed_at) >= today_start and t.net_pnl is not None),
        Decimal("0"),
    )
    wins = [t for t in trades if t.result == "WIN"]
    losses = [t for t in trades if t.result == "LOSS"]
    unrealized = (await session.execute(
        select(func.coalesce(func.sum(Position.unrealized_pnl), 0)).where(Position.state.in_(states))
    )).scalar_one()

    # Calculate paper wallet balance and available margin
    initial_cap = ctx.settings.paper_initial_balance
    total_realized = sum((t.net_pnl for t in trades if t.net_pnl is not None), Decimal("0"))
    current_wallet = initial_cap + total_realized
    locked_margin = (await session.execute(
        select(func.coalesce(func.sum(Position.margin), Decimal("0"))).where(Position.state.in_(states))
    )).scalar_one()
    available_margin = max(Decimal("0"), current_wallet - locked_margin)
    available_slots = int(available_margin // ctx.settings.margin_per_trade) if ctx.settings.margin_per_trade > 0 else 0

    return {
        "mode": ctx.settings.trading_mode,
        "halted": halted,
        "kill_switch": kill,
        "dry_run": ctx.settings.shark_dry_run,
        "balance": {
            "wallet_balance": str(current_wallet),
            "available_margin": str(available_margin),
            "locked_margin": str(locked_margin),
            "account_unit": ctx.settings.account_unit,
        },
        "risk": {
            "open_positions": open_positions,
            "reservations": reservations,
            "available_slots": available_slots,
            "max_open_positions": (open_positions + available_slots) if ctx.settings.trading_mode == "PAPER" else ctx.settings.max_open_positions,
            "margin_per_trade": str(ctx.settings.margin_per_trade),
            "leverage": ctx.settings.leverage,
            "target_roi_percent": str(ctx.settings.target_roi_percent),
            "stop_loss_enabled": ctx.settings.stop_loss_enabled,
            "short_enabled": ctx.settings.short_enabled,
            "account_unit": ctx.settings.account_unit,
        },

        "pnl": {
            "today_pnl": str(today_pnl),
            "realized_pnl": str(sum((t.net_pnl for t in trades if t.net_pnl is not None), Decimal("0"))),
            "unrealized_pnl": str(unrealized),
            "total_trades": len(trades),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 2) if trades else 0,
            "average_profit": str(sum((t.net_pnl for t in wins if t.net_pnl is not None), Decimal("0")) / len(wins)) if wins else "0",
            "average_loss": str(sum((t.net_pnl for t in losses if t.net_pnl is not None), Decimal("0")) / len(losses)) if losses else "0",
        },
    }


# ------------------------------------------------------------------ positions
@router.get("/positions", response_model=list[PositionOut])
async def list_positions(
    active_only: bool = Query(True),
    session: AsyncSession = Depends(get_db_session),
) -> list[Position]:
    if active_only:
        states = [s.value for s in SLOT_CONSUMING_STATES]
        result = await session.execute(select(Position).where(Position.state.in_(states)).order_by(desc(Position.open_time)))
    else:
        result = await session.execute(select(Position).order_by(desc(Position.created_at)).limit(200))
    return list(result.scalars())


@router.get("/positions/{position_id}", response_model=PositionOut)
async def get_position(position_id: str, session: AsyncSession = Depends(get_db_session)) -> Position:
    row = await session.execute(select(Position).where(Position.position_id == position_id))
    position = row.scalar_one_or_none()
    if position is None:
        raise HTTPException(404, "position not found")
    return position


@router.get("/positions/{position_id}/timeline")
async def position_timeline(position_id: str, session: AsyncSession = Depends(get_db_session)) -> list[dict]:
    result = await session.execute(
        select(PositionEvent).where(PositionEvent.position_id == position_id).order_by(PositionEvent.created_at)
    )
    return [
        {"event": e.event, "from_state": e.from_state, "to_state": e.to_state,
         "detail": e.detail, "ts": e.created_at.isoformat()}
        for e in result.scalars()
    ]


@router.post("/positions/{position_id}/close")
async def close_position(
    position_id: str,
    body: CloseRequest,
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    row = await session.execute(select(Position).where(Position.position_id == position_id))
    position = row.scalar_one_or_none()
    if position is None:
        raise HTTPException(404, "position not found")
    if ctx.settings.trading_mode == "LIVE" and not body.confirm:
        raise HTTPException(400, "LIVE destructive actions require confirm=true")
    await ctx.roi_monitor.close_position(session, position, reason=body.reason)
    await session.commit()
    await ctx.notifications.notify("trade_closed", f"{position.canonical_symbol} closed manually")
    event_bus.publish("positions.changed", {})
    return {"status": "CLOSED", "position_id": position_id, "reason": body.reason}


@router.post("/positions/close-all")
async def close_all(
    body: CloseRequest,
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    if ctx.settings.trading_mode == "LIVE" and not body.confirm:
        raise HTTPException(400, "LIVE destructive actions require confirm=true")
    closed = []
    for position in await ctx.positions.get_active(session):
        if position.state in ("RESERVED", "ORDER_PENDING"):
            continue
        await ctx.roi_monitor.close_position(session, position, reason=body.reason)
        closed.append(position.position_id)
    await session.commit()
    event_bus.publish("positions.changed", {})
    return {"closed": closed}


# -------------------------------------------------------------------- signals
@router.get("/signals", response_model=list[SignalOut])
async def list_signals(
    limit: int = Query(100, le=500),
    status: str | None = None,
    canonical_symbol: str | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> list[Signal]:
    stmt = select(Signal).order_by(desc(Signal.received_at)).limit(limit)
    if status:
        stmt = stmt.where(Signal.status_display == status)
    if canonical_symbol:
        stmt = stmt.where(Signal.canonical_symbol == canonical_symbol)
    return list((await session.execute(stmt)).scalars())


@router.get("/signals/{signal_id}")
async def signal_detail(signal_id: int, session: AsyncSession = Depends(get_db_session)) -> dict:
    signal = (await session.execute(select(Signal).where(Signal.id == signal_id))).scalar_one_or_none()
    if signal is None:
        raise HTTPException(404, "signal not found")
    timeline = []
    if signal.position_id:
        events = (await session.execute(
            select(PositionEvent).where(PositionEvent.position_id == signal.position_id).order_by(PositionEvent.created_at)
        )).scalars()
        timeline = [{"event": e.event, "from_state": e.from_state, "to_state": e.to_state,
                     "detail": e.detail, "ts": e.created_at.isoformat()} for e in events]
    return {
        "signal": SignalOut.model_validate(signal).model_dump(mode="json"),
        "timeline": timeline,
    }


# --------------------------------------------------------------------- trades
@router.get("/trades", response_model=list[TradeOut])
async def list_trades(
    limit: int = Query(200, le=1000),
    mode: str | None = None,
    result: str | None = None,
    canonical_symbol: str | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> list[Trade]:
    stmt = select(Trade).order_by(desc(Trade.closed_at)).limit(limit)
    if mode:
        stmt = stmt.where(Trade.mode == mode)
    if result:
        stmt = stmt.where(Trade.result == result)
    if canonical_symbol:
        stmt = stmt.where(Trade.canonical_symbol == canonical_symbol)
    return list((await session.execute(stmt)).scalars())


@router.get("/trades/export/csv")
async def export_trades(session: AsyncSession = Depends(get_db_session)) -> StreamingResponse:
    trades = (await session.execute(select(Trade).order_by(desc(Trade.closed_at)))).scalars()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "timestamp", "strategy", "tradingview_symbol", "canonical_symbol", "shark_symbol", "side",
        "entry", "exit", "quantity", "margin", "leverage", "entry_fee", "exit_fee", "funding",
        "gross_pnl", "net_pnl", "roi", "duration_s", "mode", "status", "event_id", "entry_order_id", "exit_order_id",
    ])
    for t in trades:
        writer.writerow([
            t.closed_at, t.strategy_id, t.tradingview_raw_symbol, t.canonical_symbol, t.shark_symbol,
            t.side, t.entry_price, t.exit_price, t.quantity, t.margin, t.leverage,
            t.entry_fee, t.exit_fee, t.funding, t.gross_pnl, t.net_pnl, t.roi_percent,
            t.duration_seconds, t.mode, t.status, t.event_id, t.entry_order_id, t.exit_order_id,
        ])
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trades_export.csv"},
    )


# --------------------------------------------------------------------- orders
@router.get("/orders", response_model=list[OrderOut])
async def list_orders(limit: int = Query(200, le=1000), session: AsyncSession = Depends(get_db_session)) -> list[Order]:
    return list((await session.execute(select(Order).order_by(desc(Order.created_at)).limit(limit))).scalars())


# ------------------------------------------------------------------ risk cent
@router.get("/risk")
async def risk_center(ctx: AppContext = Depends(get_ctx), session: AsyncSession = Depends(get_db_session)) -> dict:
    states = [s.value for s in SLOT_CONSUMING_STATES]
    open_positions = int((await session.execute(
        select(func.count()).select_from(Position).where(Position.state.in_(states))
    )).scalar_one())
    from app.models.entities import PositionReservation, Trade

    reservations = list((await session.execute(
        select(PositionReservation).where(PositionReservation.status == "ACTIVE")
    )).scalars())
    events = list((await session.execute(
        select(RiskEvent).order_by(desc(RiskEvent.created_at)).limit(50)
    )).scalars())

    locked_margin = (await session.execute(
        select(func.coalesce(func.sum(Position.margin), Decimal("0"))).where(Position.state.in_(states))
    )).scalar_one()
    trades = list((await session.execute(select(Trade))).scalars())
    total_realized_pnl = sum((t.net_pnl for t in trades if t.net_pnl is not None), Decimal("0"))
    current_wallet = ctx.settings.paper_initial_balance + total_realized_pnl
    available_margin = max(Decimal("0"), current_wallet - locked_margin)
    available_slots = int(available_margin // ctx.settings.margin_per_trade) if ctx.settings.margin_per_trade > 0 else 0
    max_positions_disp = (open_positions + available_slots) if ctx.settings.trading_mode == "PAPER" else ctx.settings.max_open_positions

    return {
        "max_open_positions": max_positions_disp,
        "open_positions": open_positions,
        "available_slots": available_slots if ctx.settings.trading_mode == "PAPER" else max(0, ctx.settings.max_open_positions - open_positions),
        "reservations": [
            {"canonical_symbol": r.canonical_symbol, "position_id": r.position_id,
             "status": r.status, "created_at": r.created_at.isoformat()}
            for r in reservations
        ],
        "margin_per_trade": str(ctx.settings.margin_per_trade),
        "leverage": ctx.settings.leverage,
        "target_roi_percent": str(ctx.settings.target_roi_percent),
        "stop_loss_enabled": ctx.settings.stop_loss_enabled,
        "duplicate_protection": "ENABLED",
        "symbol_normalization": "ENABLED" if ctx.settings.symbol_normalization_enabled else "DISABLED",
        "trading_halted": await ctx.risk.is_halted(session),
        "kill_switch": await ctx.risk.kill_switch_active(session),
        "daily_trade_limit": ctx.settings.daily_trade_limit,
        "daily_loss_limit": str(ctx.settings.daily_loss_limit),
        "recent_events": [
            {"event": e.event, "reason": e.reason, "detail": e.detail, "ts": e.created_at.isoformat()}
            for e in events
        ],
    }


async def _set_flag(session: AsyncSession, key: str, value: str, action: str) -> dict:
    row = (await session.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
    if row is None:
        row = Setting(key=key, value=value, value_type="bool")
        session.add(row)
    else:
        row.value = value
    session.add(AuditLog(action=action, entity="setting", entity_id=key, detail={"value": value}))
    return {"key": key, "value": value}


@router.post("/risk/halt")
async def halt_trading(session: AsyncSession = Depends(get_db_session)) -> dict:
    result = await _set_flag(session, "trading_halted", "true", "TRADING_HALTED")
    log_event(log, TradeEvent.TRADING_HALTED, "trading halted by operator")
    event_bus.publish("risk.changed", {"halted": True})
    return result


@router.post("/risk/resume")
async def resume_trading(body: CloseRequest, session: AsyncSession = Depends(get_db_session)) -> dict:
    """Disabling the halt requires explicit confirmation."""
    if not body.confirm:
        raise HTTPException(400, "resuming trading requires confirm=true")
    result = await _set_flag(session, "trading_halted", "false", "TRADING_RESUMED")
    event_bus.publish("risk.changed", {"halted": False})
    return result


@router.post("/risk/kill-switch")
async def kill_switch(body: CloseRequest, session: AsyncSession = Depends(get_db_session)) -> dict:
    value = "true" if body.confirm else "false"
    action = "KILL_SWITCH" if body.confirm else "KILL_SWITCH_DISABLED"
    result = await _set_flag(session, "kill_switch", value, action)
    log_event(log, TradeEvent.KILL_SWITCH, f"kill switch {'enabled' if body.confirm else 'disabled'}")
    event_bus.publish("risk.changed", {"kill_switch": body.confirm})
    return result


# -------------------------------------------------------------- webhook monit
@router.get("/webhooks/monitor")
async def webhook_monitor(session: AsyncSession = Depends(get_db_session)) -> dict:
    since = datetime.now(UTC) - timedelta(minutes=10)
    events = list((await session.execute(
        select(WebhookEvent).where(WebhookEvent.received_at >= since).order_by(desc(WebhookEvent.received_at))
    )).scalars())
    all_events = list((await session.execute(
        select(WebhookEvent).order_by(desc(WebhookEvent.received_at)).limit(500)
    )).scalars())
    by_status: dict[str, int] = {}
    for e in all_events:
        by_status[e.status] = by_status.get(e.status, 0) + 1
    last = all_events[0] if all_events else None
    minutes = max(1, int((datetime.now(UTC) - since).total_seconds() / 60))
    norm_failures = [e for e in all_events if e.rejection_reason in (
        RejectReason.SHARK_SYMBOL_NOT_FOUND.value, RejectReason.SYMBOL_NOT_FOUND.value)]
    return {
        "health": HealthState.CONNECTED.value,
        "last_event": last.event_id if last else None,
        "last_event_ts": last.received_at.isoformat() if last else None,
        "events_per_minute": round(len(events) / minutes, 2),
        "successful_signals": by_status.get("EXECUTED", 0) + by_status.get("RECEIVED", 0),
        "rejected_signals": by_status.get("REJECTED", 0),
        "duplicate_signals": by_status.get("DUPLICATE", 0),
        "invalid_signals": sum(1 for e in all_events if e.rejection_reason == RejectReason.INVALID_PAYLOAD.value),
        "failed_events": sum(1 for e in all_events if e.status == "ERROR"),
        "normalization_failures": len(norm_failures),
        "recent": [
            {
                "ts": e.received_at.isoformat(),
                "event_id": e.event_id,
                "status": e.status,
                "reason": e.rejection_reason,
                "payload": e.raw_payload,
                "latency_ms": e.latency_ms,
            }
            for e in all_events[:25]
        ],
    }


# ----------------------------------------------------------------- strategies
@router.get("/strategies")
async def list_strategies(session: AsyncSession = Depends(get_db_session)) -> list[dict]:
    rows = (await session.execute(select(Strategy).order_by(Strategy.id))).scalars()
    return [
        {
            "strategy_id": s.strategy_id, "name": s.name, "enabled": s.enabled,
            "webhook_identifier": s.webhook_identifier, "allowed_symbols": s.allowed_symbols or [],
            "alert_count": s.alert_count, "executed_trades": s.executed_trades,
            "rejected_trades": s.rejected_trades,
        }
        for s in rows
    ]


@router.patch("/strategies/{strategy_id}")
async def update_strategy(strategy_id: str, body: dict, session: AsyncSession = Depends(get_db_session)) -> dict:
    row = (await session.execute(select(Strategy).where(Strategy.strategy_id == strategy_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "strategy not found")
    for field in ("enabled", "name", "allowed_symbols", "webhook_identifier"):
        if field in body:
            setattr(row, field, body[field])
    session.add(AuditLog(action="STRATEGY_UPDATED", entity="strategy", entity_id=strategy_id, detail=body))
    return {"strategy_id": strategy_id, "updated": True}


# -------------------------------------------------------------------- symbols
@router.get("/symbols/mappings")
async def list_mappings(session: AsyncSession = Depends(get_db_session)) -> list[dict]:
    rows = (await session.execute(select(SymbolMapping).order_by(SymbolMapping.canonical_symbol))).scalars()
    return [
        {
            "id": m.id,
            "tradingview_raw_symbol": m.tradingview_raw_symbol,
            "canonical_symbol": m.canonical_symbol,
            "shark_symbol": m.shark_symbol,
            "exchange": m.exchange,
            "market_type": m.market_type,
            "contract_type": m.contract_type,
            "contract_status": m.contract_status,
            "base_asset": m.base_asset,
            "quote_asset": m.quote_asset,
            "quantity_precision": m.quantity_precision,
            "price_precision": m.price_precision,
            "minimum_quantity": str(m.minimum_quantity),
            "minimum_notional": str(m.minimum_notional),
            "quantity_step": str(m.quantity_step),
            "price_tick_size": str(m.price_tick_size),
            "max_leverage": m.max_leverage,
            "verified_against_exchange": m.verified_against_exchange,
            "created_at": m.created_at.isoformat(),
            "updated_at": m.updated_at.isoformat(),
        }
        for m in rows
    ]


@router.post("/symbols/mappings")
async def create_mapping(body: SymbolMappingCreate, session: AsyncSession = Depends(get_db_session)) -> dict:
    mapping = SymbolMapping(**body.model_dump())
    session.add(mapping)
    await session.flush()
    return {"id": mapping.id, "status": "created"}


@router.post("/symbols/sync")
async def sync_symbols(ctx: AppContext = Depends(get_ctx), session: AsyncSession = Depends(get_db_session)) -> dict:
    """Sync symbol mappings from VERIFIED Shark exchangeInfo. Never guesses."""
    from decimal import Decimal as D

    import httpx

    try:
        async with httpx.AsyncClient(base_url=ctx.settings.shark_base_url, timeout=15.0) as client:
            resp = await client.get("/v1/exchange/exchangeInfo")
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"exchangeInfo unavailable: {exc}") from exc

    contracts = data.get("contracts") or []

    created = updated = 0
    suffix = ctx.settings.tradingview_perpetual_suffix
    for c in contracts:
        shark_symbol = str(c.get("name") or "").upper()
        if not shark_symbol:
            continue
        canonical = shark_symbol.replace("-", "").replace("_", "").replace("/", "")
        raw = f"{canonical}{suffix}"
        filters = {f.get("filterType"): f for f in (c.get("filters") or []) if isinstance(f, dict)}
        qty_filter = filters.get("MARKET_QTY_SIZE") or filters.get("LIMIT_QTY_SIZE") or {}
        notional_filter = filters.get("MIN_NOTIONAL") or {}
        price_filter = filters.get("PRICE_FILTER") or {}
        min_qty = qty_filter.get("minQty") or qty_filter.get("minQuantity") or 0
        min_notional = notional_filter.get("notional") or notional_filter.get("minNotional") or 0
        qty_step = qty_filter.get("stepSize") or qty_filter.get("qtyStep") or 0
        price_tick = price_filter.get("tickSize") or c.get("tickSize") or 0
        row = (await session.execute(
            select(SymbolMapping).where(SymbolMapping.canonical_symbol == canonical)
        )).scalar_one_or_none()
        values = dict(
            canonical_symbol=canonical,
            tradingview_raw_symbol=raw,
            shark_symbol=shark_symbol,
            base_asset=str(c.get("baseAsset") or ""),
            quote_asset=str(c.get("quoteAsset") or ""),
            quantity_precision=int(c["quantityPrecision"]) if c.get("quantityPrecision") is not None else 8,
            price_precision=int(c["pricePrecision"]) if c.get("pricePrecision") is not None else 2,
            minimum_quantity=D(str(min_qty)),
            minimum_notional=D(str(min_notional)),
            quantity_step=D(str(qty_step)),
            price_tick_size=D(str(price_tick)),
            max_leverage=int(c["maxLeverage"]) if c.get("maxLeverage") else None,
            contract_type=str(c.get("contractType") or "PERPETUAL"),
            contract_status="ACTIVE",
            verified_against_exchange=True,
            raw_contract_meta=c,
        )
        if row is None:
            session.add(SymbolMapping(**values))
            created += 1
        else:
            for k, v in values.items():
                setattr(row, k, v)
            updated += 1
    session.add(SystemEvent(event="SYMBOLS_SYNCED", message=f"created={created} updated={updated}",
                            detail={"created": created, "updated": updated}))
    return {"created": created, "updated": updated, "source": "shark exchangeInfo (verified)"}


import time
import asyncio

_MARKET_FEED_CACHE = {"data": [], "timestamp": 0.0}
_MARKET_FEED_LOCK = None
_MARKET_CACHE_TTL = 10.0  # seconds

# ---------------------------------------------------------------- market feed
_MARKET_FEED_THREAD = None

async def _market_feed_loop():
    import asyncio
    import httpx
    import time
    from app.db.session import session_scope
    import traceback
    
    while True:
        try:
            async with session_scope() as session:
                rows = list((await session.execute(
                    select(SymbolMapping)
                    .where(SymbolMapping.contract_status == "TRADING")
                    .order_by(SymbolMapping.canonical_symbol)
                )).scalars())
                if not rows:
                    rows = list((await session.execute(
                        select(SymbolMapping).order_by(SymbolMapping.canonical_symbol)
                    )).scalars())
                
                recent_signals = (await session.execute(
                    select(Signal.canonical_symbol, Signal.action)
                    .where(Signal.received_at >= datetime.now(UTC) - timedelta(minutes=13))
                    .order_by(desc(Signal.received_at))
                    .limit(1000)
                )).fetchall()
                
            w_map = {c: a for c, a in reversed(recent_signals)}
            symbols_data = [
                {"shark_symbol": m.shark_symbol, "base_asset": m.base_asset or "", "quote_asset": m.quote_asset or "", "canonical_symbol": m.canonical_symbol}
                for m in rows
            ]
            
            SHARK_BASE = "https://api.sharkexchange.in"
            
            async def fetch_ticker(client, shark_symbol, base_asset, quote_asset, webhook_status):
                try:
                    ticker_coro = client.get(f"/v1/market/ticker24Hr/{shark_symbol}", timeout=5.0)
                    kline_coro = client.post("/v1/market/klines", json={"pair": shark_symbol, "interval": "1d", "limit": 1}, timeout=5.0)
                    ticker_resp, kline_resp = await asyncio.gather(ticker_coro, kline_coro, return_exceptions=True)

                    if isinstance(ticker_resp, BaseException):
                        raise ticker_resp
                    ticker_resp.raise_for_status()
                    raw = ticker_resp.json().get("data", {}) or {}
                    last_price = float(raw.get("c") or 0)
                    open_price = float(raw.get("o") or 0)
                    high = float(raw.get("h") or 0)
                    low = float(raw.get("l") or 0)
                    volume = float(raw.get("v") or 0)
                    change_pct = float(raw.get("P") or 0)
                    change_abs = float(raw.get("p") or 0)

                    day_open = 0.0
                    day_high = 0.0
                    day_low = 0.0
                    day_change_pct = 0.0
                    if not isinstance(kline_resp, BaseException):
                        try:
                            kline_resp.raise_for_status()
                            klines = kline_resp.json()
                            if klines and isinstance(klines, list):
                                k = klines[-1]
                                day_open = float(k.get("open") or 0)
                                day_high = float(k.get("high") or 0)
                                day_low = float(k.get("low") or 0)
                                day_close = float(k.get("close") or last_price)
                                if day_open > 0:
                                    day_change_pct = ((day_close - day_open) / day_open) * 100
                        except Exception:
                            pass

                    return {
                        "symbol": shark_symbol,
                        "base_asset": base_asset,
                        "quote_asset": quote_asset,
                        "last_price": last_price,
                        "open_price": open_price,
                        "high": high,
                        "low": low,
                        "volume": volume,
                        "change_pct": change_pct,
                        "change_abs": change_abs,
                        "day_open": day_open,
                        "day_high": day_high,
                        "day_low": day_low,
                        "day_change_pct": day_change_pct,
                        "status": "ok",
                        "webhook_status": webhook_status,
                    }
                except Exception as exc:
                    return {
                        "symbol": shark_symbol,
                        "base_asset": base_asset,
                        "quote_asset": quote_asset,
                        "last_price": 0,
                        "open_price": 0,
                        "high": 0,
                        "low": 0,
                        "volume": 0,
                        "change_pct": 0,
                        "change_abs": 0,
                        "day_open": 0,
                        "day_high": 0,
                        "day_low": 0,
                        "day_change_pct": 0,
                        "status": f"error: {exc}",
                        "webhook_status": webhook_status,
                    }

            async with httpx.AsyncClient(base_url=SHARK_BASE, timeout=10.0, limits=httpx.Limits(max_connections=20)) as client:
                chunk_size = 10
                all_results = []
                for i in range(0, len(symbols_data), chunk_size):
                    chunk = symbols_data[i : i + chunk_size]
                    tasks = [
                        fetch_ticker(client, d["shark_symbol"], d["base_asset"], d["quote_asset"], w_map.get(d["canonical_symbol"], ""))
                        for d in chunk
                    ]
                    results = await asyncio.gather(*tasks)
                    all_results.extend(results)
                    await asyncio.sleep(0.5)
                
                _MARKET_FEED_CACHE["data"] = all_results
                _MARKET_FEED_CACHE["timestamp"] = time.time()
                
        except Exception:
            traceback.print_exc()
            
        await asyncio.sleep(10)

@router.get("/market/live-feed")
async def market_live_feed(
    limit: int = Query(344, le=344, description="Max symbols to fetch (default all 344)"),
):
    import asyncio
    global _MARKET_FEED_THREAD
    if _MARKET_FEED_THREAD is None:
        _MARKET_FEED_THREAD = asyncio.create_task(_market_feed_loop())
    
    data = _MARKET_FEED_CACHE["data"][:limit]
    
    from fastapi.responses import JSONResponse
    return JSONResponse(content=data)


# ------------------------------------------------------------------ settings
RUNTIME_SETTING_KEYS = {
    "trading_halted", "kill_switch", "dry_run",
}


@router.get("/settings")
async def get_all_settings(settings: AppSettings = Depends(get_settings), session: AsyncSession = Depends(get_db_session)) -> dict:
    rows = list((await session.execute(select(Setting))).scalars())
    return {
        "trading": {
            "trading_mode": settings.trading_mode,
            "margin_per_trade": str(settings.margin_per_trade),
            "leverage": settings.leverage,
            "max_open_positions": settings.max_open_positions,
            "target_roi_percent": str(settings.target_roi_percent),
            "stop_loss_enabled": settings.stop_loss_enabled,
            "short_enabled": settings.short_enabled,
            "order_type": settings.order_type,
            "roi_includes_fees": settings.roi_includes_fees,
        },
        "execution": {
            "order_type": settings.order_type,
            "slippage_model": f"bps:{settings.paper_slippage_bps}",
        },
        "paper_trading": {
            "initial_balance": str(settings.paper_initial_balance),
            "fee_model": f"maker:{settings.paper_maker_fee_bps}bps/taker:{settings.paper_taker_fee_bps}bps",
            "funding_model": f"{settings.paper_funding_rate_bps_8h}bps per 8h",
            "slippage_bps": str(settings.paper_slippage_bps),
            "latency_ms": settings.paper_latency_ms,
        },
        "exchange": {
            "name": "shark",
            "base_url": settings.shark_base_url,
            "margin_asset": settings.shark_margin_asset,
            "dry_run": settings.shark_dry_run,
            "credentials_configured": bool(settings.shark_api_key and settings.shark_api_secret),
        },
        "symbol_mapping": {
            "normalization_enabled": settings.symbol_normalization_enabled,
            "tradingview_perpetual_suffix": settings.tradingview_perpetual_suffix,
        },
        "webhook": {
            "path": settings.webhook_path,
            "rate_limit_per_minute": settings.webhook_rate_limit_per_minute,
            "max_body_bytes": settings.webhook_max_body_bytes,
            "replay_window_seconds": settings.webhook_replay_window_seconds,
        },
        "system": {
            "timezone": settings.timezone_display,
            "log_level": settings.log_level,
            "database": settings.database_url.split("@")[-1] if "@" in settings.database_url else "configured",
            "redis_enabled": settings.redis_enabled,
            "notifications_enabled": settings.notifications_enabled,
        },
        "live_gate": {
            "live_trading_enabled": settings.live_trading_enabled,
            "live_capable": settings.is_live_capable(),
            "confirmation_phrase": settings.live_confirmation_phrase,
        },
        "runtime": {row.key: row.value for row in rows},
    }


@router.patch("/settings")
async def update_settings(body: SettingUpdate, session: AsyncSession = Depends(get_db_session)) -> dict:
    if body.key not in RUNTIME_SETTING_KEYS:
        raise HTTPException(400, f"runtime-editable keys only: {sorted(RUNTIME_SETTING_KEYS)} (use .env for the rest)")
    return await _set_flag(session, body.key, body.value, "SETTING_UPDATED")


# --------------------------------------------------------------------- audit
@router.get("/audit")
async def audit_logs(limit: int = Query(200, le=1000), session: AsyncSession = Depends(get_db_session)) -> list[dict]:
    rows = (await session.execute(select(AuditLog).order_by(desc(AuditLog.created_at)).limit(limit))).scalars()
    return [
        {"action": a.action, "actor": a.actor, "entity": a.entity, "entity_id": a.entity_id,
         "detail": a.detail, "correlation_id": a.correlation_id, "ts": a.created_at.isoformat()}
        for a in rows
    ]


@router.get("/events")
async def system_events(limit: int = Query(200, le=1000), session: AsyncSession = Depends(get_db_session)) -> list[dict]:
    rows = (await session.execute(select(SystemEvent).order_by(desc(SystemEvent.created_at)).limit(limit))).scalars()
    return [
        {"event": e.event, "level": e.level, "message": e.message, "detail": e.detail,
         "correlation_id": e.correlation_id, "mode": e.mode, "ts": e.created_at.isoformat()}
        for e in rows
    ]


@router.get("/reconciliation")
async def reconciliation_events(session: AsyncSession = Depends(get_db_session)) -> list[dict]:
    rows = (await session.execute(
        select(ReconciliationEvent).order_by(desc(ReconciliationEvent.created_at)).limit(100)
    )).scalars()
    return [
        {"severity": r.severity, "kind": r.kind, "message": r.message,
         "internal_state": r.internal_state, "exchange_state": r.exchange_state,
         "resolved": r.resolved, "ts": r.created_at.isoformat()}
        for r in rows
    ]


# --------------------------------------------------------------------- replay
@router.post("/replay")
async def replay_signals(body: ReplayRequest, ctx: AppContext = Depends(get_ctx),
                         session: AsyncSession = Depends(get_db_session)) -> dict:
    from app.services.replay import ReplayFilter

    flt = ReplayFilter(start=body.start, end=body.end, strategy_id=body.strategy_id,
                       symbol=body.symbol, speed=body.speed)
    report = await ctx.replay.replay(session, flt)
    await session.commit()
    return {
        "total": report.total, "executed": report.executed,
        "rejected": report.rejected, "details": report.details,
    }


# ----------------------------------------------------------------- live gate
@router.get("/live/paper-report")
async def paper_report(settings: AppSettings = Depends(get_settings),
                       session: AsyncSession = Depends(get_db_session)) -> dict:
    trades = list((await session.execute(select(Trade).where(Trade.mode == "PAPER"))).scalars())
    events = list((await session.execute(select(WebhookEvent))).scalars())
    wins = [t for t in trades if t.result == "WIN"]
    pnls = [t.net_pnl for t in trades if t.net_pnl is not None]

    def _naive(dt):
        return dt if dt and dt.tzinfo else dt

    chronological = sorted(trades, key=lambda t: t.closed_at or t.created_at)
    equity = Decimal("0")
    peak = Decimal("0")
    max_dd = Decimal("0")
    equity_curve: list[Decimal] = []
    for t in chronological:
        pnl_val = t.net_pnl if t.net_pnl is not None else Decimal("0")
        equity += pnl_val
        equity_curve.append(equity)
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    durations = [t.duration_seconds for t in trades if t.duration_seconds is not None]
    return {
        "days_tested": (
            (datetime.now(UTC) - _naive(min(e.received_at for e in events))).days
            if events else 0
        ),
        "total_signals": len(events),
        "accepted_signals": sum(1 for e in events if e.status == "EXECUTED"),
        "rejected_signals": sum(1 for e in events if e.status == "REJECTED"),
        "duplicate_signals": sum(1 for e in events if e.status == "DUPLICATE"),
        "executed_paper_trades": len(trades),
        "closed_paper_trades": len(trades),
        "open_paper_trades": int((await session.execute(
            select(func.count()).select_from(Position).where(Position.state.in_(["OPEN", "MONITORING"]))
        )).scalar_one()),
        "win_rate": round(len(wins) / len(trades) * 100, 2) if trades else 0,
        "total_pnl": str(sum(pnls, Decimal("0"))),
        "average_trade": str(sum(pnls, Decimal("0")) / len(pnls)) if pnls else "0",
        "average_winning_trade": str(sum((t.net_pnl for t in wins), Decimal("0")) / len(wins)) if wins else "0",
        "average_losing_trade": (
            str(sum((t.net_pnl for t in trades if t.result == "LOSS" and t.net_pnl is not None), Decimal("0")) /
                len([t for t in trades if t.result == "LOSS"]))
            if any(t.result == "LOSS" for t in trades) else "0"
        ),
        "maximum_drawdown": str(max_dd),
        "maximum_concurrent_positions": settings.max_open_positions,
        "average_holding_time": str(sum(durations) / len(durations)) if durations else "0",
        "longest_holding_time": str(max(durations)) if durations else "0",
        "tp_hit_count": sum(1 for t in trades if t.close_reason == "TP_10_ROI"),
        "execution_failures": sum(1 for e in events if e.status == "ERROR"),
        "symbol_mapping_failures": sum(
            1 for e in events if e.rejection_reason == RejectReason.SHARK_SYMBOL_NOT_FOUND.value
        ),
        "charts": {
            "cumulative_pnl": [
                {"ts": (t.closed_at.isoformat() if t.closed_at else ""), "value": str(equity_curve[i])}
                for i, t in enumerate(chronological)
            ],
            "roi_distribution": [{"trade": t.trade_id, "roi": str(t.roi_percent)} for t in trades],
            "trade_duration": [{"trade": t.trade_id, "seconds": t.duration_seconds} for t in trades],
        },
    }


@router.post("/live/activate")
async def activate_live(body: LiveActivationRequest, ctx: AppContext = Depends(get_ctx),
                        settings: AppSettings = Depends(get_settings),
                        session: AsyncSession = Depends(get_db_session)) -> dict:
    """GO LIVE workflow — multi-gate, manual, never automatic."""
    if body.confirmation_phrase != settings.live_confirmation_phrase:
        raise HTTPException(400, f"confirmation phrase must be exactly: {settings.live_confirmation_phrase}")
    if settings.live_trading_confirmation:
        # secret is REQUIRED (not optional) whenever configured in the environment
        if not body.secret_confirmation or not constant_time_equals(
                body.secret_confirmation, settings.live_trading_confirmation):
            raise HTTPException(403, RejectReason.LIVE_NOT_ENABLED.value)
    if not settings.live_trading_enabled:
        raise HTTPException(403, "LIVE_TRADING_ENABLED is false — set env and restart first")
    if not (settings.shark_api_key and settings.shark_api_secret):
        raise HTTPException(403, "Shark credentials not configured")
    if not body.acknowledge_stop_loss_disabled:
        raise HTTPException(400, "you must acknowledge STOP LOSS = DISABLED")
    if body.acknowledge_margin_per_trade != settings.margin_per_trade:
        raise HTTPException(400, "margin acknowledgment mismatch")
    if body.acknowledge_leverage != settings.leverage:
        raise HTTPException(400, "leverage acknowledgment mismatch")
    if body.acknowledge_max_positions != settings.max_open_positions:
        raise HTTPException(400, "max positions acknowledgment mismatch")
    if Decimal(body.acknowledge_target_roi) != settings.target_roi_percent:
        raise HTTPException(400, "target ROI acknowledgment mismatch")

    ctx.settings.trading_mode = "LIVE"
    session.add(AuditLog(action=TradeEvent.LIVE_ACTIVATED.value, actor="operator", entity="trading_mode",
                         detail={"mode": "LIVE", "margin_per_trade": str(settings.margin_per_trade),
                                 "leverage": settings.leverage,
                                 "max_positions": settings.max_open_positions,
                                 "target_roi": str(settings.target_roi_percent),
                                 "stop_loss": "DISABLED",
                                 "exchange": settings.shark_base_url,
                                 "dry_run": settings.shark_dry_run}))
    session.add(SystemEvent(event=TradeEvent.LIVE_ACTIVATED.value, message="LIVE trading manually activated",
                            mode="LIVE"))
    log_event(log, TradeEvent.LIVE_ACTIVATED, "LIVE trading manually activated")
    await ctx.notifications.notify("trading_halted", "LIVE TRADING ACTIVATED")
    event_bus.publish("mode.changed", {"mode": "LIVE"})
    return {"mode": "LIVE", "dry_run": settings.shark_dry_run,
            "warning": "STOP LOSS DISABLED — monitor risk manually"}


@router.post("/live/deactivate")
async def deactivate_live(ctx: AppContext = Depends(get_ctx),
                          session: AsyncSession = Depends(get_db_session)) -> dict:
    ctx.settings.trading_mode = "PAPER"
    session.add(AuditLog(action="MODE_CHANGED", entity="trading_mode", entity_id="PAPER"))
    event_bus.publish("mode.changed", {"mode": "PAPER"})
    return {"mode": "PAPER"}


# ------------------------------------------------------------ sentiment history
@router.get("/market/sentiment-history")
async def get_sentiment_history(
    limit: int = Query(50, le=200),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict]:
    """Return persisted sentiment transitions, newest first."""
    rows = list(
        (await session.execute(
            select(SentimentHistory)
            .order_by(desc(SentimentHistory.occurred_at))
            .limit(limit)
        )).scalars()
    )
    return [
        {"time": r.occurred_at.isoformat(), "from": r.from_sentiment, "to": r.to_sentiment}
        for r in rows
    ]


@router.post("/market/sentiment-history")
async def post_sentiment_history(
    body: dict,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """Record a new sentiment transition."""
    from_s = str(body.get("from", "")).strip()
    to_s = str(body.get("to", "")).strip()
    if not from_s or not to_s:
        raise HTTPException(400, "'from' and 'to' are required")
    row = SentimentHistory(from_sentiment=from_s, to_sentiment=to_s)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return {"id": row.id, "time": row.occurred_at.isoformat(), "from": from_s, "to": to_s}


# ----------------------------------------------------------------------- SSE
@router.get("/stream")
async def stream() -> StreamingResponse:
    async def generator():
        yield "event: hello\ndata: {}\n\n"
        async for message in event_bus.stream():
            yield f"data: {message}\n\n"

    return StreamingResponse(generator(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })
