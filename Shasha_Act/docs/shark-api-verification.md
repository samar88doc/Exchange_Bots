# Shark API verification and LIVE safety boundary

**Official reference inspected:** [Shark Exchange API documentation](https://docs.sharkexchange.in/). The documentation site displayed a latest changelog date of 2026-01-29 when reviewed for this project. Re-check the official reference and confirm with Shark before relying on any behavior; this document is not an exchange guarantee.

## Public information used

The implementation uses the public `exchangeInfo` metadata and 24-hour ticker endpoints, and the documented public Socket.IO ticker service/event. The ticker displays a **last price** plus 24-hour change/high/low/volume; the dashboard labels it as last price and never treats the 24-hour change as daily P&L. The public WebSocket is supplemental; REST polling is available as fallback.

The metadata parser uses only the fields documented for contract identity, base/quote, contract type, supported margin asset, order types, precision, maximum leverage, and minimum quantity/notional filters when present. Symbol resolution requires an exact Shark contract name, `PERPETUAL`, `USDT` quote and the configured margin asset.

## Private API wrapper boundary

`backend/app/shark.py` encapsulates the API reference's private account, order, order-history/trade-history, leverage and order-management routes. Authenticated requests use HMAC-SHA256 and the documented `api-key` / `signature` headers with exact serialized request bytes or query parameters. Submission timeouts and HTTP 5xx are represented as **unknown outcome**, not a safe-to-retry failure.

The project does not claim that a successful submit response is a fill. Entry/pending state is reconciled from Shark order/position responses; filled quantity is protected only after position data is confirmed. A failed or ambiguous order is never blindly resubmitted.

## Explicitly unverified — LIVE remains disabled

The reviewed official material did **not** provide enough verifiable detail to authorize unattended LIVE operation for all supported contracts. In particular, this project does not infer or generate production values for:

1. Exact per-contract `priceTick` / `tickSize` and `quantityStep` / `stepSize` behavior. Precision counts alone are not treated as exact tick/step constraints.
2. Whether split TP/SL orders have atomic OCO behavior, how one filled target changes/cancels the remaining legs, and how stop replacement is sequenced or made atomic.
3. The exact semantics needed to guarantee a residual position is protected during partial fills, edits, cancellation races and restart recovery.
4. The complete current response fields needed to treat every wallet, position, order and trade-history value as authoritative. Unknown/malformed private response shapes are treated as unavailable and do not justify closing or retrying an order.

Until those rules are confirmed from current official documentation and an appropriately isolated non-production account, both flags below must remain false:

```text
SHARK_CONTRACT_RULES_VERIFIED=false
SHARK_SPLIT_PROTECTION_VERIFIED=false
LIVE_TRADING_ENABLED=false
```

The dashboard's LIVE activation endpoint checks these conditions and currently returns HTTP 409. The executor also checks them before any LIVE entry submission. The live contract parser leaves `price_tick`, `quantity_step` and `tradable` unset, so no LIVE contract satisfies `live_safe`. Do not manually change the database or environment flags to bypass this gate. A code review and tests for the exact confirmed semantics are required before LIVE can be reconsidered.

## Operational caveats

- No real Shark order is placed by tests or by default configuration.
- Shark API availability, order acceptance, fill price, protection, liquidation price, fees, funding and profit are not guaranteed.
- Exchange-reported fees/funding can be incomplete or unavailable; API/UI explicitly mark data availability.
- PAPER uses a deterministic public-last-price simulation rule, not a recreation of the exchange matching engine.
