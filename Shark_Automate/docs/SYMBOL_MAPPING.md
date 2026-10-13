# Symbol Mapping

TradingView and Shark use different symbol formats for the **same** perpetual
contract. The platform therefore has a dedicated Symbol Normalization / Mapping
Service that runs **before** risk validation, duplicate checks, position checks,
quantity calculation and order submission.

## Required flow

```
STEP 1  receive raw symbol                     BASUSDT.P
STEP 2  store raw TradingView symbol           tradingview_raw_symbol = "BASUSDT.P"
STEP 3  normalize known perpetual suffix       strip ONLY terminal ".P"
STEP 4  resolve canonical symbol               canonical_symbol = "BASUSDT"
STEP 5  resolve Shark symbol                   shark_symbol = "BASUSDT"
STEP 6  verify Shark contract exists           symbol_mappings / exchangeInfo
STEP 7  verify Shark contract is ACTIVE        contract_status == ACTIVE
STEP 8  validate precision/rules               qty/price precision, min qty, min notional
STEP 9  continue to risk + execution           (only now)
```

Failure ⇒ `REJECT` with `SHARK_SYMBOL_NOT_FOUND` (or `CONTRACT_INACTIVE`). The
system **never guesses** another symbol.

## Normalization rules (strict)

* Only the exact **terminal** suffix `.P` is removed (`TRADINGVIEW_PERPETUAL_SUFFIX`).
* `BTCUSDT.P → BTCUSDT` · `ETHUSDT.P → ETHUSDT` · `SOLUSDT.P → SOLUSDT` · `BASUSDT.P → BASUSDT`
* `BTCUSDT.P.P → BTCUSDT.P` (a single terminal suffix is stripped)
* Symbols without the suffix are unchanged: `BTCUSDT → BTCUSDT`
* Nothing else is modified: no start/middle character removal, no blind `USDT`
  removal, no arbitrary suffix rewriting, no guessing.

## The three values are preserved everywhere

Every signal, order, position, trade, audit/timeline and debugging/replay view
shows:

```
TradingView:   BASUSDT.P
Canonical:     BASUSDT
Shark:         BASUSDT
```

| Value | Used for |
| --- | --- |
| `tradingview_raw_symbol` | audit trail, webhook logs, signal history, UI, replay, traceability |
| `canonical_symbol` | strategy logic, duplicate protection, position identity, risk, max-position logic, state |
| `shark_symbol` | Shark order submission, position lookup, contract validation, reconciliation |

**The Shark order payload always carries `shark_symbol`** — `BASUSDT.P` can never
reach the exchange (enforced in the adapter and asserted by TEST F/TEST 17).

## symbol_mappings table

`id`, `tradingview_raw_symbol`, `canonical_symbol`, `shark_symbol`, `exchange`,
`market_type`, `contract_type`, `contract_status`, `base_asset`, `quote_asset`,
`quantity_precision`, `price_precision`, `minimum_quantity`, `minimum_notional`,
`quantity_step`, `price_tick_size`, `max_leverage`, `leverage_constraints`,
`verified_against_exchange`, `raw_contract_meta`, `created_at`, `updated_at`.

Uniques: `tradingview_raw_symbol`, `canonical_symbol` (position identity).

## Population strategy (no hard-coded symbol lists)

* A tiny starter set (BAS/BTC/ETH/SOL/DOGE USDT pairs) is seeded for first-run UX.
* `POST /api/symbols/sync` pulls **all** contracts from the verified Shark
  `GET /v1/exchange/exchangeInfo` endpoint (name, precision, minQty, MIN_NOTIONAL,
  maxLeverage, margin assets, conversion rates) and upserts mappings with
  `verified_against_exchange=true`.
* Manual entries: `POST /api/symbols/mappings` for special cases.

## Identity examples (acceptance-tested)

| Existing | Incoming | Result |
| --- | --- | --- |
| position `BASUSDT` | `BASUSDT.P` | `REJECT — SYMBOL_ALREADY_OPEN` (same canonical identity) |
| — | `UNKNOWNASSET.P` | normalize first → `SHARK_SYMBOL_NOT_FOUND`, no order |
| — | `BASUSDT.P` | Shark order `symbol = BASUSDT`, never `BASUSDT.P` |
