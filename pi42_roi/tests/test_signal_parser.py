"""Parser tests: both sample fixtures, formats, rejections and direction rules."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.models import Direction
from app.signal_parser import SignalParseError, parse_signal
from tests.helpers import SIGNAL_CRV, SIGNAL_EIGEN

NOW = datetime(2026, 10, 13, 6, 0, tzinfo=timezone.utc)


def parse(text: str):
    return parse_signal(text, channel_id=-100, message_id=42, signal_time=NOW)


def test_fixture_crv_parses_exactly():
    s = parse(SIGNAL_CRV)
    assert s.symbol == "CRVUSDT"
    assert s.direction is Direction.SHORT
    assert s.entry == Decimal("0.3462")
    assert s.stop_loss == Decimal("0.3662")
    assert s.target1 == Decimal("0.3361")
    assert s.target2 == Decimal("0.32627")
    assert s.target3 == Decimal("0.28398")
    assert s.telegram_leverage == Decimal("14")
    assert (s.channel_id, s.message_id) == (-100, 42)


def test_fixture_eigen_parses_exactly():
    s = parse(SIGNAL_EIGEN)
    assert s.symbol == "EIGENUSDT"
    assert s.direction is Direction.SHORT
    assert s.entry == Decimal("0.2425")
    assert s.stop_loss == Decimal("0.34008")
    assert s.target1 == Decimal("0.21856")
    assert s.target2 is None and s.target3 is None
    assert s.telegram_leverage == Decimal("16")


def test_long_with_buy_label_and_leverage_suffix():
    text = "#BTC/USDT Buy\nEntry Price: 100\nTP1: 110\nLeverage: 10x"
    s = parse(text)
    assert s.direction is Direction.LONG
    assert s.symbol == "BTCUSDT"
    assert s.target1 == Decimal("110")
    assert s.telegram_leverage == Decimal("10")


@pytest.mark.parametrize("lev_line", ["Leverage: x14", "Leverage: 14x", "Leverage: 14", "leverage = X14"])
def test_leverage_variants(lev_line):
    s = parse(f"#CRV/USDT Short\nEntry: 0.3462\nTarget 1: 0.3361\n{lev_line}")
    assert s.telegram_leverage == Decimal("14")


@pytest.mark.parametrize("tp_label", ["Target 1", "Target1", "TP1", "TP 1", "TP-1", "take profit 1"])
def test_target1_label_variants(tp_label):
    s = parse(f"#CRV/USDT Short\nEntry: 0.3462\n{tp_label}: 0.3361\nLeverage: x5")
    assert s.target1 == Decimal("0.3361")


def test_target_10_is_not_target_1():
    with pytest.raises(SignalParseError, match="missing Target 1"):
        parse("#CRV/USDT Short\nEntry: 0.3462\nTarget 10: 0.3361\nLeverage: x5")


def test_target2_is_never_substituted_for_missing_target1():
    with pytest.raises(SignalParseError, match="missing Target 1"):
        parse("#CRV/USDT Short\nEntry: 0.3462\nTarget 2: 0.32627\nLeverage: x5")


@pytest.mark.parametrize("text, reason", [
    ("#CRV/USDT Short\nTarget 1: 0.3361\nLeverage: x5", "missing Entry"),
    ("#CRV/USDT Short\nEntry: 0.3462\nLeverage: x5", "missing Target 1"),
    ("#CRV/USDT Short\nEntry: 0.3462\nTarget 1: 0.3361", "missing Leverage"),
    ("#CRV/USDT Short\nEntry: 0\nTarget 1: 0.3361\nLeverage: x5", "Entry must be positive"),
    ("#CRV/USDT Short\nEntry: -0.3\nTarget 1: 0.3361\nLeverage: x5", "Entry must be positive"),
    ("#CRV/USDT Short\nEntry: 0.3462\nTarget 1: 0.3361\nLeverage: x0", "Leverage must be positive"),
])
def test_missing_or_invalid_fields_are_rejected(text, reason):
    with pytest.raises(SignalParseError, match=reason.split(" must")[0].replace("Entry", "Entry")):
        parse(text)


def test_invalid_long_target_direction_rejected():
    with pytest.raises(SignalParseError, match="must be above Entry"):
        parse("#CRV/USDT Long\nEntry: 0.5\nTarget 1: 0.4\nLeverage: x5")


def test_invalid_short_target_direction_rejected():
    with pytest.raises(SignalParseError, match="must be below Entry"):
        parse("#CRV/USDT Short\nEntry: 0.3\nTarget 1: 0.4\nLeverage: x5")


def test_emoji_alone_is_not_a_direction():
    with pytest.raises(SignalParseError, match="missing explicit LONG/SHORT"):
        parse("#CRV/USDT 🔴🟢\nEntry: 0.3462\nTarget 1: 0.3361\nLeverage: x5")


def test_ambiguous_direction_rejected():
    with pytest.raises(SignalParseError, match="ambiguous direction"):
        parse("#CRV/USDT Long or Short\nEntry: 0.3462\nTarget 1: 0.3361\nLeverage: x5")


def test_conflicting_duplicate_entry_rejected():
    with pytest.raises(SignalParseError, match="more than once"):
        parse("#CRV/USDT Short\nEntry: 0.3462\nEntry: 0.3500\nTarget 1: 0.3361\nLeverage: x5")


def test_entry_range_is_ambiguous_not_guessed():
    with pytest.raises(SignalParseError):
        parse("#CRV/USDT Short\nEntry: 0.3462-0.3470\nTarget 1: 0.3361\nLeverage: x5")


def test_unsupported_quote_rejected():
    with pytest.raises(SignalParseError, match="unsupported quote"):
        parse("#CRV/BTC Short\nEntry: 0.3462\nTarget 1: 0.3361\nLeverage: x5")


def test_ordinary_chat_message_is_ignorable():
    with pytest.raises(SignalParseError) as info:
        parse("Good morning everyone, market looks busy today.")
    assert info.value.ignorable is True


def test_nbsp_and_zero_width_characters_are_tolerated():
    text = "#CRV/USDT\u00a0Short\u200b\nEntry:\u00a00.3462\nTarget 1: 0.3361\nLeverage: x5"
    assert parse(text).entry == Decimal("0.3462")


def test_message_dedup_key_is_preserved_on_parse():
    s = parse(SIGNAL_CRV)
    assert s.message_id == 42 and s.channel_id == -100
