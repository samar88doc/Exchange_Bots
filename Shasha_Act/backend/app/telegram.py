"""Telegram notifier with credential-safe errors and explicit retry classes."""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings


class TelegramError(RuntimeError):
    pass


class TelegramRetryableError(TelegramError):
    def __init__(self, message: str, retry_after: int | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(message)


class TelegramPermanentError(TelegramError):
    pass


def _money(value: Decimal | None) -> str:
    if value is None:
        return "unavailable"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _price_percent(base: Decimal | None, price: Decimal | None, *, above: bool) -> str:
    if base is None or price is None or base <= 0:
        return "n/a"
    delta = (price - base) / base * Decimal("100")
    if not above:
        delta = -delta
    return _money(delta)


def format_signal_message(event: Any, trade: Any | None) -> str:
    symbol = (trade.symbol if trade else event.symbol) or event.tradingview_symbol or "unresolved"
    signal_close = _money(event.signal_close)
    if trade is None:
        reason = event.status_reason or "The alert could not be safely queued."
        return (
            "⚠️ SHASHA signal not executed\n"
            f"Symbol: {symbol}\n"
            f"Signal close: {signal_close}\n"
            f"Status: {event.status}\n"
            f"Reason: {reason}"
        )

    base = trade.actual_entry or trade.planned_entry
    target1_pct = _price_percent(base, trade.target1_price, above=True)
    target2_pct = _price_percent(base, trade.target2_price, above=True)
    target3_pct = _price_percent(base, trade.target3_price, above=True)
    stop_pct = _price_percent(base, trade.initial_stop_price, above=False)
    if trade.status in {"REJECTED", "FAILED", "CANCELLED", "UNKNOWN", "PROTECTION_ERROR"}:
        title = "⚠️ SHASHA — execution needs attention"
    else:
        title = "🟢 SHASHA — LONG SIGNAL"

    lines = [
        title,
        f"Symbol: {trade.symbol} (TradingView: {trade.tradingview_symbol})",
        f"Buy Price (Previous Candle Close): {_money(trade.planned_entry)}",
        f"Signal close: {signal_close}",
        "Direction: LONG",
        f"Target 1 (+{target1_pct}% underlying): {_money(trade.target1_price)}",
        f"Target 2 (+{target2_pct}% underlying): {_money(trade.target2_price)}",
        f"Target 3 (+{target3_pct}% underlying): {_money(trade.target3_price)}",
        f"Initial SL (−{stop_pct}% underlying): {_money(trade.initial_stop_price)}",
        f"Active SL: {_money(trade.stop_price)} ({trade.stop_stage})",
        f"Margin: {_money(trade.margin)} USDT | Leverage: {trade.leverage}x",
        f"Status: {trade.status}",
    ]
    if trade.mode == "PAPER":
        lines.insert(1, "Mode: PAPER — simulated workflow; no private trade API order was sent.")
    if trade.actual_entry is not None:
        lines.insert(4, f"Actual average fill: {_money(trade.actual_entry)}")
    if trade.status in {"ENTRY_PENDING", "ENTRY_PARTIAL", "QUEUED", "SUBMITTING"}:
        lines[-1] = f"Status: {trade.status} (LIMIT entry; not an open position)"
    if trade.status_reason:
        lines.append(f"Detail: {trade.status_reason}")
    lines.append("Trailing SL: after T1 → entry before fees; after T2 → T1 price; close remaining at T3.")
    lines.append("Targets are underlying price moves, not leveraged account ROI. Fees, funding and slippage apply.")
    return "\n".join(lines)


class TelegramNotifier:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client
        self._owns_client = client is None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.settings.telegram_request_timeout_seconds)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def send_message(self, text: str) -> dict[str, Any]:
        if not self.settings.telegram_configured:
            raise TelegramRetryableError("TELEGRAM_NOT_CONFIGURED")
        # Telegram's Bot API requires this tokenized URL. Never log it or bubble httpx
        # exceptions whose repr/str may contain the request URL.
        endpoint = (
            "https://api.telegram.org/bot"
            + quote(self.settings.telegram_bot_token, safe=":")
            + "/sendMessage"
        )
        try:
            response = await (await self._http()).post(
                endpoint,
                json={"chat_id": self.settings.telegram_chat_id, "text": text},
            )
        except httpx.TimeoutException as exc:
            raise TelegramRetryableError("TELEGRAM_TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise TelegramRetryableError("TELEGRAM_NETWORK_ERROR") from exc
        if response.status_code == 429 or response.status_code >= 500:
            retry_after = None
            try:
                retry_after = int(response.json().get("parameters", {}).get("retry_after"))
            except (ValueError, TypeError, AttributeError):
                pass
            raise TelegramRetryableError("TELEGRAM_TEMPORARY_FAILURE", retry_after)
        if response.is_error:
            raise TelegramPermanentError(f"TELEGRAM_HTTP_{response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramRetryableError("TELEGRAM_INVALID_RESPONSE") from exc
        if not isinstance(data, dict) or not data.get("ok"):
            raise TelegramRetryableError("TELEGRAM_REPORTED_FAILURE")
        return data
