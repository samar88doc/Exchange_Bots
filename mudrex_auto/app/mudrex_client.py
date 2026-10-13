"""Mudrex Futures API client.

Design rules (see README "Safety model"):
* Every request carries the ``X-Authentication`` header; the secret is never logged.
* The client is read-only unless constructed with ``allow_mutations=True``.
  PAPER mode never constructs a mutating client, and any mutation attempt
  raises ``ReadOnlyViolation`` before a request is made.
* GET requests are retried with bounded exponential backoff on timeouts,
  transport errors, 429 and 5xx.
* Mutations (POST/DELETE) are NEVER retried. A timeout or 5xx after a
  mutation is reported as ``AmbiguousMutationError`` so the caller reconciles
  by ``client_order_id`` instead of resubmitting.

Endpoints and payload shapes follow the official docs
(https://docs.trade.mudrex.com): asset listing ``GET /fapi/v1/futures``,
asset detail ``GET /fapi/v1/futures/{symbol}?is_symbol``, futures funds
``GET /fapi/v1/futures/funds``, leverage ``GET/POST /fapi/v1/futures/{symbol}/leverage``,
order ``POST /fapi/v2/futures/order?symbol=``, order detail
``GET /fapi/v1/futures/orders/detail``, cancel ``DELETE /fapi/v1/futures/orders/{order_id}``,
open orders ``GET /fapi/v1/futures/orders``, open positions ``GET /fapi/v1/futures/positions``.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Any, Awaitable, Callable

import httpx

from app.models import (
    AssetInfo,
    FundsInfo,
    LeverageInfo,
    OrderAccepted,
    OrderInfo,
    PositionInfo,
)
from app.numeric import format_decimal

logger = logging.getLogger("mudrex.client")

ORDER_LIST_LIMIT = 100


class MudrexError(Exception):
    """Base error. ``definitive`` means Mudrex clearly did not act on the request."""

    def __init__(self, message: str, *, status: int | None = None, code: int | None = None,
                 definitive: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.definitive = definitive


class MudrexAuthError(MudrexError):
    """401/403: bad key or scope. Fatal; stop trading."""


class MudrexNotFoundError(MudrexError):
    """404: the resource (or leverage setting / client order id) does not exist."""


class MudrexRateLimitError(MudrexError):
    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message, status=429, definitive=True)
        self.retry_after = retry_after


class AmbiguousMutationError(MudrexError):
    """A mutating request timed out or returned 5xx. The outcome is UNKNOWN."""

    def __init__(self, message: str) -> None:
        super().__init__(message, definitive=False)


class ReadOnlyViolation(MudrexError):
    """A mutating call was attempted on a read-only client (PAPER mode)."""


class MudrexClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_secret: str,
        connect_timeout: float = 5.0,
        read_timeout: float = 10.0,
        allow_mutations: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        backoff_cap: float = 8.0,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._secret = api_secret
        self._allow_mutations = allow_mutations
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
            transport=transport,
            headers={"Accept": "application/json"},
        )

    @property
    def allow_mutations(self) -> bool:
        return self._allow_mutations

    async def aclose(self) -> None:
        await self._http.aclose()

    def __repr__(self) -> str:  # never reveal the secret
        return f"MudrexClient(allow_mutations={self._allow_mutations}, secret=<redacted>)"

    # ------------------------------------------------------------------ core
    def _scrub(self, text: str) -> str:
        return text.replace(self._secret, "***") if self._secret else text

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        mutation = method != "GET"
        if mutation and not self._allow_mutations:
            raise ReadOnlyViolation(f"{method} {path} blocked: client is read-only")

        attempts = 1 if mutation else self._max_retries + 1
        headers = {"X-Authentication": self._secret}
        if body is not None:
            headers["Content-Type"] = "application/json"

        for attempt in range(attempts):
            last_attempt = attempt == attempts - 1
            try:
                response = await self._http.request(method, path, params=params, json=body, headers=headers)
            except httpx.TimeoutException as exc:
                if mutation:
                    raise AmbiguousMutationError(f"{method} {path} timed out; outcome unknown") from exc
                if last_attempt:
                    raise MudrexError(f"{method} {path} timed out", definitive=False) from exc
                await self._sleep(self._backoff(attempt))
                continue
            except httpx.TransportError as exc:
                if mutation:
                    raise AmbiguousMutationError(
                        f"{method} {path} transport error {type(exc).__name__}; outcome unknown"
                    ) from exc
                if last_attempt:
                    raise MudrexError(f"{method} {path} transport error", definitive=False) from exc
                await self._sleep(self._backoff(attempt))
                continue

            status = response.status_code
            if status == 429:
                retry_after = _retry_after(response)
                if mutation or last_attempt:
                    raise MudrexRateLimitError(
                        f"{method} {path} rate limited (429)", retry_after=retry_after
                    )
                await self._sleep(max(retry_after or 0, self._backoff(attempt)))
                continue
            if status >= 500:
                if mutation:
                    raise AmbiguousMutationError(f"{method} {path} returned {status}; outcome unknown")
                if last_attempt:
                    raise MudrexError(f"{method} {path} server error {status}", status=status)
                await self._sleep(self._backoff(attempt))
                continue

            payload = _json_or_none(response)
            if status >= 400 or not isinstance(payload, dict) or payload.get("success") is False:
                raise self._error_from(method, path, status, payload)
            if "data" not in payload:
                raise MudrexError(f"{method} {path} returned an unexpected payload shape")
            return payload["data"]
        raise MudrexError(f"{method} {path} failed")  # pragma: no cover - loop always returns/raises

    def _backoff(self, attempt: int) -> float:
        return min(self._backoff_cap, self._backoff_base * (2**attempt))

    def _error_from(self, method: str, path: str, status: int, payload: Any) -> MudrexError:
        code: int | None = None
        text = ""
        if isinstance(payload, dict) and payload.get("errors"):
            first = payload["errors"][0] or {}
            code = first.get("code")
            text = str(first.get("text", ""))
        message = self._scrub(f"{method} {path} -> HTTP {status}: {text or 'no error text'}")
        if status in (401, 403):
            return MudrexAuthError(message, status=status, code=code, definitive=True)
        if status == 404:
            return MudrexNotFoundError(message, status=status, code=code, definitive=True)
        return MudrexError(message, status=status, code=code, definitive=status < 500)

    # ------------------------------------------------------------ read APIs
    async def list_futures_assets(self, *, page_size: int = 100, max_pages: int = 50) -> list[AssetInfo]:
        """All tradable futures instruments, following offset/limit pagination."""
        assets: list[AssetInfo] = []
        offset = 0
        for _ in range(max_pages):
            data = await self._send(
                "GET",
                "/fapi/v1/futures",
                params={"sort": "popularity", "order": "desc", "offset": offset, "limit": page_size},
            )
            if not isinstance(data, list):
                raise MudrexError("asset listing returned a non-list payload")
            for item in data:
                try:
                    assets.append(AssetInfo.from_api(item))
                except ValueError as exc:
                    logger.warning("event=asset.skipped_invalid reason=%s", exc)
            if len(data) < page_size:
                return assets
            offset += len(data)
        raise MudrexError(f"asset listing exceeded {max_pages} pages; refusing partial metadata")

    async def get_asset_detail(self, symbol: str) -> AssetInfo:
        data = await self._send("GET", f"/fapi/v1/futures/{symbol}", params={"is_symbol": ""})
        if not isinstance(data, dict):
            raise MudrexError("asset detail returned a non-object payload")
        return AssetInfo.from_api(data)

    async def get_futures_funds(self, currency: str = "USDT") -> FundsInfo:
        data = await self._send("GET", "/fapi/v1/futures/funds", params={"trade_currency": currency})
        return FundsInfo.from_api(data)

    async def get_leverage(self, symbol: str, currency: str = "USDT") -> LeverageInfo | None:
        """Current leverage for (symbol, currency). ``None`` if never set there."""
        try:
            data = await self._send(
                "GET",
                f"/fapi/v1/futures/{symbol}/leverage",
                params={"is_symbol": "", "trade_currency": currency},
            )
        except MudrexNotFoundError:
            return None
        return LeverageInfo.from_api(data)

    async def get_order(self, order_id: str) -> OrderInfo:
        data = await self._send("GET", "/fapi/v1/futures/orders/detail", params={"order_id": order_id})
        return OrderInfo.from_api(data)

    async def get_order_by_client_id(self, client_order_id: str) -> OrderInfo:
        data = await self._send(
            "GET", "/fapi/v1/futures/orders/detail", params={"client_order_id": client_order_id}
        )
        return OrderInfo.from_api(data)

    async def list_open_orders(self, currency: str = "USDT") -> list[OrderInfo]:
        data = await self._send(
            "GET", "/fapi/v1/futures/orders", params={"trade_currency": currency, "limit": ORDER_LIST_LIMIT}
        )
        return [OrderInfo.from_api(item) for item in (data or [])]

    async def list_open_positions(self, currency: str = "USDT") -> list[PositionInfo]:
        data = await self._send(
            "GET", "/fapi/v1/futures/positions", params={"trade_currency": currency, "limit": ORDER_LIST_LIMIT}
        )
        return [PositionInfo.from_api(item) for item in (data or [])]

    # -------------------------------------------------------- mutation APIs
    async def set_leverage(self, symbol: str, leverage: Decimal, currency: str = "USDT") -> LeverageInfo:
        """Set ISOLATED leverage for (symbol, currency). Mutation, not retried."""
        data = await self._send(
            "POST",
            f"/fapi/v1/futures/{symbol}/leverage",
            params={"is_symbol": ""},
            body={
                "margin_type": "ISOLATED",
                "leverage": format_decimal(leverage),
                "trade_currency": currency,
            },
        )
        return LeverageInfo.from_api(data)

    async def place_limit_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        takeprofit_price: Decimal | None = None,
        client_order_id: str,
        trade_currency: str = "USDT",
    ) -> OrderAccepted:
        """LIMIT entry. No stop-loss field is ever sent at entry."""
        if side not in ("LONG", "SHORT"):
            raise ValueError(f"invalid side {side!r}")
        if price <= 0 or quantity <= 0:
            raise ValueError("price and quantity must be positive")
        body: dict[str, Any] = {
            "trigger_type": "LIMIT",
            "order_type": side,
            "quantity": format_decimal(quantity),
            "order_price": format_decimal(price),
            "trade_currency": trade_currency,
            "client_order_id": client_order_id,
        }
        if takeprofit_price is not None:
            if takeprofit_price <= 0:
                raise ValueError("takeprofit_price must be positive")
            body["is_takeprofit"] = True
            body["takeprofit_price"] = format_decimal(takeprofit_price)
        if "is_stoploss" in body or "stoploss_price" in body:  # hard invariant, survives -O
            raise RuntimeError("stop-loss fields must never be sent")
        data = await self._send("POST", "/fapi/v2/futures/order", params={"symbol": symbol}, body=body)
        return OrderAccepted.from_api(data)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel an open order by Mudrex order id. Mutation, not retried."""
        data = await self._send("DELETE", f"/fapi/v1/futures/orders/{order_id}")
        return data if isinstance(data, dict) else {}

    async def partial_close_position(
        self,
        position_id: str,
        *,
        quantity: Decimal,
        order_type: str = "MARKET",
        limit_price: Decimal | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """Close a portion of an open position via POST /fapi/v1/futures/positions/{position_id}/close/partial.

        order_type: 'MARKET' or 'LIMIT'.
        quantity: in base asset (must be less than total position quantity and multiple of step).
        """
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        body: dict[str, Any] = {
            "order_type": order_type.upper(),
            "quantity": format_decimal(quantity),
        }
        if order_type.upper() == "LIMIT":
            if limit_price is None or limit_price <= 0:
                raise ValueError("limit_price must be positive for LIMIT partial close")
            body["limit_price"] = format_decimal(limit_price)
        if client_order_id:
            body["client_order_id"] = client_order_id
        data = await self._send(
            "POST", f"/fapi/v1/futures/positions/{position_id}/close/partial", body=body
        )
        return data if isinstance(data, dict) else {}

    async def close_position(self, position_id: str) -> dict[str, Any]:
        """Square off an entire open position immediately via POST /fapi/v1/futures/positions/{position_id}/close."""
        data = await self._send("POST", f"/fapi/v1/futures/positions/{position_id}/close")
        return data if isinstance(data, dict) else {}

    async def set_position_risk(
        self,
        position_id: str,
        *,
        stoploss_price: Decimal | None = None,
        is_stoploss: bool = False,
        takeprofit_price: Decimal | None = None,
        is_takeprofit: bool = False,
        stoploss_client_order_id: str | None = None,
        takeprofit_client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """Set or amend SL/TP for an open position via POST /fapi/v1/futures/positions/{position_id}/riskorder."""
        body: dict[str, Any] = {
            "order_source": "API",
            "is_stoploss": is_stoploss,
            "is_takeprofit": is_takeprofit,
        }
        if is_stoploss and stoploss_price is not None:
            if stoploss_price <= 0:
                raise ValueError("stoploss_price must be positive")
            body["stoploss_price"] = format_decimal(stoploss_price)
            if stoploss_client_order_id:
                body["stoploss_client_order_id"] = stoploss_client_order_id
        if is_takeprofit and takeprofit_price is not None:
            if takeprofit_price <= 0:
                raise ValueError("takeprofit_price must be positive")
            body["takeprofit_price"] = format_decimal(takeprofit_price)
            if takeprofit_client_order_id:
                body["takeprofit_client_order_id"] = takeprofit_client_order_id
        data = await self._send(
            "POST", f"/fapi/v1/futures/positions/{position_id}/riskorder", body=body
        )
        return data if isinstance(data, dict) else {}



def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None
