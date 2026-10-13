"""Structured JSON logging with trading event codes.

Never logs secrets. Every trading-relevant line carries correlation context.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from contextvars import ContextVar
from enum import StrEnum
from pathlib import Path
from typing import Any

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")
mode_var: ContextVar[str] = ContextVar("mode", default="PAPER")


class TradeEvent(StrEnum):
    """Canonical trading/system event codes (persisted to system_events/audit)."""

    WEBHOOK_RECEIVED = "WEBHOOK_RECEIVED"
    WEBHOOK_REJECTED = "WEBHOOK_REJECTED"
    SYMBOL_NORMALIZED = "SYMBOL_NORMALIZED"
    SYMBOL_RESOLVED = "SYMBOL_RESOLVED"
    SIGNAL_VALIDATED = "SIGNAL_VALIDATED"
    SIGNAL_REJECTED = "SIGNAL_REJECTED"
    POSITION_SLOT_RESERVED = "POSITION_SLOT_RESERVED"
    POSITION_RESERVATION_RELEASED = "POSITION_RESERVATION_RELEASED"
    ORDER_CREATED = "ORDER_CREATED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_FAILED = "ORDER_FAILED"
    POSITION_OPENED = "POSITION_OPENED"
    ROI_UPDATED = "ROI_UPDATED"
    TP_TRIGGERED = "TP_TRIGGERED"
    CLOSE_SUBMITTED = "CLOSE_SUBMITTED"
    POSITION_CLOSED = "POSITION_CLOSED"
    DUPLICATE_DETECTED = "DUPLICATE_DETECTED"
    MAX_POSITION_REACHED = "MAX_POSITION_REACHED"
    SHARK_SYMBOL_NOT_FOUND = "SHARK_SYMBOL_NOT_FOUND"
    RECONCILIATION_ERROR = "RECONCILIATION_ERROR"
    SYSTEM_ERROR = "SYSTEM_ERROR"
    TRADING_HALTED = "TRADING_HALTED"
    KILL_SWITCH = "KILL_SWITCH"
    LIVE_ACTIVATED = "LIVE_ACTIVATED"
    DRY_RUN_ORDER = "DRY_RUN_ORDER"


SECRET_KEYS = {
    "secret", "api_key", "apikey", "api_secret", "apisecret", "password", "token",
    "authorization", "webhook_secret", "shark_api_key", "shark_api_secret",
    "live_trading_confirmation", "telegram_bot_token", "signature",
}


def _scrub(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("***REDACTED***" if k.lower() in SECRET_KEYS else _scrub(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    return obj


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": correlation_id_var.get(),
            "mode": mode_var.get(),
        }
        extra = getattr(record, "event_data", None)
        if extra:
            payload.update(_scrub(extra))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(_scrub(payload), default=str)


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        for name in ("app", "trading", "error", "audit"):
            fh = logging.FileHandler(log_dir / f"{name}.log")
            fh.setFormatter(JsonFormatter())
            target = {
                "app": "app",
                "trading": "trading",
                "error": "app.error",
                "audit": "audit",
            }[name]
            logging.getLogger(target).addHandler(fh)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(
    logger: logging.Logger,
    event: TradeEvent,
    message: str = "",
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    data = {"event": str(event), **fields}
    logger.log(level, message or str(event), extra={"event_data": data})
