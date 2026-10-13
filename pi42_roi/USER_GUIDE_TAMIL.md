# 📘 Pi42 Signal Trader — பயனர் வழிகாட்டி (தமிழ்)

> **புதியவர்களுக்கான முழுமையான வழிகாட்டி**
> இந்த ஆவணம் தமிழில் எழுதப்பட்டுள்ளது. இது **Pi42 broker**-க்கான bot. Mudrex bot-ஐ விட ஒரு
> முக்கிய வேறுபாடு உண்டு: **இங்கு stop-loss *எப்போதுமே* போடப்படாது** (Mudrex பதிப்பில்
> இருந்த trailing/emergency SL இங்கு இல்லை). கீழே முழுவதும் படியுங்கள்.

---

## ⚠️ முக்கியமான எச்சரிக்கை (படிக்காமல் தாண்டாதீர்கள்!)

> **இந்த bot real பணத்தில் trade செய்யும். தவறாக configure செய்தால் நீங்கள் allocate செய்த
> margin முழுவதும் போகலாம்.**
>
> - **Stop-loss *எப்போதுமே* வைக்கப்படாது.** விலை எதிர்த் திசையில் போனால் position திறந்தே இருக்கும்;
>   Pi42-ன் liquidation engine மட்டுமே அதை மூடும்.
> - `MAX_MARGIN_PER_TRADE` என்பது **allocate செய்யப்படும் margin** — இது "maximum loss" அல்ல.
> - Leverage அதிகம் என்றால் liquidation மிக வேகமாக வரும்.
> - **முதலில் PAPER mode-ல் நீண்ட நேரம் பழகுங்கள்.** REAL mode-ஐ அவசரமாக ஆரம்பிக்காதீர்கள்.
> - API key-ல் **withdrawal / transfer permission** கொடுக்கவேண்டாம். இந்த bot-ல் பணம்
>   நகர்த்தும் code **இல்லவே இல்லை** — futures wallet-க்கு நீங்களே Pi42 app-ல் fund transfer செய்ய வேண்டும்.

---

## 📋 உள்ளடக்கம்

1. இந்த Bot என்ன செய்கிறது?
2. System எப்படி வேலை செய்கிறது?
3. Signal Format எப்படி இருக்கும்?
4. Trade Management — ROI Target மற்றும் "SL இல்லை" விதி
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
15. ஒரு Trade-ன் முழு பயணம் (உதாரணம்)

---

## 1. இந்த Bot என்ன செய்கிறது?

Telegram **signal channel**-ல் வரும் signals-ஐ படித்து, **Pi42 Futures**-ல் தானாகவே
LIMIT order போட்டு, **ROI target** அடையும் வரை position-ஐ கண்காணிக்கும்.

- Signal-ஐ **சொந்த கணக்கு** மூலம் (MTProto) படிக்கும் — bot account தேவையில்லை.
- **புதிய** செய்திகள் மட்டுமே execute ஆகும்; திருத்தப்பட்ட (edited) செய்திகள் ignore.
- Bot-ஐ முதல் முறை start செய்யும்போது, பழைய history execute ஆகாது (baseline எடுக்கப்படும்).
- ஒரே Telegram message **இரண்டு முறை** execute ஆகாது (`channel_id + message_id` unique).
- Pi42-ல் சந்தேகம் வரும்போது **எதையும் மறுபடி அனுப்பாது** — `NEEDS_REVIEW` என்று நிறுத்தி
  உங்கள் முடிவுக்கு விடும்.

---

## 2. System எப்படி வேலை செய்கிறது?

### முக்கிய Parts:

| Part | வேலை |
|---|---|
| `telegram_listener.py` | Telegram channel-ஐ படிக்கும் |
| `signal_parser.py` | Message-ஐ பிரித்து symbol, entry, target, leverage எடுக்கும் |
| `symbol_mapper.py` | `#CRV/USDT` → Pi42-ன் `CRVUSDT` contract (துல்லியமான match மட்டும்) |
| `leverage_manager.py` | `min(Telegram leverage, contract maximum)` — எப்போதும் கீழே round |
| `risk_manager.py` | Margin budget-க்குள் quantity கணக்கு; deviation/funds/concurrency சோதனை |
| `execution_engine.py` | Order-ஐ Pi42-க்கு அனுப்பும் (PAPER-ல் அனுப்பாது) |
| `pi42_client.py` | Pi42 API client (HMAC-SHA256 signature) — default-ல் read-only |
| `paper_broker.py` | PAPER mode simulation (fill, ROI exit, TTL, liquidation estimate) |
| `position_monitor.py` | Fill/TP/TTL/reconciliation கண்காணிப்பு |
| `dashboard.py` | Browser-ல் status dashboard (போர்ட் 8500) |

### Signal வந்தால் என்ன நடக்கும்:

1. செய்தி database-ல் பதிவு (`signals` table) — duplicate என்றால் நிற்கும்.
2. Parse → symbol/entry/target/leverage. தவறான format என்றால் `REJECTED`/`IGNORED`.
3. சோதனைகள்: பழைய signal? (`MAX_SIGNAL_AGE_MINUTES`) · kill switch? · இதே symbol-ல் ஏற்கனவே
   trade? · 24 மணி நேரத்தில் இதே content-ஐ ஏற்கனவே ஏற்றதா?
4. Pi42 contract + live price (public ticker) → entry/target snap, deviation சோதனை,
   leverage set, quantity size, ROI target-ஐ Pi42 ஏற்குமா (instant trigger ஆகாதா) என்று சோதனை.
5. PAPER: simulated trade `ENTRY_PENDING`. REAL: leverage+margin mode-ஐ Pi42-ல் set செய்து,
   அது **உறுதிப்படுத்தப்பட்ட பிறகே** LIMIT order (take-profit உடன்) அனுப்பப்படும்.

---

## 3. Signal Format எப்படி இருக்கும்?

```
#CRV/USDT - Short🔴

Entry: 0.3462

Stop Loss: 0.3662

Target 1: 0.3361
Target 2: 0.32627
Target 3: 0.28398

Leverage: x14
```

- **கட்டாயம் தேவை:** symbol (`#BASE/USDT` அல்லது `#BASE/INR`), `Long/Buy` அல்லது `Short/Sell`
  என்ற வார்த்தை, `Entry`, `Target 1`, `Leverage: xN`.
- **Stop Loss வரிசை விருப்பம்** — அது database-ல் *reference* ஆக மட்டுமே பதிவாகும்.
  Pi42-க்கு **எப்போதுமே அனுப்பப்படாது**.
- Target 1-ன் திசை சரியாக இருக்க வேண்டும்: LONG-ல் entry-க்கு மேல், SHORT-ல் கீழ்.
- Emoji மட்டும் இருந்தால் direction ஏற்கப்படாது.
- `Target 2..5` பதிவாகும், ஆனால் **exit-க்கு பயன்படாது** (பிரிவு 4 பார்க்கவும்).

---

## 4. Trade Management — ROI Target மற்றும் "SL இல்லை" விதி

### Exit எப்படி நடக்கிறது?

Entry ஆனது LIMIT விலையில், அதனுடன் ஒரு **take-profit** இணைக்கப்படும்:

```
LONG  target = entry × (1 + TARGET_ROI_PCT/100)
SHORT target = entry × (1 − TARGET_ROI_PCT/100)
```

Default `TARGET_ROI_PCT=8.0` → entry 0.3462 (SHORT) எனில் target ≈ **0.3185**.
இந்த விலை வந்தால் Pi42 position-ஐ மூடும்; Pi42 மூடவில்லை என்றால் bot **reduce-only MARKET**
order மூலம் square-off செய்யும் (exit_reason = `TARGET_1_HIT`).

> **Telegram-ல் உள்ள Target 1/2/3 விலைகள் exit-க்கு பயன்படாது.** exit எப்போதும் ROI சதவீதத்தில்.
> இது `mudrex_roi` project-ன் அதே design.

### Trailing Stop Loss / Emergency SL இல்லை

- **இந்த bot SL *ஒருபோதும்* வைக்காது** — Mudrex பதிப்பில் இருந்த trailing SL, -75% emergency
  SL போன்ற அமைப்புகள் இங்கு **வேண்டுமென்றே இல்லை**.
- Dashboard-ல் "emergency_sl_price" என்ற field `-` என்று காட்டப்படும். அது information மட்டுமே;
  Pi42-க்கு அனுப்பப்படுவதில்லை.
- விலை எதிர்த்திசையில் சென்றால் position திறந்தே இருக்கும். எனவே **margin மற்றும் leverage-ஐ
  கவனமாக தேர்வு செய்யுங்கள்**.

### TTL — நிரப்பப்படாத order

`LIMIT_ORDER_TTL_MINUTES` (default 120) நிமிடங்களில் entry fill ஆகவில்லை என்றால் order cancel
செய்யப்படும். Cancel செய்வதற்கு முன் order **இன்னும் unfilled** என்று மீண்டும் உறுதிப்படுத்தப்படும் —
ஏற்கனவே fill ஆன order ஒருபோதும் cancel ஆகாது.

---

## 5. Setup — முதல் முறை நிறுவுதல்

### தேவையானவை:

- Python **3.11+**
- Telegram account (signal channel-ல் ஏற்கனவே உறுப்பினராக இருக்க வேண்டும்)
- Pi42 account + API key (IP whitelist உடன்)
- Pi42 **futures wallet**-ல் margin (Pi42 app-ல் நீங்களே transfer செய்ய வேண்டும்)

### Step 1: Setup script

```bash
cd pi42_roi
bash scripts/setup.sh
```

இது `.venv` உருவாக்கி, dependencies நிறுவி, database schema உருவாக்கி, **test suite-ஐ ஓட்டும்**
(அனைத்து tests-ம் offline-ல் ஓடும், network தேவையில்லை).

### Step 2: `.env` file

```bash
cp .env.example .env
nano .env      # அல்லது உங்கள் editor
```

`.env`-ஐ **எப்போதும் உங்கள் கணினியில் மட்டும்** வைத்திருங்கள். Chat/ஸ்கிரீன்ஷாட்/git-ல் பகிராதீர்கள்.

### Step 3: Pi42 API Key

Pi42 → API management → புதிய key:

1. **உங்கள் server-ன் public IP-ஐ whitelist செய்யுங்கள்.** இல்லாவிட்டால் Pi42 request-ஐ நிராகரிக்கும்.
2. Key id → `PI42_API_KEY`; Secret → `PI42_API_SECRET`.
3. PAPER-க்கு **read-only** permission போதும். REAL-க்கு trading permission தேவை.
   **Withdrawal/transfer permission எப்போதும் வேண்டாம்.**
4. Pi42-ல் **Funding wallet**-ல் இருக்கும் பணத்தை **Futures wallet**-க்கு transfer செய்யுங்கள் —
   position margin அங்கிருந்தே எடுக்கப்படும்.

### Step 4: Telegram credentials

https://my.telegram.org → *API development tools* → `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`,
`TELEGRAM_PHONE` (`+919876543210` format) `.env`-ல் இடுங்கள்.

### Step 5: Telegram login (ஒரு முறை மட்டும்)

```bash
source .venv/bin/activate
python -m app.main auth
```

Login code-ஐ locally கேட்கும்; session `data/telegram_session.session`-ல் சேமிக்கப்படும்.
அந்த file-ஐ யாருக்கும் கொடுக்காதீர்கள்.

### Step 6: சோதனை

```bash
python -m app.main check              # config + Pi42 read-only checks
python -m app.main contracts          # Pi42 contracts, max leverage, precision
```

---

## 6. `.env` Configuration விவரம்

### Mode Settings:

```bash
TRADING_MODE=PAPER       # PAPER = simulation (real பணம் இல்லை)
TRADING_ENABLED=true     # false = புதிய trades இல்லை (monitoring தொடரும்)
ENABLE_REAL_TRADING=false
LIVE_TRADING_ACKNOWLEDGEMENT=
```

### Pi42 Settings:

```bash
PI42_API_KEY=
PI42_API_SECRET=
PI42_BASE_URL=https://fapi.pi42.com        # signed endpoints
PI42_PUBLIC_BASE_URL=https://api.pi42.com  # public ticker
PI42_MARKET=USDT          # INR signals வேண்டுமானால் INR
PI42_MARGIN_ASSET=USDT    # margin currency (BTCINR -க்கு USDT-ம் ஆகும்)
PI42_MARGIN_MODE=ISOLATED # CROSS வேண்டாம் (முழு wallet-ம் ஆபத்தில் இருக்கும்)
PI42_SEND_REF_ID=true     # idempotent orders (response போனாலும் order தேட முடியும்)
```

### Risk & Sizing:

```bash
MAX_MARGIN_PER_TRADE=50   # ஒரு trade-க்கு allocate ஆகும் margin (max loss அல்ல!)
MARGIN_RESERVE=2          # budget-லிருந்து விலக்கி வைக்கப்படும் தொகை
MAX_CONCURRENT_POSITIONS=50
MAX_SIGNAL_AGE_MINUTES=5  # இதற்கு மேல் பழைய signal reject
MAX_ENTRY_DEVIATION_PCT=3 # market vs entry வித்தியாசம் இதற்கு மேல் என்றால் skip
LIMIT_ORDER_TTL_MINUTES=120
TARGET_ROI_PCT=8.0        # exit = entry ± இந்த சதவீதம்
```

### PAPER simulation:

```bash
PAPER_INITIAL_BALANCE=1000
PAPER_FEE_RATE=0.0005              # Pi42 taker fee ~0.05%
PAPER_MAINTENANCE_MARGIN_RATE=0.005
```

### Telegram (MTProto):

```bash
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_PHONE=
TELEGRAM_SESSION_NAME=./data/telegram_session
TELEGRAM_CHANNEL_INVITE_URL=https://t.me/+Xh7OTE_G_jcxMDE0
```

### Storage & runtime:

```bash
DATABASE_PATH=./data/trading.db
MONITOR_POLL_SECONDS=15
CONTRACT_CACHE_TTL_SECONDS=600
PI42_REQUESTS_PER_MINUTE=50
PI42_DELETE_REQUESTS_PER_MINUTE=25
DASHBOARD_PORT=8500
LOG_LEVEL=INFO
```

---

## 7. PAPER Mode — பயிற்சி செய்வது எப்படி?

```bash
source .venv/bin/activate
bash scripts/run_paper.sh      # foreground-ல் PAPER (real order இல்லை)
# அல்லது background:
scripts/start.sh
scripts/status.sh
tail -f data/bot.log
scripts/stop.sh
```

Log-ல் `PAPER MODE — NO LIVE ORDERS WILL BE PLACED` என்ற banner இருக்கும்.
PAPER-ல்:

- விலை entry-ஐ கடக்கும்போது LIMIT fill ஆகும் (limit விலையிலேயே).
- ROI target tick-ல் மட்டுமே take-profit நடக்கும் (fill ஆன பிறகு).
- Fill ஆகாத order TTL-க்குப் பிறகு `EXPIRED`.
- Liquidation ஒரு **approximate** calculation (Pi42 engine அல்ல).
- Fees ஒரு flat rate; funding/slippage/order-book depth model செய்யப்படவில்லை.

### PAPER trades-ஐ reset செய்ய:

```bash
scripts/stop.sh
scripts/reset_trades.sh          # dry run
scripts/reset_trades.sh --yes    # PAPER trades மட்டும் நீக்கும்
```

Backup `data/backups/trading-<timestamp>.db`-ல் இருக்கும். Signals (மறுபடி execute ஆகாமல்
இருக்க), REAL trades, Telegram session — எல்லாம் அப்படியே இருக்கும்.

---

## 8. REAL Mode — உண்மையான Trading

### REAL-க்கு முன் checklist:

- [ ] குறைந்தது 1–2 வாரம் PAPER-ல் ஓட்டியிருக்கிறேன்; signal → order → exit எல்லாம் புரிகிறது.
- [ ] Pi42 futures wallet-ல் எவ்வளவு margin வைக்கிறேன் என்று முடிவு செய்துவிட்டேன்.
- [ ] `MAX_MARGIN_PER_TRADE`-ஐ சிறிய அளவில் (உ.ம். 5) வைத்திருக்கிறேன்.
- [ ] `MARGIN_RESERVE` பூஜ்ஜியத்திற்கு மேல் இருக்கிறது.
- [ ] **Stop-loss இல்லை** என்பதையும், position திறந்தே இருக்கும் என்பதையும் புரிந்துகொண்டேன்.

### Enable செய்வது:

```bash
TRADING_MODE=REAL
ENABLE_REAL_TRADING=true
LIVE_TRADING_ACKNOWLEDGEMENT=I ACCEPT NO-STOP-LOSS LIVE TRADING RISK
```

### Start:

```bash
source .venv/bin/activate
python -m app.main check               # REAL gate problems இல்லை என்று உறுதிப்படுத்துங்கள்
python -m app.main run                 # terminal-ல் 'START LIVE TRADING' என்று type செய்யுங்கள்
```

- TTY (interactive terminal) இல்லாமல் REAL start ஆகாது.
- `--yes` flag gates எல்லாம் சரியாக இருந்தால் மட்டுமே prompt-ஐ தவிர்க்கும்.
- systemd service (`deploy/`) **PAPER-க்கு மட்டுமே** — REAL-க்கு பயன்படுத்தாதீர்கள்.

### REAL Mode-ல் Bot எப்படி நடந்துகொள்ளும்:

1. Leverage + margin mode-ஐ Pi42-ல் set செய்யும்; Pi42 அதை உறுதிப்படுத்தினால் மட்டுமே order போகும்.
2. Open position / open order ஏற்கனவே இருந்தால் **order போடாது** (reject).
3. Trade row **முதலில்** database-ல் `SUBMITTING` ஆக பதிவு ஆகும்; பிறகே Pi42 call.
4. Response கிடைக்கவில்லை என்றால் (`timeout`/5xx): `refId` → `clientOrderId` → order-history
   fingerprint என்று தேடும். கிடைக்கவில்லை என்றால் `NEEDS_REVIEW` — **மறுபடி order போடாது**.
5. Take-profit confirm ஆகவில்லை என்றால் `TAKE-PROFIT NOT CONFIRMED` என்று log ஆகும் (பொய்யாக
   "சரி" என்று சொல்லாது).

### NEEDS_REVIEW trade-ஐ கைமுறையாக முடிக்க:

```bash
python -m app.main resolve-trade 7 --status CANCELLED --note "Pi42-ல் order இல்லை என verify செய்தேன்"
```

---

## 9. Dashboard பார்ப்பது எப்படி?

```bash
scripts/dashboard.sh
# Browser: http://<server-ip>:8500   (localhost:8500)
```

Dashboard-ல்:

- **Live Positions** — ROI target, அதுவரை உள்ள இலாபம்/நஷ்டம், margin loss %.
- **Orders** — Pi42-ல் working orders + bot-ன் scheduled ROI exit leg.
- **Telegram Messages** — raw text, parse ஆன values, ஏன் accept/reject ஆனது.
- **Ledger** — PAPER-ல் simulated balance; REAL-ல் Pi42 futures wallet (read-only).
- **Activity Log** — events.
- **Manual actions** — ROI target-ஐ மாற்று (working order-ஐ amend / open position-க்கு
  split take-profit), trade-ஐ close செய், order-ஐ cancel செய்.

Dashboard-ல் **stop-loss set செய்யும் வழி இல்லை** — வேண்டுமென்றே.

---

## 10. Commands Reference

| Command | என்ன செய்யும் |
|---|---|
| `python -m app.main run` | bot-ஐ நேரடியாக ஓட்டும் (mode `.env`-ல்) |
| `python -m app.main check [--telegram]` | config + Pi42 read-only checks |
| `python -m app.main contracts` | Pi42 contracts, leverage, precision |
| `python -m app.main auth` | Telegram login (ஒரு முறை) |
| `python -m app.main status` | signals, trades, PAPER results, events |
| `python -m app.main dashboard` | web dashboard (8500) |
| `python -m app.main kill` / `resume` | புதிய trades-ஐ நிறுத்து / அனுமதி (position monitoring தொடரும்) |
| `python -m app.main parse-file msg.txt` | ஒரு message-ஐ offline-ல் parse செய்து பார் |
| `python -m app.main reset-paper [--yes]` | PAPER trades-ஐ மட்டும் நீக்கும் |
| `python -m app.main resolve-trade ID --status S --note "..."` | NEEDS_REVIEW trade-ஐ முடி |
| `scripts/start.sh` / `scripts/stop.sh` | background-ல் start / stop |
| `scripts/status.sh` | ஓடுகிறதா? database summary |

Exit codes: `2` gate/prompt நிராகரிப்பு · `3` read-only check தோல்வி · `4` Telegram fatal error.

---

## 11. Log மற்றும் Monitoring

```bash
tail -f data/bot.log              # background bot-ன் log
grep "signal.accepted\|real.order_accepted\|real.roi_target_hit" data/bot.log
grep "NEEDS_REVIEW\|ref_id_already_exists\|tp_not_confirmed" data/bot.log
```

Log-ல் முக்கிய events: `signal.parsed`, `signal.validated`, `signal.rejected`, `paper.entry_pending`,
`real.order_accepted`, `real.entry_filled`, `real.tp_confirmed`, `real.roi_target_hit`,
`real.ttl_cancelled`, `real.reconcile_not_found`, `paper.reset`.

---

## 12. Problem Solving

| பிரச்சனை | தீர்வு |
|---|---|
| `check` → `transport error` | Internet/DNS, `PI42_BASE_URL` சரியா, Pi42 IP whitelist |
| `check` → 401 / invalid signature | `PI42_API_KEY`/`PI42_API_SECRET` தவறு; key regenerate செய்யுங்கள் |
| `2305` / `2306` leverage error | அந்த contract-ல் ஏற்கனவே order/position திறந்திருக்கிறது — அதை முதலில் முடியுங்கள் |
| `3018 insufficient margin` | Futures wallet-ல் margin குறைவு — Pi42 app-ல் transfer செய்யுங்கள் (bot செய்யாது) |
| `3011` instant trigger | ROI target live விலையை ஏற்கனவே கடந்துவிட்டது → signal skip (விலை மாற்றப்படாது) |
| `3019 ref id already exists` | அதே signal-க்கு order ஏற்கனவே இருக்கிறது → bot அதை adopt செய்யும், இரட்டை order இல்லை |
| Telegram login இல்லை | `python -m app.main auth` |
| Channel resolve ஆகவில்லை | `TELEGRAM_CHANNEL_INVITE_URL` சரியா? account உறுப்பினரா? |
| Trade `NEEDS_REVIEW`-ல் நின்றது | Pi42 history-ஐ சரிபார்த்து `resolve-trade` மூலம் முடியுங்கள் |
| Dashboard திறக்கவில்லை | `DASHBOARD_PORT` (default 8500) firewall-ல் திறந்திருக்கிறதா? |

---

## 13. Project Files புரிதல்

```
pi42_roi/
├── app/                 # முழு application code
│   ├── main.py          # CLI (run/check/status/dashboard/…)
│   ├── config.py        # .env settings + REAL gates
│   ├── pi42_client.py   # Pi42 API client (default read-only)
│   ├── execution_engine.py, position_monitor.py, paper_broker.py
│   ├── signal_parser.py, symbol_mapper.py, leverage_manager.py, risk_manager.py
│   └── dashboard.py     # web dashboard (stdlib only)
├── tests/               # offline tests (fake Pi42 + signature verification)
├── data/                # database, Telegram session, logs (git-ல் போகாது)
├── scripts/             # setup / start / stop / status / dashboard / reset
├── deploy/              # systemd unit (PAPER மட்டும்)
├── .env.example         # எல்லா settings-ம் comment உடன்
└── README.md, USER_GUIDE_TAMIL.md
```

---

## 14. பாதுகாப்பு விதிகள்

1. **PAPER-ல் முதலில் பழகு.** அவசரப்பட்டு REAL-க்கு போகாதே.
2. `.env`-ஐ commit செய்யாதே; ஸ்கிரீன்ஷாட்டில் காட்டாதே; chat-ல் பகிராதே.
3. API key-ல் withdrawal/transfer permission **வேண்டாம்**.
4. Pi42-ல் IP whitelist வை.
5. Futures wallet-ல் தேவையான margin மட்டும் வை — "extra" பணம் funding wallet-ல் இருக்கட்டும்.
6. `MAX_MARGIN_PER_TRADE`-ஐ சிறியதாக வை; `MARGIN_RESERVE` > 0.
7. அதிக leverage (x20+) வேண்டாம் — liquidation வேகமாகும்.
8. ஒரு database-ஐ ஒரே நேரத்தில் இரு bot instance பயன்படுத்தக்கூடாது.
9. **Stop-loss இல்லை** என்பதை நினைவில் வை — அதனால்தான் position size சிறியதாக இருக்க வேண்டும்.
10. சந்தேகம் வந்தால்: `python -m app.main kill` → புதிய trades நிற்கும், ஏற்கனவே உள்ள position
    தொடர்ந்து monitor ஆகும்.

---

## 15. ஒரு Trade-ன் முழு பயணம் (உதாரணம்)

```
Telegram: #CRV/USDT - Short | Entry: 0.3462 | SL: 0.3662 | Target 1: 0.3361 | Leverage: x14
```

| Step | என்ன நடக்கும் |
|---|---|
| 1 | Message பதிவு → parse: CRVUSDT SHORT, entry 0.3462, leverage 14 |
| 2 | Pi42 contract `CRVUSDT` (max 20x) → leverage = min(14,20) = **14** |
| 3 | Margin budget = 50 − 2 = **48**; quantity = 48×14/0.3462 = **1941** (0.1 step-க்கு கீழே round) |
| 4 | ROI target = 0.3462 × 0.92 = **0.3185** (Pi42-ன் 4-decimal precision-க்கு snap) |
| 5 | Deviation check: market 0.3462 ±3% உள்ளே → OK |
| 6 | PAPER: simulated `ENTRY_PENDING`; REAL: leverage set → LIMIT order (TP 0.3185, refId `TG-…`) |
| 7 | விலை 0.3470 ஆனால் → entry fill (limit விலையில்) |
| 8 | விலை 0.3185 வந்தால் → **ROI target hit** → position close (`TARGET_1_HIT`) |
| 9 | 120 நிமிடம் fill ஆகவில்லை என்றால் → order cancel (`CANCELLED`) |
| 10 | எதுவும் சந்தேகம் என்றால் → `NEEDS_REVIEW` (மறுபடி order இல்லை) |

**நினைவில்:** Stop-loss இல்லை. Target வரை வரவில்லை என்றால் position திறந்தே இருக்கும்.

---

> இந்த ஆவணம் `pi42_roi` திட்டத்திற்கானது. எந்த மாற்றமும் செய்வதற்கு முன் `README.md`-ஐயும்
> `python -m pytest -q` test-result-ஐயும் பாருங்கள். சந்தேகம் இருந்தால் PAPER-ல் தொடருங்கள்.
