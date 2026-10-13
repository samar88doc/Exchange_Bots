# Operations runbook (summary)

## First launch checklist (10 steps)

1. **System health** — Dashboard health row: Database CONNECTED, Webhook CONNECTED,
   Market Data CONNECTED (Shark public), Worker CONNECTED.
2. **Configure TradingView webhook** — URL + payload template
   (docs/TRADINGVIEW_INTEGRATION.md).
3. **Configure Shark** — API key/secret in `.env` (PAPER does not need them;
   they are required later for LIVE + market data sync).
4. **Configure trading parameters** — 120 USDT / 10x / max 3 / +10% ROI / SL off.
5. **Test webhook** — curl example in TRADINGVIEW_INTEGRATION.md; watch the
   Webhook Monitor page.
6. **Verify symbol normalization** — Signals page shows
   TV / Canonical / Shark triple; run **Sync from Shark exchangeInfo**.
7. **Start PAPER mode** — default; blue banner on the Dashboard.
8. **Monitor** — positions, ROI, signals, health for ~2 trading days.
9. **Review paper report** — Settings → Generate Paper Test Report.
10. **Optional LIVE activation** — docs/LIVE_TRADING.md (manual, multi-gate).
