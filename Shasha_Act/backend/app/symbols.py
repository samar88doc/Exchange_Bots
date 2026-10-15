"""Shark contract metadata parsing and fail-closed symbol resolution."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.domain import ValidationProblem, decimal_step_for_precision


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    base_asset: str
    quote_asset: str
    contract_type: str
    margin_assets: tuple[str, ...]
    order_types: tuple[str, ...]
    minimum_quantity: Decimal
    minimum_notional: Decimal
    quantity_precision: int
    price_precision: int
    maximum_leverage: int | None
    price_tick: Decimal | None = None
    quantity_step: Decimal | None = None
    tradable: bool | None = None
    live_rules_verified: bool = False

    @property
    def paper_price_tick(self) -> Decimal:
        return self.price_tick or decimal_step_for_precision(self.price_precision)

    @property
    def paper_quantity_step(self) -> Decimal:
        return self.quantity_step or decimal_step_for_precision(self.quantity_precision)

    @property
    def live_safe(self) -> bool:
        return bool(
            self.live_rules_verified
            and self.price_tick is not None
            and self.quantity_step is not None
            and self.tradable is True
        )


class ContractUnavailable(ValidationProblem):
    pass


def _decimal(raw: Any, default: str = "0") -> Decimal:
    if raw is None:
        return Decimal(default)
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)
    return value if value.is_finite() and value >= 0 else Decimal(default)


def _precision(raw: Any, key: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ContractUnavailable(f"CONTRACT_METADATA_MISSING_{key.upper()}") from exc
    if not 0 <= value <= 18:
        raise ContractUnavailable(f"CONTRACT_METADATA_INVALID_{key.upper()}")
    return value


def parse_exchange_info(response: Any) -> dict[str, ContractSpec]:
    """Parse only fields described by Shark's public exchangeInfo reference.

    The current API reference documents decimal precision and minimum constraints,
    but does not specify exact price tick, quantity step, or an enabled/trading-status
    field. Those rules therefore remain unset and cannot satisfy `live_safe`.
    """
    root = response.get("data", response) if isinstance(response, dict) else response
    if isinstance(root, dict):
        contracts = root.get("contracts")
    else:
        contracts = None
    if not isinstance(contracts, list):
        return {}

    parsed: dict[str, ContractSpec] = {}
    for item in contracts:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip().upper()
        if not name:
            continue
        filters = item.get("filters") or []
        minimum_quantity = Decimal("0")
        minimum_notional = Decimal("0")
        if isinstance(filters, list):
            for rule in filters:
                if not isinstance(rule, dict):
                    continue
                filter_type = str(rule.get("filterType") or "").upper()
                if filter_type in {"LIMIT_QTY_SIZE", "MARKET_QTY_SIZE"}:
                    candidate = _decimal(rule.get("minQty"))
                    if candidate > 0:
                        minimum_quantity = max(minimum_quantity, candidate)
                elif filter_type == "MIN_NOTIONAL":
                    minimum_notional = max(minimum_notional, _decimal(rule.get("notional")))
        margin_assets = item.get("marginAssetsSupported") or []
        order_types = item.get("orderTypes") or []
        max_leverage = item.get("maxLeverage")
        try:
            max_leverage_value = int(max_leverage) if max_leverage is not None else None
        except (TypeError, ValueError):
            max_leverage_value = None
        try:
            spec = ContractSpec(
                symbol=name,
                base_asset=str(item.get("baseAsset") or "").upper(),
                quote_asset=str(item.get("quoteAsset") or "").upper(),
                contract_type=str(item.get("contractType") or "").upper(),
                margin_assets=tuple(str(v).upper() for v in margin_assets if isinstance(v, str)),
                order_types=tuple(str(v).upper() for v in order_types if isinstance(v, str)),
                minimum_quantity=minimum_quantity,
                minimum_notional=minimum_notional,
                quantity_precision=_precision(item.get("quantityPrecision"), "quantity_precision"),
                price_precision=_precision(item.get("pricePrecision"), "price_precision"),
                maximum_leverage=max_leverage_value,
                # Deliberately not inferred from precision: the current official reference
                # does not define tick-size/step-size semantics or tradable status.
                price_tick=None,
                quantity_step=None,
                tradable=None,
                live_rules_verified=False,
            )
        except ContractUnavailable:
            continue
        parsed[name] = spec
    return parsed


class SymbolResolver:
    def __init__(self, contracts: dict[str, ContractSpec] | None = None) -> None:
        self._contracts = {key.upper(): value for key, value in (contracts or {}).items()}

    def replace(self, contracts: dict[str, ContractSpec]) -> None:
        self._contracts = {key.upper(): value for key, value in contracts.items()}

    def all(self) -> dict[str, ContractSpec]:
        return dict(self._contracts)

    def get(self, symbol: str) -> ContractSpec:
        spec = self._contracts.get(symbol.upper())
        if spec is None:
            raise ContractUnavailable("UNKNOWN_SHARK_CONTRACT")
        return spec

    def resolve(self, symbol: str, margin_asset: str = "USDT") -> ContractSpec:
        spec = self.get(symbol)
        if spec.contract_type not in {"PERPETUAL", "TRADIFI_PERPETUAL"}:
            raise ContractUnavailable("CONTRACT_IS_NOT_PERPETUAL")
        if spec.quote_asset != "USDT":
            raise ContractUnavailable("CONTRACT_QUOTE_ASSET_IS_NOT_USDT")
        if margin_asset.upper() not in spec.margin_assets:
            raise ContractUnavailable("CONTRACT_DOES_NOT_SUPPORT_CONFIGURED_MARGIN_ASSET")
        if "LIMIT" not in spec.order_types:
            raise ContractUnavailable("CONTRACT_DOES_NOT_SUPPORT_LIMIT_ORDERS")
        if spec.minimum_quantity <= 0:
            raise ContractUnavailable("CONTRACT_MINIMUM_QUANTITY_UNAVAILABLE")
        if spec.quantity_precision < 0 or spec.price_precision < 0:
            raise ContractUnavailable("CONTRACT_PRECISION_UNAVAILABLE")
        return spec
