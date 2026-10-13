"""Test doubles: a fake Pi42 HTTP API (``httpx.MockTransport``) and fixtures.

No test in this suite contacts the network or uses a real credential. The fake
verifies the HMAC signature of every authenticated request against the exact
bytes that were sent, so a re-serialised body (which would break the signature
in production) fails the tests instead of the live account.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable
from urllib.parse import parse_qs

import httpx

from app.config import Settings
from app.pi42_client import Pi42Client

TEST_API_KEY = "TEST-ONLY-KEY-0001"
TEST_API_SECRET = "TEST-ONLY-SECRET-0123456789"
CHANNEL_ID = -1001234567890

CONTRACT_CRV = {
    "name": "CRVUSDT",
    "market": "USDT",
    "contractName": "CRVUSDT",
    "baseAsset": "CRV",
    "quoteAsset": "USDT",
    "contractType": "PERPETUAL",
    "maxLeverage": "20",
    "pricePrecision": 4,
    "quantityPrecision": 1,
    "orderTypes": ["LIMIT", "MARKET", "STOP_LIMIT", "STOP_MARKET"],
    "marginAssetsSupported": ["USDT"],
    "makerFee": "0.0002",
    "takerFee": "0.0005",
    "maintenanceMarginPercentage": "0.5",
    "limitPriceVarAllowed": "5",
    "isDefaultContract": True,
    "filters": [
        {"filterType": "LIMIT_QTY_SIZE", "minQty": "0.1", "maxQty": "1000000"},
        {"filterType": "MARKET_QTY_SIZE", "minQty": "0.1", "maxQty": "100000"},
        {"filterType": "MIN_NOTIONAL", "notional": "5"},
    ],
}

CONTRACT_EIGEN = {
    "name": "EIGENUSDT",
    "market": "USDT",
    "baseAsset": "EIGEN",
    "quoteAsset": "USDT",
    "contractType": "PERPETUAL",
    "maxLeverage": "20",
    "pricePrecision": 5,
    "quantityPrecision": 1,
    "orderTypes": ["LIMIT", "MARKET"],
    "marginAssetsSupported": ["USDT"],
    "filters": [
        {"filterType": "LIMIT_QTY_SIZE", "minQty": "0.1", "maxQty": "100000"},
        {"filterType": "MIN_NOTIONAL", "notional": "5"},
    ],
}

CONTRACT_BTC_INR = {
    "name": "BTCINR",
    "market": "INR",
    "baseAsset": "BTC",
    "quoteAsset": "INR",
    "contractType": "PERPETUAL",
    "maxLeverage": "50",
    "pricePrecision": 1,
    "quantityPrecision": 5,
    "orderTypes": ["LIMIT", "MARKET", "STOP_LIMIT", "STOP_MARKET"],
    "marginAssetsSupported": ["INR", "USDT"],
    "filters": [
        {"filterType": "LIMIT_QTY_SIZE", "minQty": "0.00001", "maxQty": "100"},
        {"filterType": "MIN_NOTIONAL", "notional": "100"},
    ],
}


def make_settings(tmp_path, **env_overrides: str) -> Settings:
    env = {
        "TRADING_MODE": "PAPER",
        "PI42_API_KEY": TEST_API_KEY,
        "PI42_API_SECRET": TEST_API_SECRET,
        "TELEGRAM_API_ID": "12345",
        "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef",
        "TELEGRAM_PHONE": "+10000000000",
        "DATABASE_PATH": str(tmp_path / "trading.db"),
        "TELEGRAM_SESSION_NAME": str(tmp_path / "session"),
    }
    env.update(env_overrides)
    return Settings.from_env(env)


class FakePi42:
    """Stateful stand-in for the authenticated + public Pi42 endpoints."""

    def __init__(
        self,
        *,
        contracts: list[dict] | None = None,
        balance: str = "500",
        tickers: dict[str, str] | None = None,
        require_valid_signature: bool = True,
    ) -> None:
        self.contracts = {
            c["name"]: dict(c) for c in (contracts if contracts is not None
                                         else [CONTRACT_CRV, CONTRACT_EIGEN, CONTRACT_BTC_INR])
        }
        self.balance = balance
        self.tickers = {k.upper(): str(v) for k, v in (tickers or {"CRVUSDT": "0.3500"}).items()}
        self.orders: dict[str, dict] = {}
        self.positions: dict[str, dict] = {}
        self.preferences: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict, Any]] = []
        self.raw_bodies: list[str] = []
        self.auth_headers: list[tuple[str | None, str | None]] = []
        self.signature_ok: list[bool] = []
        self.require_valid_signature = require_valid_signature
        self._ids = itertools.count(1)
        self.fail_next: list[Callable[[httpx.Request], httpx.Response | None]] = []

    # -- plumbing ------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self, *, allow_mutations: bool, max_retries: int = 0, sleep=None,
               rate_limit: int = 10_000) -> Pi42Client:
        async def _no_sleep(_: float) -> None:
            return None

        return Pi42Client(
            base_url="https://fapi.pi42.com",
            public_base_url="https://api.pi42.com",
            api_key=TEST_API_KEY,
            api_secret=TEST_API_SECRET,
            allow_mutations=allow_mutations,
            transport=self.transport(),
            max_retries=max_retries,
            sleep=sleep or _no_sleep,
            requests_per_minute=rate_limit,
            delete_requests_per_minute=rate_limit,
        )

    def mutating_calls(self) -> list[tuple[str, str, dict, Any]]:
        return [c for c in self.calls if c[0] != "GET"]

    def order_by_ref(self, ref_id: str) -> dict | None:
        for order in self.orders.values():
            if order.get("refId") == ref_id:
                return order
        return None

    @staticmethod
    def _ok(data: Any, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json=data)

    @staticmethod
    def _err(code: int, message: str, status: int = 400) -> httpx.Response:
        return httpx.Response(status, json={"code": code, "message": message})

    def _verify(self, request: httpx.Request) -> bool:
        """Recompute the HMAC over exactly what was sent and compare."""
        key = request.headers.get("api-key")
        signature = request.headers.get("signature")
        if not key or not signature:
            return False
        if request.method == "GET":
            data_to_sign = request.url.query.decode()
        else:
            data_to_sign = request.content.decode()
        expected = hmac.new(TEST_API_SECRET.encode(), data_to_sign.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        # parse_float=Decimal keeps the exact numbers Pi42 received, so tests can
        # assert on prices without binary-float artefacts.
        body = json.loads(request.content.decode(), parse_float=Decimal) if request.content else None
        self.calls.append((request.method, path, params, body))
        self.auth_headers.append((request.headers.get("api-key"), request.headers.get("signature")))
        if request.content:
            self.raw_bodies.append(request.content.decode())

        signed = request.headers.get("signature") is not None
        if signed:
            valid = self._verify(request)
            self.signature_ok.append(valid)
            if self.require_valid_signature and not valid:
                return self._err(1002, "Invalid signature", status=401)

        for injector in list(self.fail_next):
            result = injector(request)
            if result is not None:
                return result

        return self.route(request.method, path, params, body)

    # -- routes --------------------------------------------------------
    def route(self, method: str, path: str, params: dict, body: Any) -> httpx.Response:
        if method == "GET" and path == "/v1/exchange/exchangeInfo":
            market = (params.get("market") or "").upper()
            contracts = [c for c in self.contracts.values()
                         if not market or c.get("market", "").upper() == market]
            return self._ok({"contracts": contracts, "assetPrecisions": {"INR": 2, "USDT": 6},
                             "conversionRates": {"INR_USDT": "0.012"}})
        if method == "GET" and path.startswith("/v1/market/ticker24Hr/"):
            pair = path.rsplit("/", 1)[1].upper()
            price = self.tickers.get(pair)
            if price is None:
                return self._err(2402, f"pair {pair} not found", status=404)
            return self._ok({"pair": pair, "c": price, "h": price, "l": price, "o": price})
        if method == "GET" and path == "/v1/wallet/futures-wallet/details":
            asset = (params.get("marginAsset") or "USDT").upper()
            return self._ok({
                "marginAsset": asset, "walletBalance": self.balance, "withdrawableBalance": self.balance,
                "lockedBalance": "0", "marginBalance": self.balance, "unrealisedPnlCross": "0",
            })
        if method == "GET" and path == "/v1/order":
            order = self.orders.get(params.get("clientOrderId", ""))
            if order is None:
                return self._err(2502, "Order not found", status=404)
            return self._ok(order)
        if method == "GET" and path == "/v1/order/ref-id":
            order = self.order_by_ref(params.get("ref_id") or params.get("refId") or "")
            if order is None:
                return self._err(3012, "Invalid ref id", status=404)
            return self._ok(order)
        if method == "GET" and path == "/v1/order/open-orders":
            symbol = (params.get("symbol") or "").upper()
            open_orders = [o for o in self.orders.values()
                           if o["status"] in ("NEW", "PARTIALLY_FILLED")
                           and (not symbol or o["symbol"] == symbol)]
            return self._ok(open_orders)
        if method == "GET" and path == "/v1/order/order-history":
            symbol = (params.get("symbol") or "").upper()
            history = [o for o in self.orders.values() if not symbol or o["symbol"] == symbol]
            return self._ok(history)
        if method == "GET" and path == "/v1/positions/OPEN":
            symbol = (params.get("symbol") or "").upper()
            open_positions = [p for p in self.positions.values()
                              if not symbol or p["contractPair"] == symbol]
            return self._ok(open_positions)
        if method == "GET" and path == "/v1/positions":
            position = self.positions.get(params.get("positionId", ""))
            if position is None:
                return self._err(2502, "Position not found", status=404)
            return self._ok(position)
        if method == "POST" and path in ("/v1/exchange/update/preference", "/v1/exchange/update/leverage"):
            return self._update_preference(body or {})
        if method == "POST" and path == "/v1/order/place-order":
            return self._place_order(body or {})
        if method == "POST" and path == "/v2/order/split-tp-sl":
            return self._split_tp_sl(body or {})
        if method == "DELETE" and path == "/v1/order/delete-order":
            return self._cancel_order(body or {})
        return self._err(9001, f"fake route not implemented: {method} {path}", status=404)

    def _update_preference(self, body: dict) -> httpx.Response:
        contract = str(body.get("contractName") or "").upper()
        if contract not in self.contracts:
            return self._err(2302, "contract not found", status=404)
        if any(o["symbol"] == contract and o["status"] in ("NEW", "PARTIALLY_FILLED")
               for o in self.orders.values()) or any(
                   p["contractPair"] == contract for p in self.positions.values()
        ):
            return self._err(2305, "Cannot change leverage while position or open order exists")
        leverage = int(body["leverage"])
        margin_mode = str(body.get("marginMode") or "ISOLATED").upper()
        self.preferences[contract] = {"leverage": leverage, "marginMode": margin_mode}
        return self._ok({"contractName": contract, "updatedLeverage": leverage,
                         "marginMode": margin_mode, "message": "Leverage updated successfully"})

    def _place_order(self, body: dict) -> httpx.Response:
        symbol = str(body.get("symbol") or "").upper()
        contract = self.contracts.get(symbol)
        if contract is None:
            return self._err(2302, "contract not found", status=404)
        if body.get("type") == "LIMIT" and body.get("price") in (None, ""):
            return self._err(3005, "price not present")
        quantity = Decimal(str(body.get("quantity") or 0))
        if quantity <= 0:
            return self._err(3004, "invalid quantity")
        if body.get("reduceOnly") and not body.get("positionId"):
            return self._err(3009, "reduce only orders require a position")
        ref_id = body.get("refId")
        if ref_id and self.order_by_ref(str(ref_id)) is not None:
            return self._err(3019, "Ref id already exists")
        requested_cid = body.get("clientOrderId")
        if requested_cid and requested_cid in self.orders:
            return self._err(3019, "Ref id already exists")
        client_order_id = str(requested_cid or f"cli-{next(self._ids):08d}-ext")
        signature = self.auth_headers[-1][1] if self.auth_headers else None
        took_profit = body.get("takeProfitPrice")
        order = {
            "clientOrderId": client_order_id,
            "orderId": client_order_id,
            "refId": str(ref_id) if ref_id else None,
            "symbol": symbol,
            "contractPair": symbol,
            "side": str(body.get("side") or "").upper(),
            "type": str(body.get("type") or "").upper(),
            "placeType": str(body.get("placeType") or "ORDER_FORM").upper(),
            "status": "NEW",
            "quantity": str(quantity),
            "orderAmount": str(quantity),
            "filledQty": "0",
            "filledAmount": "0",
            "price": str(body.get("price")) if body.get("price") is not None else "0",
            "limitPrice": str(body.get("price")) if body.get("price") is not None else None,
            "takeProfitPrice": str(took_profit) if took_profit is not None else None,
            "stopLossPrice": str(body["stopLossPrice"]) if body.get("stopLossPrice") is not None else None,
            "stopPrice": str(body.get("stopPrice")) if body.get("stopPrice") is not None else None,
            "reduceOnly": bool(body.get("reduceOnly")),
            "marginAsset": str(body.get("marginAsset") or "").upper(),
            "positionId": body.get("positionId"),
            "leverage": self.preferences.get(symbol, {}).get("leverage"),
            "createdTime": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
            "signature": signature,
            "requestBody": json.dumps(body, separators=(",", ":"), default=str),
        }
        self.orders[client_order_id] = order
        return self._ok({
            "symbol": symbol, "clientOrderId": client_order_id, "orderId": client_order_id,
            "refId": order["refId"], "orderAmount": str(quantity), "filledAmount": "0",
            "price": order["price"], "type": order["type"], "side": order["side"],
            "placeType": order["placeType"], "leverage": order["leverage"],
            "lockedMargin": "1", "status": "NEW",
        })

    def _split_tp_sl(self, body: dict) -> httpx.Response:
        position = self.positions.get(str(body.get("positionId") or ""))
        if position is None:
            return self._err(2502, "Position not found", status=404)
        legs = body.get("splitTakeProfitOrders") or []
        if not legs:
            return self._err(3004, "no take-profit legs supplied")
        position["takeProfitOrders"] = [dict(leg) for leg in legs]
        return self._ok({"positionId": position["positionId"], "takeProfitOrders": len(legs),
                         "message": "Take profit updated"})

    def _cancel_order(self, body: dict) -> httpx.Response:
        client_order_id = str(body.get("clientOrderId") or "")
        order = self.orders.get(client_order_id)
        if order is None:
            return self._err(3004, "order not found", status=404)
        if order["status"] == "FILLED":
            return self._err(3004, "order already filled")
        order["status"] = "CANCELLED"
        return self._ok({"clientOrderId": client_order_id, "status": "CANCELLED",
                         "message": "Order cancelled"})

    # -- scenario helpers ---------------------------------------------
    def set_ticker(self, pair: str, price: str) -> None:
        self.tickers[pair.upper()] = str(price)

    def latest_order(self) -> dict:
        return list(self.orders.values())[-1]

    def fill_order(self, client_order_id: str, price: str | None = None) -> dict:
        """Fill an order and open the matching position (LONG/SHORT from the side)."""
        order = self.orders[client_order_id]
        fill_price = str(price if price is not None else order["price"])
        order["status"] = "FILLED"
        order["filledQty"] = order["orderAmount"]
        order["filledAmount"] = order["orderAmount"]
        order["avgPrice"] = fill_price
        if order["placeType"] == "POSITION" and order.get("positionId"):
            position = self.positions.get(order["positionId"])
            if position is not None:
                position["positionAmount"] = "0"
                position["quantity"] = "0"
                position["positionStatus"] = "CLOSED"
                position["realizedProfit"] = "1"
            return order
        position_id = f"pos-{next(self._ids)}"
        position = {
            "positionId": position_id,
            "contractPair": order["symbol"],
            "positionType": "LONG" if order["side"] == "BUY" else "SHORT",
            "positionStatus": "OPEN",
            "entryPrice": fill_price,
            "positionAmount": order["orderAmount"],
            "quantity": order["orderAmount"],
            "leverage": str(order.get("leverage") or 1),
            "marginType": "ISOLATED",
            "margin": "1",
            "marginInMarginAsset": "1",
            "marginAsset": order.get("marginAsset") or "USDT",
            "liquidationPrice": "0",
            "realizedProfit": "0",
        }
        if order.get("takeProfitPrice"):
            position["takeProfitPrice"] = order["takeProfitPrice"]
        order["positionId"] = position_id
        self.positions[position_id] = position
        return order


def utc(now: datetime | None = None) -> datetime:
    return now or datetime.now(timezone.utc)


def minutes_ago(minutes: float, now: datetime) -> datetime:
    return now - timedelta(minutes=minutes)


SIGNAL_CRV = """Vip Crypto Signal Jemal

#CRV/USDT - Short🔴

Entry: 0.3462

Stop Loss: 0.3662

Target 1: 0.3361
Target 2: 0.32627
Target 3: 0.28398

Leverage: x14"""

SIGNAL_EIGEN = """Vip Crypto Signal Jemal

#EIGEN/USDT - Short🔴

Entry: 0.2425

Stop Loss: 0.34008

Target 1: 0.21856

Leverage: x16"""


def d(value: str) -> Decimal:
    return Decimal(value)
