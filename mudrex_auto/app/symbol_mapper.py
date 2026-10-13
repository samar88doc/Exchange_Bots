"""Exact-symbol resolution against cached Mudrex futures metadata.

Only an exact symbol match is accepted (e.g. CRVUSDT). Similarly named
instruments are never substituted.
"""

from __future__ import annotations

import time
from typing import Callable, Iterable, Mapping

from app.models import AssetInfo


class UnsupportedSymbolError(Exception):
    """The Telegram symbol has no exact, unambiguous Mudrex futures instrument."""


def index_assets(assets: Iterable[AssetInfo]) -> dict[str, AssetInfo]:
    index: dict[str, AssetInfo] = {}
    for asset in assets:
        if asset.symbol in index:
            raise UnsupportedSymbolError(f"duplicate Mudrex listing for {asset.symbol}; refusing to choose")
        index[asset.symbol] = asset
    return index


def find_exact_symbol(index: Mapping[str, AssetInfo], symbol: str) -> AssetInfo:
    normalized = symbol.strip().upper()
    asset = index.get(normalized)
    if asset is None:
        raise UnsupportedSymbolError(f"{normalized} is not listed as a Mudrex futures instrument")
    return asset


class SymbolMapper:
    """Caches the full futures listing and refreshes it after ``ttl_seconds``."""

    def __init__(self, client, ttl_seconds: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._clock = clock
        self._index: dict[str, AssetInfo] | None = None
        self._loaded_at = 0.0

    async def refresh(self) -> dict[str, AssetInfo]:
        assets = await self._client.list_futures_assets()
        self._index = index_assets(assets)
        self._loaded_at = self._clock()
        return self._index

    async def get_index(self, *, force: bool = False) -> dict[str, AssetInfo]:
        stale = self._index is None or (self._clock() - self._loaded_at) >= self._ttl
        if force or stale:
            return await self.refresh()
        return self._index  # type: ignore[return-value]

    async def resolve(self, symbol: str, *, force: bool = False) -> AssetInfo:
        index = await self.get_index(force=force)
        return find_exact_symbol(index, symbol)
