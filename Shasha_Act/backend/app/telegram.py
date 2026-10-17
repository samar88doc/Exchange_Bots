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


def format_signal_message(
    event: Any,
    trade: Any | None,
    *,
    action: str | None = None,
    payload: dict[str, Any] | None = None,
) -> str:
    """Return an HTML-formatted Telegram message (use parse_mode=HTML when sending)."""
    symbol = (trade.symbol if trade else event.symbol) or event.tradingview_symbol or "unresolved"

    # ── Trade not created / rejected ──────────────────────────────────────────
    if trade is None:
        reason = event.status_reason or "The alert could not be safely queued."
        return (
            "⚠️ <b>SHASHA — Signal Not Executed</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>Symbol:</b>  {symbol}\n"
            f"❌ <b>Status:</b>  {event.status}\n"
            f"📋 <b>Reason:</b>  {reason}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<i>📚 For educational purposes only.</i>"
        )

    direction = (trade.direction or "LONG").upper()
    is_long = direction == "LONG"
    dir_emoji = "🟢" if is_long else "🔴"
    dir_label = "LONG  📈" if is_long else "SHORT  📉"
    mode_line = "\n🧪 <i>PAPER — simulated, no real order sent.</i>" if trade.mode == "PAPER" else ""

    act = (action or "accepted").upper()

    # ── Distinct Lifecycle Event: Position Opened / Order Filled ──────────────
    if act == "OPEN":
        entry = _money(trade.actual_entry or trade.planned_entry)
        qty = _money(trade.quantity or trade.remaining_quantity)
        sl = _money(trade.stop_price or trade.initial_stop_price)
        t1 = _money(trade.target1_price)
        return (
            f"⚡ <b>SHASHA — POSITION OPEN ({direction})</b>{mode_line}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>Symbol</b>     :  <code>{symbol}</code>\n"
            f"📊 <b>Direction</b>  :  <b>{dir_label}</b>\n"
            f"💰 <b>Fill Price</b> :  <code>{entry}</code>\n"
            f"📦 <b>Quantity</b>   :  <code>{qty}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>Target 1</b>   :  <code>{t1}</code>\n"
            f"🛑 <b>Stop Loss</b>  :  <code>{sl}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<i>📚 For educational purposes only.</i>"
        )

    # ── Distinct Lifecycle Event: Target Hit ──────────────────────────────────
    if act.startswith("TARGET_HIT") or act.startswith("TARGET_"):
        target_num = "1"
        if ":" in act:
            try:
                target_num = str(int(act.split(":")[1]) + 1)
            except (ValueError, IndexError):
                target_num = act.split(":")[1]
        elif "_" in act:
            target_num = act.split("_")[1]
        target_attr = f"target{target_num}_price"
        target_price = _money(getattr(trade, target_attr, None) or trade.target1_price)
        current_sl = _money(trade.stop_price or trade.initial_stop_price)
        return (
            f"🎯 <b>SHASHA — TARGET {target_num} HIT! 🚀</b>{mode_line}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>Symbol</b>     :  <code>{symbol}</code>\n"
            f"📊 <b>Direction</b>  :  <b>{dir_label}</b>\n"
            f"🎯 <b>Target {target_num}</b> :  <code>{target_price}</code>\n"
            f"🛑 <b>Active SL</b>  :  <code>{current_sl}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<i>📚 For educational purposes only.</i>"
        )

    # ── Distinct Lifecycle Event: Stop Loss Hit ───────────────────────────────
    if act == "STOP_LOSS":
        exit_price = _money(trade.stop_price or trade.initial_stop_price)
        return (
            f"🛑 <b>SHASHA — STOP LOSS HIT</b>{mode_line}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>Symbol</b>     :  <code>{symbol}</code>\n"
            f"📊 <b>Direction</b>  :  <b>{dir_label}</b>\n"
            f"🛑 <b>Exit Price</b> :  <code>{exit_price}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<i>📚 For educational purposes only.</i>"
        )

    # ── Distinct Lifecycle Event: Trailing Stop Updated ───────────────────────
    if act.startswith("TRAILING"):
        current_sl = _money(trade.stop_price)
        return (
            f"🛡️ <b>SHASHA — TRAILING STOP UPDATED</b>{mode_line}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>Symbol</b>     :  <code>{symbol}</code>\n"
            f"📊 <b>Direction</b>  :  <b>{dir_label}</b>\n"
            f"🛑 <b>New SL</b>     :  <code>{current_sl}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<i>📚 For educational purposes only.</i>"
        )

    # ── Distinct Lifecycle Event: Cancelled / Expired ─────────────────────────
    if act in ("CANCELLED", "QUEUED_ENTRY_EXPIRED", "MANUAL_CANCEL"):
        reason = trade.status_reason or event.status_reason or "Order cancelled."
        return (
            f"⚠️ <b>SHASHA — Order Cancelled</b>{mode_line}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>Symbol</b>     :  <code>{symbol}</code>\n"
            f"❌ <b>Status</b>     :  CANCELLED\n"
            f"📋 <b>Reason</b>     :  {reason}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<i>📚 For educational purposes only.</i>"
        )

    # ── Distinct Lifecycle Event: Closed ──────────────────────────────────────
    if act == "CLOSED":
        pnl = _money(trade.net_realized_pnl)
        pnl_prefix = "+" if trade.net_realized_pnl and trade.net_realized_pnl > 0 else ""
        return (
            f"🏁 <b>SHASHA — TRADE CLOSED</b>{mode_line}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>Symbol</b>     :  <code>{symbol}</code>\n"
            f"📊 <b>Direction</b>  :  <b>{dir_label}</b>\n"
            f"💵 <b>Net PnL</b>    :  <code>{pnl_prefix}{pnl} USDT</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<i>📚 For educational purposes only.</i>"
        )

    # ── Default: Signal Card (accepted / retry / initial alert) ───────────────
    is_bad = trade.status in {"REJECTED", "FAILED", "CANCELLED", "UNKNOWN", "PROTECTION_ERROR"}
    header_emoji = "⚠️" if is_bad else dir_emoji
    header_label = "Needs Attention" if is_bad else f"{direction} SIGNAL"

    entry = _money(trade.actual_entry or trade.planned_entry)
    sl    = _money(trade.stop_price or trade.initial_stop_price)
    t1    = _money(trade.target1_price)
    t2    = _money(trade.target2_price)
    t3    = _money(trade.target3_price)

    return (
        f"{header_emoji} <b>SHASHA — {header_label}</b>{mode_line}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>Symbol</b>     :  <code>{symbol}</code>\n"
        f"📊 <b>Direction</b>  :  <b>{dir_label}</b>\n"
        f"💰 <b>Entry</b>      :  <code>{entry}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Target 1</b>   :  <code>{t1}</code>\n"
        f"🎯 <b>Target 2</b>   :  <code>{t2}</code>\n"
        f"🎯 <b>Target 3</b>   :  <code>{t3}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🛑 <b>Stop Loss</b>  :  <code>{sl}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<i>📚 For educational purposes only.</i>"
    )


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
                json={
                    "chat_id": self.settings.telegram_chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                },
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
