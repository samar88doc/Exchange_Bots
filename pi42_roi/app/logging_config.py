"""Structured logging with secret redaction."""

from __future__ import annotations

import logging
import sys

_SECRETS: set[str] = set()
REDACTED = "***REDACTED***"


def register_secret(value: str) -> None:
    """Register a secret value so it is scrubbed from every log line."""
    if value and len(value) >= 4:
        _SECRETS.add(value)


def redact(text: str) -> str:
    for secret in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return text


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_RedactingFormatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for noisy in ("httpx", "httpcore", "telethon"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _fmt_value(value: object) -> str:
    text = str(value)
    if not text or any(ch.isspace() for ch in text) or "=" in text:
        return repr(text)
    return text


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: object) -> None:
    """Emit a single key=value structured log line."""
    parts = " ".join(f"{k}={_fmt_value(v)}" for k, v in fields.items() if v is not None)
    logger.log(level, "event=%s %s", event, parts)
