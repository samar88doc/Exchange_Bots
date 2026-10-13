"""Application runtime container (dependency injection root)."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.config import AppSettings
from app.exchanges.market_data import ContractRegistry, MarketDataService
from app.exchanges.paper import PaperExchangeAdapter
from app.exchanges.shark import SharkExchangeAdapter
from app.services.execution import ExecutionEngine
from app.services.idempotency import IdempotencyService
from app.services.notifications import NotificationService
from app.services.pipeline import SignalPipeline
from app.services.position_manager import PositionManager
from app.services.reconciliation import ReconciliationService
from app.services.replay import ReplayService
from app.services.reservation import ReservationManager
from app.services.risk import RiskManager
from app.services.roi_monitor import RoiMonitor
from app.services.symbols import SymbolService


@dataclass
class AppContext:
    settings: AppSettings
    symbols: SymbolService
    risk: RiskManager
    reservations: ReservationManager
    execution: ExecutionEngine
    positions: PositionManager
    pipeline: SignalPipeline
    roi_monitor: RoiMonitor
    reconciliation: ReconciliationService
    idempotency: IdempotencyService
    notifications: NotificationService
    replay: ReplayService
    market_data: MarketDataService
    contracts: ContractRegistry
    paper: PaperExchangeAdapter
    shark: SharkExchangeAdapter
    health: dict = field(default_factory=dict)


def build_context(settings: AppSettings) -> AppContext:
    market_data = MarketDataService(settings)
    contracts = ContractRegistry(settings)
    paper = PaperExchangeAdapter(settings, market_data, contracts)
    shark = SharkExchangeAdapter(settings)
    adapters = {"paper": paper, "shark": shark}
    execution = ExecutionEngine(settings, adapters)
    symbols = SymbolService(settings)
    risk = RiskManager(settings)
    reservations = ReservationManager(settings, risk)
    positions = PositionManager()
    pipeline = SignalPipeline(settings, symbols, risk, reservations, execution, positions)
    roi_monitor = RoiMonitor(settings, execution, positions, reservations)
    reconciliation = ReconciliationService(settings, execution, positions)
    idempotency = IdempotencyService(settings)
    notifications = NotificationService(settings)
    replay = ReplayService(pipeline)
    return AppContext(
        settings=settings,
        symbols=symbols,
        risk=risk,
        reservations=reservations,
        execution=execution,
        positions=positions,
        pipeline=pipeline,
        roi_monitor=roi_monitor,
        reconciliation=reconciliation,
        idempotency=idempotency,
        notifications=notifications,
        replay=replay,
        market_data=market_data,
        contracts=contracts,
        paper=paper,
        shark=shark,
    )
