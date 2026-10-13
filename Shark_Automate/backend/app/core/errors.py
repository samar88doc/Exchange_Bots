"""Error taxonomy: every failure is classified, coded and attributable."""
from __future__ import annotations

from enum import StrEnum


class FailureKind(StrEnum):
    VALIDATION = "VALIDATION"                # permanent: payload/contract invalid
    TEMPORARY = "TEMPORARY"                  # retry safe when idempotency guaranteed
    EXCHANGE_REJECTION = "EXCHANGE_REJECTION"  # permanent exchange refusal
    RISK_REJECTION = "RISK_REJECTION"        # permanent for this signal
    SYMBOL_RESOLUTION = "SYMBOL_RESOLUTION"  # permanent for this symbol
    SYSTEM = "SYSTEM"                        # unexpected internal failure
    STATE = "STATE"                          # unknown/ambiguous order state -> reconcile


class RejectReason(StrEnum):
    INVALID_PAYLOAD = "INVALID_PAYLOAD"
    INVALID_SIGNATURE = "INVALID_SIGNATURE"
    STALE_EVENT = "STALE_EVENT"
    DUPLICATE_EVENT = "DUPLICATE_EVENT"
    DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL"
    SYMBOL_ALREADY_OPEN = "SYMBOL_ALREADY_OPEN"
    MAX_OPEN_POSITIONS_REACHED = "MAX_OPEN_POSITIONS_REACHED"
    SHARK_SYMBOL_NOT_FOUND = "SHARK_SYMBOL_NOT_FOUND"
    SYMBOL_NOT_FOUND = "SYMBOL_NOT_FOUND"
    CONTRACT_INACTIVE = "CONTRACT_INACTIVE"
    INVALID_QUANTITY = "INVALID_QUANTITY"
    MIN_QUANTITY_NOT_MET = "MIN_QUANTITY_NOT_MET"
    MIN_NOTIONAL_NOT_MET = "MIN_NOTIONAL_NOT_MET"
    MARGIN_RULES_INVALID = "MARGIN_RULES_INVALID"
    LEVERAGE_UNAVAILABLE = "LEVERAGE_UNAVAILABLE"
    TRADING_HALTED = "TRADING_HALTED"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    SHORT_DISABLED = "SHORT_DISABLED"
    ACTION_NOT_SUPPORTED = "ACTION_NOT_SUPPORTED"
    STRATEGY_DISABLED = "STRATEGY_DISABLED"
    STRATEGY_NOT_FOUND = "STRATEGY_NOT_FOUND"
    LIVE_NOT_ENABLED = "LIVE_NOT_ENABLED"
    DRY_RUN = "DRY_RUN"
    SHARK_UNAVAILABLE = "SHARK_UNAVAILABLE"
    DAILY_TRADE_LIMIT = "DAILY_TRADE_LIMIT"
    DAILY_MAX_TRIGGERS_REACHED = "DAILY_MAX_TRIGGERS_REACHED"
    PRICE_NOT_BELOW_PREVIOUS = "PRICE_NOT_BELOW_PREVIOUS"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    ORDER_SUBMISSION_UNCERTAIN = "ORDER_SUBMISSION_UNCERTAIN"
    RESERVATION_LIMIT = "RESERVATION_LIMIT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class TradingError(Exception):
    """Base error carrying code + failure kind + context for auditability."""

    def __init__(self, reason: RejectReason | str, message: str = "", kind: FailureKind = FailureKind.SYSTEM, **ctx):
        self.reason = str(reason)
        self.message = message or self.reason
        self.kind = kind
        self.ctx = ctx
        super().__init__(f"{self.reason}: {self.message}")


class RiskRejection(TradingError):
    def __init__(self, reason: RejectReason, message: str = "", **ctx):
        super().__init__(reason, message, FailureKind.RISK_REJECTION, **ctx)


class SymbolError(TradingError):
    def __init__(self, reason: RejectReason, message: str = "", **ctx):
        super().__init__(reason, message, FailureKind.SYMBOL_RESOLUTION, **ctx)


class ValidationError_(TradingError):
    def __init__(self, reason: RejectReason, message: str = "", **ctx):
        super().__init__(reason, message, FailureKind.VALIDATION, **ctx)


class ExchangeError(TradingError):
    def __init__(self, reason: RejectReason | str, message: str = "", kind: FailureKind = FailureKind.EXCHANGE_REJECTION, **ctx):
        super().__init__(reason, message, kind, **ctx)


class OrderStateUncertain(TradingError):
    """Submission result unknown (timeout/network). MUST reconcile before retry."""

    def __init__(self, message: str = "", **ctx):
        super().__init__(RejectReason.ORDER_SUBMISSION_UNCERTAIN, message, FailureKind.STATE, **ctx)
