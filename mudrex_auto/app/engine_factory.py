"""Wire settings -> storage, Mudrex client, symbol mapper, engine, monitor and listener."""

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
    from app.mudrex_client import MudrexClient
    from app.telegram_listener import TelegramSignalListener

    if not settings.mudrex_api_secret:
        raise ConfigError("MUDREX_API_SECRET is required (a read-only key is enough in PAPER mode)")
    if settings.telegram_problems():
        raise ConfigError("Telegram configuration incomplete: " + "; ".join(settings.telegram_problems()))

    storage = Storage(settings.database_file)
    mudrex = client or MudrexClient(
        base_url=settings.mudrex_base_url,
        api_secret=settings.mudrex_api_secret,
        connect_timeout=settings.http_connect_timeout,
        read_timeout=settings.http_read_timeout,
        allow_mutations=mode is Mode.REAL,  # PAPER never gets a mutating client
    )
    if mode is Mode.PAPER and getattr(mudrex, "allow_mutations", False):
        raise RuntimeError("safety violation: PAPER mode received a mutating Mudrex client")

    mapper = SymbolMapper(mudrex, settings.asset_cache_ttl_seconds)
    paper = None
    if mode is Mode.PAPER:
        paper = PaperBroker(
            storage,
            initial_balance=settings.paper_initial_balance_usdt,
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
        client=mudrex,
        mapper=mapper,
        mode=mode,
        paper_broker=paper,
        notifier=notifier,
    )
    monitor = PositionMonitor(
        storage=storage,
        client=mudrex,
        mode=mode,
        currency=settings.trade_currency,
        paper_broker=paper,
        ttl_minutes=settings.limit_order_ttl_minutes,
        target_order_type=settings.target_order_type,
        notifier=notifier,
    )
    listener = TelegramSignalListener(settings=settings, storage=storage, engine=engine)
    return Runtime(mode, storage, mudrex, mapper, paper, engine, monitor, listener, notifier)
