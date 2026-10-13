"""Domain enumerations: signal/order/position state machines and statuses."""
from __future__ import annotations

import enum


class TradingMode(enum.StrEnum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class SignalAction(enum.StrEnum):
    BUY = "BUY"
    OPEN_LONG = "OPEN_LONG"
    SELL = "SELL"
    CLOSE = "CLOSE"
    SHORT = "SHORT"
    CLOSE_SHORT = "CLOSE_SHORT"


class Direction(enum.StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class ValidationStatus(enum.StrEnum):
    RECEIVED = "RECEIVED"
    VALIDATED = "VALIDATED"
    INVALID = "INVALID"


class ProcessingStatus(enum.StrEnum):
    # lifecycle
    RECEIVED = "RECEIVED"
    VALIDATING = "VALIDATING"
    SYMBOL_NORMALIZED = "SYMBOL_NORMALIZED"
    ACCEPTED = "ACCEPTED"
    EXECUTED = "EXECUTED"
    # negative terminal states
    REJECTED = "REJECTED"
    DUPLICATE = "DUPLICATE"
    ERROR = "ERROR"


class PositionState(enum.StrEnum):
    """Authoritative position state machine.

    SIGNAL_RECEIVED -> VALIDATING -> SYMBOL_NORMALIZED -> ACCEPTED
        -> POSITION_SLOT_RESERVED -> ORDER_PENDING -> ORDER_SUBMITTED
        -> PARTIALLY_FILLED -> OPEN -> MONITORING -> TP_TRIGGERED
        -> CLOSE_PENDING -> CLOSED
    Failure: REJECTED | ORDER_FAILED | CANCELLED | EXPIRED | UNKNOWN | RECONCILIATION_ERROR
    """

    SIGNAL_RECEIVED = "SIGNAL_RECEIVED"
    VALIDATING = "VALIDATING"
    SYMBOL_NORMALIZED = "SYMBOL_NORMALIZED"
    ACCEPTED = "ACCEPTED"
    POSITION_SLOT_RESERVED = "POSITION_SLOT_RESERVED"
    RESERVED = "RESERVED"                # slot held, no order yet
    ORDER_PENDING = "ORDER_PENDING"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    OPEN = "OPEN"
    MONITORING = "MONITORING"
    TP_TRIGGERED = "TP_TRIGGERED"
    CLOSE_PENDING = "CLOSE_PENDING"
    CLOSED = "CLOSED"
    # failure states
    REJECTED = "REJECTED"
    ORDER_FAILED = "ORDER_FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"
    RECONCILIATION_ERROR = "RECONCILIATION_ERROR"


# States that consume a risk slot (OPEN + RESERVED <= MAX)
SLOT_CONSUMING_STATES = frozenset(
    {
        PositionState.POSITION_SLOT_RESERVED,
        PositionState.RESERVED,
        PositionState.ORDER_PENDING,
        PositionState.ORDER_SUBMITTED,
        PositionState.PARTIALLY_FILLED,
        PositionState.OPEN,
        PositionState.MONITORING,
        PositionState.TP_TRIGGERED,
        PositionState.CLOSE_PENDING,
        PositionState.UNKNOWN,
    }
)

ACTIVE_SYMBOL_STATES = SLOT_CONSUMING_STATES  # symbol occupancy == slot occupancy


class OrderType(enum.StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"
    STOP_LIMIT = "STOP_LIMIT"


class OrderStatus(enum.StrEnum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    DRY_RUN = "DRY_RUN"


class OrderSide(enum.StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class EntryExit(enum.StrEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"


class SignalListStatus(enum.StrEnum):
    """Signal-history display statuses."""

    RECEIVED = "RECEIVED"
    VALIDATED = "VALIDATED"
    ACCEPTED = "ACCEPTED"
    EXECUTED = "EXECUTED"
    REJECTED = "REJECTED"
    DUPLICATE = "DUPLICATE"
    MAX_POSITIONS = "MAX_POSITIONS"
    SYMBOL_ALREADY_OPEN = "SYMBOL_ALREADY_OPEN"
    SHARK_SYMBOL_NOT_FOUND = "SHARK_SYMBOL_NOT_FOUND"
    DAILY_MAX_TRIGGERS = "DAILY_MAX_TRIGGERS"
    PRICE_NOT_BELOW_PREVIOUS = "PRICE_NOT_BELOW_PREVIOUS"
    INVALID = "INVALID"
    ERROR = "ERROR"


class HealthState(enum.StrEnum):
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    DISCONNECTED = "DISCONNECTED"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"


class ContractStatus(enum.StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    UNKNOWN = "UNKNOWN"


class ReservationStatus(enum.StrEnum):
    ACTIVE = "ACTIVE"
    CONVERTED = "CONVERTED"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"
