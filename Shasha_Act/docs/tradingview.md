# TradingView → Shasha_Act alert contract

## Preserve the existing strategy condition

The backend does not define or alter the SHASHA condition. Add the alert block to the existing indicator using its already-calculated `shashaAlertConditionCurrent` boolean. Keep the existing expression unchanged and keep `alert.freq_once_per_bar_close`:

```pine
if shashaAlertConditionCurrent
    string shashaJson = '{"strategy":"SHASHA","event_id":"' + syminfo.ticker + '|' + timeframe.period + '|' + str.tostring(time) + '","symbol":"' + syminfo.ticker + '","direction":"LONG","timeframe":"' + timeframe.period + '","bar_time":' + str.tostring(time) + ',"signal_close":"' + str.tostring(close, format.mintick) + '","entry_price":"' + str.tostring(close[1], format.mintick) + '"}'
    alert(shashaJson, alert.freq_once_per_bar_close)
```

The complete standalone snippet is [`tradingview-alert-template.pine`](tradingview-alert-template.pine). It is a **minimal structured alert block**, not a replacement indicator. When creating the TradingView alert, select **Any alert() function call**, enable the webhook URL, and set it to:

```text
https://<your-host>/api/webhooks/tradingview/<your-private-webhook-token>
```

Keep the token in your local deployment secrets and TradingView configuration. It is not displayed in the dashboard or included in alert JSON. Ensure TLS is active at the public endpoint.

## Required JSON fields

```json
{
  "strategy": "SHASHA",
  "event_id": "BTCUSDT|15|1792060500000",
  "symbol": "BTCUSDT.P",
  "direction": "LONG",
  "timeframe": "15",
  "bar_time": 1792060500000,
  "signal_close": "102.75",
  "entry_price": "101.50"
}
```

- `symbol` is the TradingView **ticker**, e.g. `BTCUSDT.P`; do not use `syminfo.tickerid` if it contains an exchange prefix. The raw TradingView value is retained separately.
- `entry_price` is the previous candle close (`close[1]`) and becomes the planned limit entry. `signal_close` is the current signal candle close and is stored independently. The receiver never substitutes one for the other.
- `bar_time` accepts TradingView epoch milliseconds or an ISO-8601 timestamp. The example uses `time` as an integer epoch-millisecond value.
- `event_id` must be deterministic for the same alert candle and distinct for a new candle. The idempotency key also incorporates the strategy. Repeated deliveries of the same signal candle are suppressed.
- Decimal values should be serialized as strings where possible. JSON numbers are parsed as decimal values, not binary floats.

## Symbol rules

The receiver removes only one trailing, case-insensitive `.P`. It does not strip exchange prefixes, append `USDT`, or infer a base/quote asset. The normalized candidate must exactly match Shark's public exchange metadata as a perpetual, with USDT quote and the configured margin asset. Unsupported or ambiguous symbols are rejected before order sizing.

## Legacy text

A constrained legacy text parser accepts explicit `symbol`, `close`, `entry`/`previous candle close`, `timeframe`, and `bar time` lines. It never guesses missing data. JSON is preferred because it supplies a stable `event_id` and separates `entry_price` from `signal_close` unambiguously.

## Receiver behavior

- Missing/wrong token: HTTP 401; request is not accepted.
- Body too large: HTTP 413.
- Rate limited: HTTP 429.
- Valid authenticated payload, even if its fields later fail validation: HTTP 202 after durable event persistence; the event is recorded as rejected with a safe reason.
- Duplicate event: HTTP 202 with `duplicate: true`; no second trade slot or outbox execution job is created.
- A successfully parsed event is checked against database risk controls and reserves a slot in the same database transaction as its event and outbox work.

HTTP 202 means the webhook event was durably recorded; it does **not** mean the limit order filled or the signal will be profitable.
