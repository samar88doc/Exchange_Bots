"""Environment-driven configuration with startup validation and REAL-mode gates.

Rules enforced here:
* TRADING_MODE absent or invalid -> PAPER (invalid values are reported loudly).
* REAL requires TRADING_MODE=REAL, ENABLE_REAL_TRADING=true, the exact
  LIVE_TRADING_ACKNOWLEDGEMENT phrase, a Mudrex key and Telegram credentials.
* Invalid numeric limits and unsupported currencies abort startup.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping

REAL_ACK_PHRASE = "I ACCEPT NO-STOP-LOSS LIVE TRADING RISK"
REAL_CONFIRM_PHRASE = "START LIVE TRADING"
SUPPORTED_TRADE_CURRENCIES = ("USDT",)
DEFAULT_INVITE_URL = "https://t.me/+Xh7OTE_G_jcxMDE0"
KILL_SWITCH_FILENAME = "KILL_SWITCH"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(ValueError):
    """Invalid or unsafe configuration."""


def load_env_file(path: str | Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file (no shell expansion)."""
    values: dict[str, str] = {}
    file_path = Path(path)
    if not file_path.is_file():
        return values
    for raw_line in file_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _get(env: Mapping[str, str], name: str) -> str:
    raw = env.get(name)
    return "" if raw is None else raw.strip()


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _get(env, name).lower()
    if raw == "":
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false (got {raw!r})")


def _dec(
    env: Mapping[str, str],
    name: str,
    default: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
    strict_min: bool = False,
) -> Decimal:
    raw = _get(env, name) or default
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ConfigError(f"{name} must be a number (got {raw!r})") from exc
    if not value.is_finite():
        raise ConfigError(f"{name} must be finite")
    if minimum is not None:
        too_small = value <= minimum if strict_min else value < minimum
        if too_small:
            op = ">" if strict_min else ">="
            raise ConfigError(f"{name} must be {op} {minimum} (got {value})")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be <= {maximum} (got {value})")
    return value


def _int(env: Mapping[str, str], name: str, default: str, *, minimum: int | None = None) -> int:
    value = _dec(env, name, default)
    if value != value.to_integral_value():
        raise ConfigError(f"{name} must be an integer (got {value})")
    result = int(value)
    if minimum is not None and result < minimum:
        raise ConfigError(f"{name} must be >= {minimum} (got {result})")
    return result


@dataclass(frozen=True)
class Settings:
    trading_mode: str
    trading_enabled: bool
    enable_real_trading: bool
    live_ack: str = field(repr=False)
    mudrex_api_secret: str = field(repr=False)
    mudrex_base_url: str
    trade_currency: str
    max_margin_usdt_per_trade: Decimal
    margin_reserve_usdt: Decimal
    paper_initial_balance_usdt: Decimal
    paper_fee_rate: Decimal
    paper_maintenance_margin_rate: Decimal
    max_signal_age_minutes: int
    max_entry_deviation_pct: Decimal
    limit_order_ttl_minutes: int
    max_concurrent_positions: int
    telegram_api_id: int | None
    telegram_api_hash: str = field(repr=False)
    telegram_phone: str = field(repr=False)
    telegram_session_name: str
    telegram_channel_invite_url: str
    database_path: str
    monitor_poll_seconds: float
    asset_cache_ttl_seconds: int
    http_connect_timeout: float
    http_read_timeout: float
    notify_bot_token: str = field(repr=False)
    notify_chat_id: str
    log_level: str
    target_order_type: str = "LIMIT"
    target_roi_pct: Decimal = Decimal("8.0")
    mode_warning: str | None = None

    # ----- construction -------------------------------------------------
    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        raw_mode = _get(env, "TRADING_MODE").upper()
        mode_warning = None
        if raw_mode in ("", "PAPER"):
            mode = "PAPER"
        elif raw_mode == "REAL":
            mode = "REAL"
        else:
            mode = "PAPER"
            mode_warning = (
                f"TRADING_MODE={raw_mode!r} is not a valid mode; falling back to PAPER. "
                "REAL is never activated by an invalid value."
            )

        currency = (_get(env, "TRADE_CURRENCY") or "USDT").upper()
        if currency not in SUPPORTED_TRADE_CURRENCIES:
            raise ConfigError(
                f"TRADE_CURRENCY={currency!r} is not supported; supported: "
                f"{', '.join(SUPPORTED_TRADE_CURRENCIES)}"
            )

        api_id_raw = _get(env, "TELEGRAM_API_ID")
        api_id: int | None = None
        if api_id_raw:
            if not api_id_raw.isdigit():
                raise ConfigError("TELEGRAM_API_ID must be numeric")
            api_id = int(api_id_raw)

        invite = _get(env, "TELEGRAM_CHANNEL_INVITE_URL") or DEFAULT_INVITE_URL
        if not re.match(r"^https://t\.me/", invite):
            raise ConfigError("TELEGRAM_CHANNEL_INVITE_URL must start with https://t.me/")

        base_url = (_get(env, "MUDREX_BASE_URL") or "https://trade.mudrex.com").rstrip("/")
        if not base_url.startswith("https://"):
            raise ConfigError("MUDREX_BASE_URL must use https://")

        max_margin = _dec(env, "MAX_MARGIN_USDT_PER_TRADE", "50", minimum=Decimal(0), strict_min=True)
        reserve = _dec(env, "MARGIN_RESERVE_USDT", "2", minimum=Decimal(0))
        if reserve >= max_margin:
            raise ConfigError("MARGIN_RESERVE_USDT must be smaller than MAX_MARGIN_USDT_PER_TRADE")

        return cls(
            trading_mode=mode,
            trading_enabled=_bool(env, "TRADING_ENABLED", True),
            enable_real_trading=_bool(env, "ENABLE_REAL_TRADING", False),
            live_ack=_get(env, "LIVE_TRADING_ACKNOWLEDGEMENT"),
            mudrex_api_secret=_get(env, "MUDREX_API_SECRET"),
            mudrex_base_url=base_url,
            trade_currency=currency,
            max_margin_usdt_per_trade=max_margin,
            margin_reserve_usdt=reserve,
            paper_initial_balance_usdt=_dec(
                env, "PAPER_INITIAL_BALANCE_USDT", "1000", minimum=Decimal(0), strict_min=True
            ),
            paper_fee_rate=_dec(env, "PAPER_FEE_RATE", "0.0002", minimum=Decimal(0), maximum=Decimal("0.01")),
            paper_maintenance_margin_rate=_dec(
                env, "PAPER_MAINTENANCE_MARGIN_RATE", "0.005", minimum=Decimal(0), maximum=Decimal("0.5")
            ),
            max_signal_age_minutes=_int(env, "MAX_SIGNAL_AGE_MINUTES", "5", minimum=1),
            max_entry_deviation_pct=_dec(env, "MAX_ENTRY_DEVIATION_PCT", "3", minimum=Decimal(0)),
            limit_order_ttl_minutes=_int(env, "LIMIT_ORDER_TTL_MINUTES", "120", minimum=0),
            max_concurrent_positions=_int(env, "MAX_CONCURRENT_POSITIONS", "3", minimum=1),
            telegram_api_id=api_id,
            telegram_api_hash=_get(env, "TELEGRAM_API_HASH"),
            telegram_phone=_get(env, "TELEGRAM_PHONE"),
            telegram_session_name=_get(env, "TELEGRAM_SESSION_NAME") or "./data/telegram_session",
            telegram_channel_invite_url=invite,
            database_path=_get(env, "DATABASE_PATH") or "./data/trading.db",
            monitor_poll_seconds=float(_dec(env, "MONITOR_POLL_SECONDS", "10", minimum=Decimal(2))),
            asset_cache_ttl_seconds=_int(env, "ASSET_CACHE_TTL_SECONDS", "600", minimum=0),
            http_connect_timeout=float(_dec(env, "HTTP_CONNECT_TIMEOUT", "5", minimum=Decimal(1))),
            http_read_timeout=float(_dec(env, "HTTP_READ_TIMEOUT", "10", minimum=Decimal(1))),
            notify_bot_token=_get(env, "TELEGRAM_NOTIFY_BOT_TOKEN"),
            notify_chat_id=_get(env, "TELEGRAM_NOTIFY_CHAT_ID"),
            log_level=(_get(env, "LOG_LEVEL") or "INFO").upper(),
            target_order_type=(_get(env, "TARGET_ORDER_TYPE") or "LIMIT").upper(),
            target_roi_pct=_dec(env, "TARGET_ROI_PCT", "8.0", minimum=Decimal("0.1")),
            mode_warning=mode_warning,
        )

    # ----- derived values ----------------------------------------------
    @property
    def effective_margin_budget(self) -> Decimal:
        """Initial-margin budget per trade: MAX_MARGIN minus the internal reserve."""
        return self.max_margin_usdt_per_trade - self.margin_reserve_usdt

    @property
    def database_file(self) -> Path:
        return Path(self.database_path)

    @property
    def kill_switch_file(self) -> Path:
        return self.database_file.parent / KILL_SWITCH_FILENAME

    def real_gate_problems(self) -> list[str]:
        """Every reason REAL trading is NOT allowed. Empty list means allowed."""
        problems: list[str] = []
        if self.trading_mode != "REAL":
            problems.append("TRADING_MODE is not REAL")
        if not self.enable_real_trading:
            problems.append("ENABLE_REAL_TRADING is not true")
        if self.live_ack != REAL_ACK_PHRASE:
            problems.append(
                "LIVE_TRADING_ACKNOWLEDGEMENT does not match the required phrase "
                f"exactly: {REAL_ACK_PHRASE!r}"
            )
        if not self.mudrex_api_secret:
            problems.append("MUDREX_API_SECRET is missing")
        problems.extend(self.telegram_problems())
        return problems

    def telegram_problems(self) -> list[str]:
        problems = []
        if self.telegram_api_id is None:
            problems.append("TELEGRAM_API_ID is missing")
        if not self.telegram_api_hash:
            problems.append("TELEGRAM_API_HASH is missing")
        if not self.telegram_phone:
            problems.append("TELEGRAM_PHONE is missing")
        return problems

    def redacted_summary(self) -> dict[str, str]:
        """Settings for display. Never includes secrets."""
        return {
            "TRADING_MODE": self.trading_mode,
            "TRADING_ENABLED": str(self.trading_enabled).lower(),
            "ENABLE_REAL_TRADING": str(self.enable_real_trading).lower(),
            "LIVE_TRADING_ACKNOWLEDGEMENT": "set" if self.live_ack == REAL_ACK_PHRASE else "not set",
            "MUDREX_API_SECRET": "set" if self.mudrex_api_secret else "MISSING",
            "MUDREX_BASE_URL": self.mudrex_base_url,
            "TRADE_CURRENCY": self.trade_currency,
            "MAX_MARGIN_USDT_PER_TRADE": str(self.max_margin_usdt_per_trade),
            "MARGIN_RESERVE_USDT": str(self.margin_reserve_usdt),
            "EFFECTIVE_MARGIN_BUDGET": str(self.effective_margin_budget),
            "PAPER_INITIAL_BALANCE_USDT": str(self.paper_initial_balance_usdt),
            "MAX_SIGNAL_AGE_MINUTES": str(self.max_signal_age_minutes),
            "MAX_ENTRY_DEVIATION_PCT": str(self.max_entry_deviation_pct),
            "LIMIT_ORDER_TTL_MINUTES": str(self.limit_order_ttl_minutes),
            "MAX_CONCURRENT_POSITIONS": str(self.max_concurrent_positions),
            "TELEGRAM_API_ID": str(self.telegram_api_id or "MISSING"),
            "TELEGRAM_API_HASH": "set" if self.telegram_api_hash else "MISSING",
            "TELEGRAM_PHONE": "set" if self.telegram_phone else "MISSING",
            "TELEGRAM_SESSION_NAME": self.telegram_session_name,
            "TELEGRAM_CHANNEL_INVITE_URL": self.telegram_channel_invite_url,
            "DATABASE_PATH": self.database_path,
            "TELEGRAM_NOTIFY_BOT": "enabled" if self.notify_bot_token and self.notify_chat_id else "disabled",
        }
