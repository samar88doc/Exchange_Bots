# Shasha_Act HTTP API

The OpenAPI document is served by the running application at `/api/openapi.json`; Swagger UI is at `/api/docs`. These locations describe routes but never return configured tokens or keys.

## Authentication

- Dashboard/API routes require `X-Dashboard-Token` matching the server-side `DASHBOARD_API_TOKEN`. The browser keeps it in memory only.
- TradingView ingress is authenticated by the secret URL path `/api/webhooks/tradingview/{token}`. The token is never part of the JSON body.
- In production, dashboard and webhook traffic require HTTPS as observed through the trusted TLS proxy. Do not log the full webhook request path.
- `/api/health` and `/api/ready` are public and return only operational status, queue counts and generic error classes.

## Webhook

`POST /api/webhooks/tradingview/{token}` accepts a JSON object or constrained legacy text. See [`tradingview.md`](tradingview.md) for the payload and Pine example.

Response example:

```json
{
  "accepted": true,
  "duplicate": false,
  "event_id": "server-generated-id",
  "status": "QUEUED",
  "message": "Event durably recorded"
}
```

`accepted` means persisted, not filled. Invalid-but-authenticated signal fields are persisted as `REJECTED` with a reason and do not create a trade reservation. Repeated deliveries with the same idempotency identity return the existing event.

## Dashboard read routes

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/settings` | Effective mode, non-secret configuration flags, risk limits and market-day timezone |
| `GET` | `/api/overview` | Market-local day P&L, wallet status, wins/losses and position/queue counts |
| `GET` | `/api/performance?days=30` | Daily realized net-fill series (7–90 days; fees included, funding not assumed) |
| `GET` | `/api/positions` | Active and unresolved trade state; last price/liquidation data can be unavailable |
| `GET` | `/api/pending-entries` | Queued/submitted limit entries, expiry and current status; pending limits are not counted as open positions |
| `GET` | `/api/market?q=&sort=&order=` | Fresh public ticker snapshots, never synthetic values |
| `GET` | `/api/events?status=&symbol=&limit=&offset=` | Signal events and outbox delivery attempts, without raw payloads |
| `GET` | `/api/history?mode=&symbol=&status=&start=&end=&limit=&offset=` | Paginated trade records |
| `GET` | `/api/history.csv?mode=&symbol=` | CSV export with formula-prefix escaping |
| `GET` | `/api/trades/{trade_id}` | One trade detail record |
| `GET` | `/api/trades/{trade_id}/orders` | Persisted local/exchange order states and fill quantities |
| `GET` | `/api/trades/{trade_id}/audit` | Operator/system state transition audit records |

Decimals are returned as decimal strings. A missing value is `null`/`Unavailable`; the API does not convert stale data into a current price. `unrealized_pnl` uses the most recent fresh public **last price**, not a mark price. Wallet values are returned as unavailable when no validated response is available.

## Dashboard changes and controls

| Method | Path | Purpose / guard |
|---|---|---|
| `PATCH` | `/api/settings` | Update future-entry risk controls. Target percentages must satisfy T1 < T2 < T3; margin × leverage cannot exceed the notional cap. |
| `PATCH` | `/api/trades/{trade_id}/risk` | PAPER-only open-position stop/target edit; requires `confirmation_phrase: "CONFIRM RISK CHANGE"`, fresh last price and monotonic targets. LIVE edits are blocked. |
| `POST` | `/api/control/pause` | Pause requires `PAUSE SHASHA ACT`; resume requires `RESUME SHASHA ACT`. A pause does not close positions or cancel orders. |
| `POST` | `/api/control/cancel-pending` | Requires `CANCEL PENDING ENTRIES`; reports only exchange-confirmed cancellations. |
| `POST` | `/api/trades/{trade_id}/cancel-entry` | Requires `CANCEL PENDING ENTRIES`; accepts only `ENTRY_PENDING` or `ENTRY_PARTIAL`, and reports only confirmed cancellation. It does not close any already-filled quantity. |
| `POST` | `/api/trades/{trade_id}/close` | Requires `CLOSE POSITION`; PAPER simulates, while any LIVE result must be reconciled with Shark. |
| `POST` | `/api/control/mode/paper` | Requires `SWITCH TO PAPER`; blocked while active/unresolved LIVE trades exist. |
| `POST` | `/api/control/mode/live` | Requires `ENABLE LIVE TRADING` plus risk acknowledgement, environment gates and verified contract/protection rules. Currently returns HTTP 409 because Shark rules remain unverified. |
| `POST` | `/api/events/{event_id}/retry-notification` | Queues a new idempotent Telegram retry only if a prior Telegram job failed. |

Unsafe settings return HTTP 422. Unavailable/unverified live prerequisites return HTTP 409 or 503. Exchange-confirmed states are never inferred from a successful submit response alone.
