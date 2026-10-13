import asyncio
import httpx

async def close_position(symbol):
    print(f"Closing position for {symbol}...")
    async with httpx.AsyncClient(base_url='http://localhost:8000') as client:
        resp = await client.get("/api/positions")
        positions = resp.json()
        for p in positions:
            if p["shark_symbol"] == symbol:
                await client.post(f"/api/positions/{p['position_id']}/close")
                
                # Poll until position is closed
                for _ in range(10):
                    resp = await client.get("/api/positions")
                    pos_list = resp.json()
                    current = next((x for x in pos_list if x["position_id"] == p["position_id"]), None)
                    if not current or current["state"] == "CLOSED":
                        print("Position closed.")
                        return
                    await asyncio.sleep(0.5)
                print("Position took too long to close.")
                return
    print("No open position found.")

async def send_webhook(symbol, price):
    print(f"Sending webhook for {symbol} @ {price}...")
    async with httpx.AsyncClient(base_url='http://localhost:8000') as client:
        resp = await client.post(
            "/api/webhooks/tradingview",
            json={"symbol": symbol, "action": "BUY", "alert_price": price},
            headers={"x-webhook-secret": "demo-webhook-secret-12345", "Content-Type": "application/json"}
        )
        await asyncio.sleep(1.0)
        resp = await client.get("/api/signals")
        signals = resp.json()
        latest = [s for s in signals if s["tradingview_raw_symbol"] == symbol][0]
        print(f"Signal Status: {latest['status_display']} (Reason: {latest.get('rejection_reason', '')})")
        return latest['status_display']

async def main():
    sym = "SOLUSDT.P"
    res = await send_webhook(sym, 100.0)
    assert res == "EXECUTED", "1st signal should execute"
    await close_position("SOLUSDT")
    
    res = await send_webhook(sym, 101.0)
    assert res == "PRICE_NOT_BELOW_PREVIOUS", "2nd signal higher price should reject"
    
    res = await send_webhook(sym, 99.5)
    assert res == "PRICE_NOT_BELOW_PREVIOUS", "2nd signal price not 1% lower should reject"
    
    res = await send_webhook(sym, 98.0)
    assert res == "EXECUTED", "2nd signal price 1% lower should execute"
    await close_position("SOLUSDT")
    
    res = await send_webhook(sym, 90.0)
    assert res == "DAILY_MAX_TRIGGERS", "3rd signal of the day should reject"
    print("ALL TESTS PASSED!")

if __name__ == "__main__":
    asyncio.run(main())
