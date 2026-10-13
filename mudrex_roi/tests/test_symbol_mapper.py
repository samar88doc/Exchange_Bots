"""Exact-symbol mapping and cached asset discovery."""

import asyncio
from decimal import Decimal

import pytest

from app.models import AssetInfo
from app.symbol_mapper import (
    SymbolMapper,
    UnsupportedSymbolError,
    find_exact_symbol,
    index_assets,
)
from tests.helpers import ASSET_CRV, ASSET_EIGEN, FakeMudrex


def _asset(symbol: str, asset_id: str = "x") -> AssetInfo:
    return AssetInfo.from_api({**ASSET_CRV, "id": asset_id, "symbol": symbol})


def test_exact_match_resolves():
    index = index_assets([_asset("CRVUSDT"), _asset("EIGENUSDT")])
    assert find_exact_symbol(index, "crvusdt").symbol == "CRVUSDT"


def test_similar_names_are_never_substituted():
    index = index_assets([_asset("CRV1USDT"), _asset("CRVXUSDT")])
    with pytest.raises(UnsupportedSymbolError, match="CRVUSDT is not listed"):
        find_exact_symbol(index, "CRVUSDT")


def test_unlisted_symbol_is_reported():
    index = index_assets([_asset("BTCUSDT")])
    with pytest.raises(UnsupportedSymbolError):
        find_exact_symbol(index, "DOGEUSDT")


def test_duplicate_listing_refuses_to_choose():
    with pytest.raises(UnsupportedSymbolError, match="duplicate"):
        index_assets([_asset("CRVUSDT", "a"), _asset("CRVUSDT", "b")])


def test_mapper_uses_cache_until_ttl_then_refreshes():
    fake = FakeMudrex()
    client = fake.client(allow_mutations=False)
    clock = {"t": 0.0}
    mapper = SymbolMapper(client, ttl_seconds=60, clock=lambda: clock["t"])

    async def scenario():
        first = await mapper.resolve("CRVUSDT")
        assert first.asset_id == "asset-crv"
        listing_calls = [c for c in fake.calls if c[1] == "/fapi/v1/futures"]
        await mapper.resolve("EIGENUSDT")
        assert len([c for c in fake.calls if c[1] == "/fapi/v1/futures"]) == len(listing_calls)  # cached
        clock["t"] = 61.0
        await mapper.resolve("CRVUSDT")
        assert len([c for c in fake.calls if c[1] == "/fapi/v1/futures"]) == len(listing_calls) + 1

    asyncio.run(scenario())


def test_mapper_paginates_full_listing():
    many = [{**ASSET_CRV, "id": f"id-{i}", "symbol": f"T{i:03d}USDT"} for i in range(230)]
    many.append(ASSET_EIGEN)
    fake = FakeMudrex(assets=many)
    client = fake.client(allow_mutations=False)
    mapper = SymbolMapper(client, ttl_seconds=600)

    async def scenario():
        assert (await mapper.resolve("EIGENUSDT")).asset_id == "asset-eigen"
        assert len([c for c in fake.calls if c[1] == "/fapi/v1/futures"]) == 3  # 100 + 100 + 31

    asyncio.run(scenario())


def test_listing_skips_malformed_entries_without_crashing():
    fake = FakeMudrex(assets=[ASSET_CRV, {"symbol": "BROKENUSDT"}])
    client = fake.client(allow_mutations=False)
    assets = asyncio.run(client.list_futures_assets())
    assert [a.symbol for a in assets] == ["CRVUSDT"]
    assert Decimal(assets[0].max_leverage) == Decimal("20")
