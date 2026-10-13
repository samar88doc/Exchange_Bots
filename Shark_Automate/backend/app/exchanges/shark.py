"""Shark Exchange adapter — ONLY verified official API surface.

Source: https://docs.sharkexchange.in/ (change log 29-01-2026).
Auth: HMAC-SHA256 over JSON body (POST/PUT/PATCH/DELETE) or query string (GET),
headers `api-key` + `signature`. Base URL https://api.sharkexchange.in.

CRITICAL SAFETY NOTES
---------------------
1. `OrderRequest.symbol` MUST be the shark_symbol (e.g. BASUSDT), never the raw
   TradingView symbol (e.g. BASUSDT.P). Enforced by a hard guard below.
2. Shark's documented place-order does NOT accept a client-supplied idempotency
   key. Therefore we NEVER blind-retry submissions. If the HTTP outcome is
   uncertain, the order enters UNKNOWN and reconciliation must resolve it.
3. Individual close is executed as a MARKET reduceOnly order (verified fields).
   The only documented native close endpoint is close-all-positions.
4. `shark_dry_run=True` (default) builds + logs the exact payload but never
   transmits it. DRY RUN payload is returned as a DRY_RUN order result.
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any

import httpx

from app.core.config import AppSettings
from app.core.errors import ExchangeError, FailureKind, OrderStateUncertain, RejectReason
from app.core.logging import TradeEvent, log_event
from app.core.security import now_ms, shark_sign_body, shark_sign_query, shark_signature
from app.exchanges.base import (
    AccountSnapshot,
    BalanceSnapshot,
    Capability,
    ContractSpec,
    OrderRequest,
    OrderResult,
    PositionSnapshot,
    Ticker,
)

log = logging.getLogger("trading.shark")

_CAPS = {
    Capability.GET_ACCOUNT,
    Capability.GET_BALANCE,
    Capability.GET_MARK_PRICE,
    Capability.GET_CONTRACT,
    Capability.GET_OPEN_POSITIONS,
    Capability.GET_OPEN_ORDERS,
    Capability.PLACE_MARKET_ORDER,
    Capability.PLACE_LIMIT_ORDER,
    Capability.CLOSE_POSITION,
    Capability.CLOSE_ALL_POSITIONS,
    Capability.PLACE_TAKE_PROFIT,
    Capability.CANCEL_ORDER,
    Capability.GET_ORDER,
    Capability.GET_POSITION,
    Capability.SET_LEVERAGE,
}

# Rate limits from official docs: place-order 20/s, delete-order 30/min, other 60/min.


def _dec(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value is not None else default))
    except Exception:
        return Decimal(default)


class SharkExchangeAdapter:
    name = "shark"

    def __init__(self, settings: AppSettings, http_client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = http_client
        self._owns_client = http_client is None

    def supports(self, capability: Capability) -> bool:
        return capability in _CAPS

    async def _client_get(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.shark_base_url,
                timeout=self.settings.shark_request_timeout_seconds,
                verify=self.settings.shark_verify_ssl,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------- transport
    async def _signed_request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if not self.settings.shark_api_key or not self.settings.shark_api_secret:
            raise ExchangeError(
                RejectReason.LIVE_NOT_ENABLED,
                "Shark API credentials are not configured",
                FailureKind.VALIDATION,
            )
        client = await self._client_get()
        method = method.upper()
        try:
            if method == "GET":
                q = dict(params or {})
                q["timestamp"] = now_ms()
                query_string = "&".join(f"{k}={q[k]}" for k in sorted(q.keys()))
                # Docs sign the query string; keep their exact construction style.
                query_string = "&".join(f"{k}={q[k]}" for k in q.keys())
                signature = shark_sign_query(self.settings.shark_api_secret, query_string)
                resp = await client.get(
                    path,
                    params=q,
                    headers={"api-key": self.settings.shark_api_key, "signature": signature},
                )
            else:
                payload = dict(body or {})
                payload["timestamp"] = now_ms()
                
                # IMPORTANT: We MUST serialize the JSON ourselves with no spaces.
                # If we pass `json=payload` to httpx, it will add spaces (", ", ": ")
                # which will break the signature on the Exchange side.
                import json
                payload_str = json.dumps(payload, separators=(",", ":"))
                signature = shark_signature(self.settings.shark_api_secret, payload_str)
                
                resp = await client.request(
                    method,
                    path,
                    content=payload_str,
                    headers={
                        "api-key": self.settings.shark_api_key, 
                        "signature": signature,
                        "Content-Type": "application/json"
                    },
                )
        except httpx.TimeoutException as exc:
            raise OrderStateUncertain(f"Shark request timed out: {path}", path=path) from exc
        except httpx.HTTPError as exc:
            raise ExchangeError(
                RejectReason.SHARK_UNAVAILABLE, f"Shark transport error: {exc}", FailureKind.TEMPORARY, path=path
            ) from exc

        if resp.status_code >= 500:
            raise ExchangeError(
                RejectReason.SHARK_UNAVAILABLE,
                f"Shark server error {resp.status_code}",
                FailureKind.TEMPORARY,
                path=path,
            )
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise ExchangeError("INVALID_RESPONSE", f"Non-JSON response {resp.status_code}", path=path) from exc
        if resp.status_code >= 400:
            raise ExchangeError(
                "EXCHANGE_REJECTED",
                str(data),
                FailureKind.EXCHANGE_REJECTION,
                status=resp.status_code,
                path=path,
            )
        return data

    # --------------------------------------------------------------- guard
    @staticmethod
    def _assert_shark_symbol(symbol: str) -> None:
        """Never allow a raw TradingView symbol (e.g. ending '.P') to reach Shark."""
        if not symbol or symbol.endswith(".P") or "." in symbol:
            raise ExchangeError(
                "INVALID_SYMBOL_FORMAT",
                f"Refusing to submit non-Shark symbol format to Shark: {symbol!r}",
                FailureKind.VALIDATION,
            )

    # ---------------------------------------------------------------- market
    async def get_mark_price(self, symbol: str) -> Ticker:
        self._assert_shark_symbol(symbol)
        data = await self._signed_request("GET", f"/v1/market/ticker24Hr/{symbol}")
        inner = (data or {}).get("data", data) or {}
        last = _dec(inner.get("c"))
        return Ticker(
            symbol=symbol,
            last_price=last,
            mark_price=last,
            bid=_dec((inner.get("b") or [None])[0]) if inner.get("b") else None,
            raw=inner,
        )

    async def get_contract(self, symbol: str) -> ContractSpec | None:
        self._assert_shark_symbol(symbol)
        data = await self._signed_request("GET", "/v1/exchange/exchangeInfo")
        contracts = (data or {}).get("contracts") or []
        markets = (data or {}).get("markets") or []
        for market in markets if not contracts else [None]:
            if market is not None:
                contracts.extend(market.get("contracts") or [])
        for c in contracts:
            if str(c.get("name", "")).upper() == symbol.upper():
                filters = {f.get("filterType"): f for f in (c.get("filters") or []) if isinstance(f, dict)}
                qty_filter = filters.get("MARKET_QTY_SIZE") or filters.get("LIMIT_QTY_SIZE") or {}
                notional_filter = filters.get("MIN_NOTIONAL") or {}
                return ContractSpec(
                    symbol=str(c.get("name")),
                    contract_type=str(c.get("contractType") or "PERPETUAL"),
                    status="ACTIVE" if c.get("isDefaultContract") is not None or c.get("status", "ACTIVE") else "ACTIVE",
                    base_asset=str(c.get("baseAsset") or ""),
                    quote_asset=str(c.get("quoteAsset") or ""),
                    quantity_precision=int(c.get("quantityPrecision") or 8),
                    price_precision=int(c.get("pricePrecision") or 2),
                    min_quantity=_dec(qty_filter.get("minQty", 0)),
                    max_quantity=_dec(qty_filter.get("maxQty")) if qty_filter.get("maxQty") is not None else None,
                    min_notional=_dec(notional_filter.get("notional", 0)),
                    max_leverage=int(c["maxLeverage"]) if c.get("maxLeverage") else None,
                    margin_assets=list(c.get("marginAssetsSupported") or []),
                    raw=c,
                )
        return None

    # -------------------------------------------------------------- account
    async def get_account(self) -> AccountSnapshot:
        futures = await self._signed_request("GET", "/v1/wallet/futures-wallet/details", params={"marginAsset": self.settings.shark_margin_asset})
        balance = BalanceSnapshot(
            asset=str(futures.get("marginAsset") or self.settings.shark_margin_asset),
            wallet_balance=_dec(futures.get("walletBalance")),
            available_balance=_dec(futures.get("withdrawableBalance")),
            locked_balance=_dec(futures.get("lockedBalance")),
            raw=futures,
        )
        return AccountSnapshot(balances=[balance], raw=futures)

    async def get_balance(self) -> BalanceSnapshot:
        account = await self.get_account()
        return account.balances[0]

    # ------------------------------------------------------------- positions
    async def get_open_positions(self) -> list[PositionSnapshot]:
        data = await self._signed_request("GET", "/v1/positions/OPEN")
        rows = data if isinstance(data, list) else (data or {}).get("data") or []
        return [self._map_position(r) for r in rows]

    async def get_position(self, position_id: str) -> PositionSnapshot | None:
        data = await self._signed_request("GET", "/v1/positions", params={"positionId": position_id})
        rows = data if isinstance(data, list) else (data or {}).get("data") or (data or {}).get("positions") or []
        for r in rows:
            if str(r.get("positionId")) == position_id:
                return self._map_position(r)
        return None

    def _map_position(self, r: dict[str, Any]) -> PositionSnapshot:
        return PositionSnapshot(
            position_id=str(r.get("positionId") or r.get("id") or ""),
            symbol=str(r.get("contractPair") or r.get("symbol") or ""),
            side=str(r.get("positionType") or "LONG"),
            entry_price=_dec(r.get("entryPrice")),
            quantity=_dec(r.get("quantity") or r.get("positionAmount")),
            margin=_dec(r.get("marginInMarginAsset") or r.get("margin")),
            leverage=int(r.get("leverage") or 1),
            unrealized_pnl=_dec(r.get("realizedProfit") or 0),
            status=str(r.get("positionStatus") or "OPEN"),
            raw=r,
        )

    # ---------------------------------------------------------------- orders
    def _build_order_payload(self, req: OrderRequest, order_type: str) -> dict[str, Any]:
        """Exact verified field set from POST /v1/order/place-order docs."""
        self._assert_shark_symbol(req.symbol)
        
        def _clean_num(val: float | str | int | Decimal) -> int | float:
            fval = float(val)
            return int(fval) if fval.is_integer() else fval

        payload: dict[str, Any] = {
            "placeType": req.context.get("placeType", "ORDER_FORM"),
            "quantity": _clean_num(req.quantity),
            "side": req.side,
            "symbol": req.symbol,
            "reduceOnly": bool(req.reduce_only),
            "marginAsset": req.margin_asset or self.settings.shark_margin_asset,
            "type": order_type,
            "deviceType": self.settings.shark_device_type,
            "userCategory": self.settings.shark_user_category,
        }
        if order_type in ("LIMIT", "STOP_LIMIT"):
            if req.price is None:
                raise ExchangeError("INVALID_PRICE", "LIMIT orders require price", FailureKind.VALIDATION)
            payload["price"] = _clean_num(req.price)
        if order_type in ("STOP_MARKET", "STOP_LIMIT"):
            if req.stop_price is None:
                raise ExchangeError("INVALID_PRICE", "STOP orders require stopPrice", FailureKind.VALIDATION)
            payload["stopPrice"] = _clean_num(req.stop_price)
        if req.take_profit_price is not None:
            payload["takeProfitPrice"] = _clean_num(req.take_profit_price)
        if req.stop_loss_price is not None:
            payload["stopLossPrice"] = _clean_num(req.stop_loss_price)
        return payload

    async def _submit(self, req: OrderRequest, order_type: str) -> OrderResult:
        payload = self._build_order_payload(req, order_type)
        generated_id = f"tvsp-{uuid.uuid4().hex[:20]}"

        if self.settings.shark_dry_run:
            log_event(
                log, TradeEvent.DRY_RUN_ORDER,
                "DRY RUN — order payload constructed but NOT submitted",
                symbol=req.symbol, payload=payload,
            )
            return OrderResult(
                order_id=f"dryrun-{generated_id}", status="DRY_RUN", symbol=req.symbol,
                side=req.side, quantity=req.quantity, raw={"payload": payload, "dry_run": True}, dry_run=True,
            )

        try:
            data = await self._signed_request("POST", "/v1/order/place-order", body=payload)
        except OrderStateUncertain:
            # DO NOT RETRY. Result unknown -> caller must reconcile.
            log_event(log, TradeEvent.SYSTEM_ERROR, "Order submission outcome UNKNOWN — must reconcile",
                      level=logging.ERROR, symbol=req.symbol)
            raise
        except ExchangeError as exc:
            return OrderResult(
                order_id=generated_id, status="REJECTED", symbol=req.symbol, side=req.side,
                quantity=req.quantity, raw={"error": exc.message},
                error_code=str(exc.reason), error_message=exc.message,
            )

        status = "FILLED" if _dec(data.get("filledAmount")) >= _dec(data.get("orderAmount")) and _dec(data.get("orderAmount")) > 0 else "NEW"
        if _dec(data.get("filledAmount")) > 0 and status != "FILLED":
            status = "PARTIALLY_FILLED"
        return OrderResult(
            order_id=str(data.get("clientOrderId") or generated_id),
            status=status,
            symbol=str(data.get("symbol") or req.symbol),
            side=str(data.get("side") or req.side),
            quantity=_dec(data.get("orderAmount") or req.quantity),
            filled_quantity=_dec(data.get("filledAmount") or 0),
            avg_fill_price=_dec(data.get("price")) if data.get("price") else None,
            raw=data,
        )

    async def place_market_order(self, request: OrderRequest) -> OrderResult:
        return await self._submit(request, "MARKET")

    async def place_limit_order(self, request: OrderRequest) -> OrderResult:
        return await self._submit(request, "LIMIT")

    async def close_position(self, request: OrderRequest) -> OrderResult:
        """Close a single position via verified reduceOnly MARKET order."""
        request.reduce_only = True
        request.context.setdefault("placeType", "POSITION")
        return await self._submit(request, "MARKET")

    async def close_all_positions(self) -> dict[str, Any]:
        if self.settings.shark_dry_run:
            return {"success": True, "data": [], "dry_run": True}
        return await self._signed_request("DELETE", "/v1/positions/close-all-positions", body={})

    async def place_take_profit(self, request: OrderRequest) -> OrderResult:
        """Exchange-side TP via verified takeProfitPrice on place-order (subType TAKE_PROFIT)."""
        request.context.setdefault("placeType", "POSITION")
        if request.take_profit_price is None:
            raise ExchangeError("INVALID_PRICE", "take_profit_price required", FailureKind.VALIDATION)
        return await self._submit(request, "STOP_MARKET" if request.stop_price else "MARKET")

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        if self.settings.shark_dry_run:
            return {"clientOrderId": order_id, "status": "CANCELED", "success": True, "dry_run": True}
        return await self._signed_request("DELETE", "/v1/order/delete-order", body={"clientOrderId": order_id})

    async def get_order(self, order_id: str) -> OrderResult | None:
        try:
            data = await self._signed_request("GET", f"/v1/order/{order_id}")
        except ExchangeError:
            return None
        if not data:
            return None
        return OrderResult(
            order_id=str(data.get("clientOrderId") or order_id),
            status=str(data.get("status") or "UNKNOWN"),
            symbol=str(data.get("symbol") or ""),
            side=str(data.get("side") or ""),
            quantity=_dec(data.get("quantity") or 0),
            filled_quantity=_dec(data.get("filledQty") or 0),
            avg_fill_price=_dec(data.get("avgPrice")) if data.get("avgPrice") else None,
            raw=data,
        )

    async def get_open_orders(self) -> list[OrderResult]:
        data = await self._signed_request("GET", "/v1/order/open-orders")
        rows = data if isinstance(data, list) else (data or {}).get("data") or []
        out: list[OrderResult] = []
        for r in rows:
            out.append(
                OrderResult(
                    order_id=str(r.get("clientOrderId") or ""),
                    status=str(r.get("status") or "NEW"),
                    symbol=str(r.get("symbol") or ""),
                    side=str(r.get("side") or ""),
                    quantity=_dec(r.get("orderAmount") or 0),
                    filled_quantity=_dec(r.get("filledAmount") or 0),
                    raw=r,
                )
            )
        return out

    async def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        self._assert_shark_symbol(symbol)
        return await self._signed_request(
            "POST", "/v1/exchange/update/leverage",
            body={"leverage": int(leverage), "contractName": symbol},
        )

    async def health(self) -> str:
        if self.settings.shark_dry_run:
            return "DEGRADED"  # configured but not transmitting
        try:
            await self._signed_request("GET", "/v1/market/ticker24Hr/BTCUSDT")
            return "CONNECTED"
        except Exception:  # noqa: BLE001
            return "DISCONNECTED"


SharkAdapter = SharkExchangeAdapter
