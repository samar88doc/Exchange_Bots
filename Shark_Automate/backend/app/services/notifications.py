"""Extensible notification architecture (Telegram / log / desktop-ready)."""
from __future__ import annotations

import logging
from typing import Protocol

import httpx

from app.core.config import AppSettings
from app.core.logging import TradeEvent, log_event

log = logging.getLogger("trading.notify")

IMPORTANT_EVENTS = {
    "trade_opened", "trade_closed", "roi_reached", "order_failed",
    "shark_disconnected", "trading_halted", "max_positions_reached",
    "symbol_not_found", "reconciliation_error", "system_error",
}


class Notifier(Protocol):
    async def send(self, event: str, message: str) -> None: ...


class LogNotifier:
    async def send(self, event: str, message: str) -> None:
        log_event(log, TradeEvent.SYSTEM_ERROR if event == "system_error" else TradeEvent.WEBHOOK_RECEIVED,
                  f"[notify:{event}] {message}")


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id

    async def send(self, event: str, message: str) -> None:
        if not self.token or not self.chat_id:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(url, json={"chat_id": self.chat_id, "text": f"[{event}] {message}"})
        except Exception as exc:  # noqa: BLE001 — notification failure must never break trading
            log.warning("telegram notify failed: %s", exc)


class NotificationService:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.notifiers: list[Notifier] = [LogNotifier()]
        if settings.notifications_enabled and settings.telegram_bot_token:
            self.notifiers.append(TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id))

    async def notify(self, event: str, message: str) -> None:
        if event not in IMPORTANT_EVENTS and not self.settings.notifications_enabled:
            return
        for notifier in self.notifiers:
            try:
                await notifier.send(event, message)
            except Exception:  # noqa: BLE001 — never propagate notification errors
                pass
