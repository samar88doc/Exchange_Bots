"""Decimal-only order math and the SHASHA alert contract parser."""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import (
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_UP,
    Decimal,
    InvalidOperation,
)
from typing import Any


class ValidationProblem(ValueError):
    """A payload or order parameter is unsafe to execute."""


@dataclass(frozen=True)
class ParsedSignal:
    strategy: str
    supplied_event_id: str | None
    tradingview_symbol: str
    symbol: str | None
    direction: str
    timeframe: str
    bar_time: datetime | None
    signal_close: Decimal | None
    entry_price: Decimal | None
    raw_payload: dict[str, Any] | None
    raw_body_hash: str
    idempotency_key: str
    errors: tuple[str, ...]


@dataclass(frozen=True)
class PriceLevels:
    stop: Decimal
    target1: Decimal
    target2: Decimal | None = None
    target3: Decimal | None = None


@dataclass(frozen=True)
class QuantitySplit:
    target1: Decimal
    target2: Decimal
    target3: Decimal

    @property
    def total(self) -> Decimal:
        return self.target1 + self.target2 + self.target3


@dataclass(frozen=True)
class PartialSplit:
    """Target-1 slice plus the runner that stays open for trailing protection."""

    partial: Decimal
    runner: Decimal

    @property
    def total(self) -> Decimal:
        return self.partial + self.runner


@dataclass(frozen=True)
class Candle:
    """One exchange candlestick, normalised regardless of source ordering."""

    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None

    def is_closed(self, now: datetime) -> bool:
        return self.close_time <= now


INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
    "3d": 259200,
    "1w": 604800,
}

_INTERVAL_ALIASES = {
    "1": "1m",
    "3": "3m",
    "5": "5m",
    "15": "15m",
    "30": "30m",
    "60": "1h",
    "120": "2h",
    "240": "4h",
    "360": "6h",
    "480": "8h",
    "720": "12h",
    "d": "1d",
    "1d": "1d",
    "w": "1w",
    "1w": "1w",
}


def normalize_interval(value: Any, default: str = "5m") -> str:
    """Normalize TradingView/Shark interval spellings to Shark's `1m/5m/1h/4h/1d` style."""
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in INTERVAL_SECONDS:
        return text
    if text in _INTERVAL_ALIASES:
        return _INTERVAL_ALIASES[text]
    if text.endswith("min") and text[:-3].isdigit():
        return f"{text[:-3]}m"
    if text.endswith("h") and text[:-1].isdigit():
        return f"{text[:-1]}h"
    if text.endswith("m") and text[:-1].isdigit():
        return f"{text[:-1]}m"
    if text.endswith("d") and text[:-1].isdigit():
        return f"{text[:-1]}d"
    return default


def interval_seconds(interval: str) -> int:
    return INTERVAL_SECONDS.get(normalize_interval(interval), INTERVAL_SECONDS["5m"])


def _ms_to_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not numeric.is_finite() or numeric <= 0:
        return None
    seconds = numeric / Decimal("1000") if numeric > Decimal("100000000000") else numeric
    try:
        return datetime.fromtimestamp(float(seconds), tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _candle_from_dict(item: dict[str, Any], interval: str) -> Candle | None:
    nested = item.get("k") if isinstance(item.get("k"), dict) else None
    root = nested if isinstance(nested, dict) else item
    close = decimal_or_none(root.get("c") if nested else root.get("close"))
    if close is None:
        close = decimal_or_none(root.get("close"))
    open_price = decimal_or_none(root.get("o") if nested else root.get("open")) or close
    high = decimal_or_none(root.get("h") if nested else root.get("high")) or close
    low = decimal_or_none(root.get("l") if nested else root.get("low")) or close
    volume = decimal_or_none(root.get("v") if nested else root.get("volume"))
    start = _ms_to_datetime(root.get("t") if nested else root.get("startTime", root.get("openTime")))
    end = _ms_to_datetime(root.get("T") if nested else root.get("closeTime", root.get("endTime")))
    if close is None or close <= 0 or start is None:
        return None
    if end is None:
        end = start + timedelta(seconds=interval_seconds(interval))
    return Candle(
        open_time=start,
        close_time=end,
        open=open_price if open_price and open_price > 0 else close,
        high=max(high or close, close),
        low=min(low or close, close),
        close=close,
        volume=volume,
    )


def _candle_from_list(item: list[Any], interval: str) -> Candle | None:
    # Binance-compatible positional form: [openTime, open, high, low, close, volume, closeTime, ...]
    if len(item) < 5:
        return None
    start = _ms_to_datetime(item[0])
    open_price = decimal_or_none(item[1])
    high = decimal_or_none(item[2])
    low = decimal_or_none(item[3])
    close = decimal_or_none(item[4])
    volume = decimal_or_none(item[5]) if len(item) > 5 else None
    end = _ms_to_datetime(item[6]) if len(item) > 6 else None
    if start is None or close is None or close <= 0:
        return None
    if end is None:
        end = start + timedelta(seconds=interval_seconds(interval))
    return Candle(
        open_time=start,
        close_time=end,
        open=open_price or close,
        high=max(high or close, close),
        low=min(low or close, close),
        close=close,
        volume=volume,
    )


def parse_candles(payload: Any, interval: str = "5m") -> list[Candle]:
    """Parse a Shark kline response into candles, sorted oldest → newest.

    Handles the documented object form, a nested `k` form, a positional array form, and
    both `{"data": [...]}` wrapping styles. Unknown shapes produce an empty list rather
    than a guessed price.
    """
    root: Any = payload
    if isinstance(root, dict) and "data" in root:
        root = root["data"]
    if isinstance(root, dict) and isinstance(root.get("data"), (list, dict)):
        root = root["data"]
    if isinstance(root, dict) and isinstance(root.get("list"), list):
        root = root["list"]
    if not isinstance(root, list):
        return []
    candles: list[Candle] = []
    for item in root:
        candle: Candle | None = None
        if isinstance(item, dict):
            candle = _candle_from_dict(item, interval)
        elif isinstance(item, list):
            candle = _candle_from_list(item, interval)
        if candle is not None:
            candles.append(candle)
    candles.sort(key=lambda candle: candle.open_time)
    return candles


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def last_closed_candle(
    candles: list[Candle],
    *,
    now: datetime | None = None,
    max_age_seconds: int | None = None,
) -> Candle | None:
    """Return the newest fully-closed candle, independent of source ordering.

    When `max_age_seconds` is supplied, a candle whose close time is older than that window
    is treated as unusable so a stale price never becomes an entry.
    """
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    for candle in reversed(candles):
        if not candle.is_closed(moment):
            continue
        if max_age_seconds is not None:
            age = (moment - candle.close_time).total_seconds()
            if age > max_age_seconds:
                return None
        return candle
    return None


def quantize_price(value: Decimal, precision: int, rounding: str = ROUND_DOWN) -> Decimal:
    """Represent a price with the contract's exact decimal precision (for example 0.00239)."""
    if not 0 <= precision <= 18:
        raise ValidationProblem("INVALID_EXCHANGE_PRECISION")
    if value <= 0:
        raise ValidationProblem("INVALID_PRICE")
    exponent = Decimal(1).scaleb(-precision)
    return value.quantize(exponent, rounding=rounding)


def compute_strategy_levels(
    entry: Decimal,
    price_tick: Decimal,
    stop_loss_percent: Decimal,
    target1_percent: Decimal,
    target2_percent: Decimal | None = None,
    target3_percent: Decimal | None = None,
) -> PriceLevels:
    """SHASHA levels: stop below entry, Target 1 above entry, optional later targets.

    Targets are optional because the shipped strategy closes 50% at Target 1 and then
    protects the runner with a trailing stop instead of fixed Target 2/3 exits.
    """
    if entry <= 0 or min(price_tick, stop_loss_percent, target1_percent) <= 0:
        raise ValidationProblem("INVALID_LEVEL_PARAMETERS")
    for later in (target2_percent, target3_percent):
        if later is not None and later <= 0:
            raise ValidationProblem("INVALID_LEVEL_PARAMETERS")
    if target2_percent is not None and target2_percent <= target1_percent:
        raise ValidationProblem("TARGETS_MUST_BE_STRICTLY_INCREASING")
    if target3_percent is not None and (
        target2_percent is None or target3_percent <= target2_percent
    ):
        raise ValidationProblem("TARGETS_MUST_BE_STRICTLY_INCREASING")
    # For a long, rounding the stop up avoids increasing intended downside; targets round up
    # so they do not trigger at a price movement smaller than the configured threshold.
    stop = round_up_to_step(entry * (Decimal("1") - stop_loss_percent / Decimal("100")), price_tick)
    t1 = round_up_to_step(entry * (Decimal("1") + target1_percent / Decimal("100")), price_tick)
    t2 = (
        round_up_to_step(entry * (Decimal("1") + target2_percent / Decimal("100")), price_tick)
        if target2_percent is not None
        else None
    )
    t3 = (
        round_up_to_step(entry * (Decimal("1") + target3_percent / Decimal("100")), price_tick)
        if target3_percent is not None
        else None
    )
    if stop >= entry or t1 <= entry:
        raise ValidationProblem("TICK_SIZE_COLLAPSES_RISK_LEVELS")
    if t2 is not None and t2 <= t1:
        raise ValidationProblem("TICK_SIZE_COLLAPSES_RISK_LEVELS")
    if t3 is not None and (t2 is None or t3 <= t2):
        raise ValidationProblem("TICK_SIZE_COLLAPSES_RISK_LEVELS")
    return PriceLevels(stop=stop, target1=t1, target2=t2, target3=t3)


def split_partial_quantity(
    quantity: Decimal,
    quantity_step: Decimal,
    minimum_quantity: Decimal,
    close_percent: Decimal,
) -> PartialSplit:
    """Split Q into the Target-1 slice (default 50%) and the trailing runner.

    Rounding is applied to the partial slice, and the runner always receives the remainder so
    the two pieces conserve the original quantity exactly.
    """
    if quantity <= 0 or quantity_step <= 0 or minimum_quantity < 0:
        raise ValidationProblem("INVALID_SPLIT_PARAMETERS")
    if not Decimal("0") < close_percent < Decimal("100"):
        raise ValidationProblem("INVALID_PARTIAL_CLOSE_PERCENT")
    partial = round_down_to_step(quantity * close_percent / Decimal("100"), quantity_step)
    runner = quantity - partial
    if partial <= 0 or runner <= 0:
        raise ValidationProblem("TARGET_SLICE_BELOW_MINIMUM_QUANTITY")
    for piece in (partial, runner):
        if piece % quantity_step != 0:
            raise ValidationProblem("TARGET_SLICE_NOT_ALIGNED_TO_STEP")
        if piece < minimum_quantity:
            raise ValidationProblem("TARGET_SLICE_BELOW_MINIMUM_QUANTITY")
    return PartialSplit(partial=partial, runner=runner)


def next_trailing_stop(
    *,
    high_water: Decimal,
    current_stop: Decimal | None,
    floor_price: Decimal,
    trailing_percent: Decimal,
    price_tick: Decimal,
    reference_price: Decimal | None = None,
) -> Decimal:
    """Ratchet a long-position stop upward, one step per upward percent move.

    `high_water` is the highest price seen since trailing protection was armed. The stop is
    never allowed below `floor_price` (the break-even entry after Target 1) and never allowed
    to move down, so protection can only improve.
    """
    if high_water <= 0 or trailing_percent <= 0 or price_tick <= 0:
        raise ValidationProblem("INVALID_TRAILING_PARAMETERS")
    if floor_price <= 0:
        raise ValidationProblem("INVALID_TRAILING_FLOOR")
    candidate = round_down_to_step(
        high_water * (Decimal("1") - trailing_percent / Decimal("100")), price_tick
    )
    stop = max(candidate, floor_price)
    if reference_price is not None and stop >= reference_price:
        # Never arm a stop at or above the live price; it would exit immediately at a
        # price the market has not traded.
        stop = max(floor_price, min(stop, reference_price))
    if current_stop is not None and stop <= current_stop:
        return current_stop
    if stop >= high_water:
        raise ValidationProblem("TRAILING_STOP_NOT_BELOW_PRICE")
    return stop


def normalize_symbol(symbol: str) -> str:
    """Uppercase a TradingView symbol and remove only one terminal `.P` suffix."""
    value = symbol.strip()
    if not value:
        raise ValidationProblem("MISSING_SYMBOL")
    if value[-2:].casefold() == ".p":
        value = value[:-2]
    if not re.fullmatch(r"[A-Za-z0-9]{2,64}", value):
        raise ValidationProblem("INVALID_SYMBOL_FORMAT")
    return value.upper()


def decimal_value(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValidationProblem(f"INVALID_{field.upper()}")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationProblem(f"INVALID_{field.upper()}") from exc
    if not result.is_finite() or result <= 0:
        raise ValidationProblem(f"INVALID_{field.upper()}")
    return result


def parse_bar_time(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, Decimal)) or (isinstance(value, str) and value.isdigit()):
            raw = Decimal(str(value))
            # TradingView time values are milliseconds; permit epoch seconds for integrations.
            seconds = float(raw / Decimal("1000") if raw > Decimal("100000000000") else raw)
            return datetime.fromtimestamp(seconds, tz=UTC)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (ValueError, OverflowError, InvalidOperation, OSError):
        return None


def _bucket_time(moment: datetime, timeframe: str) -> datetime:
    """Floor a timestamp to the start of its interval bucket (dedupe anchor for retries)."""
    seconds = interval_seconds(timeframe)
    epoch = int(moment.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % seconds), tz=UTC)


def _redact(value: Any, key: str = "") -> Any:
    sensitive = ("token", "secret", "signature", "api_key", "apikey", "authorization", "password")
    if any(part in key.casefold() for part in sensitive):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item, key) for item in value[:100]]
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        return str(value) if math.isfinite(value) else "[NON_FINITE]"
    if isinstance(value, str):
        from app.security import redact_text

        return redact_text(value[:2048])
    return value


def _make_idempotency_key(
    supplied_event_id: str | None,
    strategy: str,
    symbol: str | None,
    timeframe: str,
    bar_time: datetime | None,
    direction: str,
    body_hash: str,
) -> str:
    if supplied_event_id:
        source = f"event|{strategy}|{supplied_event_id.strip()}"
    elif symbol and timeframe and bar_time:
        source = f"signal|{strategy}|{symbol}|{timeframe}|{bar_time.isoformat()}|{direction}"
    else:
        # This key only deduplicates malformed/rejected payloads; it is never an execution identity.
        source = f"invalid|{body_hash}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def parse_alert(body: bytes) -> ParsedSignal:
    """Parse structured JSON or legacy text, retaining errors for a durable rejection event."""
    body_hash = hashlib.sha256(body).hexdigest()
    payload: dict[str, Any] | None = None
    text_body = body.decode("utf-8", errors="replace")
    errors: list[str] = []

    try:
        candidate = json.loads(text_body, parse_float=Decimal)
        if isinstance(candidate, dict):
            payload = candidate
        else:
            errors.append("JSON_TOP_LEVEL_MUST_BE_OBJECT")
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Legacy TradingView alert() text is deliberately supported, but never guessed.
        pass

    legacy_fields: dict[str, str] = {}
    if payload is None:
        for key, pattern in (
            ("symbol", r"(?im)^\s*(?:symbol|ticker)\s*:\s*([A-Za-z0-9.]+)\s*$"),
            ("signal_close", r"(?im)^\s*(?:signal\s*)?close\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*$"),
            ("entry_price", r"(?im)^\s*(?:entry|previous\s+candle\s+close|buy\s+price)\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*$"),
            ("timeframe", r"(?im)^\s*timeframe\s*:\s*([A-Za-z0-9]+)\s*$"),
            ("bar_time", r"(?im)^\s*(?:bar\s*time|time)\s*:\s*([^\s]+)\s*$"),
        ):
            match = re.search(pattern, text_body)
            if match:
                legacy_fields[key] = match.group(1)
        fields: dict[str, Any] = legacy_fields
        raw_payload = {"legacy_text": _redact(text_body)}
    else:
        fields = payload
        raw_payload = _redact(payload)

    raw_action = str(fields.get("action") or fields.get("side") or "").strip().upper()

    # Reject non-TradingView scraper sources (such as third-party Telegram forwarders)
    src = str(fields.get("source") or "").strip().upper()
    if src == "TELEGRAM":
        errors.append("TELEGRAM_SOURCE_PROHIBITED")

    strat_name = str(fields.get("strategy_name") or "").strip()
    strat_id = str(fields.get("strategy_id") or "").strip()
    if any(k in strat_name.upper() or k in strat_id.upper() for k in ("VIP", "JEMAL")):
        errors.append("THIRD_PARTY_VIP_PROHIBITED")

    strategy = str(fields.get("strategy") or (raw_action if raw_action not in ("BUY", "LONG") else "") or "SHASHA").strip().upper()
    if not strategy:
        strategy = "SHASHA"
    elif any(k in strategy for k in ("VIP", "JEMAL", "TELEGRAM")):
        errors.append("THIRD_PARTY_VIP_PROHIBITED")

    raw_symbol = str(fields.get("symbol") or fields.get("ticker") or "").strip()
    canonical_symbol: str | None = None
    if raw_symbol:
        try:
            canonical_symbol = normalize_symbol(raw_symbol)
        except ValidationProblem as exc:
            errors.append(str(exc))
    else:
        errors.append("MISSING_SYMBOL")

    direction = str(fields.get("direction") or "").strip().upper()
    if not direction:
        if raw_action in ("BUY", "LONG", "OPEN_LONG", "BUY_LONG") or "BUY" in raw_action or "LONG" in raw_action:
            direction = "LONG"
        elif raw_action in ("SELL", "SHORT", "OPEN_SHORT", "CLOSE"):
            direction = "SHORT"
        else:
            direction = "LONG"
    elif direction in ("BUY", "BUY_LONG"):
        direction = "LONG"

    if direction != "LONG":
        errors.append("ONLY_LONG_SIGNALS_SUPPORTED")

    timeframe = str(fields.get("timeframe") or fields.get("interval") or "").strip()
    if not timeframe:
        if "4H" in raw_action:
            timeframe = "4h"
        elif "1H" in raw_action:
            timeframe = "1h"
        elif "15" in raw_action:
            timeframe = "15"
        else:
            timeframe = "15"

    bar_time = parse_bar_time(fields.get("bar_time") or fields.get("time"))
    if bar_time is None:
        # A signal without an explicit candle time is anchored to the current interval bucket
        # so that a retried TradingView delivery cannot create a second trade for the same bar.
        bar_time = _bucket_time(datetime.now(UTC), timeframe)

    event_value = fields.get("event_id")
    supplied_event_id = str(event_value).strip() if event_value not in (None, "") else None
    if supplied_event_id and len(supplied_event_id) > 128:
        errors.append("EVENT_ID_TOO_LONG")
        supplied_event_id = None

    def parse_required(name: str, aliases: tuple[str, ...] = ()) -> Decimal | None:
        value = fields.get(name)
        if value is None:
            for alias in aliases:
                value = fields.get(alias)
                if value is not None:
                    break
        if value is None or value == "":
            errors.append(f"MISSING_{name.upper()}")
            return None
        try:
            return decimal_value(value, name)
        except ValidationProblem as exc:
            errors.append(str(exc))
            return None

    signal_close = parse_required("signal_close", ("close", "price", "alert_price", "last_price"))
    entry_price = parse_required("entry_price", ("previous_close", "price", "close", "alert_price", "last_price"))
    idempotency_key = _make_idempotency_key(
        supplied_event_id,
        strategy,
        canonical_symbol,
        timeframe,
        bar_time,
        direction,
        body_hash,
    )
    return ParsedSignal(
        strategy=strategy,
        supplied_event_id=supplied_event_id,
        tradingview_symbol=raw_symbol[:64],
        symbol=canonical_symbol,
        direction=direction,
        timeframe=timeframe[:24],
        bar_time=bar_time,
        signal_close=signal_close,
        entry_price=entry_price,
        raw_payload=raw_payload,
        raw_body_hash=body_hash,
        idempotency_key=idempotency_key,
        errors=tuple(dict.fromkeys(errors)),
    )


def decimal_step_for_precision(precision: int) -> Decimal:
    if not 0 <= precision <= 18:
        raise ValidationProblem("INVALID_EXCHANGE_PRECISION")
    return Decimal(1).scaleb(-precision)


def round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValidationProblem("INVALID_QUANTITY_STEP")
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def round_up_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValidationProblem("INVALID_PRICE_TICK")
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def align_to_tick(value: Decimal, tick: Decimal | None) -> Decimal:
    """Round a price to the nearest tradable tick before comparing it.

    Prices loaded back from storage can carry representation noise (SQLite stores NUMERIC
    through a float), so comparisons between a stored level and a live ticker price are always
    performed at tick granularity — which is also the granularity an exchange matches at.
    """
    if value is None:
        raise ValidationProblem("INVALID_PRICE")
    if tick is None:
        return value
    if tick <= 0:
        raise ValidationProblem("INVALID_PRICE_TICK")
    return (value / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick


def compute_long_levels(
    entry: Decimal,
    price_tick: Decimal,
    stop_loss_percent: Decimal,
    target1_percent: Decimal,
    target2_percent: Decimal,
    target3_percent: Decimal,
) -> PriceLevels:
    if entry <= 0 or min(price_tick, stop_loss_percent, target1_percent, target2_percent, target3_percent) <= 0:
        raise ValidationProblem("INVALID_LEVEL_PARAMETERS")
    if not target1_percent < target2_percent < target3_percent:
        raise ValidationProblem("TARGETS_MUST_BE_STRICTLY_INCREASING")
    # For a long, rounding a stop up avoids increasing intended downside; targets round up
    # so they do not trigger at a price movement smaller than the configured threshold.
    stop = round_up_to_step(entry * (Decimal("1") - stop_loss_percent / Decimal("100")), price_tick)
    t1 = round_up_to_step(entry * (Decimal("1") + target1_percent / Decimal("100")), price_tick)
    t2 = round_up_to_step(entry * (Decimal("1") + target2_percent / Decimal("100")), price_tick)
    t3 = round_up_to_step(entry * (Decimal("1") + target3_percent / Decimal("100")), price_tick)
    if stop >= entry or t1 <= entry or t2 <= t1 or t3 <= t2:
        raise ValidationProblem("TICK_SIZE_COLLAPSES_RISK_LEVELS")
    return PriceLevels(stop=stop, target1=t1, target2=t2, target3=t3)


def split_quantity(quantity: Decimal, quantity_step: Decimal, minimum_quantity: Decimal) -> QuantitySplit:
    """Split Q into valid Q1/Q2/Q3 while preserving exact total quantity."""
    if quantity <= 0 or quantity_step <= 0 or minimum_quantity < 0:
        raise ValidationProblem("INVALID_SPLIT_PARAMETERS")
    q1 = round_down_to_step(quantity / Decimal("3"), quantity_step)
    q2 = round_down_to_step((quantity - q1) / Decimal("2"), quantity_step)
    q3 = quantity - q1 - q2
    pieces = QuantitySplit(q1, q2, q3)
    if pieces.total != quantity:
        raise AssertionError("quantity conservation invariant violated")
    if min(q1, q2, q3) < minimum_quantity or min(q1, q2, q3) <= 0:
        raise ValidationProblem("TARGET_SLICE_BELOW_MINIMUM_QUANTITY")
    for piece in (q1, q2, q3):
        if piece % quantity_step != 0:
            raise ValidationProblem("TARGET_SLICE_NOT_ALIGNED_TO_STEP")
    return pieces


def quantity_from_notional(notional: Decimal, entry: Decimal, quantity_step: Decimal) -> Decimal:
    if min(notional, entry, quantity_step) <= 0:
        raise ValidationProblem("INVALID_ORDER_SIZE")
    return round_down_to_step(notional / entry, quantity_step)
