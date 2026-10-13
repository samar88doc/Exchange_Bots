"""Centralized application configuration.

EVERY tunable value lives here (or in the `settings` DB table for runtime-editable
values). No magic numbers anywhere else in the codebase.
"""
from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    """Environment-driven configuration. Secrets are only ever read here."""

    model_config = SettingsConfigDict(
        env_file=(".env",),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )


    # ------------------------------------------------------------------ core
    app_name: str = "TV-Shark Trading Platform"
    environment: Literal["development", "production", "test"] = "development"
    timezone_display: str = "Asia/Kolkata"
    log_level: str = "INFO"
    log_dir: Path = Path("logs")
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # ------------------------------------------------------------------- db
    database_url: str = "postgresql+asyncpg://trading:trading@localhost:5432/trading"
    database_url_test: str = "sqlite+aiosqlite:///./test_trading.db"
    redis_url: str = "redis://localhost:6379/0"
    redis_enabled: bool = False  # optional accelerator; app must work without it

    # ------------------------------------------------------------ risk core
    trading_mode: Literal["PAPER", "LIVE"] = "PAPER"
    margin_per_trade: Decimal = Decimal("50")       # USDT (account unit)
    leverage: int = 10
    max_open_positions: int = 4
    target_roi_percent: Decimal = Decimal("10")
    stop_loss_enabled: bool = False                 # ALWAYS off in initial strategy
    short_enabled: bool = False                     # extensible, disabled by default
    close_on_roi_percent: Decimal = Decimal("10")
    roi_includes_fees: bool = True
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    daily_trade_limit: int = 0                      # 0 = unlimited
    daily_loss_limit: Decimal = Decimal("0")        # 0 = unlimited, in account unit
    account_unit: str = "USDT"

    # ------------------------------------------------------------- live gate
    live_trading_enabled: bool = False
    live_trading_confirmation: str = ""             # secret phrase configured out-of-band
    live_confirmation_phrase: str = "ENABLE LIVE TRADING"

    # --------------------------------------------------------------- webhooks
    webhook_path: str = "/api/webhooks/tradingview"
    webhook_secret: str = "change-me-webhook-secret"
    webhook_rate_limit_per_minute: int = 120
    webhook_max_body_bytes: int = 64 * 1024
    webhook_replay_window_seconds: int = 900        # reject stale event timestamps
    tradingview_perpetual_suffix: str = ".P"
    symbol_normalization_enabled: bool = True

    # ---------------------------------------------------------- paper engine
    paper_initial_balance: Decimal = Decimal("10000")
    paper_taker_fee_bps: Decimal = Decimal("5")     # 5 bps = 0.05 %
    paper_maker_fee_bps: Decimal = Decimal("2")
    paper_slippage_bps: Decimal = Decimal("2")      # 0.02 % adverse slippage
    paper_funding_rate_bps_8h: Decimal = Decimal("0")
    paper_latency_ms: int = 0

    # ------------------------------------------------------------ shark api
    shark_base_url: str = "https://api.sharkexchange.in"
    shark_api_key: str = ""
    shark_api_secret: str = ""
    shark_margin_asset: str = "INR"
    shark_device_type: str = "WEB"
    shark_user_category: str = "EXTERNAL"
    shark_request_timeout_seconds: float = 10.0
    shark_dry_run: bool = True                      # build + log payloads, never submit
    shark_verify_ssl: bool = True

    # ------------------------------------------------------------- workers
    roi_monitor_interval_seconds: float = 1.0
    reconciliation_interval_seconds: float = 60.0
    market_poll_interval_seconds: float = 2.0
    stale_reservation_seconds: int = 120
    order_submit_timeout_seconds: float = 15.0
    order_status_poll_attempts: int = 6

    # -------------------------------------------------------- notifications
    notifications_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ---------------------------------------------------------------- tests
    testing: bool = False
    inline_processing: bool = False  # deterministic webhook processing (tests)

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def position_notional(self) -> Decimal:
        return self.margin_per_trade * Decimal(self.leverage)

    def is_live_capable(self) -> bool:
        """LIVE is possible only when every safety condition is satisfied."""
        return (
            self.live_trading_enabled
            and bool(self.live_trading_confirmation)
            and bool(self.shark_api_key)
            and bool(self.shark_api_secret)
        )


@lru_cache
def get_settings() -> AppSettings:
    return AppSettings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()
