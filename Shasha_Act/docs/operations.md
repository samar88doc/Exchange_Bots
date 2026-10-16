# Operations and security runbook

## Deployment checklist

1. Use a dedicated PostgreSQL database, least-privilege role, encrypted backups and a separate database URL for this service.
2. Copy `backend/.env.example` to a local `backend/.env`; it is ignored by Git. Generate a high-entropy TradingView token and set a strong `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` for the first `OWNER` account. Dashboard authentication is mandatory and must never be disabled. If an initial password cannot be provided by the deployment secret manager, set `INITIAL_OWNER_SIGNUP_ENABLED=true` only long enough to create the first owner through the sign-up screen, then turn it off and restart. `DASHBOARD_API_TOKEN` is optional server-to-server automation only, never a browser credential. Review the subscription block (`SUBSCRIPTION_FEE`, `SUBSCRIPTION_CURRENCY`, `ENFORCE_SUBSCRIPTION`) and the disclaimer gate (`REQUIRE_DISCLAIMER_ACK`) before exposing the console. Never put Shark keys or the Telegram bot token in source, a Pine script, alert JSON, screenshots or the dashboard.
3. Keep `TRADING_MODE=PAPER`, `LIVE_TRADING_ENABLED=false`, and both Shark verification flags false.
4. Run `alembic upgrade head` before starting the API. Migrations are explicit; runtime does not create tables.
5. Terminate TLS at a trusted reverse proxy. Configure the ASGI server to trust forwarded scheme/client headers only from that proxy. Disable access logs or redact the tokenized webhook path at every hop.
6. Expose only the required HTTPS endpoints. Set a restrictive `CORS_ORIGINS` list; use a network allowlist/rate limit at the proxy in addition to the in-process webhook limiter.
7. Confirm `/api/ready`, that sign-in with the bootstrap account works and is rate-limited, that an unacknowledged disclaimer blocks mutations (`403 DISCLAIMER_NOT_ACCEPTED`), that the subscription state is what you expect, plus the TradingView URL/token, PostgreSQL, worker and Telegram configuration. Never use a real signal as a smoke test unless all order execution remains PAPER and the server LIVE gate is closed.
8. Back up PostgreSQL and test restore/reconciliation procedures before production use.

## Accounts, sessions, subscription and disclaimer

- The first `OWNER` account is created at startup from `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD`
  when no operator account exists yet. Alternatively, the one-time sign-up route can create it only
  while `INITIAL_OWNER_SIGNUP_ENABLED=true` and no account exists. The plain password is never stored
  or logged; only a PBKDF2-HMAC-SHA256 hash with `PASSWORD_HASH_ITERATIONS` rounds.
- Browser sign-in issues an opaque session token kept server-side as a hash and sent as
  `X-Session-Token`. Without **Keep me signed in**, the browser keeps it in session storage; with that
  option it uses local storage. Nothing is stored in a cookie, so there is no CSRF surface. **Settings →
  Dashboard access → Clear saved browser session** removes it locally, `POST /api/auth/logout`
  revokes it server-side, and expiry is `DASHBOARD_SESSION_HOURS` or the longer finite
  `DASHBOARD_REMEMBER_SESSION_DAYS` period.
- `DASHBOARD_API_TOKEN` (sent as `X-Dashboard-Token`) is optional server-to-server automation only.
  It must not be copied into a browser, URL, client-side code or operator workflow.
- Roles are `VIEWER < OPERATOR < ADMIN < OWNER`. Only an OWNER can create, promote, modify, demote
  or disable an OWNER; the final active OWNER cannot be demoted or disabled. Changing a password or
  disabling an account revokes all of that account's sessions.
- Repeated failed sign-ins lock the account for `LOGIN_LOCKOUT_MINUTES` after
  `LOGIN_MAX_FAILURES` attempts, and an unknown username still performs a dummy hash so timing does
  not disclose whether the account exists.
- Mutating calls (anything other than `GET`/`HEAD`/`OPTIONS`) require an acknowledged disclaimer
  when `REQUIRE_DISCLAIMER_ACK=true`; with `ENFORCE_SUBSCRIPTION=true` an expired, suspended or
  cancelled plan returns `402` and new signals are refused at the risk gate. Read-only routes stay
  open so an operator can always inspect account and audit state.
- Subscription plan, fee, currency, cycle and expiry are per service record and per operator
  account, and every change is written to the audit trail.

## Mode and order states

| State | Meaning |
|---|---|
| `QUEUED` | Signal and reserved trade slot committed; execution job awaits worker processing. |
| `SUBMITTING` | A LIVE order request was armed and may have reached Shark; no duplicate submission is sent after a crash. |
| `ENTRY_PENDING` | LIMIT entry is awaiting a simulated PAPER touch or a Shark-reported order state; this is **not** an open position. |
| `ENTRY_PARTIAL` | Partial entry is confirmed; only confirmed filled quantity is protected/managed. The remainder must be canceled and confirmed before assuming the total fill is final. |
| `OPEN` / `PARTIALLY_CLOSED` | Position is confirmed/simulated open with its current remaining quantity. |
| `PROTECTION_ERROR` | A protective-order invariant or confirmation failed. Treat as urgent; do not assume protection. |
| `UNKNOWN` | Order/position result is ambiguous or cannot be reconciled. No blind retry. |
| `CLOSING` | Operator close is in progress; remains open/uncertain until exchange position and order state confirm closure. |
| `CLOSED` | Exit/position state is confirmed or PAPER simulation completed. |
| `REJECTED` / `CANCELLED` / `FAILED` | Request did not become an open position; inspect the reason and audit trail. |

PAPER protection is simulated and is not an exchange-side stop. LIVE remains blocked until official contract/protection semantics are verified.

## Routine monitoring

- Check dashboard service status, outbox counts, failed Telegram attempts and any `UNKNOWN` / `PROTECTION_ERROR` trades.
- Verify market data freshness before using mark/last-sensitive controls. The UI suppresses stale ticker values and never fabricates wallet, liquidation or P&L fields.
- Review PostgreSQL disk/connection health, migration version, backup completion and error-rate metrics.
- Use the Daily P&L Ledger in the configured timezone (`Asia/Kolkata` by default), not the ticker's 24-hour change. It separates gross realized P&L, transaction fees, persisted funding, net realized P&L, fully closed wins/losses, open unrealized P&L and net including open exposure. Partial exits affect realized cash but do not inflate completed-trade win/loss counts. Funding is explicitly marked unavailable when no persisted record exists.
- Retain audit events and outbox rows for a period consistent with operating and regulatory needs. Do not store request headers or webhook tokens in logs.

## If a webhook is not processed

1. Check that the URL is HTTPS, the path token matches, and the reverse proxy did not rewrite/drop the request body.
2. Check webhook rate limits, body size, HTTP status and event log. Do not repeatedly send a new event ID to work around a rejection.
3. Inspect the persisted rejection reason (symbol/metadata, required field, risk cap, duplicate symbol, stale market check).
4. Check the durable outbox for `PENDING`, `PROCESSING`, or `FAILED`, then the worker health. Telegram failure does not imply trade failure.
5. Do not resend an order manually when the event/trade state is `SUBMITTING`, `UNKNOWN` or `PROTECTION_ERROR`. Reconcile with Shark first.

## If an exchange state is unknown

- Stop new trading with the dashboard pause/kill switch.
- Inspect the persisted local entry/protection/close orders and audit events.
- Query the same Shark order/position using the documented order ID/symbol/time range. Confirm exchange position and remaining order quantities before taking action.
- Do not blind-retry a market or limit order after a timeout, 5xx, missing order ID or incomplete response.
- If a position exists without confirmed protection, treat it as urgent and use Shark's official manual controls while recording the operator action. Current automated LIVE activation is disabled.
- Resolve every orphaned stop/target/entry and verify residual size before resuming.

## Incident controls

`PAUSE SHASHA ACT` blocks new reservations but does not cancel pending entries or close open positions. Use the separate typed cancel/close actions only after checking exact state. An environment emergency pause cannot be cleared from the dashboard; change it through the server's approved deployment process after incident review.

## Security and secret handling

- Never echo `.env` values in terminal output, issue text, docs, tests, screenshots or commit messages.
- Use a distinct webhook token and rotate it through your secrets manager. If server-to-server automation is enabled, give it a separate `DASHBOARD_API_TOKEN` and rotate it independently. Webhook tokens in paths can be captured by access logs; redact them.
- Keep API keys server-side with the least possible permissions. PAPER does not require private Shark keys.
- The browser holds only its operator session token (in session storage by default, optionally in `localStorage` when remembered); automation tokens never enter browser storage. No third-party JavaScript, font or CDN is used by the dashboard. The Content-Security-Policy is `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'`, so the console ships a single local script and stylesheet.
- Keep production HTTPS, proxy trust, firewall and database TLS controls enabled; do not expose PostgreSQL publicly.
