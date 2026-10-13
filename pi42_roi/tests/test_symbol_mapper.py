"""Contract resolution: exact Pi42 symbols, market discipline, cached listing."""

import asyncio
from dataclasses import replace
from decimal import Decimal

import pytest

from app.models import ContractInfo
from app.symbol_mapper import (
    SymbolMapper,
    UnsupportedSymbolError,
    contract_pair,
    find_exact_symbol,
    index_contracts,
)
from tests.helpers import CONTRACT_BTC_INR, CONTRACT_CRV, FakePi42


def contract(**overrides) -> ContractInfo:
    return ContractInfo.from_api({**CONTRACT_CRV, **overrides})


def test_exact_match_resolves():
    index = index_contracts([contract(), ContractInfo.from_api({**CONTRACT_CRV, "name": "EIGENUSDT"})])
    assert find_exact_symbol(index, "crvusdt").symbol == "CRVUSDT"


def test_similar_names_are_never_substituted():
    index = index_contracts([contract(name="CRV1USDT")])
    with pytest.raises(UnsupportedSymbolError, match="CRVUSDT is not listed"):
        find_exact_symbol(index, "CRVUSDT")


def test_duplicate_listing_refuses_to_choose():
    with pytest.raises(UnsupportedSymbolError, match="duplicate"):
        index_contracts([contract(), contract()])


def test_contract_pair_is_concatenated_without_a_slash():
    assert contract_pair("crv", "usdt") == "CRVUSDT"
    assert contract_pair("BTC", "INR") == "BTCINR"


def test_mapper_rejects_a_signal_quoted_in_another_currency():
    fake = FakePi42()
    mapper = SymbolMapper(fake.client(allow_mutations=False), 600, market="USDT")

    async def scenario():
        with pytest.raises(UnsupportedSymbolError, match="never converted"):
            await mapper.resolve("CRVINR", quote="INR")
        with pytest.raises(UnsupportedSymbolError, match="does not belong to the USDT market"):
            await mapper.resolve("CRVINR")

    asyncio.run(scenario())


def test_inr_market_resolves_the_concatenated_pair():
    fake = FakePi42()
    mapper = SymbolMapper(fake.client(allow_mutations=False), 600, market="INR")

    async def scenario():
        resolved = await mapper.resolve("BTCINR", quote="INR")
        await mapper._client.aclose()
        return resolved

    assert asyncio.run(scenario()).symbol == "BTCINR"


def test_mapper_uses_the_cache_until_the_ttl_expires():
    fake = FakePi42()
    client = fake.client(allow_mutations=False)
    clock = {"t": 0.0}
    mapper = SymbolMapper(client, 60, market="USDT", clock=lambda: clock["t"])

    async def scenario():
        await mapper.resolve("CRVUSDT")
        listings = len([c for c in fake.calls if c[1] == "/v1/exchange/exchangeInfo"])
        await mapper.resolve("EIGENUSDT")
        assert len([c for c in fake.calls if c[1] == "/v1/exchange/exchangeInfo"]) == listings
        clock["t"] = 61.0
        await mapper.resolve("CRVUSDT")
        assert len([c for c in fake.calls if c[1] == "/v1/exchange/exchangeInfo"]) == listings + 1
        await client.aclose()

    asyncio.run(scenario())


def test_mapper_passes_the_configured_market_to_pi42():
    fake = FakePi42()
    mapper = SymbolMapper(fake.client(allow_mutations=False), 600, market="INR")

    async def scenario():
        await mapper.resolve("BTCINR")
        await mapper._client.aclose()

    asyncio.run(scenario())
    call = next(c for c in fake.calls if c[1] == "/v1/exchange/exchangeInfo")
    assert call[2]["market"] == "INR"


def test_empty_listing_refuses_to_trade_blind():
    fake = FakePi42(contracts=[CONTRACT_BTC_INR])  # no USDT contracts at all
    mapper = SymbolMapper(fake.client(allow_mutations=False), 600, market="USDT")

    async def scenario():
        with pytest.raises(UnsupportedSymbolError, match="refusing to trade blind"):
            await mapper.resolve("CRVUSDT")
        await mapper._client.aclose()

    asyncio.run(scenario())


def test_contract_precision_becomes_the_price_and_quantity_step():
    crv = contract()
    assert crv.price_step == Decimal("0.0001") and crv.quantity_step == Decimal("0.1")
    btc = ContractInfo.from_api(CONTRACT_BTC_INR)
    assert btc.price_step == Decimal("0.1") and btc.quantity_step == Decimal("0.00001")
    assert btc.market == "INR" and btc.quote_asset == "INR"


def test_min_qty_comes_from_the_tighter_filter_and_min_notional_is_kept():
    crv = contract()
    assert crv.min_qty == Decimal("0.1") and crv.max_qty == Decimal("1000000")
    assert crv.min_notional == Decimal("5")
    tighter = contract(filters=[
        {"filterType": "LIMIT_QTY_SIZE", "minQty": "1", "maxQty": "1000"},
        {"filterType": "MARKET_QTY_SIZE", "minQty": "0.5", "maxQty": "5000"},
    ])
    assert tighter.min_qty == Decimal("1") and tighter.max_qty == Decimal("1000")


def test_malformed_contracts_are_skipped_not_fatal():
    fake = FakePi42(contracts=[CONTRACT_CRV, {"name": "BROKENUSDT"}])
    client = fake.client(allow_mutations=False)

    async def scenario():
        contracts = await client.list_contracts("USDT")
        await client.aclose()
        return contracts

    contracts = asyncio.run(scenario())
    assert [c.symbol for c in contracts] == ["CRVUSDT"]


def test_contract_without_order_types_refuses_limit_support():
    no_limit = contract(orderTypes=["MARKET"])
    assert no_limit.supports_order_type("LIMIT") is False
    assert replace(no_limit, order_types=("LIMIT", "MARKET")).supports_order_type("LIMIT") is True
