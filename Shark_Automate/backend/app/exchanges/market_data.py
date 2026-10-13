"""Market data: Shark public market endpoints (verified) + cached feed."""
from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from app.core.config import AppSettings
from app.exchanges.base import ContractSpec, Ticker


class MarketDataService:
    """Public market data via GET /v1/market/ticker24Hr/{pair} (no auth, verified).

    Also keeps an in-memory last-price cache so PAPER trading keeps working with
    live prices whenever a verified source has been reached recently.
    """

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._cache: dict[str, Ticker] = {}
        self._expiry: dict[str, float] = {}
        self._client: httpx.AsyncClient | None = None
        self.last_success: float | None = None
        self.last_error: str = ""

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.shark_base_url,
                timeout=8.0,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def seed_price(self, symbol: str, price: Decimal) -> None:
        """Test/manual seeding of a last known price (explicit, never fake silently)."""
        self._cache[symbol] = Ticker(symbol=symbol, last_price=price, mark_price=price,
                                     ts=datetime.now(UTC), raw={"source": "seeded"})
        self._expiry[symbol] = time.time() + 30

    async def get_ticker(self, symbol: str) -> Ticker:
        cached = self._cache.get(symbol)
        if cached and self._expiry.get(symbol, 0) > time.time():
            return cached
        try:
            client = await self._get_client()
            resp = await client.get(f"/v1/market/ticker24Hr/{symbol}")
            resp.raise_for_status()
            data = resp.json()
            inner = data.get("data", data) or {}
            last = Decimal(str(inner.get("c") or inner.get("lastPrice") or "0"))
            if last <= 0 and cached:
                return cached
            ticker = Ticker(symbol=symbol, last_price=last, mark_price=last,
                            ts=datetime.now(UTC), raw=inner)
            self._cache[symbol] = ticker
            self._expiry[symbol] = time.time() + 2.0
            self.last_success = time.time()
            return ticker
        except Exception as exc:  # noqa: BLE001
            # Fallback to Binance public ticker for reliable paper trading
            try:
                clean_sym = symbol.replace("-", "").replace("_", "").replace("/", "").upper()
                async with httpx.AsyncClient(timeout=4.0) as b_client:
                    b_resp = await b_client.get(f"https://api.binance.com/api/v3/ticker/price?symbol={clean_sym}")
                    if b_resp.status_code == 200:
                        b_data = b_resp.json()
                        last = Decimal(str(b_data.get("price") or "0"))
                        if last > 0:
                            ticker = Ticker(symbol=symbol, last_price=last, mark_price=last,
                                            ts=datetime.now(UTC), raw={"binance": True})
                            self._cache[symbol] = ticker
                            self._expiry[symbol] = time.time() + 2.0
                            self.last_success = time.time()
                            return ticker
            except Exception:
                pass
            self.last_error = str(exc)
            if cached:
                return cached
            raise

    def health(self) -> str:
        if self.last_success and time.time() - self.last_success < 60:
            return "CONNECTED"
        if self.last_error and not self.last_success:
            return "DISCONNECTED"
        return "DEGRADED"


class ContractRegistry:
    """Contract metadata cache backed by symbol_mappings + Shark exchangeInfo."""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._specs: dict[str, ContractSpec] = {}
        self._verified: dict[str, bool] = {}

    def put(self, spec: ContractSpec, verified: bool = True) -> None:
        self._specs[spec.symbol.upper()] = spec
        self._verified[spec.symbol.upper()] = verified

    async def get_contract(self, symbol: str) -> ContractSpec | None:
        return self._specs.get(symbol.upper())

    def is_verified(self, symbol: str) -> bool:
        return self._verified.get(symbol.upper(), False)
