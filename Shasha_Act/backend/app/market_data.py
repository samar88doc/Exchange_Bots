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


@dataclass
class DayKlineSnapshot:
    """Crypto market day candlestick (starts 00:00:00 UTC = 05:30:00 AM IST)."""
    symbol: str
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: Decimal | None
    start_time: datetime
    end_time: datetime | None = None
    received_at: datetime = datetime.now(UTC)


def parse_kline(data: Any, symbol: str | None = None) -> DayKlineSnapshot | None:
    """Parse 1d candlestick (kline) data from Shark's public WebSocket or REST API."""
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    if not isinstance(data, dict):
        return None

    # WebSocket format: {"e": "continuous_kline", "ps": "BTCUSDT", "k": {"t": 1792022400000, "o": "...", ...}}
    if "k" in data and isinstance(data["k"], dict):
        k = data["k"]
        sym = str(data.get("ps") or data.get("s") or symbol or "").upper()
        interval = str(k.get("i") or "")
        if interval and interval != "1d":
            return None
        o = _decimal(k.get("o"))
        h = _decimal(k.get("h"))
        low_value = _decimal(k.get("l"))
        c = _decimal(k.get("c"))
        v = _decimal(k.get("v"))
        t = _decimal(k.get("t"))
        T = _decimal(k.get("T"))
    else:
        # REST format: {"startTime": "1792022400000", "open": "...", "high": "...", "low": "...", "close": "...", "endTime": "..."}
        sym = str(symbol or data.get("symbol") or data.get("pair") or "").upper()
        o = _decimal(data.get("open"))
        h = _decimal(data.get("high"))
        low_value = _decimal(data.get("low"))
        c = _decimal(data.get("close"))
        v = _decimal(data.get("volume"))
        t = _decimal(data.get("startTime"))
        T = _decimal(data.get("endTime"))

    if not sym or o is None or o <= 0:
        return None

    start_dt = None
    if t is not None and t > 0:
        try:
            start_dt = datetime.fromtimestamp(float(t / Decimal("1000")), tz=UTC)
        except (ValueError, OverflowError, OSError):
            start_dt = None
    if start_dt is None:
        start_dt = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    end_dt = None
    if T is not None and T > 0:
        try:
            end_dt = datetime.fromtimestamp(float(T / Decimal("1000")), tz=UTC)
        except (ValueError, OverflowError, OSError):
            end_dt = None

    return DayKlineSnapshot(
        symbol=sym,
        open_price=o,
        high_price=h if h is not None and h > 0 else o,
        low_price=low_value if low_value is not None and low_value > 0 else o,
        close_price=c if c is not None and c > 0 else o,
        volume=v,
        start_time=start_dt,
        end_time=end_dt,
        received_at=datetime.now(UTC),
    )


class MarketDataService:
    def __init__(self, settings: Settings, shark: SharkClient, on_ticker: TickerCallback | None = None) -> None:
        self.settings = settings
        self.shark = shark
        self.on_ticker = on_ticker
        self.resolver = SymbolResolver()
        self.tickers: dict[str, TickerSnapshot] = {}
        self.day_klines: dict[str, DayKlineSnapshot] = {}
        self.last_success: datetime | None = None
        self.last_error: str = ""
        self.websocket_connected = False
        self.websocket_last_message: datetime | None = None
        self.websocket_error: str = ""
        self._last_metadata_fetch = 0.0

    async def refresh_contracts(self, *, force: bool = False) -> dict[str, ContractSpec]:
        if not force and self.resolver_contracts and time.monotonic() - self._last_metadata_fetch < 300:
            return self.resolver_contracts
        raw = await self.shark.exchange_info()
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

    def accept_kline(self, kline: DayKlineSnapshot) -> None:
        self.day_klines[kline.symbol] = kline

    async def accept_ticker(self, ticker: TickerSnapshot) -> None:
        self.tickers[ticker.symbol] = ticker
        self.last_success = ticker.received_at
        self.last_error = ""
        if ticker.source.startswith("SHARK_WS"):
            self.websocket_last_message = ticker.received_at

        # Real-time maintenance of Day % from 05:30 AM IST / 00:00 UTC
        now_utc = datetime.now(UTC)
        day_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        existing_kline = self.day_klines.get(ticker.symbol)
        if existing_kline is not None:
            if existing_kline.start_time < day_start:
                # Calendar day rollover
                self.day_klines[ticker.symbol] = DayKlineSnapshot(
                    symbol=ticker.symbol,
                    open_price=ticker.last_price,
                    high_price=ticker.last_price,
                    low_price=ticker.last_price,
                    close_price=ticker.last_price,
                    volume=Decimal("0"),
                    start_time=day_start,
                    end_time=day_start.replace(hour=23, minute=59, second=59, microsecond=999000),
                    received_at=now_utc,
                )
            else:
                existing_kline.close_price = ticker.last_price
                if ticker.last_price > existing_kline.high_price:
                    existing_kline.high_price = ticker.last_price
                if existing_kline.low_price <= 0 or (0 < ticker.last_price < existing_kline.low_price):
                    existing_kline.low_price = ticker.last_price
                existing_kline.received_at = now_utc

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
            if spec.contract_type in {"PERPETUAL", "TRADIFI_PERPETUAL"}
        ][: self.settings.market_data_max_subscriptions]

        semaphore = asyncio.Semaphore(25)
        await asyncio.gather(*(self._poll_symbol(symbol, semaphore) for symbol in chosen))

    async def _poll_symbol(self, symbol: str, semaphore: asyncio.Semaphore) -> None:
        """Fetch one symbol's ticker and market-day kline, ignoring individual failures."""
        async with semaphore:
            try:
                await self.get_ticker(symbol, refresh=True)
            except Exception:
                pass
            try:
                raw_kline = await self.shark.klines(symbol, interval="1d", limit=1)
                if isinstance(raw_kline, list) and raw_kline:
                    kline = parse_kline(raw_kline[0], symbol=symbol)
                    if kline is not None:
                        self.accept_kline(kline)
            except Exception:
                pass

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

            # Crypto Market Day stats (starts 00:00:00 UTC = 05:30:00 AM IST)
            kline = self.day_klines.get(ticker.symbol)
            day_open = None
            day_high = None
            day_low = None
            day_pct = None
            day_abs = None
            if kline is not None and kline.open_price > 0:
                day_open = kline.open_price
                day_high = max(kline.high_price, ticker.last_price)
                day_low = min(kline.low_price, ticker.last_price) if kline.low_price > 0 else ticker.last_price
                day_pct = ((ticker.last_price - day_open) / day_open) * Decimal("100")
                day_abs = ticker.last_price - day_open

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
                    "day_open": str(day_open) if day_open is not None else None,
                    "day_high": str(day_high) if day_high is not None else None,
                    "day_low": str(day_low) if day_low is not None else None,
                    "day_change_pct": str(day_pct) if day_pct is not None else None,
                    "day_change_abs": str(day_abs) if day_abs is not None else None,
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
            "note": "24h % is rolling 24-hour ticker; Day % is crypto market day change since 05:30 AM IST (00:00 UTC).",
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

                @client.on("kline")
                async def handle_kline(data: Any) -> None:
                    parsed = parse_kline(data)
                    if parsed is not None:
                        self.accept_kline(parsed)

                @client.event
                async def connect(*_args: Any, socket_client: Any = client) -> None:
                    self.websocket_connected = True
                    self.websocket_error = ""
                    symbols = [
                        name for name, spec in self.resolver_contracts.items()
                        if spec.contract_type in {"PERPETUAL", "TRADIFI_PERPETUAL"}
                    ][: self.settings.market_data_max_subscriptions]
                    topics = [f"{symbol.lower()}@ticker" for symbol in symbols] + [f"{symbol.lower()}@kline_1d" for symbol in symbols]
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
                    if spec.contract_type in {"PERPETUAL", "TRADIFI_PERPETUAL"}
                ][: self.settings.market_data_max_subscriptions]
                
                semaphore = asyncio.Semaphore(20)
                await asyncio.gather(*(self._poll_symbol(symbol, semaphore) for symbol in symbols))
            except Exception as exc:
                self.last_error = type(exc).__name__
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.settings.market_data_poll_seconds)
            except TimeoutError:
                pass
