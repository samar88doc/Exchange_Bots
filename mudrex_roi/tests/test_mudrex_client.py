"""HTTP client behaviour: auth header, retries, 429/5xx/timeouts, ambiguous mutations."""

import asyncio

import httpx
import pytest

from app.mudrex_client import (
    AmbiguousMutationError,
    MudrexAuthError,
    MudrexError,
    MudrexNotFoundError,
    ReadOnlyViolation,
)
from tests.helpers import TEST_SECRET, FakeMudrex


def run(coro):
    return asyncio.run(coro)


def test_auth_header_sent_and_secret_never_in_repr():
    fake = FakeMudrex()
    client = fake.client(allow_mutations=False)
    run(client.list_futures_assets())
    assert set(fake.auth_headers) == {TEST_SECRET}
    assert TEST_SECRET not in repr(client)


def test_get_retries_on_5xx_then_succeeds():
    fake = FakeMudrex()
    failures = {"left": 2}

    def flaky(request):
        if request.url.path == "/fapi/v1/futures/funds" and failures["left"] > 0:
            failures["left"] -= 1
            return httpx.Response(503, text="busy")
        return None

    fake.fail_next.append(flaky)
    client = fake.client(allow_mutations=False)
    funds = run(client.get_futures_funds("USDT"))
    assert str(funds.balance) == "500"
    assert len([c for c in fake.calls if c[1] == "/fapi/v1/futures/funds"]) == 3  # 2 failures + 1 success


def test_get_honours_retry_after_on_429():
    fake = FakeMudrex()
    waits = []

    async def record_sleep(seconds):
        waits.append(seconds)

    state = {"done": False}

    def throttle(request):
        if not state["done"] and request.url.path == "/fapi/v1/futures/funds":
            state["done"] = True
            return httpx.Response(429, headers={"Retry-After": "7"}, json={"success": False})
        return None

    fake.fail_next.append(throttle)
    client = fake.client(allow_mutations=False, sleep=record_sleep)
    run(client.get_futures_funds("USDT"))
    assert waits and waits[0] >= 7


def test_get_timeout_exhausts_retries_and_raises():
    fake = FakeMudrex()

    def always_timeout(request):
        if request.url.path == "/fapi/v1/futures/funds":
            raise httpx.ReadTimeout("slow", request=request)
        return None

    fake.fail_next.extend([always_timeout] * 10)
    client = fake.client(allow_mutations=False)
    with pytest.raises(MudrexError):
        run(client.get_futures_funds("USDT"))


def test_order_timeout_is_ambiguous_and_never_resubmitted():
    fake = FakeMudrex()
    fake.leverage[("CRVUSDT", "USDT")] = {"leverage": "14", "margin_type": "ISOLATED"}

    def timeout_post(request):
        if request.method == "POST" and request.url.path == "/fapi/v2/futures/order":
            raise httpx.ReadTimeout("no response", request=request)
        return None

    fake.fail_next.append(timeout_post)
    client = fake.client(allow_mutations=True)
    with pytest.raises(AmbiguousMutationError):
        run(client.place_limit_order(symbol="CRVUSDT", side="SHORT", quantity=__import__("decimal").Decimal("10"),
                                     price=__import__("decimal").Decimal("0.3462"),
                                     takeprofit_price=__import__("decimal").Decimal("0.3361"),
                                     client_order_id="TG-1-abc"))
    posts = [c for c in fake.calls if c[0] == "POST" and c[1] == "/fapi/v2/futures/order"]
    assert len(posts) == 1, "a timed-out order must not be blindly resubmitted"


def test_order_5xx_is_ambiguous_single_attempt():
    fake = FakeMudrex()
    fake.leverage[("CRVUSDT", "USDT")] = {"leverage": "14", "margin_type": "ISOLATED"}
    fake.fail_next.append(lambda r: httpx.Response(500, text="boom")
                          if r.method == "POST" else None)
    client = fake.client(allow_mutations=True)
    from decimal import Decimal
    with pytest.raises(AmbiguousMutationError):
        run(client.place_limit_order(symbol="CRVUSDT", side="LONG", quantity=Decimal("1"),
                                     price=Decimal("1"), takeprofit_price=Decimal("2"), client_order_id="TG-2"))
    assert len([c for c in fake.calls if c[0] == "POST"]) == 1


def test_order_400_is_definitive_rejection():
    fake = FakeMudrex()  # no leverage set -> 400 leverage not found
    client = fake.client(allow_mutations=True)
    from decimal import Decimal
    with pytest.raises(MudrexError) as info:
        run(client.place_limit_order(symbol="CRVUSDT", side="SHORT", quantity=Decimal("1"),
                                     price=Decimal("0.3462"), takeprofit_price=Decimal("0.3361"),
                                     client_order_id="TG-3"))
    assert info.value.definitive is True and info.value.status == 400
    assert "leverage not found" in str(info.value)


def test_401_maps_to_auth_error_and_never_retries():
    fake = FakeMudrex()
    fake.fail_next.append(lambda r: httpx.Response(401, json={"success": False,
                                                              "errors": [{"code": 401, "text": "bad key"}]}))
    client = fake.client(allow_mutations=False)
    with pytest.raises(MudrexAuthError):
        run(client.get_futures_funds("USDT"))
    assert len([c for c in fake.calls if c[1] == "/fapi/v1/futures/funds"]) == 1


def test_404_leverage_read_returns_none():
    fake = FakeMudrex()
    client = fake.client(allow_mutations=False)
    assert run(client.get_leverage("CRVUSDT", "USDT")) is None


def test_missing_order_lookup_raises_not_found():
    fake = FakeMudrex()
    client = fake.client(allow_mutations=False)
    with pytest.raises(MudrexNotFoundError):
        run(client.get_order_by_client_id("TG-unknown"))


def test_client_side_mutation_block_without_network():
    fake = FakeMudrex()
    client = fake.client(allow_mutations=False)
    from decimal import Decimal
    with pytest.raises(ReadOnlyViolation):
        run(client.place_limit_order(symbol="CRVUSDT", side="SHORT", quantity=Decimal("1"),
                                     price=Decimal("1"), takeprofit_price=Decimal("0.5"), client_order_id="x"))
    with pytest.raises(ReadOnlyViolation):
        run(client.cancel_order("order-1"))
    with pytest.raises(ReadOnlyViolation):
        run(client.set_leverage("CRVUSDT", Decimal("5")))
    assert fake.calls == [], "a blocked mutation must not reach the network"


def test_order_body_contains_limit_entry_tp_and_no_stop_loss():
    fake = FakeMudrex()
    fake.leverage[("CRVUSDT", "USDT")] = {"leverage": "14", "margin_type": "ISOLATED"}
    client = fake.client(allow_mutations=True)
    from decimal import Decimal
    accepted = run(client.place_limit_order(symbol="CRVUSDT", side="SHORT", quantity=Decimal("1941.0"),
                                            price=Decimal("0.3462"), takeprofit_price=Decimal("0.3361"),
                                            client_order_id="TG-9-abc"))
    method, path, params, body = [c for c in fake.calls if c[0] == "POST"][-1]
    assert path == "/fapi/v2/futures/order" and params == {"symbol": "CRVUSDT"}
    assert body["trigger_type"] == "LIMIT"
    assert body["order_type"] == "SHORT"
    assert body["order_price"] == "0.3462"
    assert body["takeprofit_price"] == "0.3361"
    assert body["is_takeprofit"] is True
    assert "is_stoploss" not in body and "stoploss_price" not in body
    assert body["client_order_id"] == "TG-9-abc"
    assert accepted.order_id.startswith("order-")


def test_market_orders_cannot_be_built_by_the_client():
    # place_limit_order has no market parameter; the trigger type is fixed to LIMIT.
    import inspect

    from app.mudrex_client import MudrexClient
    sig = inspect.signature(MudrexClient.place_limit_order)
    assert "trigger_type" not in sig.parameters and "order_type" not in sig.parameters


def test_pagination_collects_every_page():
    from tests.helpers import ASSET_CRV
    many = [{**ASSET_CRV, "id": f"id-{i}", "symbol": f"T{i:03d}USDT"} for i in range(205)]
    fake = FakeMudrex(assets=many)
    assets = run(fake.client(allow_mutations=False).list_futures_assets())
    assert len(assets) == 205


def test_api_error_text_is_scrubbed_of_secret():
    fake = FakeMudrex()
    fake.fail_next.append(lambda r: httpx.Response(400, json={
        "success": False, "errors": [{"code": 400, "text": f"echo {TEST_SECRET}"}]}))
    client = fake.client(allow_mutations=False)
    with pytest.raises(MudrexError) as info:
        run(client.get_futures_funds("USDT"))
    assert TEST_SECRET not in str(info.value)
