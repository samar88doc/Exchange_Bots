# Trading Logic

## Entry conditions (ALL must hold)

```
TradingView BUY alert
AND system not halted AND kill switch off
AND symbol normalization succeeds
AND valid canonical symbol
AND valid Shark symbol + contract ACTIVE + precision rules valid
AND no existing position/reservation for canonical_symbol
AND open_positions + reservations < 3
AND margin rules valid (never silently changed)
AND account / risk checks pass
THEN: create position reservation → calculate quantity → place market order
      → verify fill → store ACTUAL entry → start ROI monitoring
```

Supported initially: **LONG ENTRY** + **POSITION CLOSE**. SHORT / CLOSE_SHORT are
wired in the state machine but disabled by default (`SHORT_ENABLED=false`).

## Symbol flow (before ANY risk/duplicate/position/quantity/order work)

```
tradingview_raw_symbol = "BASUSDT.P"     (preserved forever: audit, UI, replay)
        ↓ strip ONLY the terminal ".P" suffix
canonical_symbol       = "BASUSDT"       (internal identity: dupes, risk, state)
        ↓ resolve symbol_mappings (verified contract)
shark_symbol           = "BASUSDT"       (order submission, position lookup)
```

## Position sizing — 120 USDT margin / 10x leverage

```
target_notional = MARGIN_PER_TRADE × LEVERAGE = 120 × 10 = 1200 USDT
quantity        = target_notional / entry_price
                → apply quantity step, precision (ROUND DOWN)
                → enforce min quantity / min notional
```

Final accounting always uses the **actual filled entry price and quantity**.
If the contract rules cannot express exactly 120 USDT margin / 10x (e.g. min
quantity not met) the order is **rejected** (`MIN_QUANTITY_NOT_MET` /
`MIN_NOTIONAL_NOT_MET`) — margin, leverage or quantity are **never silently
changed** to force an order through.

## ROI math (margin-based — NOT price change)

```
LONG:  P&L        = (mark − entry) × quantity
       ROI %      = P&L / position_margin × 100
                  = (mark − entry) / entry × leverage × 100
```

At 10x leverage a **+1% price move = +10% ROI**. The UI always shows both
**ROI %** and **Price Change %**.

Take profit: **ROI ≥ +10%** → close. Target price (no fees):

```
target = entry × (1 + target_roi/100 / leverage) = entry × 1.01
```

With `ROI_INCLUDES_FEES=true` (default) the monitored ROI is net of the entry fee
and the projected exit fee, so the true trigger sits slightly beyond +1% price —
this is intentional and visible in Settings.

Stop loss: **DISABLED** (`STOP_LOSS_ENABLED=false`).

## Exit flow

```
ROI ≥ +10%
  → TP_TRIGGERED
  → reduceOnly MARKET close (verified Shark fields only)
  → fill verified → POSITION_CLOSED
  → trade record: gross/net P&L, fees, funding, ROI %, price Δ %, duration
  → slot released (OPEN + RESERVED ≤ 3 again)
```

Exits can also be triggered manually (single close / close all) and by
close-reason `MANUAL`. LIVE destructive actions require explicit confirmation.

## Fill realism (PAPER)

| Knob | Default | Meaning |
| --- | --- | --- |
| `PAPER_SLIPPAGE_BPS` | 2 | adverse slippage on market fills |
| `PAPER_TAKER_FEE_BPS` | 5 | taker fee on notional |
| `PAPER_MAKER_FEE_BPS` | 2 | maker fee (limit) |
| `PAPER_FUNDING_RATE_BPS_8H` | 0 | funding accrual model |
| `PAPER_LATENCY_MS` | 0 | artificial latency |

## Order lifecycle rule (financial safety)

Shark's `place-order` accepts **no client idempotency key** (verified). Therefore:

1. the submission fingerprint is persisted **before** the HTTP call;
2. an uncertain outcome (timeout / dropped connection) marks the order `UNKNOWN`
   and the position `UNKNOWN` — **never retried blindly**;
3. reconciliation must prove the exchange never received it before any resubmit.
