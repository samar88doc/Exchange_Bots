# Security

## Secret handling

* All secrets come from environment variables (`.env`, `.env.paper`, `.env.live`);
  `.env*` files (except `.env.example`) are git-ignored.
* The frontend **never** receives Shark credentials — all exchange access is
  backend-only. `/api/settings` reports `credentials_configured: true/false` only.
* Logs redact keys matching secret patterns (`secret`, `api_key`, `token`,
  `password`, `signature`, …) via a logging scrubber.
* Webhook payloads are persisted with the `secret` field stripped.

## Webhook authentication

Shared secret compared in constant time. Accepted via (in order):

1. `x-webhook-secret: <secret>` header
2. `Authorization: Bearer <secret>`
3. `secret` field in the JSON payload (TradingView-friendly)

Missing/wrong secret ⇒ `401 INVALID_SIGNATURE`.

## Request protections

| Control | Value |
| --- | --- |
| Body size limit | `WEBHOOK_MAX_BODY_BYTES` (64 KiB) ⇒ 413 |
| Rate limit | `WEBHOOK_RATE_LIMIT_PER_MINUTE` (120) per IP ⇒ 429 |
| Schema validation | strict Pydantic (`TradingViewAlert`) ⇒ 400 |
| Replay protection | `WEBHOOK_REPLAY_WINDOW_SECONDS` (900 s) ⇒ `STALE_EVENT` |
| Idempotency | UNIQUE `event_id` + fingerprint ⇒ `DUPLICATE` |
| Correlation IDs | `X-Correlation-Id` on every request/response + every log line |

## HTTP hardening

* Strict CORS allowlist (`CORS_ORIGINS`).
* Secure headers on every response: `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`.
* CSRF: state-changing endpoints are JSON-only APIs used by the same-origin
  dashboard (no cookie auth) — cookie-based CSRF does not apply; webhook auth is
  header/secret based.

## Operational security

* LIVE activation requires env gate + secret confirmation + typed phrase +
  configuration acknowledgements (see LIVE_TRADING.md).
* Destructive LIVE actions (close position / close all) require `confirm=true`.
* Resume from halt requires explicit confirmation.
* No secrets in frontend bundles, API responses, CSV exports or audit details.
* PostgreSQL backup/restore scripts avoid touching `trading`/`audit` log tables
  during maintenance windows (see DEPLOYMENT.md).
