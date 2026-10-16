# Shasha_Act HTTP API

The OpenAPI document is served by the running application at `/api/openapi.json`; Swagger UI is at `/api/docs`. These locations describe routes but never return configured tokens or keys.

## Authentication

Browser dashboard requests require an opaque, cookie-free operator session:

| Credential | Header | Issued by |
|---|---|---|
| Operator session | `X-Session-Token` | `POST /api/auth/login` (username + password), stored server-side as a hash, revocable |

Because no ambient cookie credential exists, no CSRF token is needed. The dashboard does not use,
display or accept an automation token in its browser flow. A resolved session is the only identity
that can hold a disclaimer or subscription obligation. `DASHBOARD_API_TOKEN` / `X-Dashboard-Token`,
when deliberately configured, remains a server-to-server automation integration credential; do not
put it in a browser, URL, client-side code or documentation intended for operators. Framing is denied
by default and must be explicitly allowlisted if a trusted integration requires it.

- Mutating calls from a **session** are gated by `require_mutation_allowed`: an unacknowledged
  risk disclaimer returns `403 DISCLAIMER_NOT_ACCEPTED`, and an expired/suspended/cancelled
  subscription with `ENFORCE_SUBSCRIPTION=true` returns `402`. `/api/auth/*` is always exempt so
  an operator can sign in and read account state.
- Failed sign-ins are rate-limited per username+client and locked after `LOGIN_MAX_FAILURES`
  attempts for `LOGIN_LOCKOUT_MINUTES`. An unknown username still performs a dummy password hash
  so response timing does not reveal account existence.
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

## Account, subscription and disclaimer

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/public/config` | Unauthenticated sign-in metadata: app name, whether initial-owner sign-up is currently available, disclaimer text, plan name/fee/currency/cycle and whether the subscription is enforced. Never returns a secret. |
| `POST` | `/api/auth/signup` | Creates exactly one initial `OWNER` and returns a session only when `INITIAL_OWNER_SIGNUP_ENABLED=true` and no account exists. Otherwise refuses the request. |
| `POST` | `/api/auth/login` | Username + password → session token, finite expiry, user, subscription and disclaimer state. `remember: true` selects the longer configured server-side lifetime. |
| `POST` | `/api/auth/logout` | Revokes the presented session. |
| `GET` | `/api/auth/me` | Current identity, auth method, subscription state, disclaimer state, role permissions and `session_expires_at`, so the console can warn before the session runs out. |
| `POST` | `/api/auth/disclaimer` | Records the operator's acknowledgement of the risk disclaimer (session required). |
| `GET` | `/api/subscription` | Service subscription, the signed-in operator's own plan/expiry, whether enforcement is on, the plan catalogue and billing cycles. |
| `PATCH` | `/api/subscription` | Admin-only update of plan, fee, currency, cycle, status, contact or expiry. Audited. |
| `GET` | `/api/users` | Admin-only operator list (usernames, roles, active flag, plan, expiry, last login). |
| `POST` | `/api/users` | Admin-only operator creation. Password is hashed with PBKDF2-HMAC-SHA256; the plain value is never stored or returned. |
| `PATCH` | `/api/users/{user_id}` | Admin-only role, active flag, display name, password reset or subscription change. Audited. |

Roles are ordered `VIEWER < OPERATOR < ADMIN < OWNER` and are enforced server-side on every
mutating route:

| Minimum role | Routes |
|---|---|
| `VIEWER` | read-only dashboard routes, `/api/auth/me`, `/api/subscription` (read) |
| `OPERATOR` | `PATCH /api/settings` (risk + strategy), `POST /api/control/pause`, `POST /api/control/cancel-pending`, `POST /api/control/mode/paper`, `POST /api/trades/{id}/risk\|close\|cancel-entry`, `POST /api/events/{id}/retry-notification` |
| `ADMIN` | `POST /api/control/mode/live`, `PATCH /api/subscription`, operator management excluding OWNER accounts |
| `OWNER` | Create, promote, edit, demote or disable OWNER accounts; owner-only account hierarchy decisions |

Authentication cannot be disabled. `POST /api/control/dashboard-auth` is retained solely as a
compatibility no-op for `{"enabled": true}` and rejects an attempt to turn access off. A refused call
returns `403` with the required role in the detail and changes nothing. An ADMIN cannot create, promote,
edit, demote, reset the password of, or disable an OWNER. The final active OWNER cannot be demoted or
disabled; password changes or disablement revoke that user's outstanding sessions. The optional server
automation token is an owner-level integration identity, not a browser login or a role-bypass pattern.

## Dashboard read routes

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/settings` | Effective mode, non-secret configuration flags, risk limits, the strategy block (entry candle interval, stop %, Target 1 %, Target 1 close %, trailing %, entry-source provenance) and market-day timezone |
| `GET` | `/api/overview` | Market-local day P&L, wallet status, wins/losses and position/queue counts |
| `GET` | `/api/performance?days=30` | Daily realized net-fill series (7–90 days; transaction fees and persisted funding included, with funding-record availability stated explicitly) |
| `GET` | `/api/positions` | Active and unresolved trade state; last price/liquidation data can be unavailable |
| `GET` | `/api/pending-entries` | Queued/submitted limit entries, expiry and current status; pending limits are not counted as open positions |
| `GET` | `/api/market?q=&sort=&order=` | Fresh public ticker snapshots, never synthetic values |
| `GET` | `/api/events?status=&symbol=&limit=&offset=` | Signal events and outbox delivery attempts, without raw payloads |
| `GET` | `/api/history?mode=&symbol=&status=&start=&end=&limit=&offset=` | Paginated trade records |
| `GET` | `/api/history.csv?mode=&symbol=` | CSV export with formula-prefix escaping |
| `GET` | `/api/trades/{trade_id}` | One trade detail record |
| `GET` | `/api/trades/{trade_id}/orders` | Persisted local/exchange order states and fill quantities |
| `GET` | `/api/trades/{trade_id}/audit` | Operator/system state transition audit records |

Decimals are returned as decimal strings. Persisted values stay exact: the SQLite development
database stores `NUMERIC` through a type that re-quantizes on load, so a price such as `80515.2` is
never returned as `80515.199999999997`. A missing value is `null`/`Unavailable`; the API does not convert
stale data into a current price. `unrealized_pnl` uses the most recent fresh public **last price**, not a mark price. Wallet values are returned as unavailable when no validated response is available.

## Dashboard changes and controls

| Method | Path | Purpose / guard |
|---|---|---|
| `PATCH` | `/api/settings` | Update future-entry risk **and strategy** controls (`stop_loss_percent`, `target1_percent`, `target1_close_percent`, `trailing_stop_percent`, `trailing_enabled`, `enable_secondary_targets`, `entry_candle_interval`, `limit_entry_expiry_seconds`, symbol filters). With secondary targets disabled only Target 1 must be above entry; with them enabled the fixed ladder must still satisfy T1 < T2 < T3. Margin × leverage cannot exceed the notional cap. Every change is audited. |
| `PATCH` | `/api/trades/{trade_id}/risk` | PAPER-only open-position stop/target edit; requires `confirmation_phrase: "CONFIRM RISK CHANGE"`, fresh last price and monotonic targets. LIVE edits are blocked. |
| `POST` | `/api/control/pause` | Pause requires `PAUSE SHASHA ACT`; resume requires `RESUME SHASHA ACT`. A pause does not close positions or cancel orders. |
| `POST` | `/api/control/cancel-pending` | Requires `CANCEL PENDING ENTRIES`; reports only exchange-confirmed cancellations. |
| `POST` | `/api/trades/{trade_id}/cancel-entry` | Requires `CANCEL PENDING ENTRIES`; accepts only `ENTRY_PENDING` or `ENTRY_PARTIAL`, and reports only confirmed cancellation. It does not close any already-filled quantity. |
| `POST` | `/api/trades/{trade_id}/close` | Requires `CLOSE POSITION`; PAPER simulates, while any LIVE result must be reconciled with Shark. |
| `POST` | `/api/control/mode/paper` | Requires `SWITCH TO PAPER`; blocked while active/unresolved LIVE trades exist. |
| `POST` | `/api/control/mode/live` | Requires `ENABLE LIVE TRADING` plus risk acknowledgement, environment gates and verified contract/protection rules. Currently returns HTTP 409 because Shark rules remain unverified. |
| `POST` | `/api/events/{event_id}/retry-notification` | Queues a new idempotent Telegram retry only if a prior Telegram job failed. |

Unsafe settings return HTTP 422. Unavailable/unverified live prerequisites return HTTP 409 or 503. Exchange-confirmed states are never inferred from a successful submit response alone.
