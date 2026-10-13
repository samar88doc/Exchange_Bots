"""Strict, pure parser for the signal format posted in the Telegram channel.

The parser never guesses. It rejects a message when a required field is
missing, zero/negative, duplicated with conflicting values, or ambiguous.
Direction must come from an explicit LONG/BUY or SHORT/SELL word; emoji
alone are never used.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from decimal import Decimal

from app.models import Direction, ParsedSignal
from app.numeric import to_decimal

_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
_LONG_RE = re.compile(r"\b(?:long|buy)\b", re.IGNORECASE)
_SHORT_RE = re.compile(r"\b(?:short|sell)\b", re.IGNORECASE)
_HASH_PAIR_RE = re.compile(r"#\s*([A-Za-z0-9]{2,20})\s*/\s*([A-Za-z0-9]{2,10})(?![A-Za-z0-9])")
_HASH_JOINED_RE = re.compile(r"#([A-Za-z0-9]{3,30})(?![A-Za-z0-9/])")
_BARE_PAIR_RE = re.compile(r"(?<![A-Za-z0-9#/])([A-Z][A-Z0-9]{1,19})/(USDT|INR)(?![A-Za-z0-9])")

# Quote currencies Pi42 lists futures against. Which of them is actually traded
# is decided by PI42_MARKET in the symbol mapper, never by the parser.
_SUPPORTED_QUOTES = ("USDT", "INR")

_LABELS = {
    "entry": r"entry(?:\s*price)?",
    "stop_loss": r"(?:stop\s*-?\s*loss|sl)",
    "target1": r"(?:target\s*-?\s*1|tp\s*-?\s*1|take\s*profit\s*-?\s*1)",
    "target2": r"(?:target\s*-?\s*2|tp\s*-?\s*2|take\s*profit\s*-?\s*2)",
    "target3": r"(?:target\s*-?\s*3|tp\s*-?\s*3|take\s*profit\s*-?\s*3)",
    "target4": r"(?:target\s*-?\s*4|tp\s*-?\s*4|take\s*profit\s*-?\s*4)",
    "target5": r"(?:target\s*-?\s*5|tp\s*-?\s*5|take\s*profit\s*-?\s*5)",
    "leverage": r"leverage",
}


class SignalParseError(Exception):
    """A message could not be turned into a valid signal.

    ``ignorable`` is True for ordinary chat text that is not signal-shaped;
    those are recorded as IGNORED rather than REJECTED.
    """

    def __init__(self, reason: str, *, ignorable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.ignorable = ignorable


def normalize_text(text: str) -> str:
    cleaned = unicodedata.normalize("NFKC", text or "")
    return cleaned.replace("\u00a0", " ").replace("\u200b", "").replace("\ufeff", "")


_NEXT_LABEL = r"(?=\s+(?:entry|stop|sl\b|target|tp\s*\d|take\s*profit|leverage)|\s*$)"


def _labelled_values(text: str, label: str) -> list[str]:
    """Return the value text that follows each ``label:`` occurrence.

    The capture stops at the end of the line or at the next known label, so
    single-line messages such as ``Entry: 0.3 Target 2: 0.2`` still work.
    """
    pattern = re.compile(rf"(?im)\b{label}\b\s*[:=]\s*(.*?){_NEXT_LABEL}")
    return [match.group(1) for match in pattern.finditer(text)]


def _single_number(values: list[str], field: str) -> Decimal | None:
    """Extract exactly one number from each labelled occurrence; all must agree."""
    if not values:
        return None
    found: set[Decimal] = set()
    for raw in values:
        numbers = _NUMBER_RE.findall(raw)
        if len(numbers) != 1:
            raise SignalParseError(f"{field} value is missing, ambiguous or invalid: {raw.strip()!r}")
        found.add(to_decimal(numbers[0], field))
    if len(found) > 1:
        raise SignalParseError(f"{field} appears more than once with conflicting values")
    return next(iter(found))


def _require_positive(value: Decimal | None, field: str) -> Decimal:
    if value is None:
        raise SignalParseError(f"missing {field}")
    if value <= 0:
        raise SignalParseError(f"{field} must be positive (got {value})")
    return value


def _optional_positive(value: Decimal | None, field: str) -> Decimal | None:
    if value is None:
        return None
    if value <= 0:
        raise SignalParseError(f"{field} must be positive (got {value})")
    return value


def _split_joined(token: str) -> tuple[str, str] | None:
    """Split a ``#CRVUSDT`` style token into its base asset and quote currency."""
    for quote in _SUPPORTED_QUOTES:
        if token.endswith(quote) and len(token) > len(quote):
            return token[: -len(quote)], quote
    return None


def parse_symbol(text: str) -> tuple[str, str, str]:
    """Return (symbol, base, quote), e.g. ('CRVUSDT', 'CRV', 'USDT')."""
    pairs = {(b.upper(), q.upper()) for b, q in _HASH_PAIR_RE.findall(text)}
    if not pairs:
        joined = [m.upper() for m in _HASH_JOINED_RE.findall(text)]
        pairs = {pair for token in joined if (pair := _split_joined(token)) is not None}
    if not pairs:
        pairs = set(_BARE_PAIR_RE.findall(text))
    if not pairs:
        raise SignalParseError("missing symbol")
    if len(pairs) > 1:
        raise SignalParseError(f"ambiguous symbol: {sorted(pairs)}")
    base, quote = pairs.pop()
    if quote not in _SUPPORTED_QUOTES:
        raise SignalParseError(
            f"unsupported quote asset {quote!r}; supported: {', '.join(_SUPPORTED_QUOTES)}"
        )
    if not re.fullmatch(r"[A-Z0-9]{2,20}", base):
        raise SignalParseError(f"invalid base asset {base!r}")
    return f"{base}{quote}", base, quote


def parse_direction(text: str) -> Direction:
    longs = bool(_LONG_RE.search(text))
    shorts = bool(_SHORT_RE.search(text))
    if longs and shorts:
        raise SignalParseError("ambiguous direction: both LONG and SHORT words present")
    if longs:
        return Direction.LONG
    if shorts:
        return Direction.SHORT
    raise SignalParseError("missing explicit LONG/SHORT direction")


def validate_target_direction(direction: Direction, entry: Decimal, target: Decimal) -> None:
    """LONG: target above entry. SHORT: target below entry. Never adjusts prices."""
    if direction is Direction.LONG and not target > entry:
        raise SignalParseError(f"invalid LONG target: Target 1 {target} must be above Entry {entry}")
    if direction is Direction.SHORT and not target < entry:
        raise SignalParseError(f"invalid SHORT target: Target 1 {target} must be below Entry {entry}")


def _looks_like_signal(text: str) -> bool:
    return bool(
        _labelled_values(text, _LABELS["entry"])
        or _labelled_values(text, _LABELS["target1"])
        or _LONG_RE.search(text)
        or _SHORT_RE.search(text)
        or _HASH_PAIR_RE.search(text)
    )


def parse_signal(text: str, *, channel_id: int, message_id: int, signal_time: datetime) -> ParsedSignal:
    """Parse one Telegram message. Raises SignalParseError on any problem."""
    norm = normalize_text(text)
    if not _looks_like_signal(norm):
        raise SignalParseError("not a signal-format message", ignorable=True)

    symbol, base, quote = parse_symbol(norm)
    direction = parse_direction(norm)

    entry = _require_positive(_single_number(_labelled_values(norm, _LABELS["entry"]), "Entry"), "Entry")
    target1 = _require_positive(
        _single_number(_labelled_values(norm, _LABELS["target1"]), "Target 1"),
        "Target 1",
    )
    leverage = _require_positive(
        _single_number(_labelled_values(norm, _LABELS["leverage"]), "Leverage"),
        "Leverage",
    )
    stop_loss = _optional_positive(
        _single_number(_labelled_values(norm, _LABELS["stop_loss"]), "Stop Loss"), "Stop Loss"
    )
    target2 = _optional_positive(_single_number(_labelled_values(norm, _LABELS["target2"]), "Target 2"), "Target 2")
    target3 = _optional_positive(_single_number(_labelled_values(norm, _LABELS["target3"]), "Target 3"), "Target 3")
    target4 = _optional_positive(_single_number(_labelled_values(norm, _LABELS["target4"]), "Target 4"), "Target 4")
    target5 = _optional_positive(_single_number(_labelled_values(norm, _LABELS["target5"]), "Target 5"), "Target 5")

    validate_target_direction(direction, entry, target1)

    return ParsedSignal(
        channel_id=channel_id,
        message_id=message_id,
        signal_time=signal_time,
        symbol=symbol,
        base_asset=base,
        quote_asset=quote,
        direction=direction,
        entry=entry,
        target1=target1,
        telegram_leverage=leverage,
        stop_loss=stop_loss,
        target2=target2,
        target3=target3,
        target4=target4,
        target5=target5,
    )
