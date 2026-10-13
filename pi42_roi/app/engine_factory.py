"""Wire settings -> storage, Pi42 client, contract mapper, engine, monitor and listener."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import ConfigError, Settings
from app.execution_engine import ExecutionEngine
from app.models import Mode
from app.notifications import Notifier, NullNotifier
from app.paper_broker import PaperBroker
from app.position_monitor import PositionMonitor
from app.storage import Storage
from app.symbol_mapper import SymbolMapper


@dataclass
class Runtime:
    mode: Mode
    storage: Storage
    client: Any
    mapper: SymbolMapper
    paper: PaperBroker | None
    engine: ExecutionEngine
    monitor: PositionMonitor
    listener: Any
    notifier: Any


def build_engine(settings: Settings, mode: Mode, *, client=None, notifier=None) -> Runtime:
    from app.pi42_client import Pi42Client
    from app.telegram_listener import TelegramSignalListener

    if not settings.pi42_api_key or not settings.pi42_api_secret:
        raise ConfigError(
            "PI42_API_KEY and PI42_API_SECRET are required "
            "(PAPER mode works with a read-only key; public price endpoints need none)"
        )
    if settings.telegram_problems():
        raise ConfigError("Telegram configuration incomplete: " + "; ".join(settings.telegram_problems()))

    storage = Storage(settings.database_file)
    pi42 = client or Pi42Client(
        base_url=settings.pi42_base_url,
        public_base_url=settings.pi42_public_base_url,
        api_key=settings.pi42_api_key,
        api_secret=settings.pi42_api_secret,
        connect_timeout=settings.http_connect_timeout,
        read_timeout=settings.http_read_timeout,
        allow_mutations=mode is Mode.REAL,  # PAPER never gets a mutating client
        requests_per_minute=settings.requests_per_minute,
        delete_requests_per_minute=settings.delete_requests_per_minute,
    )
    if mode is Mode.PAPER and getattr(pi42, "allow_mutations", False):
        raise RuntimeError("safety violation: PAPER mode received a mutating Pi42 client")

    mapper = SymbolMapper(pi42, settings.contract_cache_ttl_seconds, market=settings.market)
    paper = None
    if mode is Mode.PAPER:
        paper = PaperBroker(
            storage,
            initial_balance=settings.paper_initial_balance,
            fee_rate=settings.paper_fee_rate,
            maintenance_margin_rate=settings.paper_maintenance_margin_rate,
            ttl_minutes=settings.limit_order_ttl_minutes,
        )
    if notifier is None:
        notifier = Notifier(settings.notify_bot_token, settings.notify_chat_id) if (
            settings.notify_bot_token and settings.notify_chat_id) else NullNotifier()

    engine = ExecutionEngine(
        settings=settings,
        storage=storage,
        client=pi42,
        mapper=mapper,
        mode=mode,
        paper_broker=paper,
        notifier=notifier,
    )
    monitor = PositionMonitor(
        storage=storage,
        client=pi42,
        mode=mode,
        market=settings.market,
        margin_asset=settings.margin_asset,
        paper_broker=paper,
        ttl_minutes=settings.limit_order_ttl_minutes,
        target_order_type=settings.target_order_type,
        target_roi_pct=settings.target_roi_pct,
        notifier=notifier,
    )
    listener = TelegramSignalListener(settings=settings, storage=storage, engine=engine)
    return Runtime(mode, storage, pi42, mapper, paper, engine, monitor, listener, notifier)
