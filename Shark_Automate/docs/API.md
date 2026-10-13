# API Reference

Base URL: `http://<host>:8000`. All request/response bodies are JSON.
Every response carries an `X-Correlation-Id` header.

## TradingView webhook

### `POST /api/webhooks/tradingview`

Secure ingestion. Auth: header `x-webhook-secret: <WEBHOOK_SECRET>`
(or `Authorization: Bearer …`, or a `secret` field in the payload).

Request (conceptual Pine/TradingView payload):

```json
{
  "event_id": "1726313209122-BASUSDT.P",
  "strategy_id": "strategy_1",
  "strategy_name": "Strategy 1",
  "symbol": "BASUSDT.P",
  "action": "BUY",
  "timestamp": "2026-09-21T10:00:00Z",
  "timeframe": "5m",
  "price": 100.5,
  "source": "TRADINGVIEW",
  "secret": "<WEBHOOK_SECRET>"
}
```

Response `202` (processing continues asynchronously — TradingView is never kept
waiting for exchange operations):

```json
{
  "status": "ACCEPTED",
  "event_id": "1726313209122-BASUSDT.P",
  "correlation_id": "9f86d081884c7d65",
  "message": "event accepted for async processing",
  "tradingview_raw_symbol": "BASUSDT.P",
  "canonical_symbol": "BASUSDT",
  "shark_symbol": "BASUSDT"
}
```

`status` ∈ `ACCEPTED` | `DUPLICATE` | `REJECTED` (symbol resolution failures return
`202` with `REJECTED` and the reason — the event is still persisted for audit).

Errors: `400 INVALID_PAYLOAD` · `400 STALE_EVENT` · `401 INVALID_SIGNATURE` ·
`413 payload too large` · `429 rate limit exceeded`.

## Status / health

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/health` | Component health: database, webhook, shark, market data, redis, worker, position monitor, symbol mapping, reconciliation |
| GET | `/api/status` | Dashboard aggregate: mode, halt, risk counts, P&L stats |
| GET | `/api/stream` | SSE live updates |

## Signals

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/signals` | Signal history (`limit`, `status`, `canonical_symbol` filters) |
| GET | `/api/signals/{id}` | Signal detail + full event timeline |

Statuses: `RECEIVED`, `VALIDATED`, `ACCEPTED`, `EXECUTED`, `REJECTED`, `DUPLICATE`,
`MAX_POSITIONS`, `SYMBOL_ALREADY_OPEN`, `SHARK_SYMBOL_NOT_FOUND`, `INVALID`, `ERROR`.

## Positions

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/positions?active_only=true` | Positions (every row carries the raw/canonical/shark triple) |
| GET | `/api/positions/{id}` | Position detail |
| GET | `/api/positions/{id}/timeline` | Chronological explainability timeline |
| POST | `/api/positions/{id}/close` | `{"reason":"MANUAL","confirm":true}` (LIVE requires `confirm=true`) |
| POST | `/api/positions/close-all` | Close all (LIVE requires `confirm=true`) |

## Trades / orders

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/trades` | Closed trades (`mode`, `result`, `canonical_symbol` filters) |
| GET | `/api/trades/export/csv` | CSV export |
| GET | `/api/orders` | Order lifecycle rows incl. submitted payload + dry-run flag |

## Risk center

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/risk` | Limits, slots, reservations, halt/kill state, recent risk events |
| POST | `/api/risk/halt` | Emergency halt (no new entries; monitoring continues) |
| POST | `/api/risk/resume` | Requires body `{"confirm": true}` |
| POST | `/api/risk/kill-switch` | `{"confirm": true}` enables, `false` disables |

## Webhook monitor

`GET /api/webhooks/monitor` — health, last event, events/minute, successful /
rejected / duplicate / invalid / failed counters, normalization failures, feed.

## Strategies

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/strategies` | Strategy 1 / Strategy 2 config + counters |
| PATCH | `/api/strategies/{id}` | `{"enabled": false}` etc. |

## Symbols

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/symbols/mappings` | Symbol mapping table (raw / canonical / shark + contract rules) |
| POST | `/api/symbols/mappings` | Manual mapping entry |
| POST | `/api/symbols/sync` | Sync all contracts from verified Shark `exchangeInfo` |

## Settings / system

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/settings` | All configuration sections (secrets excluded) |
| PATCH | `/api/settings` | Runtime flags only: `trading_halted`, `kill_switch`, `dry_run` |
| GET | `/api/audit` | Audit log |
| GET | `/api/events` | System events |
| GET | `/api/reconciliation` | CRITICAL_RECONCILIATION_EVENT rows |

## Replay

`POST /api/replay`

```json
{ "start": "…", "end": "…", "strategy_id": "strategy_1", "symbol": "BASUSDT.P", "speed": 0 }
```

## Live activation

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/live/paper-report` | Complete paper-trading report + chart data |
| POST | `/api/live/activate` | GO LIVE (see LIVE_TRADING.md for required body) |
| POST | `/api/live/deactivate` | Back to PAPER |
