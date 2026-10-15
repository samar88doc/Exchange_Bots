"""Environment settings for the isolated Shasha_Act service."""
from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Shasha_Act"
    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+asyncpg://shasha_act:change-me@localhost:5432/shasha_act"
    timezone: str = "Asia/Kolkata"
    cors_origins: str = "http://localhost:3000"

    trading_mode: Literal["PAPER", "LIVE"] = "PAPER"
    live_trading_enabled: bool = False
    shark_contract_rules_verified: bool = False
    shark_split_protection_verified: bool = False
    dashboard_api_token: str = ""
    tradingview_webhook_token: str = ""

    margin_per_trade: Decimal = Decimal("100")
    leverage: int = 10
    max_open_positions: int = 3
    target1_percent: Decimal = Decimal("1")
    target2_percent: Decimal = Decimal("2")
    target3_percent: Decimal = Decimal("3")
    stop_loss_percent: Decimal = Decimal("2")
    max_notional_per_trade: Decimal = Decimal("1000")
    max_daily_loss: Decimal = Decimal("200")
    limit_entry_expiry_seconds: int = 900
    cancel_stale_entry_orders: bool = True
    emergency_trading_disabled: bool = False
    allow_symbols: str = ""
    deny_symbols: str = ""

    paper_initial_balance: Decimal = Decimal("10000")
    paper_maker_fee_bps: Decimal = Decimal("2")
    paper_taker_fee_bps: Decimal = Decimal("5")

    shark_base_url: str = "https://api.sharkexchange.in"
    shark_public_ws_url: str = "https://fawss.sharkexchange.in"
    shark_api_key: str = ""
    shark_api_secret: str = ""
    shark_margin_asset: str = "USDT"
    shark_device_type: str = "WEB"
    shark_user_category: str = "EXTERNAL"
    shark_request_timeout_seconds: float = 10.0
    market_data_max_subscriptions: int = 20
    market_data_stale_seconds: int = 30
    market_data_poll_seconds: float = 60.0

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_request_timeout_seconds: float = 8.0

    webhook_max_body_bytes: int = 16_384
    webhook_rate_limit_per_minute: int = 30
    worker_poll_seconds: float = 0.5
    outbox_lease_seconds: int = 60
    outbox_max_attempts: int = 12
    reconciliation_interval_seconds: int = 20
    run_workers: bool = True
    run_market_stream: bool = True
    run_market_poll: bool = True

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def allow_symbols_list(self) -> list[str]:
        return [item.strip().upper() for item in self.allow_symbols.split(",") if item.strip()]

    @property
    def deny_symbols_list(self) -> list[str]:
        return [item.strip().upper() for item in self.deny_symbols.split(",") if item.strip()]

    @property
    def shark_credentials_configured(self) -> bool:
        return bool(self.shark_api_key and self.shark_api_secret)

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def live_server_gate_open(self) -> bool:
        return self.live_trading_enabled and self.shark_credentials_configured

    @property
    def live_execution_rules_verified(self) -> bool:
        return self.shark_contract_rules_verified and self.shark_split_protection_verified


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()
