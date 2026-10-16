"""SHORT (SELL) signal support: parsing, levels, PAPER lifecycle, API and LIVE side mapping."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db import Base, make_engine
from app.domain import (
    ValidationProblem,
    compute_strategy_levels,
    directional_pnl,
    entry_side,
    exit_side,
    favorable_extreme,
    is_entry_fillable,
    is_stop_triggered,
    is_target_reached,
    next_trailing_stop,
    parse_alert,
    simulated_liquidation_price,
    split_partial_quantity,
)
from app.market_data import TickerSnapshot
from app.models import AuditEvent, Fill, Order, SignalEvent, Trade
from app.services.paper import PaperExecutionAdapter
from app.services.pnl import position_unrealized_pnl
from app.shark import SharkClient
from tests.conftest import valid_alert


def _ticker(symbol: str, price: str, *, seconds_ago: int = 0) -> TickerSnapshot:
    return TickerSnapshot(
        symbol=symbol,
        last_price=Decimal(price),
        change_24h_percent=Decimal("0"),
        volume_24h=Decimal("1"),
        high_24h=Decimal(price),
        low_24h=Decimal(price),
        source="SHARK_WS_LAST",
        received_at=datetime.now(UTC) - timedelta(seconds=seconds_ago),
    )


# ───────────────────────────── pure domain rules ─────────────────────────────


def test_sell_alert_is_parsed_as_a_short_entry_without_errors() -> None:
    body = json.dumps(
        {
            "strategy": "SHASHA",
            "event_id": "short-json-1",
            "symbol": "BTCUSDT.P",
            "direction": "SHORT",
            "timeframe": "15",
            "bar_time": "2026-10-15T10:15:00Z",
            "signal_close": "101.00",
            "entry_price": "101.50",
        }
    ).encode()
    parsed = parse_alert(body)
    assert parsed.errors == ()
    assert parsed.direction == "SHORT"
    assert parsed.symbol == "BTCUSDT"
    assert parsed.strategy == "SHASHA"

    by_action = parse_alert(
        json.dumps({"symbol": "BTCUSDT.P", "action": "SELL", "price": "101.5", "signal_close": "101", "entry_price": "101.5", "bar_time": "2026-10-15T10:15:00Z"}).encode()
    )
    assert by_action.errors == ()
    assert by_action.direction == "SHORT"
    # A SELL action is never mistaken for a strategy name.
    assert by_action.strategy == "SHASHA"


def test_long_remains_the_default_and_buy_still_maps_to_long() -> None:
    parsed = parse_alert(json.dumps(valid_alert()).encode())
    assert parsed.errors == ()
    assert parsed.direction == "LONG"
    buy = parse_alert(
        json.dumps({"symbol": "BTCUSDT.P", "action": "BUY", "signal_close": "1", "entry_price": "1", "bar_time": "2026-10-15T10:15:00Z"}).encode()
    )
    assert buy.errors == () and buy.direction == "LONG"


def test_exit_actions_and_direction_conflicts_are_rejected_not_flipped() -> None:
    base = {"symbol": "BTCUSDT.P", "signal_close": "1", "entry_price": "1", "bar_time": "2026-10-15T10:15:00Z"}
    close = parse_alert(json.dumps({**base, "action": "CLOSE"}).encode())
    assert "UNSUPPORTED_ACTION" in close.errors
    assert close.direction != "SHORT"

    conflict = parse_alert(json.dumps({**base, "direction": "LONG", "action": "SELL"}).encode())
    assert "DIRECTION_ACTION_CONFLICT" in conflict.errors

    unknown = parse_alert(json.dumps({**base, "direction": "FLAT"}).encode())
    assert "UNSUPPORTED_DIRECTION" in unknown.errors


def test_short_levels_mirror_long_levels_with_adverse_rounding() -> None:
    long_levels = compute_strategy_levels(
        Decimal("100"), Decimal("0.01"), Decimal("2"), Decimal("2"), direction="LONG"
    )
    short_levels = compute_strategy_levels(
        Decimal("100"), Decimal("0.01"), Decimal("2"), Decimal("2"), direction="SHORT"
    )
    assert long_levels.stop == Decimal("98.00") and long_levels.target1 == Decimal("102.00")
    assert short_levels.stop == Decimal("102.00") and short_levels.target1 == Decimal("98.00")

    # Non-tick-aligned entry: both sides round each level toward the conservative side.
    odd_long = compute_strategy_levels(Decimal("0.00239"), Decimal("0.00001"), Decimal("2"), Decimal("2"), direction="LONG")
    odd_short = compute_strategy_levels(Decimal("0.00239"), Decimal("0.00001"), Decimal("2"), Decimal("2"), direction="SHORT")
    assert odd_long.stop == Decimal("0.00235")  # 0.0023422 rounded up
    assert odd_short.stop == Decimal("0.00243")  # 0.0024378 rounded down
    assert odd_short.target1 == Decimal("0.00234")  # 0.0023422 rounded down, below entry

    with_targets = compute_strategy_levels(
        Decimal("100"), Decimal("0.01"), Decimal("2"), Decimal("2"), Decimal("3"), Decimal("4"), direction="SHORT"
    )
    assert with_targets.target2 == Decimal("97.00")
    assert with_targets.target3 == Decimal("96.00")


def test_short_levels_refuse_a_tick_that_collapses_the_risk_geometry() -> None:
    with pytest.raises(ValidationProblem):
        compute_strategy_levels(Decimal("100"), Decimal("5"), Decimal("2"), Decimal("2"), direction="SHORT")


def test_short_trailing_stop_ratchets_down_and_never_crosses_the_price() -> None:
    first = next_trailing_stop(
        high_water=Decimal("90"),
        current_stop=Decimal("100"),
        floor_price=Decimal("100"),
        trailing_percent=Decimal("1"),
        price_tick=Decimal("0.01"),
        reference_price=Decimal("90.5"),
        direction="SHORT",
    )
    assert first == Decimal("90.90")  # 90 × 1.01 is already on the 0.01 tick
    lower = next_trailing_stop(
        high_water=Decimal("85"),
        current_stop=first,
        floor_price=Decimal("100"),
        trailing_percent=Decimal("1"),
        price_tick=Decimal("0.01"),
        reference_price=Decimal("84"),
        direction="SHORT",
    )
    assert lower == Decimal("85.85") and lower < first
    # Price bouncing back up must never move the short stop up.
    bounce = next_trailing_stop(
        high_water=Decimal("85"),
        current_stop=lower,
        floor_price=Decimal("100"),
        trailing_percent=Decimal("1"),
        price_tick=Decimal("0.01"),
        reference_price=Decimal("94"),
        direction="SHORT",
    )
    assert bounce == lower
    # A stop is never armed at or below the live price: it is lifted to just above it.
    clamped = next_trailing_stop(
        high_water=Decimal("90"),
        current_stop=None,
        floor_price=Decimal("100"),
        trailing_percent=Decimal("1"),
        price_tick=Decimal("0.01"),
        reference_price=Decimal("91"),
        direction="SHORT",
    )
    assert clamped == Decimal("91")


def test_directional_helpers_cover_sides_fills_stops_targets_and_pnl() -> None:
    assert (entry_side("LONG"), exit_side("LONG"), entry_side("SHORT"), exit_side("SHORT")) == (
        "BUY",
        "SELL",
        "SELL",
        "BUY",
    )
    assert directional_pnl("SHORT", Decimal("100"), Decimal("95"), Decimal("2")) == Decimal("10")
    assert directional_pnl("SHORT", Decimal("100"), Decimal("105"), Decimal("2")) == Decimal("-10")
    assert directional_pnl("LONG", Decimal("100"), Decimal("105"), Decimal("2")) == Decimal("10")
    assert position_unrealized_pnl("SHORT", Decimal("100"), Decimal("99"), Decimal("3")) == Decimal("3")

    assert is_entry_fillable("SHORT", Decimal("100"), Decimal("100.5"))
    assert not is_entry_fillable("SHORT", Decimal("100"), Decimal("99"))
    assert is_stop_triggered("SHORT", Decimal("102"), Decimal("102.1"))
    assert not is_stop_triggered("SHORT", Decimal("102"), Decimal("101"))
    assert is_target_reached("SHORT", Decimal("98"), Decimal("97.9"))
    assert not is_target_reached("SHORT", Decimal("98"), Decimal("98.1"))
    assert favorable_extreme("SHORT", Decimal("100"), Decimal("97")) == Decimal("97")
    assert favorable_extreme("LONG", Decimal("100"), Decimal("97")) == Decimal("100")
    with pytest.raises(ValidationProblem):
        directional_pnl("FLAT", Decimal("1"), Decimal("1"), Decimal("1"))


def test_short_liquidation_estimate_sits_above_entry() -> None:
    short_liq = simulated_liquidation_price("SHORT", Decimal("100"), 10)
    long_liq = simulated_liquidation_price("LONG", Decimal("100"), 10)
    assert short_liq > Decimal("100") > long_liq


# ───────────────────────────── PAPER engine lifecycle ────────────────────────


async def _open_short_trade(session_factory, adapter: PaperExecutionAdapter) -> str:
    async with session_factory() as session:
        event = SignalEvent(
            id="short-event-1",
            idempotency_key="short-key-1",
            supplied_event_id="short-event-1",
            strategy="SHASHA",
            tradingview_symbol="BTCUSDT.P",
            symbol="BTCUSDT",
            direction="SHORT",
            timeframe="15",
            bar_time=datetime.now(UTC),
            signal_close=Decimal("101"),
            entry_price=Decimal("100"),
            raw_payload={},
            raw_body_hash="b" * 64,
            status="ENTRY_PENDING",
            status_reason="",
            mode="PAPER",
        )
        levels = compute_strategy_levels(
            Decimal("100"), Decimal("0.01"), Decimal("2"), Decimal("2"), direction="SHORT"
        )
        partial = split_partial_quantity(Decimal("10"), Decimal("1"), Decimal("1"), Decimal("50"))
        trade = Trade(
            id="short-trade-1",
            event_id=event.id,
            mode="PAPER",
            tradingview_symbol="BTCUSDT.P",
            symbol="BTCUSDT",
            direction="SHORT",
            status="ENTRY_PENDING",
            status_reason="",
            planned_entry=Decimal("100"),
            signal_close=Decimal("101"),
            quantity=Decimal("10"),
            remaining_quantity=Decimal("10"),
            target1_quantity=partial.partial,
            margin=Decimal("30"),
            notional=Decimal("300"),
            leverage=10,
            stop_price=levels.stop,
            initial_stop_price=levels.stop,
            target1_price=levels.target1,
            secondary_targets_enabled=False,
            trailing_enabled=True,
            trailing_stop_percent=Decimal("1"),
            partial_close_percent=Decimal("50"),
            price_tick=Decimal("0.01"),
            entry_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        session.add(event)
        session.add(trade)
        await session.flush()
        await adapter.create_limit_entry(session, trade)
        await session.commit()
        return trade.id


async def _tick(session_factory, adapter: PaperExecutionAdapter, trade_id: str, price: str) -> None:
    async with session_factory() as session:
        trade = await session.get(Trade, trade_id)
        event = await session.get(SignalEvent, trade.event_id)
        await adapter.process_ticker(session, trade, _ticker("BTCUSDT", price), event)
        await session.commit()


@pytest.mark.asyncio
async def test_paper_short_lifecycle_fills_up_takes_target_down_and_trails_down(tmp_path) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'short.sqlite3'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(_env_file=None, environment="test", run_workers=False, run_market_stream=False, run_market_poll=False)
    adapter = PaperExecutionAdapter(settings)
    trade_id = await _open_short_trade(sessions, adapter)

    # A SHORT limit sell does not fill while price is below the limit...
    await _tick(sessions, adapter, trade_id, "99.00")
    async with sessions() as session:
        trade = await session.get(Trade, trade_id)
        assert trade.status == "ENTRY_PENDING"

    # ...and fills once price rises to it.
    await _tick(sessions, adapter, trade_id, "100.50")
    async with sessions() as session:
        trade = await session.get(Trade, trade_id)
        assert trade.status == "OPEN"
        assert trade.actual_entry == Decimal("100")
        assert trade.stop_price == Decimal("102.00")
        entry_order = await session.scalar(select(Order).where(Order.trade_id == trade_id, Order.kind == "ENTRY"))
        stop_order = await session.scalar(select(Order).where(Order.trade_id == trade_id, Order.kind == "STOP_LOSS"))
        assert entry_order.side == "SELL" and entry_order.status == "FILLED"
        assert stop_order.side == "BUY" and stop_order.reduce_only is True
        entry_fill = await session.scalar(select(Fill).where(Fill.trade_id == trade_id, Fill.entry_exit == "ENTRY"))
        assert entry_fill.side == "SELL"

    # Price falls through Target 1 (98): 5 units close at 98 for +10 gross, stop drops to break-even
    # and then immediately trails below the new low.
    await _tick(sessions, adapter, trade_id, "97.90")
    async with sessions() as session:
        trade = await session.get(Trade, trade_id)
        assert trade.status == "PARTIALLY_CLOSED"
        assert trade.remaining_quantity == Decimal("5")
        assert trade.gross_realized_pnl == Decimal("10")
        # The trailing step runs in the same tick (as for a LONG), so the stop is already trailing.
        assert trade.stop_stage == "TRAILING"
        assert trade.stop_price == Decimal("98.88")  # ceil(97.9 × 1.01), below entry, above price

    # A further drop lowers the stop; a bounce that does not reach it leaves the stop unchanged.
    await _tick(sessions, adapter, trade_id, "95.00")
    await _tick(sessions, adapter, trade_id, "95.50")
    async with sessions() as session:
        trade = await session.get(Trade, trade_id)
        assert trade.stop_price == Decimal("95.95")
        assert trade.trailing_high_price == Decimal("95.00")
        assert trade.status == "PARTIALLY_CLOSED"

    # Rising through the trailing stop closes the runner at the observed price (worse than the stop).
    await _tick(sessions, adapter, trade_id, "96.00")
    async with sessions() as session:
        trade = await session.get(Trade, trade_id)
        assert trade.status == "CLOSED"
        assert trade.exit_price == Decimal("96")
        assert trade.gross_realized_pnl == Decimal("30")  # 2×5 at Target 1 + 4×5 at the trailing exit
        assert trade.net_realized_pnl < trade.gross_realized_pnl
        assert trade.net_realized_pnl > 0
        assert trade.stop_stage == "TRAILING"
        assert "SIMULATED_TRAILING_STOP" in trade.status_reason
        audit_actions = set(
            (await session.scalars(select(AuditEvent.action).where(AuditEvent.entity_id == trade_id))).all()
        )
        assert {"PAPER_ENTRY_FILLED", "PAPER_TARGET_1_FILLED", "PAPER_TRAILING_STOP_UPDATED", "PAPER_STOP_TRIGGERED"} <= audit_actions
    await engine.dispose()


@pytest.mark.asyncio
async def test_paper_short_stop_triggers_on_a_rise_with_the_worse_observed_fill(tmp_path) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'short-stop.sqlite3'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(_env_file=None, environment="test", run_workers=False, run_market_stream=False, run_market_poll=False)
    adapter = PaperExecutionAdapter(settings)
    trade_id = await _open_short_trade(sessions, adapter)
    await _tick(sessions, adapter, trade_id, "100.00")

    # Price gaps well above the 102.00 stop: the loss uses the observed price, not the stop.
    await _tick(sessions, adapter, trade_id, "102.50")
    async with sessions() as session:
        trade = await session.get(Trade, trade_id)
        assert trade.status == "CLOSED"
        assert trade.exit_price == Decimal("102.50")
        assert trade.gross_realized_pnl == Decimal("-25")
        assert "SIMULATED_STOP" in trade.status_reason
    await engine.dispose()


# ───────────────────────────── webhook / API ─────────────────────────────────


def test_webhook_accepts_a_sell_alert_and_rejects_a_close_alert(
    test_client: TestClient, dashboard_headers: dict[str, str], webhook_url: str
) -> None:
    sell = {
        "strategy": "SHASHA",
        "event_id": "sell-api-1",
        "symbol": "BTCUSDT.P",
        "action": "SELL",
        "timeframe": "15",
        "bar_time": "2026-10-15T10:15:00Z",
        "signal_close": "101.00",
        "entry_price": "101.50",
    }
    accepted = test_client.post(webhook_url, json=sell)
    assert accepted.status_code == 202
    assert accepted.json()["status"] in ("QUEUED", "ENTRY_PENDING")

    close = test_client.post(webhook_url, json={**sell, "event_id": "close-api-1", "action": "CLOSE"})
    assert close.status_code == 202
    assert close.json()["status"] == "REJECTED"

    events = {item["supplied_event_id"]: item for item in test_client.get("/api/events", headers=dashboard_headers).json()["items"]}
    assert events["sell-api-1"]["direction"] == "SHORT"
    assert "UNSUPPORTED_ACTION" in events["close-api-1"]["status_reason"]
    history = test_client.get("/api/history", headers=dashboard_headers).json()["items"]
    assert [item["direction"] for item in history] == ["SHORT"]

    settings = test_client.get("/api/settings", headers=dashboard_headers).json()
    assert settings["strategy"]["supported_directions"] == ["LONG", "SHORT"]
    assert "SHORT (SELL)" in settings["strategy"]["description"]


def test_trade_risk_edit_validates_short_geometry(
    test_client: TestClient, dashboard_headers: dict[str, str]
) -> None:
    session_factory = test_client.app.state.session_factory
    now = datetime.now(UTC)

    async def seed() -> None:
        async with session_factory() as session:
            event = SignalEvent(
                id="risk-short-event",
                idempotency_key="risk-short-key",
                supplied_event_id="risk-short-event",
                strategy="SHASHA",
                tradingview_symbol="BTCUSDT.P",
                symbol="BTCUSDT",
                direction="SHORT",
                timeframe="15",
                bar_time=now,
                signal_close=Decimal("101"),
                entry_price=Decimal("100"),
                raw_payload={},
                raw_body_hash="c" * 64,
                status="OPEN",
                status_reason="",
                mode="PAPER",
            )
            trade = Trade(
                id="risk-short-trade",
                event_id=event.id,
                mode="PAPER",
                tradingview_symbol="BTCUSDT.P",
                symbol="BTCUSDT",
                direction="SHORT",
                status="OPEN",
                status_reason="",
                planned_entry=Decimal("100"),
                signal_close=Decimal("101"),
                actual_entry=Decimal("100"),
                quantity=Decimal("10"),
                remaining_quantity=Decimal("10"),
                target1_quantity=Decimal("5"),
                margin=Decimal("30"),
                notional=Decimal("300"),
                leverage=10,
                stop_price=Decimal("102"),
                initial_stop_price=Decimal("102"),
                target1_price=Decimal("98"),
                secondary_targets_enabled=False,
                trailing_enabled=True,
                trailing_stop_percent=Decimal("1"),
                partial_close_percent=Decimal("50"),
                price_tick=Decimal("0.01"),
                protection_state="SIMULATED_ACTIVE",
                opened_at=now,
            )
            session.add_all([event, trade])
            await session.commit()

    test_client.portal.call(seed)
    # Fresh last price 99: a SHORT stop must sit ABOVE it and Target 1 must sit BELOW entry.
    test_client.app.state.context.market.tickers["BTCUSDT"] = _ticker("BTCUSDT", "99")
    url = "/api/trades/risk-short-trade/risk"
    phrase = {"confirmation_phrase": "CONFIRM RISK CHANGE"}

    stop_below_price = test_client.patch(url, headers=dashboard_headers, json={**phrase, "stop_price": "98.5"})
    assert stop_below_price.status_code == 422
    target_above_entry = test_client.patch(url, headers=dashboard_headers, json={**phrase, "target1_price": "101"})
    assert target_above_entry.status_code == 422

    valid = test_client.patch(url, headers=dashboard_headers, json={**phrase, "stop_price": "101.5", "target1_price": "97.5"})
    assert valid.status_code == 200, valid.text
    payload = valid.json()["trade"]
    assert payload["direction"] == "SHORT"
    assert payload["stop_price"] == "101.5"
    assert payload["target1_price"] == "97.5"
    assert payload["unrealized_pnl"] == "10"  # (100 − 99) × 10 units on the short


# ───────────────────────────── LIVE side mapping ─────────────────────────────


@pytest.mark.asyncio
async def test_shark_client_sends_sell_to_open_a_short_and_buy_to_close_it() -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        shark_base_url="https://shark.test",
        shark_api_key="TEST_ONLY_API_KEY",
        shark_api_secret="TEST_ONLY_API_SECRET",
    )
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content or b"{}"))
        return httpx.Response(200, json={"data": {"clientOrderId": "x"}})

    client = httpx.AsyncClient(base_url=settings.shark_base_url, transport=httpx.MockTransport(handler))
    shark = SharkClient(settings, client, clock_ms=lambda: 123)
    try:
        await shark.place_limit_order("BTCUSDT", Decimal("1"), Decimal("100"), side="SELL")
        await shark.place_reduce_only_market("BTCUSDT", Decimal("1"), side="BUY")
        await shark.place_limit_buy("BTCUSDT", Decimal("1"), Decimal("100"))
        with pytest.raises(ValueError):
            await shark.place_limit_order("BTCUSDT", Decimal("1"), Decimal("1"), side="HOLD")
    finally:
        await client.aclose()

    assert sent[0]["side"] == "SELL" and sent[0]["reduceOnly"] is False and sent[0]["type"] == "LIMIT"
    assert sent[1]["side"] == "BUY" and sent[1]["reduceOnly"] is True and sent[1]["type"] == "MARKET"
    assert sent[2]["side"] == "BUY"  # backwards-compatible LONG entry
    assert len(sent) == 3  # the invalid side never reached the wire


def test_direction_is_stored_on_the_reserved_trade_for_short(test_client: TestClient, webhook_url: str) -> None:
    response = test_client.post(
        webhook_url,
        json={
            "strategy": "SHASHA",
            "event_id": "reserve-short-1",
            "symbol": "BTCUSDT.P",
            "direction": "SHORT",
            "timeframe": "15",
            "bar_time": "2026-10-15T10:15:00Z",
            "signal_close": "101.00",
            "entry_price": "101.50",
        },
    )
    assert response.status_code == 202

    session_factory = test_client.app.state.session_factory

    async def fetch() -> str:
        async with session_factory() as session:
            trade = await session.scalar(select(Trade).where(Trade.symbol == "BTCUSDT"))
            assert trade is not None
            return trade.direction

    assert test_client.portal.call(fetch) == "SHORT"
