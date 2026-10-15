"""Shark Exchange REST client using only the documented API surface.

Source inspected 2026-10-15: https://docs.sharkexchange.in/ (latest changelog
shown there: 2026-01-29). Authenticated requests use HMAC-SHA256 over the exact
compact body bytes or exact insertion-ordered query string, with `timestamp`
appended to the parameters and `api-key` / `signature` headers.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings
from app.security import canonical_json_bytes, shark_query_string, shark_signature


class SharkApiError(RuntimeError):
    def __init__(self, status_code: int, exchange_code: str = "") -> None:
        self.status_code = status_code
        self.exchange_code = exchange_code
        code = f" ({exchange_code})" if exchange_code else ""
        super().__init__(f"Shark API request failed with HTTP {status_code}{code}")


class SharkOutcomeUnknown(SharkApiError):
    """An order request may have reached Shark; never resubmit without reconciliation."""

    def __init__(self) -> None:
        super().__init__(0, "ORDER_OUTCOME_UNKNOWN")
        self.args = ("Shark order outcome is unknown; reconciliation is required",)


@dataclass(frozen=True)
class SharkResponse:
    data: Any
    status_code: int


class SharkClient:
    def __init__(
        self,
        settings: Settings,
        http_client: httpx.AsyncClient | None = None,
        *,
        clock_ms: Any | None = None,
    ) -> None:
        self.settings = settings
        self._client = http_client
        self._owns_client = http_client is None
        self._clock_ms = clock_ms
        self.last_private_ok: bool | None = None
        self.last_private_error: str = ""

    def _now_ms(self) -> str:
        if self._clock_ms is not None:
            return str(self._clock_ms())
        import time

        return str(int(time.time() * 1000))

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.shark_base_url.rstrip("/"),
                timeout=self.settings.shark_request_timeout_seconds,
                follow_redirects=False,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _error_code(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return ""
        if not isinstance(body, dict):
            return ""
        code = body.get("code") or body.get("errorCode")
        return str(code)[:32] if code is not None else ""

    async def _request(
        self,
        method: str,
        path: str,
        *,
        query: list[tuple[str, Any]] | None = None,
        body: dict[str, Any] | None = None,
        private: bool = False,
        order_submission: bool = False,
    ) -> Any:
        if private and not self.settings.shark_credentials_configured:
            raise SharkApiError(0, "PRIVATE_API_CREDENTIALS_NOT_CONFIGURED")
        client = await self._http()
        method = method.upper()
        headers: dict[str, str] = {}
        request_path = path
        raw_body: bytes | None = None

        if method == "GET":
            pairs = list(query or [])
            if private:
                pairs.append(("timestamp", self._now_ms()))
            if pairs:
                query_string = shark_query_string(pairs)
                request_path = f"{path}?{query_string}"
                if private:
                    headers["api-key"] = self.settings.shark_api_key
                    headers["signature"] = shark_signature(self.settings.shark_api_secret, query_string)
        else:
            if private:
                payload = dict(body or {})
                payload["timestamp"] = self._now_ms()
                raw_body = canonical_json_bytes(payload)
                headers = {
                    "api-key": self.settings.shark_api_key,
                    "signature": shark_signature(self.settings.shark_api_secret, raw_body),
                    "Content-Type": "application/json",
                }
            elif body is not None:
                raw_body = canonical_json_bytes(body)
                headers["Content-Type"] = "application/json"

        try:
            response = await client.request(method, request_path, content=raw_body, headers=headers)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError) as exc:
            if order_submission:
                raise SharkOutcomeUnknown() from exc
            raise SharkApiError(0, "NETWORK_ERROR") from exc

        if response.is_error:
            self.last_private_ok = False if private else self.last_private_ok
            code = self._error_code(response)
            if order_submission and response.status_code >= 500:
                raise SharkOutcomeUnknown() from None
            raise SharkApiError(response.status_code, code)
        try:
            result = response.json()
        except ValueError as exc:
            raise SharkApiError(response.status_code, "INVALID_JSON_RESPONSE") from exc
        if private:
            self.last_private_ok = True
            self.last_private_error = ""
        return result

    # ---------------------------- public market/contract data
    async def exchange_info(self, market: str | None = None) -> Any:
        query = [("market", market)] if market else []
        return await self._request("GET", "/v1/exchange/exchangeInfo", query=query)

    async def ticker_24h(self, symbol: str) -> Any:
        clean = symbol.upper()
        if not clean.isalnum():
            raise ValueError("invalid Shark symbol")
        return await self._request("GET", f"/v1/market/ticker24Hr/{clean}")

    async def klines(self, symbol: str, interval: str = "1d", limit: int = 1) -> Any:
        clean = symbol.upper()
        if not clean.isalnum():
            raise ValueError("invalid Shark symbol")
        return await self._request(
            "POST",
            "/v1/market/klines",
            body={"pair": clean, "interval": interval, "limit": limit},
        )

    # ----------------------------------------- private account read APIs
    async def futures_wallet(self, margin_asset: str = "USDT") -> Any:
        return await self._request(
            "GET", "/v1/wallet/futures-wallet/details", query=[("marginAsset", margin_asset)], private=True
        )

    async def positions(self, status: str = "OPEN", symbol: str | None = None) -> Any:
        normalized = status.upper()
        if normalized not in {"OPEN", "CLOSED", "LIQUIDATED"}:
            raise ValueError("unsupported Shark position status")
        query = [("symbol", symbol)] if symbol else []
        return await self._request("GET", f"/v1/positions/{normalized}", query=query, private=True)

    async def position(self, position_id: str) -> Any:
        return await self._request(
            "GET", "/v1/positions", query=[("positionId", position_id)], private=True
        )

    async def open_orders(self, symbol: str | None = None) -> Any:
        query = [("symbol", symbol)] if symbol else []
        return await self._request("GET", "/v1/order/open-orders", query=query, private=True)

    async def order(self, client_order_id: str) -> Any:
        safe_id = quote(client_order_id, safe="")
        return await self._request("GET", f"/v1/order/{safe_id}", private=True)

    async def order_history(self, *, symbol: str, start_ms: int, end_ms: int | None = None) -> Any:
        query: list[tuple[str, Any]] = [
            ("sortOrder", "desc"),
            ("pageSize", "100"),
            ("startTimestamp", str(start_ms)),
        ]
        if end_ms is not None:
            query.append(("endTimestamp", str(end_ms)))
        query.append(("symbol", symbol))
        return await self._request("GET", "/v1/order/order-history", query=query, private=True)

    async def trade_history(self, *, symbol: str | None = None, start_ms: int | None = None) -> Any:
        query: list[tuple[str, Any]] = [("sortOrder", "desc"), ("pageSize", "100")]
        if start_ms is not None:
            query.append(("startTimestamp", str(start_ms)))
        if symbol:
            query.append(("symbol", symbol))
        return await self._request("GET", "/v1/user-data/trade-history", query=query, private=True)

    # ---------------------------------------- private order management
    async def update_leverage(self, symbol: str, leverage: int) -> Any:
        return await self._request(
            "POST",
            "/v1/exchange/update/leverage",
            body={"leverage": leverage, "contractName": symbol},
            private=True,
        )

    async def place_limit_buy(self, symbol: str, quantity: Decimal, price: Decimal) -> Any:
        return await self._request(
            "POST",
            "/v1/order/place-order",
            body={
                "placeType": "ORDER_FORM",
                "quantity": quantity,
                "side": "BUY",
                "symbol": symbol,
                "reduceOnly": False,
                "marginAsset": self.settings.shark_margin_asset.upper(),
                "type": "LIMIT",
                "price": price,
                "deviceType": self.settings.shark_device_type,
                "userCategory": self.settings.shark_user_category,
            },
            private=True,
            order_submission=True,
        )

    async def split_take_profit_stop_loss(
        self,
        position_id: str,
        *,
        take_profits: list[dict[str, Decimal]] | None = None,
        stop_losses: list[dict[str, Decimal]] | None = None,
    ) -> Any:
        return await self._request(
            "POST",
            "/v2/order/split-tp-sl",
            body={
                "positionId": position_id,
                "splitTakeProfitOrders": take_profits or [],
                "splitStopLossOrders": stop_losses or [],
            },
            private=True,
            order_submission=True,
        )

    async def edit_order(
        self,
        client_order_id: str,
        *,
        quantity: Decimal | None = None,
        price: Decimal | None = None,
        stop_price: Decimal | None = None,
    ) -> Any:
        payload: dict[str, Any] = {"clientOrderId": client_order_id}
        if quantity is not None:
            payload["quantity"] = quantity
        if price is not None:
            payload["price"] = price
        if stop_price is not None:
            payload["stopPrice"] = stop_price
        return await self._request("PATCH", "/v1/order/edit-order", body=payload, private=True)

    async def cancel_order(self, client_order_id: str) -> Any:
        return await self._request(
            "DELETE",
            "/v1/order/delete-order",
            body={"clientOrderId": client_order_id},
            private=True,
        )

    async def place_reduce_only_market_sell(self, symbol: str, quantity: Decimal) -> Any:
        return await self._request(
            "POST",
            "/v1/order/place-order",
            body={
                "placeType": "POSITION",
                "quantity": quantity,
                "side": "SELL",
                "symbol": symbol,
                "reduceOnly": True,
                "marginAsset": self.settings.shark_margin_asset.upper(),
                "type": "MARKET",
                "deviceType": self.settings.shark_device_type,
                "userCategory": self.settings.shark_user_category,
            },
            private=True,
            order_submission=True,
        )
