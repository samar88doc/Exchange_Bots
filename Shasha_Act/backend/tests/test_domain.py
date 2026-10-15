from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.domain import (
    ValidationProblem,
    compute_long_levels,
    parse_alert,
    quantity_from_notional,
    split_quantity,
)


def alert_body(**overrides: object) -> bytes:
    payload: dict[str, object] = {
        "strategy": "SHASHA",
        "event_id": "sample-bar-1700000000",
        "symbol": "BTCUSDT.P",
        "direction": "LONG",
        "timeframe": "15",
        "bar_time": "2026-10-15T10:15:00Z",
        "signal_close": "101.25",
        "entry_price": "100.50",
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


def test_structured_alert_keeps_previous_close_and_signal_close_separate() -> None:
    parsed = parse_alert(alert_body())
    assert parsed.errors == ()
    assert parsed.tradingview_symbol == "BTCUSDT.P"
    assert parsed.symbol == "BTCUSDT"
    assert parsed.entry_price == Decimal("100.50")
    assert parsed.signal_close == Decimal("101.25")


def test_symbol_normalization_only_strips_trailing_perpetual_suffix() -> None:
    parsed = parse_alert(alert_body(symbol="BTCUSDT.p"))
    assert parsed.symbol == "BTCUSDT"
    assert parsed.tradingview_symbol == "BTCUSDT.p"

    invalid = parse_alert(alert_body(symbol="BTCUSDT.P.EXTRA"))
    assert invalid.symbol is None
    assert "INVALID_SYMBOL_FORMAT" in invalid.errors


def test_payload_decimal_values_are_json_safe_and_sensitive_fields_redacted() -> None:
    parsed = parse_alert(
        b'{"strategy":"SHASHA","event_id":"safe-id","symbol":"BTCUSDT.P",'
        b'"direction":"LONG","timeframe":"1","bar_time":"2026-10-15T00:00:00Z",'
        b'"signal_close":101.25,"entry_price":100.50,"token":"not-a-real-secret"}'
    )
    assert parsed.raw_payload is not None
    assert parsed.raw_payload["signal_close"] == "101.25"
    assert parsed.raw_payload["token"] == "[REDACTED]"


def test_missing_prices_or_symbols_are_rejected_without_guessing() -> None:
    parsed = parse_alert(b'{"strategy":"SHASHA","direction":"LONG"}')
    assert parsed.symbol is None
    assert parsed.entry_price is None
    assert parsed.signal_close is None
    assert "MISSING_SYMBOL" in parsed.errors
    assert "MISSING_ENTRY_PRICE" in parsed.errors
    assert "MISSING_SIGNAL_CLOSE" in parsed.errors


def test_legacy_text_parser_is_constrained_and_does_not_fill_missing_fields() -> None:
    parsed = parse_alert(
        b"SHASHA alert\nSymbol: BTCUSDT.P\nTimeframe: 5\nBar time: 2026-10-15T10:00:00Z\n"
        b"Signal close: 102.75\nPrevious candle close: 101.50"
    )
    assert parsed.symbol == "BTCUSDT"
    assert parsed.entry_price == Decimal("101.50")
    assert parsed.signal_close == Decimal("102.75")
    assert parsed.errors == ()

    missing = parse_alert(b"BUY BTCUSDT now")
    assert missing.symbol is None
    assert missing.entry_price is None
    assert "MISSING_SYMBOL" in missing.errors


def test_same_tv_bar_is_idempotent_but_distinct_bar_is_not() -> None:
    first = parse_alert(alert_body(event_id=""))
    duplicate = parse_alert(alert_body(event_id=""))
    next_bar = parse_alert(alert_body(event_id="", bar_time="2026-10-15T10:30:00Z"))
    assert first.idempotency_key == duplicate.idempotency_key
    assert first.idempotency_key != next_bar.idempotency_key


def test_decimal_levels_and_quantity_split_are_exact() -> None:
    levels = compute_long_levels(
        Decimal("100"),
        Decimal("0.1"),
        Decimal("2"),
        Decimal("1"),
        Decimal("2"),
        Decimal("3"),
    )
    assert levels.stop == Decimal("98.0")
    assert (levels.target1, levels.target2, levels.target3) == (
        Decimal("101.0"),
        Decimal("102.0"),
        Decimal("103.0"),
    )
    split = split_quantity(Decimal("10.01"), Decimal("0.01"), Decimal("0.01"))
    assert split.total == Decimal("10.01")
    assert all(piece % Decimal("0.01") == 0 for piece in (split.target1, split.target2, split.target3))
    assert quantity_from_notional(Decimal("1000"), Decimal("99.5"), Decimal("0.01")) == Decimal("10.05")


def test_invalid_target_order_and_tiny_splits_fail_closed() -> None:
    with pytest.raises(ValidationProblem, match="TARGETS_MUST_BE_STRICTLY_INCREASING"):
        compute_long_levels(
            Decimal("100"), Decimal("0.1"), Decimal("2"), Decimal("2"), Decimal("1"), Decimal("3")
        )
    with pytest.raises(ValidationProblem, match="TARGET_SLICE_BELOW_MINIMUM_QUANTITY"):
        split_quantity(Decimal("0.02"), Decimal("0.01"), Decimal("0.01"))
