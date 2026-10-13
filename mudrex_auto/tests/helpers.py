"""Test doubles: a fake Mudrex HTTP server (httpx.MockTransport) and settings builders.

No test in this suite contacts the network or uses a real credential.
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable
from urllib.parse import parse_qs

import httpx

from app.config import Settings
from app.mudrex_client import MudrexClient

TEST_SECRET = "TEST-ONLY-SECRET-0123456789"
CHANNEL_ID = -1001234567890
ASSET_CRV = {
    "id": "asset-crv",
    "name": "Curve",
    "symbol": "CRVUSDT",
    "min_contract": "1",
    "max_contract": "1000000",
    "quantity_step": "0.1",
    "min_price": "0.0001",
    "max_price": "1000",
    "price_step": "0.0001",
    "max_market_contract": "100000",
    "min_notional_value": "5",
    "leverage_step": "1",
    "min_leverage": "1",
    "max_leverage": "20",
    "price": "0.3500",
}
ASSET_EIGEN = {
    "id": "asset-eigen",
    "name": "Eigen",
    "symbol": "EIGENUSDT",
    "min_contract": "0.1",
    "max_contract": "100000",
    "quantity_step": "0.1",
    "min_price": "0.00001",
    "max_price": "1000",
    "price_step": "0.00001",
    "max_market_contract": "10000",
    "min_notional_value": "5",
    "leverage_step": "1",
    "min_leverage": "1",
    "max_leverage": "20",
    "price": "0.2430",
}


def make_settings(tmp_path, **env_overrides: str) -> Settings:
    env = {
        "TRADING_MODE": "PAPER",
        "MUDREX_API_SECRET": TEST_SECRET,
        "TELEGRAM_API_ID": "12345",
        "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef",
        "TELEGRAM_PHONE": "+10000000000",
        "DATABASE_PATH": str(tmp_path / "trading.db"),
        "TELEGRAM_SESSION_NAME": str(tmp_path / "session"),
    }
    env.update(env_overrides)
    return Settings.from_env(env)


class FakeMudrex:
    """Minimal stateful stand-in for the Mudrex futures API used by the app."""

    def __init__(self, *, assets: list[dict] | None = None, balance: str = "500") -> None:
        self.assets = {a["symbol"]: dict(a) for a in (assets or [ASSET_CRV, ASSET_EIGEN])}
        self.balance = balance
        self.leverage: dict[tuple[str, str], dict] = {}
        self.orders: dict[str, dict] = {}
        self.positions: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict, Any]] = []
        self.auth_headers: list[str | None] = []
        self._ids = itertools.count(1)
        self.fail_next: list[Callable[[httpx.Request], httpx.Response | None]] = []
        self.order_status_on_create = "CREATED"

    # -- plumbing ------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self, *, allow_mutations: bool, sleep=None) -> MudrexClient:
        async def _no_sleep(_: float) -> None:
            return None

        return MudrexClient(
            base_url="https://trade.mudrex.com",
            api_secret=TEST_SECRET,
            allow_mutations=allow_mutations,
            transport=self.transport(),
            sleep=sleep or _no_sleep,
        )

    def mutating_calls(self) -> list[tuple[str, str, dict, Any]]:
        return [c for c in self.calls if c[0] != "GET"]

    @staticmethod
    def _ok(data: Any, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json={"success": True, "data": data})

    @staticmethod
    def _err(status: int, text: str) -> httpx.Response:
        return httpx.Response(status, json={"success": False, "errors": [{"code": status, "text": text}]})

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        body = json.loads(request.content.decode()) if request.content else None
        self.calls.append((request.method, path, params, body))
        self.auth_headers.append(request.headers.get("X-Authentication"))

        for injector in list(self.fail_next):  # injectors persist; they decide when to act
            result = injector(request)
            if result is not None:
                return result

        m, p = request.method, path
        if m == "GET" and p == "/fapi/v1/futures":
            offset, limit = int(params.get("offset", 0)), int(params.get("limit", 20))
            items = list(self.assets.values())[offset: offset + limit]
            return self._ok(items)
        if m == "GET" and p == "/fapi/v1/futures/funds":
            return self._ok({"balance": self.balance, "locked_amount": "0", "first_time_user": False})
        if p.startswith("/fapi/v1/futures/") and p.endswith("/leverage"):
            symbol = p[len("/fapi/v1/futures/"): -len("/leverage")]
            currency = params.get("trade_currency", "USDT")
            if m == "GET":
                entry = self.leverage.get((symbol, currency))
                return self._err(404, "leverage not found") if entry is None else self._ok(entry)
            if m == "POST":
                lev = str(body["leverage"])
                self.leverage[(symbol, body.get("trade_currency", "USDT"))] = {
                    "margin_type": "ISOLATED", "leverage": lev, "collateral_currency_id": 0}
                return self._ok(self.leverage[(symbol, body.get("trade_currency", "USDT"))])
        if m == "GET" and p == "/fapi/v1/futures/orders/detail":
            order = self._find_order(params)
            return self._err(404, "order not found") if order is None else self._ok(order)
        if m == "GET" and p == "/fapi/v1/futures/orders":
            return self._ok([o for o in self.orders.values() if o["status"] in ("CREATED", "PARTIAL_FILLED")])
        if m == "GET" and p == "/fapi/v1/futures/positions":
            return self._ok(list(self.positions.values()))
        if m == "POST" and p == "/fapi/v2/futures/order":
            return self._create_order(params, body)
        if m == "DELETE" and p.startswith("/fapi/v1/futures/orders/"):
            order_id = p.rsplit("/", 1)[1]
            order = self.orders.get(order_id)
            if order is not None:
                order["status"] = "CANCELLED"
                return self._ok({"message": "OK", "order_id": order_id, "status": "CANCELLED"})
            # Check position TP/SL orders
            for pos in self.positions.values():
                tp = pos.get("takeprofit")
                if tp and tp.get("order_id") == order_id:
                    pos["takeprofit"] = None
                    return self._ok({"message": "OK", "order_id": order_id, "status": "CANCELLED"})
                sl = pos.get("stoploss")
                if sl and sl.get("order_id") == order_id:
                    pos["stoploss"] = None
                    return self._ok({"message": "OK", "order_id": order_id, "status": "CANCELLED"})
            return self._err(404, "order not found")
        if m == "POST" and p.startswith("/fapi/v1/futures/positions/") and p.endswith("/close/partial"):
            pos_id = p[len("/fapi/v1/futures/positions/"): -len("/close/partial")]
            pos = self.positions.get(pos_id)
            if pos is None:
                return self._err(400, "position is not open")
            pos["quantity"] = str(Decimal(pos["quantity"]) - Decimal(str(body["quantity"])))
            return self._ok({"order_id": f"part-{next(self._ids)}", "position_id": pos_id, "quantity": str(body["quantity"])})
        if m == "POST" and p.startswith("/fapi/v1/futures/positions/") and p.endswith("/close"):
            pos_id = p[len("/fapi/v1/futures/positions/"): -len("/close")]
            if pos_id not in self.positions:
                return self._err(400, "position is not open")
            del self.positions[pos_id]
            return self._ok({"order_id": f"close-{next(self._ids)}", "position_id": pos_id, "status": "CREATED", "message": "OK"})
        if m == "POST" and p.startswith("/fapi/v1/futures/positions/") and p.endswith("/riskorder"):
            pos_id = p[len("/fapi/v1/futures/positions/"): -len("/riskorder")]
            pos = self.positions.get(pos_id)
            if pos is None:
                return self._err(404, "Position not found")
            if body.get("is_stoploss"):
                pos["stoploss"] = {"price": str(body.get("stoploss_price")), "order_id": f"sl-{next(self._ids)}"}
            if body.get("is_takeprofit"):
                pos["takeprofit"] = {"price": str(body.get("takeprofit_price")), "order_id": f"tp-{next(self._ids)}"}
            return self._ok({"position_id": pos_id, "status": "CREATED", "message": "Risk order placed successfully"})
        if m == "GET" and p.startswith("/fapi/v1/futures/") and p.count("/") == 4:
            symbol = p.rsplit("/", 1)[1]
            asset = self.assets.get(symbol)
            return self._err(404, "asset not found") if asset is None else self._ok(asset)
        return self._err(404, f"fake route not implemented: {m} {p}")

    def _find_order(self, params: dict) -> dict | None:
        if "order_id" in params:
            return self.orders.get(params["order_id"])
        if "client_order_id" in params:
            for order in self.orders.values():
                if order.get("client_order_id") == params["client_order_id"]:
                    return order
        return None

    def _create_order(self, params: dict, body: dict) -> httpx.Response:
        symbol = params.get("symbol", "")
        asset = self.assets.get(symbol)
        if asset is None:
            return self._err(400, "asset not found")
        if (symbol, body.get("trade_currency", "USDT")) not in self.leverage:
            return self._err(400, "leverage not found")
        order_id = f"order-{next(self._ids)}"
        position_id = f"pos-{order_id}"
        cid = body.get("client_order_id")
        if cid and any(o.get("client_order_id") == cid for o in self.orders.values()):
            return self._err(409, "client order id already exists")
        order = {
            "id": order_id,
            "client_order_id": cid,
            "position_id": position_id,
            "future_position_uuid": position_id,
            "symbol": symbol,
            "status": self.order_status_on_create,
            "order_type": body["order_type"],
            "trigger_type": body["trigger_type"],
            "quantity": body["quantity"],
            "filled_quantity": "0",
            "price": body["order_price"],
            "filled_price": "0",
            "leverage": self.leverage[(symbol, body.get("trade_currency", "USDT"))]["leverage"],
            "trade_currency": body.get("trade_currency", "USDT"),
            "takeprofit": body.get("takeprofit_price"),
        }
        self.orders[order_id] = order
        return self._ok({
            "leverage": order["leverage"], "quantity": body["quantity"], "price": body["order_price"],
            "order_id": order_id, "status": "CREATED", "message": "OK",
            "client_order_id": cid, "position_id": position_id,
        }, status=202)

    # -- scenario helpers ---------------------------------------------
    def fill_order(self, order_id: str, price: str | None = None) -> None:
        order = self.orders[order_id]
        order["status"] = "FILLED"
        order["filled_quantity"] = order["quantity"]
        order["filled_price"] = price or order["price"]
        self.positions[order["position_id"]] = {
            "id": order["position_id"], "position_id": order["position_id"], "symbol": order["symbol"],
            "order_type": order["order_type"], "status": "OPEN", "entry_price": order["price"],
            "quantity": order["quantity"], "leverage": order["leverage"],
            "takeprofit": {"price": order["takeprofit"], "order_id": f"tp-{order_id}",
                           "order_type": "TAKEPROFIT"} if order.get("takeprofit") else None,
        }


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
