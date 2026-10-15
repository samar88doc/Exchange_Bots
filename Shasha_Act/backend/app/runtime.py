"""Application service container and process lifecycle."""
from __future__ import annotations

from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.market_data import MarketDataService
from app.services.execution import TradeExecutor
from app.services.outbox import OutboxWorker
from app.services.paper import PaperExecutionAdapter
from app.services.reconciliation import ReconciliationService
from app.shark import SharkClient
from app.telegram import TelegramNotifier


@dataclass
class AppContext:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    shark: SharkClient
    market: MarketDataService
    paper: PaperExecutionAdapter
    executor: TradeExecutor
    reconciler: ReconciliationService
    notifier: TelegramNotifier
    worker: OutboxWorker

    async def close(self) -> None:
        await self.notifier.aclose()
        await self.shark.aclose()


def build_context(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    shark_http_client: httpx.AsyncClient | None = None,
    telegram_http_client: httpx.AsyncClient | None = None,
) -> AppContext:
    shark = SharkClient(settings, shark_http_client)
    paper = PaperExecutionAdapter(settings)
    market = MarketDataService(settings, shark)
    notifier = TelegramNotifier(settings, telegram_http_client)
    executor = TradeExecutor(settings, session_factory, market, shark, paper)
    reconciler = ReconciliationService(settings, session_factory, market, shark, paper)
    reconciler.set_executor(executor)
    market.on_ticker = reconciler.on_ticker
    worker = OutboxWorker(session_factory, None, settings)
    context = AppContext(
        settings=settings,
        session_factory=session_factory,
        shark=shark,
        market=market,
        paper=paper,
        executor=executor,
        reconciler=reconciler,
        notifier=notifier,
        worker=worker,
    )
    worker.context = context
    return context
