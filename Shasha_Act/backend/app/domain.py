"""Decimal-only order math and the SHASHA alert contract parser."""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
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
    target2: Decimal
    target3: Decimal


@dataclass(frozen=True)
class QuantitySplit:
    target1: Decimal
    target2: Decimal
    target3: Decimal

    @property
    def total(self) -> Decimal:
        return self.target1 + self.target2 + self.target3


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

    strategy = str(fields.get("strategy") or "SHASHA").strip().upper()
    if strategy != "SHASHA":
        errors.append("UNSUPPORTED_STRATEGY")

    raw_symbol = str(fields.get("symbol") or fields.get("ticker") or "").strip()
    canonical_symbol: str | None = None
    if raw_symbol:
        try:
            canonical_symbol = normalize_symbol(raw_symbol)
        except ValidationProblem as exc:
            errors.append(str(exc))
    else:
        errors.append("MISSING_SYMBOL")

    direction = str(fields.get("direction") or "LONG").strip().upper()
    if direction != "LONG":
        errors.append("ONLY_LONG_SIGNALS_SUPPORTED")

    timeframe = str(fields.get("timeframe") or "").strip()
    if not timeframe:
        errors.append("MISSING_TIMEFRAME")

    bar_time = parse_bar_time(fields.get("bar_time") or fields.get("time"))
    if bar_time is None:
        errors.append("MISSING_OR_INVALID_BAR_TIME")

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

    signal_close = parse_required("signal_close", ("close",))
    entry_price = parse_required("entry_price", ("previous_close",))
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
