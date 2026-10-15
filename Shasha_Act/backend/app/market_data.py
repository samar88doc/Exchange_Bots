"""Shark REST bootstrap plus documented public Socket.IO ticker stream."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.config import Settings
from app.shark import SharkApiError, SharkClient
from app.symbols import ContractSpec, SymbolResolver, parse_exchange_info

log = logging.getLogger("shasha_act.market")
TickerCallback = Callable[["TickerSnapshot"], Awaitable[None]]


@dataclass(frozen=True)
class TickerSnapshot:
    symbol: str
    last_price: Decimal
    change_24h_percent: Decimal | None
    volume_24h: Decimal | None
    high_24h: Decimal | None
    low_24h: Decimal | None
    source: str
    received_at: datetime
    exchange_time: datetime | None = None

    def is_stale(self, stale_after_seconds: int, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        received = self.received_at
        if received.tzinfo is None:
            received = received.replace(tzinfo=UTC)
        return (current - received).total_seconds() > stale_after_seconds


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def parse_ticker(data: Any, source: str = "SHARK_LAST") -> TickerSnapshot | None:
    """Parse Shark's documented 24hrTicker fields (`s`, `c`, `P`, `v`, `h`, `l`, `E`)."""
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    if not isinstance(data, dict):
        return None
    symbol = str(data.get("s") or "").upper()
    last = _decimal(data.get("c"))
    if not symbol or last is None or last <= 0:
        return None
    timestamp = _decimal(data.get("E"))
    exchange_time = None
    if timestamp is not None and timestamp > 0:
        try:
            exchange_time = datetime.fromtimestamp(float(timestamp / Decimal("1000")), tz=UTC)
        except (ValueError, OverflowError, OSError):
            exchange_time = None
    return TickerSnapshot(
        symbol=symbol,
        last_price=last,
        change_24h_percent=_decimal(data.get("P")),
        volume_24h=_decimal(data.get("v")),
        high_24h=_decimal(data.get("h")),
        low_24h=_decimal(data.get("l")),
        source=source,
        received_at=datetime.now(UTC),
        exchange_time=exchange_time,
    )


class MarketDataService:
    def __init__(self, settings: Settings, shark: SharkClient, on_ticker: TickerCallback | None = None) -> None:
        self.settings = settings
        self.shark = shark
        self.on_ticker = on_ticker
        self.resolver = SymbolResolver()
        self.tickers: dict[str, TickerSnapshot] = {}
        self.last_success: datetime | None = None
        self.last_error: str = ""
        self.websocket_connected = False
        self.websocket_last_message: datetime | None = None
        self.websocket_error: str = ""
        self._last_metadata_fetch = 0.0

    async def refresh_contracts(self, *, force: bool = False) -> dict[str, ContractSpec]:
        if not force and self.resolver_contracts and time.monotonic() - self._last_metadata_fetch < 300:
            return self.resolver_contracts
        raw = await self.shark.exchange_info("USDT")
        contracts = parse_exchange_info(raw)
        if not contracts:
            self.last_error = "SHARK_EXCHANGE_INFO_EMPTY_OR_UNRECOGNIZED"
            raise SharkApiError(200, "EXCHANGE_INFO_UNAVAILABLE")
        self.resolver.replace(contracts)
        self._last_metadata_fetch = time.monotonic()
        self.last_success = datetime.now(UTC)
        self.last_error = ""
        return contracts

    @property
    def resolver_contracts(self) -> dict[str, ContractSpec]:
        return self.resolver.all()

    async def get_ticker(self, symbol: str, *, refresh: bool = True) -> TickerSnapshot | None:
        cached = self.tickers.get(symbol.upper())
        if cached and not cached.is_stale(self.settings.market_data_stale_seconds):
            return cached
        if not refresh:
            return cached
        try:
            result = await self.shark.ticker_24h(symbol)
            snapshot = parse_ticker(result, source="SHARK_REST_LAST")
            if snapshot is None:
                self.last_error = "SHARK_TICKER_RESPONSE_UNAVAILABLE"
                return None
            await self.accept_ticker(snapshot)
            return snapshot
        except Exception as exc:  # network details are not sent to user-facing logs
            self.last_error = type(exc).__name__
            return cached if cached and not cached.is_stale(self.settings.market_data_stale_seconds) else None

    async def accept_ticker(self, ticker: TickerSnapshot) -> None:
        self.tickers[ticker.symbol] = ticker
        self.last_success = ticker.received_at
        self.last_error = ""
        if ticker.source.startswith("SHARK_WS"):
            self.websocket_last_message = ticker.received_at
        if self.on_ticker is not None:
            await self.on_ticker(ticker)

    async def rest_bootstrap(self, symbols: list[str] | None = None) -> None:
        if not self.resolver_contracts:
            try:
                await self.refresh_contracts(force=True)
            except Exception:
                return
        chosen = symbols or [
            name for name, spec in self.resolver_contracts.items()
            if spec.quote_asset == "USDT" and spec.contract_type == "PERPETUAL"
        ]
        for symbol in chosen[: self.settings.market_data_max_subscriptions]:
            await self.get_ticker(symbol, refresh=True)

    def health(self) -> str:
        now = datetime.now(UTC)
        fresh = self.last_success and (now - self.last_success).total_seconds() <= self.settings.market_data_stale_seconds
        if fresh and self.websocket_connected:
            return "CONNECTED"
        if fresh:
            return "REST_FALLBACK"
        if self.last_error:
            return "DEGRADED"
        return "DISCONNECTED"

    def state(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        items = []
        for ticker in self.tickers.values():
            if ticker.is_stale(self.settings.market_data_stale_seconds, now):
                continue
            spec = self.resolver_contracts.get(ticker.symbol)
            base_asset = spec.base_asset if spec else (ticker.symbol[:-4] if ticker.symbol.endswith("USDT") else ticker.symbol)
            quote_asset = spec.quote_asset if spec else "USDT"
            pct = ticker.change_24h_percent
            change_abs = None
            if pct is not None and ticker.last_price > 0:
                # Calculate absolute change: p = last - (last / (1 + P/100))
                divisor = Decimal("1") + (pct / Decimal("100"))
                if divisor > 0:
                    open_approx = ticker.last_price / divisor
                    change_abs = ticker.last_price - open_approx

            items.append(
                {
                    "symbol": ticker.symbol,
                    "base_asset": base_asset,
                    "quote_asset": quote_asset,
                    "last_price": str(ticker.last_price),
                    "price_label": "Last price",
                    "change_24h_percent": (
                        str(ticker.change_24h_percent) if ticker.change_24h_percent is not None else None
                    ),
                    "change_abs": str(change_abs) if change_abs is not None else None,
                    "volume_24h": str(ticker.volume_24h) if ticker.volume_24h is not None else None,
                    "high_24h": str(ticker.high_24h) if ticker.high_24h is not None else None,
                    "low_24h": str(ticker.low_24h) if ticker.low_24h is not None else None,
                    "status": "ok",
                    "source": ticker.source,
                    "updated_at": ticker.received_at.isoformat(),
                }
            )
        return {
            "items": items,
            "health": self.health(),
            "stale_after_seconds": self.settings.market_data_stale_seconds,
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "websocket_connected": self.websocket_connected,
            "websocket_last_message": (
                self.websocket_last_message.isoformat() if self.websocket_last_message else None
            ),
            "error": self.last_error or self.websocket_error,
            "note": "Change is Shark 24-hour ticker change; prices are last prices, not mark prices.",
        }

    async def run_public_stream(self, stop: asyncio.Event) -> None:
        """Connect to the documented public Socket.IO stream with bounded backoff."""
        delay = 1.0
        while not stop.is_set():
            client = None
            try:
                await self.refresh_contracts()
                import socketio

                client = socketio.AsyncClient(reconnection=False, logger=False, engineio_logger=False)

                @client.on("24hrTicker")
                async def handle_ticker(data: Any) -> None:
                    parsed = parse_ticker(data, source="SHARK_WS_LAST")
                    if parsed is not None:
                        await self.accept_ticker(parsed)

                @client.event
                async def connect(*_args: Any, socket_client: Any = client) -> None:
                    self.websocket_connected = True
                    self.websocket_error = ""
                    symbols = [
                        name for name, spec in self.resolver_contracts.items()
                        if spec.quote_asset == "USDT" and spec.contract_type == "PERPETUAL"
                    ][: self.settings.market_data_max_subscriptions]
                    topics = [f"{symbol.lower()}@ticker" for symbol in symbols]
                    if topics:
                        await socket_client.emit("subscribe", {"params": topics})

                @client.event
                async def disconnect(*_args: Any) -> None:
                    self.websocket_connected = False

                await client.connect(
                    self.settings.shark_public_ws_url,
                    transports=["websocket"],
                    wait_timeout=10,
                )
                delay = 1.0
                await client.wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.websocket_connected = False
                self.websocket_error = type(exc).__name__
                log.warning("Shark public market stream unavailable (%s)", type(exc).__name__)
            finally:
                try:
                    if client is not None and client.connected:
                        await client.disconnect()
                except Exception:
                    pass
            if not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
                delay = min(delay * 2, 60.0)

    async def run_rest_fallback(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                if not self.resolver_contracts:
                    await self.refresh_contracts(force=True)
                symbols = [
                    name for name, spec in self.resolver_contracts.items()
                    if spec.quote_asset == "USDT" and spec.contract_type == "PERPETUAL"
                ][: self.settings.market_data_max_subscriptions]
                for symbol in symbols:
                    await self.get_ticker(symbol, refresh=True)
            except Exception as exc:
                self.last_error = type(exc).__name__
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.settings.market_data_poll_seconds)
            except TimeoutError:
                pass
