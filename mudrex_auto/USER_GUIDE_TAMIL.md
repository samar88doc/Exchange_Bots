# 📘 Mudrex Signal Trader — பயனர் வழிகாட்டி (தமிழ்)

> **புதியவர்களுக்கான முழுமையான வழிகாட்டி**
> இந்த ஆவணம் தமிழில் எழுதப்பட்டுள்ளது. திட்டத்தை முழுமையாக புரிந்துகொண்டு பாதுகாப்பாக பயன்படுத்துவதற்காக இது உருவாக்கப்பட்டது.

---

## ⚠️ முக்கியமான எச்சரிக்கை (படிக்காமல் தாண்டாதீர்கள்!)

> **இந்த bot real பணத்தில் trade செய்யும். தவறாக configure செய்தால் நீங்கள் முதலீடு செய்த பணம் முழுவதும் போகலாம்.**
>
> - **Stop-loss தானாகவே வைக்காது** — -75% margin hit ஆனால் மட்டுமே SL போடும்.
> - **Leverage அதிகமாக இருந்தால் liquidation வேகமாக வரும்.**
> - **முதலில் PAPER mode-ல் பழகுங்கள். Real mode ஆரம்பிக்காதீர்கள்.**

---

## 📋 உள்ளடக்கம்

1. இந்த Bot என்ன செய்கிறது?
2. System எப்படி வேலை செய்கிறது?
3. Signal Format எப்படி இருக்கும்?
4. Trade Management — புதிய Logic
5. Setup — முதல் முறை நிறுவுதல்
6. .env Configuration விவரம்
7. PAPER Mode — பயிற்சி செய்வது எப்படி?
8. REAL Mode — உண்மையான Trading
9. Dashboard பார்ப்பது எப்படி?
10. Commands Reference
11. Log மற்றும் Monitoring
12. Problem Solving
13. Project Files புரிதல்
14. பாதுகாப்பு விதிகள்
15. Real Trade Example

---

## 1. இந்த Bot என்ன செய்கிறது?

இந்த bot **Telegram signal channel**-ல் வரும் trading signals-ஐ படித்து, **Mudrex Futures Exchange**-ல் தானாகவே order போட்டு, targets hit ஆகும்வரை position manage செய்யும்.

### சுருக்கமாக சொன்னால்:

```
Telegram Channel Signal வருகிறது
        ↓
Bot சிக்னலை படிக்கிறது (Entry, Targets, SL, Leverage)
        ↓
Mudrex-ல் LIMIT Entry Order போடுகிறது
        ↓
Order Fill ஆகிறது
        ↓
ஒவ்வொரு Target-க்கும் Reduce-Only Limit Order போடுகிறது
        ↓
Target 1 hit → SL Entry Price-க்கு move ஆகிறது (Break-Even)
Target 2 hit → SL Target 1-க்கு move ஆகிறது
...மேலும் போகிறது...
        ↓
எல்லா targets hit ஆனால் / SL hit ஆனால் → Position Close
```

---

## 2. System எப்படி வேலை செய்கிறது?

### முக்கிய Parts:

| Part | என்ன செய்கிறது |
|---|---|
| **Telegram Listener** | Channel-ல் புதிய signals monitor செய்கிறது |
| **Signal Parser** | Signal text-ஐ படித்து Entry, Targets, SL, Leverage எடுக்கிறது |
| **Risk Manager** | Signal valid-ஆ? Funds போதுமானதா? என்று check செய்கிறது |
| **Execution Engine** | Mudrex-ல் order போடுகிறது |
| **Position Monitor** | Fill ஆன பிறகு targets monitor செய்து SL move செய்கிறது |
| **Dashboard** | Web browser-ல் நடக்கும் trades பார்க்க |
| **Storage (SQLite)** | எல்லாவற்றையும் database-ல் save செய்கிறது |

### Signal வந்தால் என்ன நடக்கும் — Step by Step:

```
Step 1:  Signal வருகிறது
Step 2:  Duplicate check (இதே signal முன்பு வந்ததா?)
Step 3:  Age check (5 நிமிடத்திற்கு மேல் பழையதா? → Reject)
Step 4:  Symbol check (Mudrex-ல் trade ஆகுமா?)
Step 5:  Leverage select (min(telegram_leverage, asset_max))
Step 6:  Fund check (balance போதுமா?)
Step 7:  Entry deviation check (market vs entry 3% limit)
Step 8:  Quantity calculate (Budget × Leverage ÷ Entry Price)
Step 9:  LIMIT order submit to Mudrex
Step 10: Order fill ஆகும் வரை wait (max 120 நிமிடம்)
Step 11: Fill → Targets-க்கு reduce-only orders போடு
Step 12: SL monitor every 10 seconds
```

---

## 3. Signal Format எப்படி இருக்கும்?

### 3 Targets உள்ள Signal:

```
#XMR/USDT - Long
Entry: 540.07
Stop Loss: 520.92522
Target 1: 550.45534
Target 2: 560.90363
Target 3: 600.15987
Leverage: x14
```

### 5 Targets உள்ள Signal:

```
#HYPE/USDT - Long
Entry: 83.119
Stop Loss: 82.54842
Target 1: 83.14441
Target 2: 83.48915
Target 3: 83.96717
Target 4: 84.3469
Target 5: 84.45979
Leverage: x64
```

### 1 Target மட்டும் உள்ள Signal:

```
#SYN/USDT - Long
Entry: 0.2021
Stop Loss: 0.12106
Target 1: 0.22155
Leverage: x14
```

### Bot என்னென்ன படிக்கும்:

| Field | Required? | விளக்கம் |
|---|---|---|
| `#SYMBOL/USDT` | ✅ Yes | Trading pair (USDT மட்டும் support) |
| `Long` / `Short` | ✅ Yes | Direction (emoji மட்டும் போதாது!) |
| `Entry:` | ✅ Yes | Limit order price |
| `Target 1:` | ✅ Yes | முதல் profit level |
| `Leverage: xN` | ✅ Yes | Leverage amount |
| `Stop Loss:` | Optional | -75% margin hit ஆனால் மட்டும் வைக்கும் |
| `Target 2–5:` | Optional | கூடுதல் profit levels |

---

## 4. Trade Management — புதிய Logic

### Multiple Targets — எப்படி வேலை செய்கிறது?

Signal-ல் **3 targets** இருந்தால், bot total quantity-ஐ **3 equal parts**-ஆக பிரிக்கும்:

```
Total Qty = 300 contracts (உதாரணம்)

Target 1-க்கு → 100 contracts (Reduce-Only Limit Sell)
Target 2-க்கு → 100 contracts (Reduce-Only Limit Sell)
Target 3-க்கு → 100 contracts (Reduce-Only Limit Sell)
```

### SL Management — Trailing Stop Loss:

| நிகழ்வு | SL எங்கே போகும்? |
|---|---|
| **Entry Fill** | SL இல்லை |
| **Margin -75%** | Telegram-ல் வந்த SL price |
| **Target 1 Hit** | SL → Entry Price (Break-Even) |
| **Target 2 Hit** | SL → Target 1 Price |
| **Target 3 Hit** | SL → Target 2 Price |
| **Target 4 Hit** | SL → Target 3 Price |

### Emergency SL — -75% Margin Rule:

Entry போட்ட உடனே SL வைக்க முடியாது. Telegram-ல் வரும் SL price பெரும்பாலும் **liquidation price-க்கும் கீழே** இருக்கும் — Mudrex reject செய்யும்.

Bot ஒவ்வொரு 10 seconds-க்கும் unrealised PnL check செய்கிறது:

```
LONG position-க்கு:
  unrealised_pnl = (current_price - fill_price) × quantity

margin_loss% = unrealised_pnl ÷ est_margin

-75% threshold கடந்தால் → SL இப்போதே போடுகிறது
```

Telegram notification வரும்:
`⚠️ [REAL] XMRUSDT margin down -76.3%! Emergency SL placing at 520.92.`

---

## 5. Setup — முதல் முறை நிறுவுதல்

### தேவையான Prerequisites:

- Python 3.12 அல்லது அதற்கு மேல்
- Mudrex account (Futures trading enabled)
- Telegram account (Signal channel-ல் member ஆக இருக்க வேண்டும்)

### Step 1: Project folder-க்கு போங்கள்

```bash
cd /home/yourname/Downloads/mudrex_auto
```

### Step 2: Setup Script Run செய்யுங்கள்

```bash
bash scripts/setup.sh
```

இது செய்வது:
- Python virtual environment உருவாக்கும் (`.venv/`)
- Dependencies install செய்யும்
- Database schema உருவாக்கும்
- Tests run செய்யும்

### Step 3: .env File உருவாக்குங்கள்

```bash
cp .env.example .env
```

`.env` file-ஐ text editor-ல் திறந்து fill செய்யுங்கள்.

### Step 4: Mudrex API Key எடுங்கள்

1. [trade.mudrex.com](https://trade.mudrex.com) login
2. Account Settings → API Keys → Create New
3. **"Secret Key"** மட்டும் copy (API Key அல்ல!)
4. `.env`-ல் `MUDREX_API_SECRET=` இடத்தில் paste

> **Secret Key ஒரு முறை மட்டுமே காட்டும்!** Save செய்துகொள்ளுங்கள்.

### Step 5: Telegram Credentials எடுங்கள்

1. [my.telegram.org](https://my.telegram.org) போங்கள்
2. "API development tools" click
3. App உருவாக்குங்கள்
4. `api_id` மற்றும் `api_hash` copy
5. `.env`-ல் fill செய்யுங்கள்

### Step 6: Telegram Login செய்யுங்கள் (ஒரு முறை மட்டும்)

```bash
source .venv/bin/activate
python -m app.main auth
```

Phone-க்கு OTP வரும். Enter செய்யுங்கள். Session file save ஆகும்.

---

## 6. .env Configuration விவரம்

### Mode Settings:

```env
# PAPER = Practice mode (real பணம் இல்லை)
# REAL = உண்மையான trading
TRADING_MODE=PAPER

# false = புதிய trades போடாதே (monitoring மட்டும்)
TRADING_ENABLED=true
```

### Real Trading Gates:

```env
ENABLE_REAL_TRADING=true
LIVE_TRADING_ACKNOWLEDGEMENT=I ACCEPT NO-STOP-LOSS LIVE TRADING RISK
```

> இந்த exact phrase இல்லாவிட்டால் REAL mode start ஆகாது!

### Mudrex Settings:

```env
MUDREX_API_SECRET=your_secret_key_here
MUDREX_BASE_URL=https://trade.mudrex.com
```

### Risk & Sizing Settings:

```env
# ஒரு trade-க்கு maximum margin (USDT)
MAX_MARGIN_USDT_PER_TRADE=50

# Reserve (0 = எல்லாவற்றையும் use செய்)
MARGIN_RESERVE_USDT=0

# ஒரே நேரத்தில் maximum open positions
MAX_CONCURRENT_POSITIONS=3

# Signal வந்து இவ்வளவு நிமிடம் கடந்தால் reject
MAX_SIGNAL_AGE_MINUTES=5

# Market vs Entry இவ்வளவு % வித்தியாசம் இருந்தால் reject
MAX_ENTRY_DEVIATION_PCT=3

# Entry fill ஆகாவிட்டால் இவ்வளவு நிமிடங்கள் பிறகு cancel
LIMIT_ORDER_TTL_MINUTES=120
```

### Telegram Settings:

```env
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abcdef1234567890abcdef1234567890
TELEGRAM_PHONE=+919876543210
TELEGRAM_SESSION_NAME=./data/telegram_session
TELEGRAM_CHANNEL_INVITE_URL=https://t.me/+Xh7OTE_G_jcxMDE0
```

### Optional Notification Bot:

Trade போட்டால் / target hit ஆனால் உங்கள் Telegram-க்கு message வரும்:

```env
TELEGRAM_NOTIFY_BOT_TOKEN=your_bot_token
TELEGRAM_NOTIFY_CHAT_ID=your_chat_id
```

> Notification bot உருவாக்க: Telegram-ல் @BotFather → `/newbot`

---

## 7. PAPER Mode — பயிற்சி செய்வது எப்படி?

**PAPER mode என்பது simulator.** உண்மையான பணம் போகாது. Mudrex-ல் order போகாது.

### Start:

```bash
source .venv/bin/activate
python -m app.main check --telegram    # எல்லாம் சரியா check
python -m app.main run                 # PAPER mode (default)
```

### Dashboard:

```bash
python -m app.main dashboard --port 8500
# Browser: http://localhost:8500
```

### Status:

```bash
python -m app.main status
```

காட்டுவது:
- எத்தனை signals வந்தன / accepted / rejected
- Active trades
- Paper P&L

### Reset:

```bash
bash scripts/stop.sh
bash scripts/reset_trades.sh --yes
```

---

## 8. REAL Mode — உண்மையான Trading

### REAL Mode-க்கு போவதற்கு முன் Checklist:

- [ ] குறைந்தது 1 வாரம் PAPER mode-ல் பழகினேன்
- [ ] `python -m app.main status` results நல்லதாக இருக்கிறது
- [ ] `MAX_MARGIN_USDT_PER_TRADE` சரியான amount-ல் உள்ளது
- [ ] Mudrex account-ல் Futures trading enabled
- [ ] API Key-ல் Trading scope மட்டும் (Withdrawal இல்லை!)

### .env-ல் REAL Mode Enable:

```env
TRADING_MODE=REAL
ENABLE_REAL_TRADING=true
LIVE_TRADING_ACKNOWLEDGEMENT=I ACCEPT NO-STOP-LOSS LIVE TRADING RISK
MARGIN_RESERVE_USDT=0
MAX_MARGIN_USDT_PER_TRADE=50
```

### Start:

```bash
source .venv/bin/activate
python -m app.main run --yes
```

Terminal prompt வரும்:
```
Type exactly 'START LIVE TRADING' to start REAL trading:
>
```

`START LIVE TRADING` type செய்து Enter.

### REAL Mode Log:

```
================================================================
REAL MODE — LIVE MUDREX FUTURES ORDERS WILL BE PLACED
================================================================
INFO [main] event=startup mode=REAL
INFO [main] event=real.readonly_check_ok futures_balance=873.13
INFO [telegram] event=telegram.connected

# Signal வந்தால்:
INFO [engine] event=signal.parsed symbol=XMRUSDT direction=LONG

# Order:
INFO [engine] event=real.order_accepted trade_id=6

# Fill:
INFO [monitor] event=real.entry_filled trade_id=6 fill_price=540.07

# TP orders:
INFO [monitor] event=real.tp_order_placed target=1 price=550.45

# Target hit:
INFO [monitor] event=real.tp_hit target=1
INFO [monitor] event=real.sl_moved sl_price=540.07

# Emergency SL:
WARNING [monitor] event=real.emergency_sl_triggered margin_loss_pct=-76.5%
```

---

## 9. Dashboard பார்ப்பது எப்படி?

```bash
python -m app.main dashboard --port 8500
# Browser: http://localhost:8500
```

### Active Trades Table columns:

| Column | விளக்கம் |
|---|---|
| Trade # | Trade ID |
| Symbol | Coin name |
| Side | LONG / SHORT |
| Entry | Enter செய்த price |
| Target 1 | முதல் profit target |
| Leverage | எத்தனை x |
| Status | PENDING / FILLED / CLOSED |
| Current Price | Live market price |
| P/L | Unrealised profit/loss % |

---

## 10. Commands Reference

```bash
# Bot start (PAPER)
python -m app.main run

# Bot start (REAL)
python -m app.main run --yes

# Config check
python -m app.main check
python -m app.main check --telegram

# First-time Telegram login
python -m app.main auth

# Status
python -m app.main status

# Dashboard
python -m app.main dashboard --port 8500

# Stop new trades (emergency)
python -m app.main kill

# Resume trading
python -m app.main resume

# Resolve stuck trade
python -m app.main resolve-trade 5 --status CLOSED --note "verified"

# Test parse a signal file
python -m app.main parse-file signal.txt
```

### Shell Scripts:

```bash
bash scripts/setup.sh          # First time install
bash scripts/start.sh          # Background start (PAPER only)
bash scripts/stop.sh           # Stop bot
bash scripts/status.sh         # Quick status
bash scripts/reset_trades.sh   # PAPER trades delete (--yes)
bash scripts/dashboard.sh      # Dashboard
```

---

## 11. Log மற்றும் Monitoring

### முக்கியமான Log Events:

| Event | அர்த்தம் |
|---|---|
| `signal.ignored` | Normal chat message, signal இல்லை |
| `signal.rejected` | Condition fail — reject |
| `signal.accepted` | Order போட்டோம் |
| `real.entry_filled` | Order fill ஆனது |
| `real.tp_order_placed` | Target order போட்டோம் |
| `real.tp_hit` | Target hit! |
| `real.sl_moved` | SL move ஆனது |
| `real.emergency_sl_triggered` | -75% margin threshold |
| `real.position_closed_unverified` | Position gone on Mudrex |
| `real.ttl_cancelled` | Entry TTL expire, cancel |
| `real.reconcile_not_found` | Order not found — NEEDS_REVIEW |

### Rejection Reasons:

| Reason | என்ன பிரச்சனை |
|---|---|
| `stale signal: 594s old` | 5 நிமிடத்திற்கு மேல் பழையது |
| `invalid SHORT target` | SHORT signal-ல் Target > Entry |
| `active trade already exists` | அந்த coin-ல் trade ஏற்கனவே open |
| `insufficient available balance` | Balance போதவில்லை |
| `market is X% away from Entry` | Entry, market-ல் இருந்து மிகவும் தூரம் |
| `duplicate signal content` | 24 மணி நேரத்தில் same signal |

### Log கட்டளைகள்:

```bash
# Live log watch
tail -f data/bot.log

# Last 100 lines
tail -100 data/bot.log

# Errors மட்டும்
grep "ERROR\|WARNING" data/bot.log

# Trade events மட்டும்
grep "tp_hit\|sl_moved\|entry_filled" data/bot.log
```

---

## 12. Problem Solving — பிரச்சனை வந்தால்?

### Bot start ஆகவில்லை

```bash
python -m app.main check
```

பொதுவான காரணங்கள்:
1. `MUDREX_API_SECRET` missing → `.env`-ல் fill
2. Telegram session இல்லை → `python -m app.main auth`
3. Python பழையது → Python 3.12+ தேவை
4. `.venv` activate செய்யவில்லை → `source .venv/bin/activate`

### Signal வருகிறது ஆனால் trade போகவில்லை

```bash
python -m app.main status
# அல்லது
tail -f data/bot.log | grep "signal\|rejected"
```

### Order fill ஆனது ஆனால் TP order இல்லை

```bash
grep "tp_order" data/bot.log
```

`real.tp_order_failed` இருந்தால் → Mudrex API issue. அடுத்த 10 seconds cycle-ல் retry ஆகும்.

### "NEEDS_REVIEW" trade என்னவது?

Submit timeout அல்லது error. Bot auto retry செய்யாது (safety).

```bash
# Mudrex-ல் போய் manually check
# பிறகு:
python -m app.main resolve-trade <ID> --status CLOSED --note "verified on Mudrex"
```

### Bot crash ஆனால்

```bash
python -m app.main run --yes
```

Database-ல் எல்லாம் saved. Active trades மீண்டும் monitor ஆகும்.

### Telegram "Session expired"

```bash
rm data/telegram_session.session
python -m app.main auth
```

---

## 13. Project Files புரிதல்

```
mudrex_auto/
├── .env                          ← உங்கள் secrets (NEVER share!)
├── .env.example                  ← Template file
├── USER_GUIDE_TAMIL.md           ← இந்த guide
├── README.md                     ← English documentation
│
├── data/
│   ├── trading.db                ← Database (signals, trades, events)
│   └── telegram_session.session  ← Telegram login (NEVER share!)
│
├── app/
│   ├── main.py             ← CLI (run, check, auth, status, dashboard...)
│   ├── config.py           ← .env settings load & validate
│   ├── telegram_listener.py← Telegram channel reader
│   ├── signal_parser.py    ← Signal text → Entry/Targets/SL/Leverage
│   ├── execution_engine.py ← Order submit logic
│   ├── position_monitor.py ← TP/SL management (CORE)
│   ├── mudrex_client.py    ← Mudrex API HTTP calls
│   ├── risk_manager.py     ← Fund check, sizing, deviation
│   ├── leverage_manager.py ← Leverage grid selection
│   ├── storage.py          ← SQLite database operations
│   ├── models.py           ← Data classes (TradeRecord, Signal...)
│   ├── dashboard.py        ← Web UI (HTML/JS)
│   ├── paper_broker.py     ← PAPER mode simulation
│   └── notifications.py    ← Telegram notify bot
│
└── scripts/
    ├── setup.sh            ← First time install
    ├── start.sh            ← Background start (PAPER)
    ├── stop.sh             ← Stop bot
    ├── status.sh           ← Quick status
    ├── reset_trades.sh     ← PAPER trade reset
    └── dashboard.sh        ← Dashboard start
```

### Database Tables:

| Table | என்ன store ஆகும் |
|---|---|
| `signals` | எல்லா Telegram signals (accepted/rejected/ignored) |
| `trades` | Open மற்றும் closed positions |
| `events` | Audit log (fill, TP hit, SL move, errors) |
| `meta` | Bot state (last message ID, channel ID) |

### Trade Status Values:

| Status | அர்த்தம் |
|---|---|
| `SUBMITTING` | Mudrex-க்கு send செய்கிறோம் |
| `ENTRY_PENDING` | Order open, fill ஆகவில்லை |
| `ENTRY_FILLED` | Position open, targets monitoring |
| `CLOSED` | Position close ஆனது |
| `CANCELLED` | Entry fill ஆகாமல் cancel |
| `NEEDS_REVIEW` | Error — manual attention தேவை |
| `REJECTED` | Order Mudrex reject |
| `LIQUIDATED` | Liquidation |

---

## 14. பாதுகாப்பு விதிகள்

### Secrets பாதுகாப்பு:

1. **`.env` file** — Git-ல் commit செய்யாதீர்கள்
2. **`telegram_session.session`** — யாருக்கும் share செய்யாதீர்கள்
3. **Mudrex API Key** — Withdrawal/Transfer scope வேண்டாம்
4. **API Key leak** ஆனால் — Mudrex-ல் உடனே revoke

### Financial Safety:

1. **PAPER mode-ல் பழகுங்கள்** — குறைந்தது 1 வாரம்
2. **Small amount-ல் start** — `MAX_MARGIN_USDT_PER_TRADE=10`
3. **Daily monitor** — `python -m app.main status`
4. **Emergency stop** — `python -m app.main kill`
5. **High leverage கவனம்** — Bot automatically limit செய்யும்

### Bot என்ன செய்யாது (Safety):

- Withdrawal / Transfer செய்யாது
- Stop-loss entry-போடும்போது வைக்காது (-75% trigger மட்டும்)
- Edited Telegram messages process செய்யாது
- Historical messages execute செய்யாது
- REAL mode-ல் automatic restart இல்லை (Manual confirmation)
- 5 நிமிடத்திற்கு மேல் பழைய signals reject
- Mutating API calls retry இல்லை (safety)

---

## 15. Real Trade Example — முழு Flow

### Signal வந்தது:

```
#XMR/USDT - Long
Entry: 540.07
Stop Loss: 520.92
Target 1: 550.45  (+1.9%)
Target 2: 560.90  (+3.9%)
Target 3: 600.15  (+11.1%)
Leverage: x14
```

### Bot என்ன செய்தது:

```
1. Signal parsed: XMRUSDT LONG ✅
2. Leverage: selected=14 (within asset max) ✅
3. Budget: 50 USDT margin ✅
4. Quantity: 50 × 14 ÷ 540.07 ≈ 1.29 XMR ✅
5. LIMIT order @ 540.07, qty=1.29 XMR ✅

--- 2 minutes later ---

6. ENTRY FILLED @ 540.07 ✅

7. TP Order 1: 0.43 XMR @ 550.45 (reduce-only) ✅
8. TP Order 2: 0.43 XMR @ 560.90 (reduce-only) ✅
9. TP Order 3: 0.43 XMR @ 600.15 (reduce-only) ✅

--- Target 1 hit ---

10. 0.43 XMR sold @ 550.45 ✅
11. SL → 540.07 (Break-Even) ✅
    Notification: "Target 1 HIT! 1/3 done. SL at 540.07"

--- Target 2 hit ---

12. 0.43 XMR sold @ 560.90 ✅
13. SL → 550.45 (Target 1 price) ✅
    Notification: "Target 2 HIT! 2/3 done. SL at 550.45"

--- Target 3 hit ---

14. 0.43 XMR sold @ 600.15 ✅
15. Position fully closed! ✅
    Notification: "Target 3 HIT! 3/3 done."
```

### Final P&L:

```
T1: 0.43 × (550.45 - 540.07) × 14 ≈ $62.5 profit
T2: 0.43 × (560.90 - 540.07) × 14 ≈ $125.4 profit
T3: 0.43 × (600.15 - 540.07) × 14 ≈ $361.8 profit
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Total ≈ $549 profit on $50 margin invested
```

### Trade தோற்றால் என்ன நடக்கும்:

```
1. Entry fill @ 540.07
2. Price கீழே போகிறது...
3. Bot monitor: margin_loss = -40% → OK, wait
4. Bot monitor: margin_loss = -75% → EMERGENCY!
5. SL placed @ 520.92 (Telegram signal SL price)
6. Price 520.92 hit → Position closed with loss
   Approximate loss: ~37.50 USDT (75% of 50 USDT margin)
```

---

## Support மற்றும் Help

### Problem வந்தால் செய்யுங்கள்:

1. `tail -f data/bot.log` → Log பாருங்கள்
2. `python -m app.main status` → Summary பாருங்கள்
3. Browser `http://localhost:8500` → Dashboard பாருங்கள்
4. `data/trading.db` → SQLite browser-ல் திறந்து events table பாருங்கள்

### Useful grep commands:

```bash
# எல்லா errors
grep "ERROR\|WARNING" data/bot.log

# Trade events மட்டும்
grep "entry_filled\|tp_hit\|sl_moved\|emergency" data/bot.log

# Rejected signals மட்டும்
grep "signal.rejected" data/bot.log
```

---

*Guide உருவாக்கப்பட்டது: 2026-10-13*
*Version: Multi-Target TP/SL System (Target 1–5 support)*

---

> **நினைவில் வைத்துக்கொள்ளுங்கள்:** Trading எப்போதும் risk உள்ளது. இந்த bot ஒரு tool மட்டுமே. Signal channel நம்பகமானதா என்று நீங்களே verify செய்யுங்கள். **உங்கள் பணத்திற்கு நீங்களே பொறுப்பு.**
