"""Pi42 client: HMAC signing, envelope handling, safety guards, error mapping."""

import asyncio
import hashlib
import hmac
import json
from decimal import Decimal

import httpx
import pytest

from app.pi42_client import (
    AmbiguousMutationError,
    Pi42AuthError,
    Pi42Client,
    Pi42Error,
    Pi42NotFoundError,
    Pi42RateLimitError,
    RateLimiter,
    ReadOnlyViolation,
    build_query_string,
    compact_json,
    is_duplicate_ref_error,
)
from tests.helpers import TEST_API_KEY, TEST_API_SECRET, FakePi42


def run(coro):
    return asyncio.run(coro)


def test_get_requests_are_signed_over_the_exact_query_string():
    fake = FakePi42(require_valid_signature=True)
    client = fake.client(allow_mutations=False)

    async def scenario():
        await client.list_contracts("USDT")
        await client.aclose()

    run(scenario())
    method, path, params, _ = fake.calls[0]
    assert (method, path) == ("GET", "/v1/exchange/exchangeInfo")
    assert "timestamp" in params and params["timestamp"].isdigit()
    signature = fake.auth_headers[0][1]
    expected = hmac.new(TEST_API_SECRET.encode(), build_query_string(params).encode(),
                        hashlib.sha256).hexdigest()
    assert signature == expected, "signature must cover the query string exactly as sent"


def test_mutating_requests_are_signed_over_the_exact_bytes_sent():
    fake = FakePi42(require_valid_signature=True)
    client = fake.client(allow_mutations=True)

    async def scenario():
        await client.set_preference("CRVUSDT", Decimal("14"), "ISOLATED")
        await client.aclose()

    run(scenario())
    raw = fake.raw_bodies[0]
    signature = fake.auth_headers[0][1]
    assert hmac.new(TEST_API_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest() == signature
    # The body must be compact JSON with no spaces, otherwise Pi42's HMAC check fails.
    assert ", " not in raw and '": ' not in raw
    assert json.loads(raw)["timestamp"].isdigit()


def test_payload_encoding_never_uses_exponent_notation_or_floats():
    payload = {"price": Decimal("0.00001"), "quantity": Decimal("1941.0"),
               "reduceOnly": True, "leverage": 14, "refId": None}
    assert compact_json(payload) == (
        '{"price":0.00001,"quantity":1941,"reduceOnly":true,"leverage":14,"refId":null}'
    )
    assert build_query_string({"market": "USDT", "pageSize": 100, "skip": None}) == "market=USDT&pageSize=100"


def test_public_price_endpoint_needs_no_credentials_and_sends_no_signature():
    fake = FakePi42()
    client = fake.client(allow_mutations=False)

    async def scenario():
        price = await client.get_ticker_price("CRVUSDT")
        await client.aclose()
        return price

    assert run(scenario()) == Decimal("0.3500")
    assert fake.auth_headers[0] == (None, None)


def test_wrong_secret_is_rejected_by_the_exchange_signature_check():
    fake = FakePi42(require_valid_signature=True)
    client = Pi42Client(
        base_url="https://fapi.pi42.com", api_key=TEST_API_KEY, api_secret="WRONG-SECRET",
        transport=fake.transport(), max_retries=0,
        sleep=lambda _: asyncio.sleep(0),
    )

    async def scenario():
        with pytest.raises(Pi42AuthError):
            await client.list_contracts("USDT")
        await client.aclose()

    run(scenario())


def test_missing_credentials_raise_before_any_network_call():
    fake = FakePi42()
    client = Pi42Client(base_url="https://fapi.pi42.com", api_key="", api_secret="",
                        transport=fake.transport())

    async def scenario():
        with pytest.raises(Pi42AuthError):
            await client.list_contracts("USDT")
        with pytest.raises(Pi42AuthError):
            await client.get_futures_wallet("USDT")
        await client.aclose()

    run(scenario())
    assert fake.calls == []


def test_read_only_client_blocks_every_mutation_before_the_http_layer():
    fake = FakePi42()
    client = fake.client(allow_mutations=False)

    async def scenario():
        blockers = [
            client.place_limit_order(symbol="CRVUSDT", side="SELL", quantity=Decimal("1"),
                                     price=Decimal("0.3462"), margin_asset="USDT",
                                     take_profit_price=Decimal("0.3185")),
            client.close_position(symbol="CRVUSDT", position_id="pos-1", side_of_position="SHORT",
                                  quantity=Decimal("1"), margin_asset="USDT"),
            client.set_leverage("CRVUSDT", Decimal("5")),
            client.set_preference("CRVUSDT", Decimal("5"), "ISOLATED"),
            client.cancel_order("cli-1"),
            client.split_tp_sl(position_id="pos-1", take_profit_orders=[{"quantity": 1, "price": 1}]),
        ]
        blocked = 0
        for coro in blockers:
            try:
                await coro
            except ReadOnlyViolation:
                blocked += 1
        await client.aclose()
        return blocked

    assert run(scenario()) == 6
    assert fake.calls == [], "blocked mutations must never reach the HTTP layer"


def test_entry_order_helper_refuses_a_stop_loss_argument_by_construction():
    """There is no stop-loss parameter on the client API at all."""
    import inspect

    signature = inspect.signature(Pi42Client.place_limit_order)
    assert not any("stop_loss" in name or "stoploss" in name for name in signature.parameters)
    assert not any("stop_loss" in name for name in inspect.signature(Pi42Client.close_position).parameters)


def test_split_tp_sl_refuses_stop_loss_legs():
    fake = FakePi42()
    client = fake.client(allow_mutations=True)

    async def scenario():
        with pytest.raises(RuntimeError, match="stop-loss legs"):
            await client.split_tp_sl(position_id="pos-1", stop_loss_orders=[{"price": 1}])
        await client.aclose()

    run(scenario())
    assert fake.calls == []


def test_place_order_guards_invalid_input():
    fake = FakePi42()
    client = fake.client(allow_mutations=True)

    async def scenario():
        with pytest.raises(ValueError, match="invalid side"):
            await client.place_limit_order(symbol="CRVUSDT", side="SHORT", quantity=Decimal("1"),
                                           price=Decimal("1"), margin_asset="USDT")
        with pytest.raises(ValueError, match="must be positive"):
            await client.place_limit_order(symbol="CRVUSDT", side="SELL", quantity=Decimal("0"),
                                           price=Decimal("1"), margin_asset="USDT")
        with pytest.raises(ValueError, match="reduce-only"):
            await client.place_limit_order(symbol="CRVUSDT", side="SELL", quantity=Decimal("1"),
                                           price=Decimal("1"), margin_asset="USDT", reduce_only=True)
        await client.aclose()

    run(scenario())
    assert fake.calls == []


def test_duplicate_ref_id_is_recognised_for_adoption():
    fake = FakePi42()
    client = fake.client(allow_mutations=True)

    async def scenario():
        first = await client.place_limit_order(symbol="CRVUSDT", side="SELL", quantity=Decimal("1"),
                                               price=Decimal("0.3462"), margin_asset="USDT",
                                               ref_id="TG-1-abc")
        with pytest.raises(Pi42Error) as info:
            await client.place_limit_order(symbol="CRVUSDT", side="SELL", quantity=Decimal("1"),
                                           price=Decimal("0.3462"), margin_asset="USDT",
                                           ref_id="TG-1-abc")
        await client.aclose()
        return first, info.value

    first, error = run(scenario())
    assert first.client_order_id.startswith("cli-")
    assert error.code == 3019 and is_duplicate_ref_error(error)
    assert not is_duplicate_ref_error(Pi42Error("boom", code=3005))


def test_order_lookup_by_ref_id_uses_the_parameter_pi42_documents():
    fake = FakePi42()
    client = fake.client(allow_mutations=True)

    async def scenario():
        await client.place_limit_order(symbol="CRVUSDT", side="SELL", quantity=Decimal("1"),
                                       price=Decimal("0.3462"), margin_asset="USDT", ref_id="TG-7-xyz")
        order = await client.get_order_by_ref_id("TG-7-xyz")
        await client.aclose()
        return order

    order = run(scenario())
    lookup = [c for c in fake.calls if c[1] == "/v1/order/ref-id"][-1]
    assert lookup[2]["ref_id"] == "TG-7-xyz" and "timestamp" in lookup[2]
    assert order.ref_id == "TG-7-xyz" and order.order_type == "LIMIT" and order.side == "SELL"


def test_not_found_is_a_distinct_exception():
    fake = FakePi42()
    client = fake.client(allow_mutations=False)

    async def scenario():
        with pytest.raises(Pi42NotFoundError):
            await client.get_order("does-not-exist")
        await client.aclose()

    run(scenario())


def test_server_error_on_a_read_is_retried_then_raised():
    fake = FakePi42()
    calls = {"n": 0}

    def fail_500(request):
        calls["n"] += 1
        return httpx.Response(500, json={"message": "internal error"})

    fake.fail_next.append(fail_500)
    client = fake.client(allow_mutations=False, max_retries=2)

    async def scenario():
        with pytest.raises(Pi42Error) as info:
            await client.list_open_orders()
        await client.aclose()
        return info.value

    error = run(scenario())
    assert calls["n"] == 3, "read failures are retried (1 + max_retries)"
    assert error.status == 500 and error.definitive is False


def test_mutation_timeout_is_ambiguous_and_never_retried():
    fake = FakePi42()
    attempts = {"n": 0}

    def timeout(request):
        attempts["n"] += 1
        raise httpx.ReadTimeout("lost", request=request)

    fake.fail_next.append(timeout)
    client = fake.client(allow_mutations=True, max_retries=3)

    async def scenario():
        with pytest.raises(AmbiguousMutationError):
            await client.place_limit_order(symbol="CRVUSDT", side="SELL", quantity=Decimal("1"),
                                           price=Decimal("0.3462"), margin_asset="USDT")
        await client.aclose()

    run(scenario())
    assert attempts["n"] == 1, "a mutating request must never be retried automatically"


def test_mutation_5xx_is_ambiguous_not_retried():
    fake = FakePi42()
    attempts = {"n": 0}

    def fail_500(request):
        attempts["n"] += 1
        return httpx.Response(502, json={"message": "bad gateway"})

    fake.fail_next.append(fail_500)
    client = fake.client(allow_mutations=True)

    async def scenario():
        with pytest.raises(AmbiguousMutationError):
            await client.cancel_order("cli-1")
        await client.aclose()

    run(scenario())
    assert attempts["n"] == 1


def test_rate_limit_response_is_surfaced_with_retry_after():
    fake = FakePi42()
    fake.fail_next.append(lambda request: httpx.Response(429, headers={"Retry-After": "2"},
                                                        json={"message": "too many requests"}))
    client = fake.client(allow_mutations=True)

    async def scenario():
        with pytest.raises(Pi42RateLimitError) as info:
            await client.place_limit_order(symbol="CRVUSDT", side="SELL", quantity=Decimal("1"),
                                           price=Decimal("0.3462"), margin_asset="USDT")
        await client.aclose()
        return info.value

    assert run(scenario()).retry_after == 2.0


def test_envelope_with_data_is_unwrapped_and_success_false_raises():
    fake = FakePi42()
    client = fake.client(allow_mutations=False)

    def envelope(request):
        if request.url.path == "/v1/order/open-orders":
            return httpx.Response(200, json={"success": True, "data": []})
        if request.url.path == "/v1/order/order-history":
            return httpx.Response(200, json={"success": False, "message": "not permitted"})
        return None

    fake.fail_next.append(envelope)

    async def scenario():
        assert await client.list_open_orders() == []
        with pytest.raises(Pi42Error, match="not permitted"):
            await client.list_order_history()
        await client.aclose()

    run(scenario())


def test_client_repr_never_leaks_the_secret():
    client = Pi42Client(base_url="https://fapi.pi42.com", api_key=TEST_API_KEY,
                        api_secret=TEST_API_SECRET, transport=FakePi42().transport())
    text = repr(client)
    assert TEST_API_SECRET not in text and "TEST-ONLY-KEY" not in text


def test_rate_limiter_waits_instead_of_bursting():
    slept = []
    clock = {"t": 1000.0}

    async def fake_sleep(seconds):
        slept.append(seconds)
        clock["t"] += seconds

    limiter = RateLimiter({"default": 2}, clock=lambda: clock["t"], sleep=fake_sleep)

    async def scenario():
        await limiter.acquire("default")
        await limiter.acquire("default")
        await limiter.acquire("default")

    run(scenario())
    assert slept and slept[0] > 0


def test_order_and_position_payloads_are_parsed_conservatively():
    fake = FakePi42()
    client = fake.client(allow_mutations=True)

    async def scenario():
        accepted = await client.place_limit_order(
            symbol="CRVUSDT", side="SELL", quantity=Decimal("1941"),
            price=Decimal("0.3462"), margin_asset="USDT",
            take_profit_price=Decimal("0.3185"), ref_id="TG-9-q", device_type="web",
            user_category="retail",
        )
        fake.fill_order(accepted.client_order_id, price="0.3462")
        order = await client.get_order(accepted.client_order_id)
        positions = await client.list_open_positions("CRVUSDT")
        await client.aclose()
        return order, positions

    order, positions = run(scenario())
    assert order.status == "FILLED" and order.filled_quantity == Decimal("1941")
    assert order.take_profit_price == Decimal("0.3185") and order.stop_loss_price is None
    assert order.ref_id == "TG-9-q" and order.margin_asset == "USDT"
    assert len(positions) == 1 and positions[0].side == "SHORT"
    assert positions[0].entry_price == Decimal("0.3462")
    body = next(c for c in fake.calls if c[1] == "/v1/order/place-order")[3]
    assert body["deviceType"] == "WEB" and body["userCategory"] == "RETAIL"
    assert body["type"] == "LIMIT" and body["placeType"] == "ORDER_FORM"
