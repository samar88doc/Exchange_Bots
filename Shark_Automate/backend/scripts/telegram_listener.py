import os
import re
import sys
import logging
import asyncio
import httpx
from telethon import TelegramClient, events

# Try to load .env
try:
    from dotenv import load_dotenv
    # Load backend .env
    env_path_backend = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env')
    load_dotenv(env_path_backend)
    # Load root .env
    env_path_root = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), '.env')
    load_dotenv(env_path_root)
except ImportError:
    pass

API_ID = os.environ.get("TELEGRAM_API_ID")
API_HASH = os.environ.get("TELEGRAM_API_HASH")
TELEGRAM_CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "") # e.g. "Crypto Futures" or channel ID
WEBHOOK_URL = os.environ.get("VIP_TELEGRAM_WEBHOOK_URL", "") # Disabled by default: do not forward to port 8000
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "demo-webhook-secret-12345")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("telegram_listener")

if not API_ID or not API_HASH:
    logger.error("TELEGRAM_API_ID and TELEGRAM_API_HASH must be set in the .env file.")
    logger.error("You can get these from https://my.telegram.org/")
    sys.exit(1)

session_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'shark_automater_session')
client = TelegramClient(session_path, int(API_ID), API_HASH)

@client.on(events.NewMessage)
async def handler(event):
    chat = await event.get_chat()
    chat_title = getattr(chat, 'title', '')
    chat_username = getattr(chat, 'username', '')
    chat_id_str = str(getattr(chat, 'id', ''))
    
    # Check if we should filter by channel
    if TELEGRAM_CHANNEL:
        tc_lower = TELEGRAM_CHANNEL.lower()
        if (tc_lower not in str(chat_title).lower() and 
            tc_lower not in str(chat_username).lower() and 
            TELEGRAM_CHANNEL not in chat_id_str and
            chat_id_str not in ("-1002450079596", "2450079596")):
            return

    message = event.raw_text
    if not message:
        return
        
    # Match signal header: e.g. #XLM/USDT - Short🔴 or #BTC/USDT - Long🟢
    match_symbol = re.search(r'#([A-Z0-9]+)/([A-Z0-9]+)\s*-\s*(Long|Short)', message, re.IGNORECASE)
    if not match_symbol:
        return
        
    base = match_symbol.group(1).upper()
    quote = match_symbol.group(2).upper()
    symbol = f"{base}{quote}" # e.g. XLMUSDT
    direction = match_symbol.group(3).upper() # LONG or SHORT
    action = "BUY" if direction == "LONG" else "SHORT"
    
    match_entry = re.search(r'Entry:\s*[`\s]*([0-9.]+)', message, re.IGNORECASE)
    if not match_entry:
        return
    entry_price = match_entry.group(1)
    
    match_sl = re.search(r'Stop Loss:\s*[`\s]*([0-9.]+)', message, re.IGNORECASE)
    stop_loss = match_sl.group(1) if match_sl else None
    
    match_t1 = re.search(r'Target 1:\s*[`\s]*([0-9.]+)', message, re.IGNORECASE)
    target_1 = match_t1.group(1) if match_t1 else None

    match_t2 = re.search(r'Target 2:\s*[`\s]*([0-9.]+)', message, re.IGNORECASE)
    target_2 = match_t2.group(1) if match_t2 else None

    all_targets = re.findall(r'Target\s*\d+:\s*[`\s]*([0-9.]+)', message, re.IGNORECASE)
    
    match_lev = re.search(r'Leverage:\s*x?([0-9]+)', message, re.IGNORECASE)
    leverage = int(match_lev.group(1)) if match_lev else 10
    
    if symbol and action and entry_price:
        logger.info(f"Signal detected in '{chat_title}': {action} ({direction}) {symbol} @ {entry_price} (Lev: x{leverage}, TP1: {target_1}, TP2: {target_2})")
        
        payload = {
            "event_id": f"tg-{event.id}-{symbol}",
            "strategy_id": "vip_telegram",
            "strategy_name": "Vip Crypto Signal Jemal",
            "symbol": symbol,
            "action": action,
            "price": float(entry_price),
            "alert_price": float(entry_price),
            "source": "TELEGRAM",
            "secret": WEBHOOK_SECRET,
            "metadata": {
                "direction": direction,
                "entry": entry_price,
                "stop_loss": stop_loss,
                "target_1": target_1,
                "target_2": target_2,
                "all_targets": all_targets,
                "leverage": leverage,
                "margin": 50,
            }
        }
        
        headers = {
            "Content-Type": "application/json",
            "x-webhook-secret": WEBHOOK_SECRET
        }
        
        if not WEBHOOK_URL:
            logger.info("WEBHOOK_URL not configured; skipping signal forwarding.")
            return

        try:
            async with httpx.AsyncClient() as http_client:
                response = await http_client.post(WEBHOOK_URL, json=payload, headers=headers)
                if response.status_code == 202:
                    logger.info("Successfully forwarded signal to Shark Automate webhook!")
                else:
                    logger.error(f"Failed to forward signal: HTTP {response.status_code} {response.text}")
        except Exception as e:
            logger.error(f"Error forwarding signal to webhook: {e}")

async def main():
    logger.info("Starting Telegram listener...")
    await client.start()
    logger.info("Connected to Telegram!")
    if TELEGRAM_CHANNEL:
        logger.info(f"Listening for signals in channel: {TELEGRAM_CHANNEL}")
    else:
        logger.info(f"Listening for signals in all channels...")
        
    await client.run_until_disconnected()

if __name__ == '__main__':
    client.loop.run_until_complete(main())

