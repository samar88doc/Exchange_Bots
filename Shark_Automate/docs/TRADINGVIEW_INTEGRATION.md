# TradingView Integration

## Alert setup

1. Open the chart of the perpetual you want (e.g. `BASUSDT.P`).
2. Add your Pine strategy (Indicators → Technical → Strategies) — Strategy 1 and
   Strategy 2 are configured in the app; Pine logic is never modified.
3. **Add Alert** → condition: the strategy.
4. **Notifications** → enable **Webhook** → URL:

```
http://<your-host>:8000/api/webhooks/tradingview
```

5. Paste the message template below into the alert message box.
6. Repeat for the second watchlist alert / strategy.

TradingView premium is required for webhooks.

## Message template (must match your Pine alert placeholders)

```json
{
  "event_id": "{{timenow}}-{{ticker}}",
  "strategy_id": "strategy_1",
  "strategy_name": "Strategy 1",
  "symbol": "{{ticker}}",
  "action": "BUY",
  "timestamp": "{{timenow}}",
  "timeframe": "{{interval}}",
  "price": {{close}},
  "source": "TRADINGVIEW",
  "secret": "<your WEBHOOK_SECRET>"
}
```

| Field | Notes |
| --- | --- |
| `event_id` | optional but recommended; otherwise a deterministic fingerprint is derived |
| `strategy_id` | `strategy_1` / `strategy_2` — modular, never hard-coded through the app |
| `symbol` | `{{ticker}}` yields e.g. `BASUSDT.P` — **raw value preserved** |
| `action` | `BUY` (= OPEN_LONG). Future: `SELL`/`CLOSE`, `SHORT`, `CLOSE_SHORT` |
| `timestamp` | ISO-8601 or epoch ms; events older than 15 min are `STALE_EVENT` |
| `secret` | shared secret — alternatively send header `x-webhook-secret` |

Numbers (`price`) must not be quoted strings.

## Example payloads

Long entry:

```json
{
  "event_id": "manual-001",
  "strategy_id": "strategy_1",
  "symbol": "BASUSDT.P",
  "action": "BUY",
  "timestamp": "2026-09-21T10:00:00Z",
  "timeframe": "5m",
  "price": 100.5,
  "source": "TRADINGVIEW",
  "secret": "<WEBHOOK_SECRET>"
}
```

Response `202`:

```json
{
  "status": "ACCEPTED",
  "event_id": "manual-001",
  "correlation_id": "…",
  "tradingview_raw_symbol": "BASUSDT.P",
  "canonical_symbol": "BASUSDT",
  "shark_symbol": "BASUSDT"
}
```

## Test webhook (curl)

```bash
curl -X POST http://localhost:8000/api/webhooks/tradingview \
  -H 'Content-Type: application/json' \
  -H 'x-webhook-secret: <WEBHOOK_SECRET>' \
  -d '{"event_id":"test-1","strategy_id":"strategy_1","symbol":"BASUSDT.P","action":"BUY","timestamp":"'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'"}'
```

Check the **Webhook Monitor** page for real-time health: last event,
events/minute, successful/rejected/duplicate/invalid/failed counters and
normalization failures.

## Behavior notes

* Processing is asynchronous: the endpoint answers `202` quickly and TradingView
  is never kept waiting on exchange operations.
* Retries are safe: the same event (same `event_id` or same fingerprint) is acked
  `DUPLICATE` and never produces a second order.
* `{{ticker}}` values differ from Shark contract names in general (e.g.
  `BASUSDT.P` vs `BASUSDT`) — normalization handles the documented `.P`
  perpetual suffix; see SYMBOL_MAPPING.md.
