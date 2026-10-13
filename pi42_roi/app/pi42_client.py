"""Pi42 Futures API client (https://docs.pi42.com).

Design rules (mirrors the safety model of the Mudrex client this project was
ported from):

* Authenticated endpoints are called with the ``api-key`` and ``signature``
  headers. ``signature = HMAC-SHA256(secret, data_to_sign)`` hex encoded, where
  ``data_to_sign`` is
    - the **query string** (including ``timestamp``, in milliseconds) for GET, and
    - the **compact JSON body** (``separators=(',', ':')``, including
      ``timestamp``) for POST/PATCH/PUT/DELETE.
  The payload we sign is byte-for-byte the payload we send: the body is
  serialised once and posted as raw content, and the query string is appended
  verbatim. (``requests.post(json=...)`` in the Pi42 examples would insert
  spaces and change the bytes, which is why this client never lets a library
  re-serialise a signed payload.)
* Decimal values are rendered as plain JSON numbers (never ``1E-5``) so Pi42's
  quantity/price precision checks see normal decimals.
* The client is read-only unless constructed with ``allow_mutations=True``.
  PAPER mode never constructs a mutating client, and any mutation attempt
  raises ``ReadOnlyViolation`` before a request is made.
* GET requests are retried with bounded exponential backoff on timeouts,
  transport errors, 429 and 5xx. Mutations (POST/PATCH/DELETE) are NEVER
  retried: a timeout or 5xx after a mutation raises ``AmbiguousMutationError``
  so the caller reconciles by ``refId``/``clientOrderId`` instead of resubmitting.
* A sliding-window rate limiter keeps the client inside Pi42's published limits
  (place-order 20/s, delete-order 30/min, everything else 60/min). The limiter
  is configurable and can be disabled in tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections import deque
from decimal import Decimal
from typing import Any, Awaitable, Callable, Iterable, Mapping

import httpx

from app.models import (
    ContractInfo,
    LeverageInfo,
    OrderAccepted,
    OrderInfo,
    PositionInfo,
    WalletInfo,
)
from app.numeric import format_decimal

logger = logging.getLogger("pi42.client")

ORDER_LIST_LIMIT = 100
DUPLICATE_REF_ERROR_CODE = 3019  # "Ref id already exists" on /v1/order/place-order


class Pi42Error(Exception):
    """Base error. ``definitive`` means Pi42 clearly did not act on the request."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: int | None = None,
        definitive: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.definitive = definitive


class Pi42AuthError(Pi42Error):
    """401/403: bad key, bad signature or missing scope. Fatal; stop trading."""


class Pi42NotFoundError(Pi42Error):
    """404 or a "not found" business code: the resource does not exist."""


class Pi42RateLimitError(Pi42Error):
    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message, status=429, definitive=True)
        self.retry_after = retry_after


class AmbiguousMutationError(Pi42Error):
    """A mutating request timed out or returned 5xx. The outcome is UNKNOWN."""

    def __init__(self, message: str) -> None:
        super().__init__(message, definitive=False)


class ReadOnlyViolation(Pi42Error):
    """A mutating call was attempted on a read-only client (PAPER mode)."""


def is_duplicate_ref_error(exc: Pi42Error) -> bool:
    """True when Pi42 rejected a place-order because our ``refId`` already exists.

    That means an earlier (possibly response-lost) attempt already created the
    order, so the caller must adopt it instead of sending a second one.
    """
    if exc.code == DUPLICATE_REF_ERROR_CODE:
        return True
    text = str(exc).lower()
    return "ref id already exists" in text or "refid already exists" in text


def compact_json(payload: Mapping[str, Any]) -> str:
    """Serialise a request body exactly the way it will be signed and sent."""
    return _encode(payload)


def _encode(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format_decimal(value)
    if isinstance(value, int):
        return str(value)
    if value is None:
        return "null"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, Mapping):
        return "{" + ",".join(f"{json.dumps(str(k))}:{_encode(v)}" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_encode(v) for v in value) + "]"
    raise TypeError(f"cannot serialise {type(value).__name__} into a Pi42 payload")


def build_query_string(params: Mapping[str, Any]) -> str:
    """Build the exact query string that will be signed and sent."""
    parts: list[str] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, Decimal):
            rendered = format_decimal(value)
        else:
            rendered = str(value)
        parts.append(f"{key}={rendered}")
    return "&".join(parts)


class RateLimiter:
    """Sliding-window limiter (requests per 60s) per bucket."""

    def __init__(
        self,
        limits: Mapping[str, int],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        enabled: bool = True,
    ) -> None:
        self._limits = dict(limits)
        self._clock = clock
        self._sleep = sleep
        self._enabled = enabled
        self._events: dict[str, deque[float]] = {bucket: deque() for bucket in limits}

    async def acquire(self, bucket: str) -> None:
        if not self._enabled:
            return
        limit = self._limits.get(bucket)
        if not limit or limit <= 0:
            return
        events = self._events.setdefault(bucket, deque())
        while True:
            now = self._clock()
            while events and now - events[0] >= 60.0:
                events.popleft()
            if len(events) < limit:
                events.append(now)
                return
            wait = 60.0 - (now - events[0]) + 0.01
            logger.warning("event=rate_limit.wait bucket=%s seconds=%.2f", bucket, wait)
            await self._sleep(max(0.05, wait))


class Pi42Client:
    def __init__(
        self,
        *,
        base_url: str = "https://fapi.pi42.com",
        public_base_url: str = "https://api.pi42.com",
        api_key: str = "",
        api_secret: str = "",
        connect_timeout: float = 5.0,
        read_timeout: float = 10.0,
        allow_mutations: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        backoff_cap: float = 8.0,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
        rate_limiter: RateLimiter | None = None,
        requests_per_minute: int = 50,
        delete_requests_per_minute: int = 25,
    ) -> None:
        self._key = api_key
        self._secret = api_secret
        self._allow_mutations = allow_mutations
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._sleep = sleep
        self._clock = clock
        self._public_base_url = public_base_url.rstrip("/")
        self._limiter = rate_limiter or RateLimiter(
            {"default": requests_per_minute, "delete": delete_requests_per_minute},
            clock=clock if clock is time.time else time.monotonic,
            sleep=sleep,
        )
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
            transport=transport,
            headers={"Accept": "application/json"},
        )

    @property
    def allow_mutations(self) -> bool:
        return self._allow_mutations

    @property
    def has_credentials(self) -> bool:
        return bool(self._key and self._secret)

    async def aclose(self) -> None:
        await self._http.aclose()

    def __repr__(self) -> str:  # never reveal the secret
        return f"Pi42Client(allow_mutations={self._allow_mutations}, key={self._key[:4] + '…' if self._key else ''}, secret=<redacted>)"

    # ------------------------------------------------------------------ signing
    def _timestamp_ms(self) -> str:
        return str(int(self._clock() * 1000))

    def _sign(self, data_to_sign: str) -> str:
        return hmac.new(
            self._secret.encode("utf-8"), data_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def _scrub(self, text: str) -> str:
        for secret in (self._secret, self._key):
            if secret:
                text = text.replace(secret, "***")
        return text

    # ------------------------------------------------------------------ transport
    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        public: bool = False,
        bucket: str = "default",
    ) -> Any:
        mutation = method != "GET"
        if mutation and not self._allow_mutations:
            raise ReadOnlyViolation(f"{method} {path} blocked: client is read-only")

        signed = not public
        if signed and not self.has_credentials:
            raise Pi42AuthError(f"{method} {path} requires PI42_API_KEY/PI42_API_SECRET")

        headers: dict[str, str] = {}
        url = path
        content: bytes | None = None

        if mutation:
            payload = dict(body or {})
            if signed:
                payload["timestamp"] = self._timestamp_ms()
                raw = compact_json(payload)
                headers["signature"] = self._sign(raw)
                headers["api-key"] = self._key
                headers["Content-Type"] = "application/json"
                content = raw.encode("utf-8")
            else:
                content = json.dumps(payload).encode("utf-8")
                headers["Content-Type"] = "application/json"
        else:
            query_params = dict(params or {})
            if signed:
                query_params["timestamp"] = self._timestamp_ms()
                query = build_query_string(query_params)
                headers["signature"] = self._sign(query)
                headers["api-key"] = self._key
                url = f"{path}?{query}" if query else path
            elif query_params:
                query = build_query_string(query_params)
                url = f"{path}?{query}" if query else path

        attempts = 1 if mutation else self._max_retries + 1
        for attempt in range(attempts):
            last_attempt = attempt == attempts - 1
            await self._limiter.acquire("delete" if (mutation and path.endswith("delete-order")) else bucket)
            try:
                response = await self._http.request(method, url, content=content, headers=headers)
            except httpx.TimeoutException as exc:
                if mutation:
                    raise AmbiguousMutationError(f"{method} {path} timed out; outcome unknown") from exc
                if last_attempt:
                    raise Pi42Error(f"{method} {path} timed out", definitive=False) from exc
                await self._sleep(self._backoff(attempt))
                continue
            except httpx.TransportError as exc:
                if mutation:
                    raise AmbiguousMutationError(
                        f"{method} {path} transport error {type(exc).__name__}; outcome unknown"
                    ) from exc
                if last_attempt:
                    raise Pi42Error(f"{method} {path} transport error", definitive=False) from exc
                await self._sleep(self._backoff(attempt))
                continue

            status = response.status_code
            if status == 429:
                retry_after = _retry_after(response)
                if mutation or last_attempt:
                    raise Pi42RateLimitError(
                        f"{method} {path} rate limited (429)", retry_after=retry_after
                    )
                await self._sleep(max(retry_after or 0.0, self._backoff(attempt)))
                continue
            if status >= 500:
                if mutation:
                    raise AmbiguousMutationError(f"{method} {path} returned {status}; outcome unknown")
                if last_attempt:
                    raise Pi42Error(f"{method} {path} server error {status}", status=status)
                await self._sleep(self._backoff(attempt))
                continue

            payload = _json_or_none(response)
            if status >= 400:
                raise self._error_from(method, path, status, payload)
            return self._unwrap(method, path, payload)

        raise Pi42Error(f"{method} {path} failed")  # pragma: no cover - loop always returns/raises

    def _unwrap(self, method: str, path: str, payload: Any) -> Any:
        """Return ``payload["data"]`` for Pi42 envelopes, the payload otherwise.

        Pi42 mixes shapes: most endpoints return the object/array directly while
        a few (cancel-all-orders, some cancel responses) wrap it in
        ``{"success": true, "data": ...}``.
        """
        if isinstance(payload, Mapping):
            if payload.get("success") is False:
                raise self._error_from(method, path, 200, payload)
            if "data" in payload and set(payload.keys()) <= {"data", "success", "message", "count"}:
                return payload["data"]
        return payload

    def _backoff(self, attempt: int) -> float:
        return min(self._backoff_cap, self._backoff_base * (2**attempt))

    def _error_from(self, method: str, path: str, status: int, payload: Any) -> Pi42Error:
        code: int | None = None
        text = ""
        if isinstance(payload, Mapping):
            for key in ("code", "errorCode", "statusCode"):
                value = payload.get(key)
                if value is None:
                    continue
                try:
                    code = int(value)
                    break
                except (TypeError, ValueError):
                    continue
            error = payload.get("error")
            if isinstance(error, Mapping):
                text = str(error.get("message") or error.get("text") or error.get("code") or "")
                if code is None and error.get("code") is not None:
                    try:
                        code = int(error["code"])
                    except (TypeError, ValueError):
                        code = None
            elif error:
                text = str(error)
            if not text:
                text = str(payload.get("message") or payload.get("msg") or payload.get("text") or "")
        code_label = f" [Pi42 code {code}]" if code is not None else ""
        message = self._scrub(f"{method} {path} -> HTTP {status}{code_label}: {text or 'no error text'}")
        if status in (401, 403):
            return Pi42AuthError(message, status=status, code=code, definitive=True)
        if status == 404:
            return Pi42NotFoundError(message, status=status, code=code, definitive=True)
        if code in (2502, 3060, 3043):  # position/order not found
            return Pi42NotFoundError(message, status=status, code=code, definitive=True)
        return Pi42Error(message, status=status, code=code, definitive=status < 500)

    # ------------------------------------------------------------ read APIs
    async def list_contracts(self, market: str | None = None) -> list[ContractInfo]:
        """Every tradable perpetual contract (``GET /v1/exchange/exchangeInfo``)."""
        params = {"market": market.upper()} if market else None
        data = await self._send("GET", "/v1/exchange/exchangeInfo", params=params)
        contracts = data.get("contracts") if isinstance(data, Mapping) else data
        if not isinstance(contracts, list):
            raise Pi42Error("exchangeInfo returned a non-list contracts payload")
        result: list[ContractInfo] = []
        for item in contracts:
            try:
                result.append(ContractInfo.from_api(item))
            except ValueError as exc:
                logger.warning("event=contract.skipped_invalid reason=%s", exc)
        return result

    async def get_exchange_info(self, market: str | None = None) -> Mapping[str, Any]:
        """Raw exchangeInfo document (contracts, markets, assetPrecisions, conversionRates)."""
        params = {"market": market.upper()} if market else None
        data = await self._send("GET", "/v1/exchange/exchangeInfo", params=params)
        if not isinstance(data, Mapping):
            raise Pi42Error("exchangeInfo returned a non-object payload")
        return data

    async def get_futures_wallet(self, margin_asset: str = "USDT") -> WalletInfo:
        data = await self._send(
            "GET", "/v1/wallet/futures-wallet/details", params={"marginAsset": margin_asset.upper()}
        )
        if not isinstance(data, Mapping):
            raise Pi42Error("futures wallet returned a non-object payload")
        wallet = WalletInfo.from_api(data)
        if not wallet.margin_asset:
            wallet = WalletInfo(
                margin_asset=margin_asset.upper(),
                balance=wallet.balance,
                available=wallet.available,
                locked=wallet.locked,
                margin_balance=wallet.margin_balance,
                unrealised_pnl=wallet.unrealised_pnl,
            )
        return wallet

    async def get_ticker_price(self, contract_pair: str) -> Decimal:
        """Last traded price from the public ticker (no credentials required)."""
        data = await self._send(
            "GET", f"/v1/market/ticker24Hr/{contract_pair.upper()}", public=True
        )
        payload = data.get("data") if isinstance(data, Mapping) and "data" in data else data
        if not isinstance(payload, Mapping) or payload.get("c") is None:
            raise Pi42Error(f"ticker for {contract_pair} did not contain a last price")
        from app.numeric import to_decimal

        return to_decimal(payload["c"], "c")

    async def get_order(self, client_order_id: str) -> OrderInfo:
        """Order details by our own/returned ``clientOrderId`` (``GET /v1/order``)."""
        data = await self._send("GET", "/v1/order", params={"clientOrderId": client_order_id})
        payload = data[0] if isinstance(data, list) and data else data
        if not isinstance(payload, Mapping):
            raise Pi42NotFoundError(f"order {client_order_id} not found")
        return OrderInfo.from_api(payload)

    async def get_order_by_ref_id(self, ref_id: str) -> OrderInfo:
        """Order details by the caller supplied ``refId`` (idempotency lookup)."""
        data = await self._send("GET", "/v1/order/ref-id", params={"ref_id": ref_id})
        payload = data[0] if isinstance(data, list) and data else data
        if not isinstance(payload, Mapping):
            raise Pi42NotFoundError(f"order with refId {ref_id} not found")
        return OrderInfo.from_api(payload)

    async def list_open_orders(self, symbol: str | None = None, page_size: int = ORDER_LIST_LIMIT) -> list[OrderInfo]:
        params: dict[str, Any] = {"pageSize": str(page_size)}
        if symbol:
            params["symbol"] = symbol.upper()
        data = await self._send("GET", "/v1/order/open-orders", params=params)
        return _order_list(data)

    async def list_order_history(
        self, symbol: str | None = None, page_size: int = ORDER_LIST_LIMIT
    ) -> list[OrderInfo]:
        params: dict[str, Any] = {"pageSize": str(page_size), "sortOrder": "desc"}
        if symbol:
            params["symbol"] = symbol.upper()
        data = await self._send("GET", "/v1/order/order-history", params=params)
        return _order_list(data)

    async def list_open_positions(self, symbol: str | None = None) -> list[PositionInfo]:
        params: dict[str, Any] = {"pageSize": str(ORDER_LIST_LIMIT), "sortOrder": "desc"}
        if symbol:
            params["symbol"] = symbol.upper()
        data = await self._send("GET", "/v1/positions/OPEN", params=params)
        return _position_list(data)

    async def get_position(self, position_id: str) -> PositionInfo:
        data = await self._send("GET", "/v1/positions", params={"positionId": position_id})
        payload = data[0] if isinstance(data, list) and data else data
        if not isinstance(payload, Mapping):
            raise Pi42NotFoundError(f"position {position_id} not found")
        return PositionInfo.from_api(payload)

    # -------------------------------------------------------- mutation APIs
    async def set_leverage(self, contract: str, leverage: Decimal) -> LeverageInfo:
        """Set the leverage for one contract (mutation, never retried)."""
        data = await self._send(
            "POST",
            "/v1/exchange/update/leverage",
            body={"leverage": _as_int(leverage), "contractName": contract.upper()},
        )
        if not isinstance(data, Mapping):
            raise Pi42Error("leverage update returned a non-object payload")
        return LeverageInfo.from_api(data)

    async def set_preference(
        self, contract: str, leverage: Decimal, margin_mode: str = "ISOLATED"
    ) -> LeverageInfo:
        """Set leverage **and** margin mode for one contract (mutation, never retried)."""
        data = await self._send(
            "POST",
            "/v1/exchange/update/preference",
            body={
                "leverage": _as_int(leverage),
                "marginMode": margin_mode.upper(),
                "contractName": contract.upper(),
            },
        )
        if not isinstance(data, Mapping):
            raise Pi42Error("preference update returned a non-object payload")
        return LeverageInfo.from_api(data)

    async def place_limit_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        take_profit_price: Decimal | None = None,
        margin_asset: str,
        ref_id: str | None = None,
        client_order_id: str | None = None,
        reduce_only: bool = False,
        position_id: str | None = None,
        device_type: str = "",
        user_category: str = "",
    ) -> OrderAccepted:
        """Place a LIMIT entry order. No stop-loss price is ever sent.

        ``side`` is ``BUY``/``SELL`` (Pi42's own vocabulary). A take-profit price
        may be attached, exactly like the Mudrex ``takeprofit_price`` field this
        bot used before. ``reduce_only`` + ``position_id`` are used when closing
        from an existing position, never for entries.
        """
        if side.upper() not in ("BUY", "SELL"):
            raise ValueError(f"invalid side {side!r}")
        if price <= 0 or quantity <= 0:
            raise ValueError("price and quantity must be positive")
        if reduce_only and not position_id:
            raise ValueError("reduce-only orders require a position_id")

        body: dict[str, Any] = {
            "placeType": "POSITION" if position_id else "ORDER_FORM",
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": "LIMIT",
            "quantity": quantity,
            "price": price,
            "reduceOnly": bool(reduce_only),
            "marginAsset": margin_asset.upper(),
        }
        if position_id:
            body["positionId"] = position_id
        if take_profit_price is not None:
            if take_profit_price <= 0:
                raise ValueError("take_profit_price must be positive")
            body["takeProfitPrice"] = take_profit_price
        if ref_id:
            body["refId"] = ref_id
        if client_order_id:
            body["clientOrderId"] = client_order_id
        if device_type:
            body["deviceType"] = device_type.upper()
        if user_category:
            body["userCategory"] = user_category.upper()
        if "stopLossPrice" in body:  # hard invariant, survives -O
            raise RuntimeError("stop-loss fields must never be sent at entry")

        data = await self._send("POST", "/v1/order/place-order", body=body)
        if not isinstance(data, Mapping):
            raise Pi42Error("place-order returned a non-object payload")
        return OrderAccepted.from_api(data)

    async def close_position(
        self,
        *,
        symbol: str,
        position_id: str,
        side_of_position: str,
        quantity: Decimal,
        margin_asset: str,
        ref_id: str | None = None,
        device_type: str = "",
        user_category: str = "",
    ) -> OrderAccepted:
        """Square off a whole position with a reduce-only MARKET order.

        Pi42 closes a single position by placing an order *from* it
        (``placeType=POSITION`` + ``positionId``) on the opposite side with
        ``reduceOnly``. There is no per-position ``/close`` endpoint.
        """
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        closing_side = "SELL" if side_of_position.upper() == "LONG" else "BUY"
        body: dict[str, Any] = {
            "placeType": "POSITION",
            "positionId": position_id,
            "symbol": symbol.upper(),
            "side": closing_side,
            "type": "MARKET",
            "quantity": quantity,
            "reduceOnly": True,
            "marginAsset": margin_asset.upper(),
        }
        if ref_id:
            body["refId"] = ref_id
        if device_type:
            body["deviceType"] = device_type.upper()
        if user_category:
            body["userCategory"] = user_category.upper()
        data = await self._send("POST", "/v1/order/place-order", body=body)
        if not isinstance(data, Mapping):
            raise Pi42Error("close order returned a non-object payload")
        return OrderAccepted.from_api(data)

    async def cancel_order(self, client_order_id: str) -> Mapping[str, Any]:
        """Cancel one open order by ``clientOrderId`` (mutation, never retried)."""
        data = await self._send(
            "DELETE", "/v1/order/delete-order", body={"clientOrderId": client_order_id}
        )
        return data if isinstance(data, Mapping) else {}

    async def split_tp_sl(
        self,
        *,
        position_id: str,
        take_profit_orders: Iterable[Mapping[str, Decimal]] | None = None,
        stop_loss_orders: Iterable[Mapping[str, Decimal]] | None = None,
    ) -> Mapping[str, Any]:
        """Attach split TP/SL legs to an open position (``POST /v2/order/split-tp-sl``).

        Used only for take-profit legs; this bot never sends stop-loss legs.
        """
        if stop_loss_orders:
            raise RuntimeError("stop-loss legs must never be sent")
        body: dict[str, Any] = {
            "positionId": position_id,
            "splitTakeProfitOrders": [dict(leg) for leg in (take_profit_orders or [])],
        }
        data = await self._send("POST", "/v2/order/split-tp-sl", body=body)
        return data if isinstance(data, Mapping) else {}


def _as_int(value: Decimal) -> int:
    """Pi42 leverage is an integer; snap down rather than round up."""
    return int(value.to_integral_value(rounding="ROUND_FLOOR"))


def _order_list(data: Any) -> list[OrderInfo]:
    if data is None:
        return []
    if isinstance(data, Mapping):
        data = data.get("orders") or data.get("data") or []
    if not isinstance(data, list):
        raise Pi42Error("order listing returned an unexpected payload")
    orders: list[OrderInfo] = []
    for item in data:
        try:
            orders.append(OrderInfo.from_api(item))
        except ValueError as exc:
            logger.warning("event=order.skipped_invalid reason=%s", exc)
    return orders


def _position_list(data: Any) -> list[PositionInfo]:
    if data is None:
        return []
    if isinstance(data, Mapping):
        data = data.get("positions") or data.get("data") or []
    if not isinstance(data, list):
        raise Pi42Error("position listing returned an unexpected payload")
    positions: list[PositionInfo] = []
    for item in data:
        try:
            positions.append(PositionInfo.from_api(item))
        except ValueError as exc:
            logger.warning("event=position.skipped_invalid reason=%s", exc)
    return positions


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
