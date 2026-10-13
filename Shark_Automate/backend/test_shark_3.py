import asyncio
import json
import time
import httpx
import hmac
import hashlib
from app.core.config import get_settings

def shark_signature(api_secret: str, data_to_sign: str) -> str:
    return hmac.new(api_secret.encode(), data_to_sign.encode(), hashlib.sha256).hexdigest()

async def test_api():
    settings = get_settings()
    api_key = settings.shark_api_key
    api_secret = settings.shark_api_secret

    ts = int(time.time() * 1000)
    payload = {
        "symbol": "BANUSDT", "side": "BUY", "quantity": 17296.0, 
        "type": "MARKET", "reduceOnly": False, "placeType": "ORDER_FORM",
        "timestamp": ts
    }
    payload_str = json.dumps(payload, separators=(",", ":"))
    signature = shark_signature(api_secret, payload_str)
    
    print(f"Payload str: {payload_str}")
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.sharkexchange.in/v1/order/place-order",
            content=payload_str,
            headers={
                "api-key": api_key, 
                "signature": signature,
                "Content-Type": "application/json"
            }
        )
        print(f"{resp.status_code} {resp.text}")

asyncio.run(test_api())
