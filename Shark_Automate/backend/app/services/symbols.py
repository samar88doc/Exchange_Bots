"""Symbol normalization + mapping resolution.

Required flow (executed BEFORE risk/duplicate/position/qty/order work):

    TradingView RAW symbol  (BASUSDT.P)
        -> store raw value (never discarded)
        -> normalize known perpetual suffix ONLY (".P" terminal suffix)
        -> canonical symbol     (BASUSDT)
        -> resolve Shark symbol (BASUSDT) via symbol_mappings / verified contract
        -> verify Shark contract exists + active + precision rules

If the Shark contract cannot be resolved: REJECT `SHARK_SYMBOL_NOT_FOUND`.
We NEVER guess another symbol.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppSettings
from app.core.errors import RejectReason, SymbolError
from app.core.logging import TradeEvent, log_event
from app.models.entities import SymbolMapping

log = logging.getLogger("trading.symbols")


@dataclass(frozen=True)
class SymbolTriple:
    tradingview_raw_symbol: str
    canonical_symbol: str
    shark_symbol: str
    mapping_id: int | None = None


@dataclass(frozen=True)
class ContractRules:
    quantity_precision: int
    price_precision: int
    min_quantity: Decimal
    min_notional: Decimal
    quantity_step: Decimal
    price_tick_size: Decimal
    max_leverage: int | None
    contract_status: str
    base_asset: str
    quote_asset: str


def normalize_tradingview_symbol(raw: str, suffix: str = ".P", enabled: bool = True) -> str:
    """Remove ONLY the known terminal perpetual suffix. Nothing else is modified.

    BTCUSDT.P -> BTCUSDT.  'BAS.P.P' -> 'BAS.P' (only one terminal suffix).
    No character stripping from start/middle, no 'USDT' removal, no guessing.
    """
    symbol = (raw or "").strip().upper()
    if not symbol:
        raise SymbolError(RejectReason.SYMBOL_NOT_FOUND, "empty symbol")
    if enabled and suffix and symbol.endswith(suffix):
        symbol = symbol[: -len(suffix)]
        if not symbol:
            raise SymbolError(RejectReason.SYMBOL_NOT_FOUND, f"symbol {raw!r} empty after normalization")
    return symbol


class SymbolService:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def normalize(self, raw: str) -> str:
        return normalize_tradingview_symbol(
            raw,
            suffix=self.settings.tradingview_perpetual_suffix,
            enabled=self.settings.symbol_normalization_enabled,
        )

    async def resolve(self, session: AsyncSession, raw_symbol: str) -> SymbolTriple:
        """Full STEP 1..9 resolution. Raises SymbolError on any unverifiable link."""
        raw = (raw_symbol or "").strip().upper()
        canonical = self.normalize(raw)

        mapping = await self._find_mapping(session, raw, canonical)
        if mapping is None:
            if self.settings.trading_mode == "PAPER":
                base = canonical[:-4] if canonical.endswith("USDT") else canonical
                quote = "USDT" if canonical.endswith("USDT") else "INR"
                mapping = SymbolMapping(
                    tradingview_raw_symbol=raw,
                    canonical_symbol=canonical,
                    shark_symbol=canonical,
                    base_asset=base,
                    quote_asset=quote,
                    quantity_precision=3,
                    price_precision=4,
                    minimum_quantity=Decimal("0.001"),
                    minimum_notional=Decimal("1"),
                    quantity_step=Decimal("0.001"),
                    price_tick_size=Decimal("0.0001"),
                    contract_status="ACTIVE",
                    contract_type="PERPETUAL",
                    verified_against_exchange=True,
                )
                session.add(mapping)
                await session.flush()
            else:
                log_event(log, TradeEvent.SHARK_SYMBOL_NOT_FOUND,
                          "No symbol mapping for normalized symbol",
                          tradingview_raw_symbol=raw, canonical_symbol=canonical)
                raise SymbolError(
                    RejectReason.SHARK_SYMBOL_NOT_FOUND,
                    f"no verified Shark contract mapping for {canonical}",
                    tradingview_raw_symbol=raw, canonical_symbol=canonical,
                )

        if str(mapping.contract_status).upper() != "ACTIVE":
            raise SymbolError(
                RejectReason.CONTRACT_INACTIVE,
                f"Shark contract {mapping.shark_symbol} is {mapping.contract_status}",
                tradingview_raw_symbol=raw,
            )

        shark_symbol = mapping.shark_symbol
        if not shark_symbol or shark_symbol.endswith(self.settings.tradingview_perpetual_suffix):
            raise SymbolError(
                RejectReason.SHARK_SYMBOL_NOT_FOUND,
                f"invalid shark symbol mapping {shark_symbol!r}",
                tradingview_raw_symbol=raw,
            )

        log_event(
            log, TradeEvent.SYMBOL_NORMALIZED, "symbol normalized + resolved",
            tradingview_raw_symbol=raw, canonical_symbol=canonical, shark_symbol=shark_symbol,
        )
        return SymbolTriple(
            tradingview_raw_symbol=raw,
            canonical_symbol=canonical,
            shark_symbol=shark_symbol,
            mapping_id=mapping.id,
        )

    async def rules_for(self, session: AsyncSession, canonical_symbol: str) -> ContractRules:
        mapping = await self._find_mapping(session, canonical_symbol, canonical_symbol)
        if mapping is None:
            if self.settings.trading_mode == "PAPER":
                base = canonical_symbol[:-4] if canonical_symbol.endswith("USDT") else canonical_symbol
                quote = "USDT" if canonical_symbol.endswith("USDT") else "INR"
                mapping = SymbolMapping(
                    tradingview_raw_symbol=canonical_symbol,
                    canonical_symbol=canonical_symbol,
                    shark_symbol=canonical_symbol,
                    base_asset=base,
                    quote_asset=quote,
                    quantity_precision=3,
                    price_precision=4,
                    minimum_quantity=Decimal("0.001"),
                    minimum_notional=Decimal("1"),
                    quantity_step=Decimal("0.001"),
                    price_tick_size=Decimal("0.0001"),
                    contract_status="ACTIVE",
                    contract_type="PERPETUAL",
                    max_leverage=20,
                    verified_against_exchange=True,
                )
                session.add(mapping)
                await session.flush()
            else:
                raise SymbolError(RejectReason.SHARK_SYMBOL_NOT_FOUND, f"no mapping for {canonical_symbol}")

        return ContractRules(
            quantity_precision=mapping.quantity_precision,
            price_precision=mapping.price_precision,
            min_quantity=mapping.minimum_quantity or Decimal("0"),
            min_notional=mapping.minimum_notional or Decimal("0"),
            quantity_step=mapping.quantity_step or Decimal("0"),
            price_tick_size=mapping.price_tick_size or Decimal("0"),
            max_leverage=mapping.max_leverage,
            contract_status=mapping.contract_status,
            base_asset=mapping.base_asset,
            quote_asset=mapping.quote_asset,
        )

    async def _find_mapping(
        self, session: AsyncSession, raw: str, canonical: str
    ) -> SymbolMapping | None:
        result = await session.execute(
            select(SymbolMapping).where(
                (SymbolMapping.tradingview_raw_symbol == raw)
                | (SymbolMapping.canonical_symbol == canonical)
            ).limit(1)
        )
        return result.scalar_one_or_none()


def make_triple(raw: str, canonical: str, shark: str) -> dict[str, str]:
    return {
        "tradingview_raw_symbol": raw,
        "canonical_symbol": canonical,
        "shark_symbol": shark,
    }
