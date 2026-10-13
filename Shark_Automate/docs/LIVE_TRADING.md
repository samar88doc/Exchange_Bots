# Live Trading

> ⚠ LIVE trading places real-money orders on Shark Exchange. Every gate below is
> deliberate. **The system never switches to LIVE automatically** — not after 48 h,
> not ever.

## Safety gates (ALL required)

| Gate | Where | Requirement |
| --- | --- | --- |
| 1. Config mode | env `TRADING_MODE=LIVE` | env file must be loaded explicitly |
| 2. Env enable | `LIVE_TRADING_ENABLED=true` | `false` ⇒ every live order refused `LIVE_NOT_ENABLED` |
| 3. Secret confirmation | `LIVE_TRADING_CONFIRMATION=<secret>` | typed back into the GO LIVE form |
| 4. Credentials | `SHARK_API_KEY` / `SHARK_API_SECRET` | present, backend-only |
| 5. UI mode switch | Settings → GO LIVE panel | manual form |
| 6. Typed phrase | `ENABLE LIVE TRADING` | exact, case-sensitive |
| 7. Config acknowledgement | margin 120 · 10x · max 3 · ROI 10% · SL disabled | all four must match config |
| 8. Stop-loss acknowledgement | checkbox | must explicitly acknowledge SL = DISABLED |

The final warning shows: margin per trade, leverage, max positions, target ROI,
**stop loss = DISABLED**, exchange + account/environment, margin asset and the
symbol-normalization behavior.

`POST /api/live/activate` enforces every gate server-side (403/400 on any mismatch).
`POST /api/live/deactivate` returns to PAPER at any time.

## DRY RUN (supported in LIVE)

`SHARK_DRY_RUN=true` (default):

* the **exact** order payload is constructed with verified Shark fields,
* displayed in the UI and written to `logs/trading.log` (`DRY_RUN_ORDER`),
* **never transmitted** — `POST /v1/order/place-order` is not called,
* the position does not open and no slot is consumed.

Example payload preview:

```
TradingView:  BASUSDT.P        Canonical: BASUSDT     Shark: BASUSDT
Side: BUY     Margin: 120 USDT   Leverage: 10x        Notional: 1200 USDT
Quantity: X   Target ROI: 10%    Target price: entry × 1.01

{"placeType":"ORDER_FORM","quantity":X,"side":"BUY","symbol":"BASUSDT",
 "reduceOnly":false,"marginAsset":"INR","type":"MARKET","deviceType":"WEB",
 "userCategory":"EXTERNAL","timestamp":"…"}
```

Set `SHARK_DRY_RUN=false` only after verifying payloads in the logs and the
Orders tab.

## Pre-flight checks before every live order

balance availability · current positions · open orders · trading halt · kill
switch · max-position rule · canonical-symbol duplicate · Shark contract status ·
margin requirements · quantity validity. **Any failure ⇒ REJECT.**

## GO LIVE workflow

1. Run PAPER ≥ 2 trading days.
2. Settings → *Generate Paper Test Report* — review days tested, signals, trades,
   P&L, win rate, max drawdown, errors, rejected/duplicate signals,
   symbol-resolution errors, exchange connection, risk configuration.
3. `cp .env.live .env` (fill Shark credentials; keep `SHARK_DRY_RUN=true`).
4. Restart. Complete the GO LIVE form (phrase + secret + acknowledgements).
5. Verify dry-run payloads on a small test signal.
6. Flip `SHARK_DRY_RUN=false`.

## Order submission safety (LIVE)

* `symbol` in the Shark payload is **always `shark_symbol`** (`BASUSDT`), never
  `BASUSDT.P` — enforced twice (adapter hard guard + execution layer).
* No blind retries: Shark's place-order has no client idempotency key (verified),
  so uncertain submissions become `UNKNOWN` and are resolved by reconciliation
  before anything else happens.
* If the Shark connection is unhealthy, new live entries pause and the reason is
  recorded (`SHARK_UNAVAILABLE`).

## Reconciliation (LIVE only)

Runs every `RECONCILIATION_INTERVAL_SECONDS` (60 s): internal vs Shark positions,
orders, quantities, entries, symbols. Mismatches create
`CRITICAL_RECONCILIATION_EVENT` rows surfaced in Audit & Events — never silent
auto-correction.
