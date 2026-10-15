# Operations and security runbook

## Deployment checklist

1. Use a dedicated PostgreSQL database, least-privilege role, encrypted backups and a separate database URL for this service.
2. Copy `backend/.env.example` to a local `backend/.env`; it is ignored by Git. Generate independent high-entropy dashboard and TradingView tokens. Never put Shark keys or the Telegram bot token in source, a Pine script, alert JSON, screenshots or the dashboard.
3. Keep `TRADING_MODE=PAPER`, `LIVE_TRADING_ENABLED=false`, and both Shark verification flags false.
4. Run `alembic upgrade head` before starting the API. Migrations are explicit; runtime does not create tables.
5. Terminate TLS at a trusted reverse proxy. Configure the ASGI server to trust forwarded scheme/client headers only from that proxy. Disable access logs or redact the tokenized webhook path at every hop.
6. Expose only the required HTTPS endpoints. Set a restrictive `CORS_ORIGINS` list; use a network allowlist/rate limit at the proxy in addition to the in-process webhook limiter.
7. Confirm `/api/ready`, dashboard authentication, TradingView URL/token, PostgreSQL, worker and Telegram configuration. Never use a real signal as a smoke test unless all order execution remains PAPER and the server LIVE gate is closed.
8. Back up PostgreSQL and test restore/reconciliation procedures before production use.

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
- Use the day P&L displayed in the configured timezone (`Asia/Kolkata` by default). It is not the ticker's 24-hour change. Fee/funding fields are explicitly marked when unavailable.
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
- Use distinct dashboard/webhook tokens and rotate them through your secrets manager. Webhook tokens in paths can be captured by access logs; redact them.
- Keep API keys server-side with the least possible permissions. PAPER does not require private Shark keys.
- The browser holds the dashboard token in memory only, and no third-party JavaScript/CDN is used by the dashboard.
- Keep production HTTPS, proxy trust, firewall and database TLS controls enabled; do not expose PostgreSQL publicly.
