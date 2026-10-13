# Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Webhook returns `401 INVALID_SIGNATURE` | wrong/missing secret | send `x-webhook-secret` or `secret` field matching `WEBHOOK_SECRET` |
| Webhook returns `400 STALE_EVENT` | alert timestamp older than 15 min (or clock skew) | use `{{timenow}}` in the payload; sync server clocks |
| Webhook returns `400 INVALID_PAYLOAD` | schema mismatch | check `symbol` and `action` exist; numbers not quoted |
| Response `{"status":"DUPLICATE"}` | same event_id/fingerprint already processed | expected — exactly-once semantics; check Signals page |
| Signal `SHARK_SYMBOL_NOT_FOUND` | contract not in `symbol_mappings` | run **Sync from Shark exchangeInfo** (Settings) or add a mapping manually |
| Signal `SYMBOL_ALREADY_OPEN` | canonical symbol already has a position/reservation | intended (e.g. `BASUSDT.P` while `BASUSDT` is open) |
| Signal `MAX_OPEN_POSITIONS_REACHED` | 3 slots full (OPEN + RESERVED) | close a position or wait for TP |
| Signal `MIN_QUANTITY_NOT_MET` / `MIN_NOTIONAL_NOT_MET` | contract rules can't express 120 USDT / 10x | intended rejection — risk is never silently resized |
| Position stuck `RESERVED` | crash between reservation and order | auto-expires after `STALE_RESERVATION_SECONDS` (120 s) |
| Position `UNKNOWN` | order submission outcome uncertain | **do not resubmit** — inspect Audit → Reconciliation; it resolves via `GET /v1/order` |
| `CRITICAL_RECONCILIATION_EVENT` rows | internal vs Shark mismatch | manual review required; system will not auto-fix with new orders |
| Orders rejected `LIVE_NOT_ENABLED` in LIVE | safety gates incomplete | set `LIVE_TRADING_ENABLED=true` + `LIVE_TRADING_CONFIRMATION` + Shark credentials, restart, re-run GO LIVE |
| LIVE orders missing but no error | `SHARK_DRY_RUN=true` | intended: payload is logged/displayed only; set `false` when ready |
| Prices stale in PAPER | Shark public ticker unreachable | check network; seeded/last prices are used as explicit fallback (visible in market data health) |
| `alembic` → *Can't locate revision* | version table points at a removed migration | `psql -c "DROP TABLE alembic_version"` then `alembic upgrade head` (dev only) |
| Frontend can't reach API | wrong `BACKEND_URL` | set `BACKEND_URL=http://localhost:8000` (dev) — Next.js proxies `/api/*` |
| DB errors on startup | Postgres down / migrations not applied | start Postgres, run `alembic upgrade head` |

## Log locations

`backend/logs/app.log` · `trading.log` · `error.log` · `audit.log` (JSON lines,
with `correlation_id` for tracing a request end-to-end).
