# Risk Engine

## Hard limits (server-side — TradingView cannot bypass)

| Rule | Value | Enforcement |
| --- | --- | --- |
| Max open positions | **3** | atomic reservation under DB lock; `OPEN + RESERVED ≤ 3` |
| Margin per trade | **120 USDT** | fixed config; never silently changed |
| Leverage | **10x** | fixed config |
| Target ROI | **+10% on margin** | ROI monitor |
| Stop loss | **DISABLED** | config |
| SHORT | disabled by default | `SHORT_ENABLED=false` |

The rules live in the **backend**, not in Pine Script. The database — not memory —
is the authoritative position state.

## Duplicate protection (canonical identity)

Duplicate protection uses `canonical_symbol` — never the raw TradingView symbol.

```
Existing position:  canonical_symbol = BASUSDT
Incoming alert:     BASUSDT.P
Normalized:         BASUSDT
Result:             REJECT — SYMBOL_ALREADY_OPEN
```

`BASUSDT`, `BASUSDT.P`, `basusdt.p` are the **same contract** and can never hold
two positions.

## Idempotency layers (all persistent)

1. `webhook_events.event_id` UNIQUE
2. `webhook_events.fingerprint` UNIQUE — deterministic SHA-256 over immutable
   fields (strategy, symbol, action, timestamp, price, timeframe) when TradingView
   supplies no event id
3. raw body hash stored for diagnostics
4. normalized symbol fingerprint + action check at processing time
5. time-window check — events older than `WEBHOOK_REPLAY_WINDOW_SECONDS` (900 s)
   are rejected as `STALE_EVENT` (replay protection)

Two identical webhooks arriving simultaneously → exactly **one** trade: the second
INSERT hits a UNIQUE constraint and is acked `DUPLICATE`.

## Atomic position reservation

```
START TRANSACTION / take risk lock (state_locks FOR UPDATE)
    normalize + resolve symbol               (done upstream, re-validated)
    reserve position slot                    (INSERT reservation + RESERVED position)
    re-check active positions                (count under the lock)
    re-check reservations                    (count under the lock)
    re-check canonical symbol                (UNIQUE constraint is the final word)
    COMMIT                                   (inside the lock!)
ONLY THEN → external order submission
```

Four simultaneous new symbols with 0 open positions → **exactly three accepted**,
the fourth is `MAX_OPEN_POSITIONS_REACHED`. Verified by concurrency tests
(TEST 7, TEST 18) against PostgreSQL.

Reservations expire (`STALE_RESERVATION_SECONDS`, default 120 s) and their paired
RESERVED positions are cancelled — slots can never leak.

## Halt vs kill switch

| | TRADING HALTED | KILL SWITCH |
| --- | --- | --- |
| New entries | rejected (`TRADING_HALTED`) | rejected (`KILL_SWITCH_ACTIVE`) |
| Signals | still received + logged | still received + logged |
| Existing positions | monitored; TP still works | monitored; TP still works |
| Manual closes | allowed | allowed |
| Auto-close all | never (unless explicitly configured) | never |
| To disable | explicit confirmation (`confirm=true`) | explicit action |

The UI shows a screaming banner whenever either is active.

## Daily limits (optional)

`DAILY_TRADE_LIMIT` and `DAILY_LOSS_LIMIT` (0 = unlimited). Loss limit is computed
from realized net P&L over a rolling 24 h window.

## Reconciliation safety

On mismatch (internal OPEN/exchange CLOSED, quantity/entry mismatch, unexpected
exchange position, unknown order) a `CRITICAL_RECONCILIATION_EVENT` is recorded
and surfaced in the UI. The system **never** creates a new position to “fix” a
mismatch and never resubmits an unknown order automatically.
