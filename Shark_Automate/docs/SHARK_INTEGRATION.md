# Shark Integration

Source of truth: **official Shark API documentation** — https://docs.sharkexchange.in/
(change log through 29-01-2026). Only verified endpoints, fields and auth are used.
Nothing below is invented.

## Authentication (verified)

* HMAC-SHA256 over the **JSON body** (POST/PATCH/PUT/DELETE) or the **query string**
  (GET), hex digest, using the API secret.
* Headers: `api-key: <key>`, `signature: <hex>`.
* Body/query includes `timestamp` (ms).
* Base URL: `https://api.sharkexchange.in`.
* Rate limits (verified): `place-order` 20/s · `delete-order` 30/min · other 60/min.

## Verified REST surface used by `SharkExchangeAdapter`

| Capability | Endpoint | Notes |
| --- | --- | --- |
| place market/limit order | `POST /v1/order/place-order` | fields: `quantity`, `price`, `placeType` (ORDER_FORM/POSITION), `side`, `symbol`, `reduceOnly`, `marginAsset`, `type` (MARKET/LIMIT/STOP_MARKET/STOP_LIMIT), `takeProfitPrice?`, `stopLossPrice?`, `stopPrice?`, `deviceType`, `userCategory` |
| get order | `GET /v1/order/{clientOrderId}` | status verification after submit |
| open orders | `GET /v1/order/open-orders` | pre-flight + reconciliation |
| order history | `GET /v1/order/order-history` | audit |
| cancel order | `DELETE /v1/order/delete-order` | `clientOrderId` |
| cancel all | `DELETE /v1/order/cancel-all-orders` | |
| get positions | `GET /v1/positions/{OPEN\|CLOSED\|LIQUIDATED}` | reconciliation |
| get position | `GET /v1/positions?positionId=` | |
| close **all** positions | `DELETE /v1/positions/close-all-positions` | documented native close |
| add/reduce margin | `POST /v1/order/add-margin`, `/reduce-margin` | |
| split TP/SL | `POST /v2/order/split-tp-sl` | position-level split TP/SL |
| update leverage | `POST /v1/exchange/update/leverage` | `leverage`, `contractName` |
| update preference | `POST /v1/exchange/update/preference` | leverage + marginMode |
| contracts/rules | `GET /v1/exchange/exchangeInfo` | minQty, MIN_NOTIONAL, precision, maxLeverage, marginAssets |
| futures wallet | `GET /v1/wallet/futures-wallet/details` | balances |
| trade history | `GET /v1/user-data/trade-history` | fills |
| ticker / depth / aggTrade | `GET /v1/market/ticker24Hr/{pair}` etc. | **public, no auth** — market data for PAPER |
| klines | `POST /v1/market/klines?priceType=MARK_PRICE\|LAST_PRICE` | public |

## Design decisions forced by the documentation

1. **Single-position close** — the only documented native close is
   `close-all-positions`. A single close is executed as a verified **MARKET
   `reduceOnly`** order (`placeType: POSITION`) — fields that officially exist.
2. **No client idempotency key on place-order** — we therefore NEVER blind-retry
   submissions; uncertain outcomes become `UNKNOWN` + reconciliation.
3. **Native exchange-side TP** exists (`takeProfitPrice` on place-order,
   `split-tp-sl` for open positions) but the strategy target is **ROI on margin
   computed from the actual fill**, so the primary TP is the ROI monitor closing
   with a reduceOnly market order. Exchange-side TP fields are available as an
   optional safety net (`place_take_profit` capability) and are never fabricated.
4. **Leverage** is an exchange-side per-contract setting
   (`/v1/exchange/update/leverage`, verified) — documented in the adapter as
   `set_leverage`; order payloads contain no invented leverage field.
5. **marginAsset** is sent as configured (`INR` per official examples);
   `exchangeInfo.conversionRates` documents INR↔USDT margin/settlement conversion.

## Symbol rule (hard guard)

`SharkExchangeAdapter._assert_shark_symbol` **raises** if the symbol contains a
TradingView suffix (`BASUSDT.P`) — the order layer also sets
`symbol = shark_symbol` explicitly. TEST 17 verifies
`submitted_payload.symbol == "BASUSDT"` and never `BASUSDT.P`.

## Not implemented / to be verified later

| Item | Status |
| --- | --- |
| Authenticated user WebSocket (listen key, socket.io `orderFilled`/`closePosition` events) | verified in docs but not yet consumed — REST polling is used for order/position verification; WS stream is planned as an enhancement |
| OAuth2 auth style | docs mention it; HMAC api-key flow is the documented concrete scheme and is what we implement |
| WebSockets public market stream | REST ticker polling used; WS is an optimization |

Anything else required later must first be verified against the official docs and
implemented strictly inside `exchanges/shark.py` behind `ExchangeAdapter`.
