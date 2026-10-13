"""Security helpers: webhook auth, request fingerprints, Shark HMAC signing."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def webhook_authenticated(provided: str | None, expected: str) -> bool:
    if not provided or not expected:
        return False
    return constant_time_equals(provided, expected)


def canonical_json(payload: dict[str, Any]) -> str:
    """Stable JSON serialization used for fingerprints and signatures."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)


def event_fingerprint(payload: dict[str, Any]) -> str:
    """Deterministic fingerprint when TradingView supplies no unique event id.

    Uses immutable signal fields only (never volatile fields like delivery time).
    """
    basis = {
        "strategy_id": payload.get("strategy_id"),
        "symbol": payload.get("symbol"),
        "action": payload.get("action"),
        "timestamp": payload.get("timestamp"),
        "price": payload.get("price") or payload.get("alert_price"),
        "timeframe": payload.get("timeframe"),
    }
    return hashlib.sha256(canonical_json(basis).encode()).hexdigest()


def request_hash(raw_body: bytes) -> str:
    return hashlib.sha256(raw_body).hexdigest()


def shark_signature(api_secret: str, data_to_sign: str) -> str:
    """HMAC-SHA256 signature exactly as specified by Shark API docs (2025-12-16)."""
    return hmac.new(api_secret.encode(), data_to_sign.encode(), hashlib.sha256).hexdigest()


def shark_sign_body(api_secret: str, body: dict[str, Any]) -> str:
    return shark_signature(api_secret, json.dumps(body, separators=(",", ":")))


def shark_sign_query(api_secret: str, query_string: str) -> str:
    return shark_signature(api_secret, query_string)


def now_ms() -> str:
    return str(int(time.time() * 1000))
