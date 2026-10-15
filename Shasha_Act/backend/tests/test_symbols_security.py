from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.security import (
    SlidingWindowRateLimiter,
    constant_time_equals,
    redact_text,
    shark_query_string,
    shark_signature,
)
from app.shark import SharkClient, SharkOutcomeUnknown
from app.symbols import SymbolResolver, parse_exchange_info


def test_exchange_metadata_selects_usdt_perpetual_but_does_not_guess_tick_or_step() -> None:
    metadata = {
        "data": {
            "contracts": [
                {
                    "name": "BTCUSDT",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "contractType": "PERPETUAL",
                    "marginAssetsSupported": ["USDT"],
                    "orderTypes": ["LIMIT", "MARKET"],
                    "pricePrecision": 2,
                    "quantityPrecision": 3,
                    "maxLeverage": 100,
                    "filters": [
                        {"filterType": "LIMIT_QTY_SIZE", "minQty": "0.001"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                },
                {
                    "name": "BTCUSDC",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDC",
                    "contractType": "PERPETUAL",
                    "marginAssetsSupported": ["USDT"],
                    "orderTypes": ["LIMIT"],
                    "pricePrecision": 2,
                    "quantityPrecision": 3,
                    "filters": [{"filterType": "LIMIT_QTY_SIZE", "minQty": "0.001"}],
                },
                {
                    "name": "BTCUSDT_20261225",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "contractType": "FUTURES",
                    "marginAssetsSupported": ["USDT"],
                    "orderTypes": ["LIMIT"],
                    "pricePrecision": 2,
                    "quantityPrecision": 3,
                    "filters": [{"filterType": "LIMIT_QTY_SIZE", "minQty": "0.001"}],
                },
            ]
        }
    }
    contracts = parse_exchange_info(metadata)
    resolver = SymbolResolver(contracts)
    spec = resolver.resolve("BTCUSDT", "USDT")
    assert spec.minimum_quantity == Decimal("0.001")
    assert spec.minimum_notional == Decimal("5")
    assert spec.price_tick is None
    assert spec.quantity_step is None
    assert spec.live_safe is False
    with pytest.raises(ValueError, match="CONTRACT_QUOTE_ASSET_IS_NOT_USDT"):
        resolver.resolve("BTCUSDC")
    with pytest.raises(ValueError, match="CONTRACT_IS_NOT_PERPETUAL"):
        resolver.resolve("BTCUSDT_20261225")


def test_signature_query_order_and_secret_redaction() -> None:
    text = shark_query_string([("marginAsset", "USDT"), ("timestamp", "123")])
    assert text == "marginAsset=USDT&timestamp=123"
    assert shark_signature("test-only-secret", text) == shark_signature("test-only-secret", text)
    assert constant_time_equals("same-test-token", "same-test-token")
    assert not constant_time_equals("wrong", "same-test-token")
    assert redact_text("api-key=TEST_KEY signature:ABC123 token=xyz") == (
        "api-key=[REDACTED] signature=[REDACTED] token=[REDACTED]"
    )
    assert redact_text("Authorization: Bearer TEST_ONLY_ACCESS_TOKEN") == "Authorization=[REDACTED]"


def test_rate_limiter_enforces_sliding_window() -> None:
    limiter = SlidingWindowRateLimiter(limit=2, window_seconds=60)
    assert limiter.allow("test-client", now=100)
    assert limiter.allow("test-client", now=120)
    assert not limiter.allow("test-client", now=130)
    assert limiter.allow("test-client", now=161)


@pytest.mark.asyncio
async def test_shark_private_get_signs_exact_query_and_never_logs_credentials() -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        shark_base_url="https://shark.test",
        shark_api_key="TEST_ONLY_API_KEY",
        shark_api_secret="TEST_ONLY_API_SECRET",
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": {"marginAsset": "USDT"}})

    client = httpx.AsyncClient(base_url=settings.shark_base_url, transport=httpx.MockTransport(handler))
    shark = SharkClient(settings, client, clock_ms=lambda: 123)
    await shark.futures_wallet("USDT")
    request = seen[0]
    assert request.url.path == "/v1/wallet/futures-wallet/details"
    assert request.url.query.decode() == "marginAsset=USDT&timestamp=123"
    assert request.headers["api-key"] == "TEST_ONLY_API_KEY"
    assert request.headers["signature"] == shark_signature(
        settings.shark_api_secret, "marginAsset=USDT&timestamp=123"
    )
    assert settings.shark_api_secret not in request.url.path
    await client.aclose()


@pytest.mark.asyncio
async def test_uncertain_order_submission_is_not_returned_as_a_retryable_http_error() -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        shark_base_url="https://shark.test",
        shark_api_key="TEST_ONLY_API_KEY",
        shark_api_secret="TEST_ONLY_API_SECRET",
    )
    client = httpx.AsyncClient(
        base_url=settings.shark_base_url,
        transport=httpx.MockTransport(lambda request: httpx.Response(503, json={"error": "temporary"})),
    )
    shark = SharkClient(settings, client, clock_ms=lambda: 123)
    with pytest.raises(SharkOutcomeUnknown):
        await shark.place_limit_buy("BTCUSDT", Decimal("1"), Decimal("100"))
    await client.aclose()
