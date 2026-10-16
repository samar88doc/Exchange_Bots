"""Authentication, exact Shark request signing, and webhook rate limiting."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from collections import defaultdict, deque
from decimal import Decimal
from threading import Lock
from typing import Any
from urllib.parse import urlencode

PASSWORD_ALGORITHM = "pbkdf2_sha256"
_SENSITIVE_TEXT = re.compile(
    r"(?i)(token|secret|signature|api[_-]?key|authorization|password)\s*[:=]\s*(?:bearer\s+)?([^\s,;&\"']+)"
)


def constant_time_equals(candidate: str | None, expected: str | None) -> bool:
    if not candidate or not expected:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def hash_password(password: str, iterations: int = 240_000, *, salt: bytes | None = None) -> str:
    """PBKDF2-HMAC-SHA256 password hash usable without any third-party dependency."""
    if not password:
        raise ValueError("password must not be empty")
    if iterations < 50_000:
        raise ValueError("password hash iterations are below the minimum accepted strength")
    salt_bytes = salt or secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, iterations)
    return "$".join(
        (
            PASSWORD_ALGORITHM,
            str(iterations),
            base64.b64encode(salt_bytes).decode("ascii"),
            base64.b64encode(derived).decode("ascii"),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time verification of an encoded PBKDF2 hash."""
    if not password or not encoded:
        return False
    try:
        algorithm, iterations, salt_b64, digest_b64 = encoded.split("$")
    except ValueError:
        return False
    if algorithm != PASSWORD_ALGORITHM:
        return False
    try:
        iterations_value = int(iterations)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except (ValueError, TypeError):
        return False
    if iterations_value < 50_000 or not salt or not expected:
        return False
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations_value)
    return hmac.compare_digest(derived, expected)


def new_session_token() -> str:
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def redact_text(text: str) -> str:
    return _SENSITIVE_TEXT.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)


def _json_number(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("non-finite Decimal cannot be serialized")
    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".") or "0"
    return normalized


def canonical_json_bytes(value: Any) -> bytes:
    """Compact JSON with insertion-order keys and Decimal values emitted as JSON numbers.

    Shark's examples sign the exact compact JSON bytes (`JSON.stringify` / compact
    `json.dumps`). This serializer preserves dict insertion order and the exact body
    bytes sent by the HTTP client; it never sorts keys or round-trips Decimal through
    binary floating point.
    """
    if value is None:
        return b"null"
    if value is True:
        return b"true"
    if value is False:
        return b"false"
    if isinstance(value, Decimal):
        return _json_number(value).encode("ascii")
    if isinstance(value, int):
        return str(value).encode("ascii")
    if isinstance(value, float):
        raise TypeError("float values are prohibited in signed exchange payloads")
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if isinstance(value, list) or isinstance(value, tuple):
        return b"[" + b",".join(canonical_json_bytes(item) for item in value) + b"]"
    if isinstance(value, dict):
        chunks: list[bytes] = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            chunks.append(canonical_json_bytes(key) + b":" + canonical_json_bytes(item))
        return b"{" + b",".join(chunks) + b"}"
    raise TypeError(f"unsupported signed JSON type: {type(value).__name__}")


def shark_signature(api_secret: str, message: bytes | str) -> str:
    payload = message.encode("utf-8") if isinstance(message, str) else message
    return hmac.new(api_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def shark_query_string(pairs: list[tuple[str, Any]]) -> str:
    """URL-encode query pairs in caller-supplied order, matching URLSearchParams."""
    normalized = [(key, str(value)) for key, value in pairs]
    return urlencode(normalized, doseq=True)


class SlidingWindowRateLimiter:
    """Small per-process ingress limiter; deploy a proxy-level limit for multi-worker setups."""

    def __init__(self, limit: int, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        cutoff = moment - self.window_seconds
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(moment)
            if len(self._hits) > 4096:
                stale = [name for name, values in self._hits.items() if not values or values[-1] <= cutoff]
                for name in stale:
                    self._hits.pop(name, None)
            return True
