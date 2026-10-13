"""Exact contract resolution against Pi42's cached exchange metadata.

A Telegram signal (``#CRV/USDT``) becomes a Pi42 contract pair only when the
quote matches the configured market **exactly**: ``PI42_MARKET=USDT`` ->
``CRVUSDT``, ``PI42_MARKET=INR`` -> ``CRVINR``. Similarly named contracts are
never substituted and prices are never converted between currencies.
"""

from __future__ import annotations

import time
from typing import Callable, Iterable, Mapping

from app.models import ContractInfo


class UnsupportedSymbolError(Exception):
    """The Telegram symbol has no exact, unambiguous Pi42 contract."""


def index_contracts(contracts: Iterable[ContractInfo]) -> dict[str, ContractInfo]:
    index: dict[str, ContractInfo] = {}
    for contract in contracts:
        if contract.symbol in index:
            raise UnsupportedSymbolError(f"duplicate Pi42 listing for {contract.symbol}; refusing to choose")
        index[contract.symbol] = contract
    return index


def contract_pair(base_asset: str, market: str) -> str:
    return f"{base_asset.strip().upper()}{market.strip().upper()}"


def find_exact_symbol(index: Mapping[str, ContractInfo], symbol: str) -> ContractInfo:
    normalized = symbol.strip().upper()
    contract = index.get(normalized)
    if contract is None:
        raise UnsupportedSymbolError(f"{normalized} is not listed as a Pi42 perpetual contract")
    return contract


class SymbolMapper:
    """Caches Pi42's contract listing and refreshes it after ``ttl_seconds``."""

    def __init__(
        self,
        client,
        ttl_seconds: int,
        *,
        market: str = "USDT",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._market = market.upper()
        self._clock = clock
        self._index: dict[str, ContractInfo] | None = None
        self._loaded_at = 0.0

    @property
    def market(self) -> str:
        return self._market

    async def refresh(self) -> dict[str, ContractInfo]:
        contracts = await self._client.list_contracts(self._market)
        if not contracts:
            raise UnsupportedSymbolError(
                f"Pi42 returned no {self._market}-margined contracts; refusing to trade blind"
            )
        self._index = index_contracts(contracts)
        self._loaded_at = self._clock()
        return self._index

    async def get_index(self, *, force: bool = False) -> dict[str, ContractInfo]:
        stale = self._index is None or (self._clock() - self._loaded_at) >= self._ttl
        if force or stale:
            return await self.refresh()
        return self._index  # type: ignore[return-value]

    async def resolve(self, symbol: str, *, quote: str | None = None, force: bool = False) -> ContractInfo:
        """Resolve a signal symbol to a Pi42 contract, or raise with a precise reason."""
        normalized = symbol.strip().upper()
        if quote and quote.strip().upper() != self._market:
            raise UnsupportedSymbolError(
                f"signal is quoted in {quote.strip().upper()} but PI42_MARKET={self._market}; "
                "prices are never converted between currencies"
            )
        if not normalized.endswith(self._market):
            raise UnsupportedSymbolError(
                f"{normalized} does not belong to the {self._market} market (PI42_MARKET={self._market})"
            )
        index = await self.get_index(force=force)
        try:
            return find_exact_symbol(index, normalized)
        except UnsupportedSymbolError as exc:
            raise UnsupportedSymbolError(
                f"{exc} (Pi42 {self._market}-margined futures)"
            ) from exc
