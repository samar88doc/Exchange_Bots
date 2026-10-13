"""Optional Telegram notification bot. Never required for trading.

Notifications contain trade summaries only. Tokens and API secrets are never
included, and any failure here is logged and swallowed.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("notify")


class Notifier:
    def __init__(self, token: str, chat_id: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._token = token
        self._chat_id = chat_id
        self._http = httpx.AsyncClient(timeout=10.0, transport=transport)

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat_id)

    async def send(self, text: str) -> None:
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        try:
            response = await self._http.post(url, json={"chat_id": self._chat_id, "text": text[:3900]})
            if response.status_code != 200:
                logger.warning("event=notify.failed http_status=%s", response.status_code)
        except Exception as exc:  # noqa: BLE001 - notifications must never break trading
            logger.warning("event=notify.error error=%s", type(exc).__name__)

    async def aclose(self) -> None:
        await self._http.aclose()


class NullNotifier:
    enabled = False

    async def send(self, text: str) -> None:
        return None

    async def aclose(self) -> None:
        return None
