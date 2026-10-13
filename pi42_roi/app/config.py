"""Environment-driven configuration with startup validation and REAL-mode gates.

Rules enforced here:
* TRADING_MODE absent or invalid -> PAPER (invalid values are reported loudly).
* REAL requires TRADING_MODE=REAL, ENABLE_REAL_TRADING=true, the exact
  LIVE_TRADING_ACKNOWLEDGEMENT phrase, a Pi42 API key/secret and Telegram credentials.
* Invalid numeric limits and unsupported markets/margin assets abort startup.

Pi42 specifics:
* ``PI42_MARKET`` selects which contract family the signals are traded on and,
  together with the signal quote, must match exactly (``USDT`` -> ``BTCUSDT``,
  ``INR`` -> ``BTCINR``). A signal quoted in a different currency is rejected
  rather than converted: prices are never silently adjusted.
* ``PI42_MARGIN_ASSET`` defaults to the market currency. Futures trading on Pi42
  is margined from the *futures wallet*; funds must be transferred there in the
  Pi42 app (this bot never moves funds).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping

REAL_ACK_PHRASE = "I ACCEPT NO-STOP-LOSS LIVE TRADING RISK"
REAL_CONFIRM_PHRASE = "START LIVE TRADING"
SUPPORTED_MARKETS = ("USDT", "INR")
SUPPORTED_MARGIN_MODES = ("ISOLATED", "CROSS")
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


def _first_of(env: Mapping[str, str], *names: str) -> str:
    """First non-empty value among ``names`` (supports legacy aliases)."""
    for name in names:
        value = _get(env, name)
        if value:
            return value
    return ""


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
    pi42_api_key: str = field(repr=False)
    pi42_api_secret: str = field(repr=False)
    pi42_base_url: str
    pi42_public_base_url: str
    market: str
    margin_asset: str
    margin_mode: str
    send_ref_id: bool
    device_type: str
    user_category: str
    max_margin_per_trade: Decimal
    margin_reserve: Decimal
    paper_initial_balance: Decimal
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
    contract_cache_ttl_seconds: int
    http_connect_timeout: float
    http_read_timeout: float
    requests_per_minute: int
    delete_requests_per_minute: int
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

        market = (_get(env, "PI42_MARKET") or "USDT").upper()
        if market not in SUPPORTED_MARKETS:
            raise ConfigError(
                f"PI42_MARKET={market!r} is not supported; supported: {', '.join(SUPPORTED_MARKETS)}"
            )

        margin_asset = (_get(env, "PI42_MARGIN_ASSET") or market).upper()
        if margin_asset not in SUPPORTED_MARKETS:
            raise ConfigError(
                f"PI42_MARGIN_ASSET={margin_asset!r} is not supported; "
                f"supported: {', '.join(SUPPORTED_MARKETS)}"
            )

        margin_mode = (_get(env, "PI42_MARGIN_MODE") or "ISOLATED").upper()
        if margin_mode not in SUPPORTED_MARGIN_MODES:
            raise ConfigError(
                f"PI42_MARGIN_MODE={margin_mode!r} is not supported; "
                f"supported: {', '.join(SUPPORTED_MARGIN_MODES)}"
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

        base_url = (_get(env, "PI42_BASE_URL") or "https://fapi.pi42.com").rstrip("/")
        if not base_url.startswith("https://"):
            raise ConfigError("PI42_BASE_URL must use https://")
        public_base_url = (_get(env, "PI42_PUBLIC_BASE_URL") or "https://api.pi42.com").rstrip("/")
        if not public_base_url.startswith("https://"):
            raise ConfigError("PI42_PUBLIC_BASE_URL must use https://")

        max_margin = _dec(
            env, "MAX_MARGIN_PER_TRADE", _first_of(env, "MAX_MARGIN_USDT_PER_TRADE") or "50",
            minimum=Decimal(0), strict_min=True,
        )
        reserve = _dec(
            env, "MARGIN_RESERVE", _first_of(env, "MARGIN_RESERVE_USDT") or "2", minimum=Decimal(0)
        )
        if reserve >= max_margin:
            raise ConfigError("MARGIN_RESERVE must be smaller than MAX_MARGIN_PER_TRADE")

        return cls(
            trading_mode=mode,
            trading_enabled=_bool(env, "TRADING_ENABLED", True),
            enable_real_trading=_bool(env, "ENABLE_REAL_TRADING", False),
            live_ack=_get(env, "LIVE_TRADING_ACKNOWLEDGEMENT"),
            pi42_api_key=_get(env, "PI42_API_KEY"),
            pi42_api_secret=_get(env, "PI42_API_SECRET"),
            pi42_base_url=base_url,
            pi42_public_base_url=public_base_url,
            market=market,
            margin_asset=margin_asset,
            margin_mode=margin_mode,
            send_ref_id=_bool(env, "PI42_SEND_REF_ID", True),
            device_type=_get(env, "PI42_DEVICE_TYPE").upper(),
            user_category=_get(env, "PI42_USER_CATEGORY").upper(),
            max_margin_per_trade=max_margin,
            margin_reserve=reserve,
            paper_initial_balance=_dec(
                env,
                "PAPER_INITIAL_BALANCE",
                _first_of(env, "PAPER_INITIAL_BALANCE_USDT") or "1000",
                minimum=Decimal(0),
                strict_min=True,
            ),
            paper_fee_rate=_dec(env, "PAPER_FEE_RATE", "0.0005", minimum=Decimal(0), maximum=Decimal("0.01")),
            paper_maintenance_margin_rate=_dec(
                env, "PAPER_MAINTENANCE_MARGIN_RATE", "0.005", minimum=Decimal(0), maximum=Decimal("0.5")
            ),
            max_signal_age_minutes=_int(env, "MAX_SIGNAL_AGE_MINUTES", "5", minimum=1),
            max_entry_deviation_pct=_dec(env, "MAX_ENTRY_DEVIATION_PCT", "3", minimum=Decimal(0)),
            limit_order_ttl_minutes=_int(env, "LIMIT_ORDER_TTL_MINUTES", "120", minimum=0),
            max_concurrent_positions=_int(env, "MAX_CONCURRENT_POSITIONS", "50", minimum=1),
            telegram_api_id=api_id,
            telegram_api_hash=_get(env, "TELEGRAM_API_HASH"),
            telegram_phone=_get(env, "TELEGRAM_PHONE"),
            telegram_session_name=_get(env, "TELEGRAM_SESSION_NAME") or "./data/telegram_session",
            telegram_channel_invite_url=invite,
            database_path=_get(env, "DATABASE_PATH") or "./data/trading.db",
            monitor_poll_seconds=float(_dec(env, "MONITOR_POLL_SECONDS", "15", minimum=Decimal(5))),
            contract_cache_ttl_seconds=_int(env, "CONTRACT_CACHE_TTL_SECONDS", "600", minimum=0),
            http_connect_timeout=float(_dec(env, "HTTP_CONNECT_TIMEOUT", "5", minimum=Decimal(1))),
            http_read_timeout=float(_dec(env, "HTTP_READ_TIMEOUT", "10", minimum=Decimal(1))),
            requests_per_minute=_int(env, "PI42_REQUESTS_PER_MINUTE", "50", minimum=5),
            delete_requests_per_minute=_int(env, "PI42_DELETE_REQUESTS_PER_MINUTE", "25", minimum=1),
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
        """Initial-margin budget per trade: MAX_MARGIN_PER_TRADE minus the internal reserve."""
        return self.max_margin_per_trade - self.margin_reserve

    @property
    def database_file(self) -> Path:
        return Path(self.database_path)

    @property
    def kill_switch_file(self) -> Path:
        return self.database_file.parent / KILL_SWITCH_FILENAME

    @property
    def has_credentials(self) -> bool:
        return bool(self.pi42_api_key and self.pi42_api_secret)

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
        if not self.pi42_api_key:
            problems.append("PI42_API_KEY is missing")
        if not self.pi42_api_secret:
            problems.append("PI42_API_SECRET is missing")
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
            "PI42_API_KEY": "set" if self.pi42_api_key else "not set",
            "PI42_API_SECRET": "set" if self.pi42_api_secret else "not set",
            "PI42_BASE_URL": self.pi42_base_url,
            "PI42_PUBLIC_BASE_URL": self.pi42_public_base_url,
            "PI42_MARKET": self.market,
            "PI42_MARGIN_ASSET": self.margin_asset,
            "PI42_MARGIN_MODE": self.margin_mode,
            "PI42_SEND_REF_ID": str(self.send_ref_id).lower(),
            "MAX_MARGIN_PER_TRADE": str(self.max_margin_per_trade),
            "MARGIN_RESERVE": str(self.margin_reserve),
            "EFFECTIVE_MARGIN_BUDGET": str(self.effective_margin_budget),
            "PAPER_INITIAL_BALANCE": str(self.paper_initial_balance),
            "MAX_SIGNAL_AGE_MINUTES": str(self.max_signal_age_minutes),
            "MAX_ENTRY_DEVIATION_PCT": str(self.max_entry_deviation_pct),
            "LIMIT_ORDER_TTL_MINUTES": str(self.limit_order_ttl_minutes),
            "MAX_CONCURRENT_POSITIONS": str(self.max_concurrent_positions),
            "TARGET_ROI_PCT": str(self.target_roi_pct),
            "MONITOR_POLL_SECONDS": str(self.monitor_poll_seconds),
            "TELEGRAM_API_ID": str(self.telegram_api_id or "MISSING"),
            "TELEGRAM_API_HASH": "set" if self.telegram_api_hash else "MISSING",
            "TELEGRAM_PHONE": "set" if self.telegram_phone else "MISSING",
            "TELEGRAM_SESSION_NAME": self.telegram_session_name,
            "TELEGRAM_CHANNEL_INVITE_URL": self.telegram_channel_invite_url,
            "DATABASE_PATH": self.database_path,
            "TELEGRAM_NOTIFY_BOT": "enabled" if self.notify_bot_token and self.notify_chat_id else "disabled",
        }
