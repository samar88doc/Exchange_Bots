# Paper Trading

PAPER is the **default and mandatory first phase** (~2 full trading days) before
LIVE can even be considered. There is no code path from PAPER to LIVE that runs
without explicit manual action.

## What is real in PAPER

* TradingView alerts — real
* Webhook ingestion, authentication, idempotency — real
* Symbol normalization + mapping — real
* Risk rules, position limits, duplicate protection, reservations — real
* Market prices — real/current from Shark's **public** ticker endpoint
  (`GET /v1/market/ticker24Hr/{pair}`, verified) whenever available
* Persistence — real (PostgreSQL)

## What is simulated

* Order fills (market orders fill immediately at mark ± slippage)
* Fees / funding / latency (configurable models, visible in Settings)

## What NEVER happens in PAPER

* No call to Shark `POST /v1/order/place-order`
* No real-money order of any kind (`PaperExchangeAdapter` only)

## Configuration (Settings → Paper Trading)

| Setting | Default | Notes |
| --- | --- | --- |
| `PAPER_INITIAL_BALANCE` | 10000 | account unit (USDT) |
| `PAPER_TAKER_FEE_BPS` | 5 | 0.05 % of notional |
| `PAPER_MAKER_FEE_BPS` | 2 | limit orders |
| `PAPER_SLIPPAGE_BPS` | 2 | adverse: buys fill higher, sells lower |
| `PAPER_FUNDING_RATE_BPS_8H` | 0 | funding accrual model |
| `PAPER_LATENCY_MS` | 0 | artificial fill latency |

## Fill model

```
entry:  fill_price = mark × (1 + slippage_bps/10⁴)     (BUY)
exit:   fill_price = mark × (1 − slippage_bps/10⁴)     (SELL reduceOnly)
fee     = notional × fee_bps / 10⁴
P&L     = (exit − entry) × qty − fees − funding
ROI %   = P&L / margin × 100
```

## Paper report (after ~2 trading days)

Settings → **Generate Paper Test Report** (also `GET /api/live/paper-report`):
total / normalized / accepted / rejected signals, executed / closed / open paper
trades, win rate, total P&L, average trade / win / loss, maximum drawdown, maximum
concurrent positions, average + longest holding time, max-position rejections,
duplicate rejections, symbol-mapping failures, TP hit count, execution failures,
plus chart data (cumulative P&L, ROI distribution, trade duration).

## Signal replay tool

Re-run stored webhook events through the engine in PAPER:

* filters: start/end timestamp, strategy, raw/canonical/shark symbol
* speed: 0 = max speed, 1 = realtime spacing

Used to reproduce bugs and validate logic changes safely. Replay events get fresh
identities (`#replay-…`) so history is never rewritten.
