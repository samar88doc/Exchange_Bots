"""Decimal helpers. All prices, quantities, balances and leverage use Decimal."""

from __future__ import annotations

from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Mapping


def to_decimal(value: Any, field: str = "value") -> Decimal:
    """Convert an API/config value to a finite Decimal.

    Floats are converted through ``str`` so binary artefacts such as
    ``0.1 + 0.2`` never leak into prices or quantities.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field}: invalid decimal {value!r}")
    if isinstance(value, float):
        value = repr(value)
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field}: invalid decimal {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"{field}: non-finite decimal {value!r}")
    return result


def req_decimal(data: Mapping[str, Any], key: str) -> Decimal:
    """Read a required decimal field from an API payload."""
    if key not in data or data[key] is None or data[key] == "":
        raise ValueError(f"missing required field {key!r}")
    return to_decimal(data[key], key)


def opt_decimal(data: Mapping[str, Any], key: str) -> Decimal | None:
    """Read an optional decimal field; missing/null/empty yields None."""
    value = data.get(key)
    if value is None or value == "":
        return None
    return to_decimal(value, key)


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round ``value`` DOWN to the nearest multiple of ``step``."""
    if step <= 0:
        raise ValueError("step must be positive")
    units = (value / step).to_integral_value(rounding=ROUND_FLOOR)
    return units * step


def round_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round ``value`` to the nearest multiple of ``step``."""
    if step <= 0:
        raise ValueError("step must be positive")
    units = (value / step).to_integral_value(rounding=ROUND_HALF_UP)
    return units * step


def format_decimal(value: Decimal) -> str:
    """Render a Decimal without exponent notation or trailing zeros."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
