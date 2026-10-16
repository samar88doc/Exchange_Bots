#!/usr/bin/env bash
# ============================================================
# SHASHA_ACT — System Status Checker
# Shows operational health, workers, trading stats, & webhooks
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
PID_FILE="$BACKEND_DIR/app.pid"
ENV_FILE="$BACKEND_DIR/.env"

# Handle watch mode
if [ "${1:-}" = "-w" ] || [ "${1:-}" = "--watch" ]; then
  shift
  while true; do
    clear
    "$0" "$@"
    echo ""
    echo -e "\033[2mAuto-refreshing every 2s... (Press Ctrl+C to exit)\033[0m"
    sleep 2
  done
  exit 0
fi

SHOW_LOGS=false
SHOW_POSITIONS=false
if [ "${1:-}" = "-l" ] || [ "${1:-}" = "--logs" ]; then
  SHOW_LOGS=true
elif [ "${1:-}" = "-p" ] || [ "${1:-}" = "--positions" ]; then
  SHOW_POSITIONS=true
elif [ "${1:-}" = "-v" ] || [ "${1:-}" = "--verbose" ]; then
  SHOW_LOGS=true
  SHOW_POSITIONS=true
elif [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  echo "Usage: ./status.sh [OPTIONS]"
  echo ""
  echo "Options:"
  echo "  -w, --watch        Continuously refresh status every 2 seconds"
  echo "  -p, --positions    Show full table of all active open positions"
  echo "  -l, --logs         Display last 15 lines of backend logs"
  echo "  -v, --verbose      Display both all positions and recent logs"
  echo "  -h, --help         Show this help message"
  exit 0
fi

PYTHON_BIN="python3"
if [ -x "$BACKEND_DIR/.venv/bin/python" ]; then
  PYTHON_BIN="$BACKEND_DIR/.venv/bin/python"
fi

"$PYTHON_BIN" - "$SCRIPT_DIR" "$SHOW_LOGS" "$SHOW_POSITIONS" << 'PY_EOF'
import sys
import os
import json
import urllib.request
import urllib.error
import subprocess
from datetime import datetime

script_dir = sys.argv[1]
show_logs = sys.argv[2] == "true"
show_positions = sys.argv[3] == "true"

backend_dir = os.path.join(script_dir, "backend")
env_file = os.path.join(backend_dir, ".env")
pid_file = os.path.join(backend_dir, "app.pid")
log_file = os.path.join(backend_dir, "logs", "app.log")

# Colors
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_GREEN = "\033[0;32m"
C_RED = "\033[0;31m"
C_YELLOW = "\033[1;33m"
C_CYAN = "\033[0;36m"
C_BLUE = "\033[0;34m"
C_MAGENTA = "\033[0;35m"

def color_text(text, color):
    return f"{color}{text}{C_RESET}"

# Parse .env
env_vars = {}
if os.path.isfile(env_file):
    with open(env_file, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env_vars[k.strip()] = v.strip().strip("'\"")

port = int(env_vars.get("PORT", "8000"))
dashboard_token = env_vars.get("DASHBOARD_API_TOKEN", "")
webhook_token = env_vars.get("TRADINGVIEW_WEBHOOK_TOKEN", "")
auth_enabled = env_vars.get("DASHBOARD_AUTH_ENABLED", "true").lower() != "false"

# Check Process Status
pid = None
if os.path.isfile(pid_file):
    try:
        with open(pid_file, "r") as f:
            pid = int(f.read().strip())
    except Exception:
        pid = None

is_running = False
cpu_pct = ""
mem_pct = ""
uptime = ""

if pid:
    try:
        os.kill(pid, 0)
        is_running = True
        ps_out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "%cpu,%mem,etime", "--no-headers"],
            stderr=subprocess.DEVNULL
        ).decode().strip()
        parts = ps_out.split()
        if len(parts) >= 3:
            cpu_pct = f"{parts[0]}%"
            mem_pct = f"{parts[1]}%"
            uptime = parts[2]
    except (OSError, subprocess.SubprocessError):
        is_running = False

# Fallback: check if port 8000 is occupied
port_occupied = False
port_pids = []
try:
    fuser_out = subprocess.check_output(
        ["fuser", f"{port}/tcp"],
        stderr=subprocess.DEVNULL
    ).decode().strip()
    if fuser_out:
        port_occupied = True
        port_pids = [int(p) for p in fuser_out.split() if p.isdigit()]
except Exception:
    pass

# Helper for API requests
def fetch_json(url, headers=None, timeout=2):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None

base_url = f"http://127.0.0.1:{port}"
api_headers = {"X-Dashboard-Token": dashboard_token} if dashboard_token else {}

health_data = fetch_json(f"{base_url}/api/health")
ready_data = fetch_json(f"{base_url}/api/ready")
overview_data = fetch_json(f"{base_url}/api/overview", headers=api_headers)
positions_data = fetch_json(f"{base_url}/api/positions", headers=api_headers)

# Ngrok tunnel check
ngrok_url = ""
try:
    ngrok_data = fetch_json("http://127.0.0.1:4040/api/tunnels", timeout=1)
    if ngrok_data and "tunnels" in ngrok_data:
        for t in ngrok_data["tunnels"]:
            if t.get("proto") == "https" and "public_url" in t:
                ngrok_url = t["public_url"]
                break
except Exception:
    ngrok_url = ""

# PostgreSQL check
pg_online = False
try:
    rc = subprocess.call(["pg_isready"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pg_online = (rc == 0)
except Exception:
    pg_online = False

print("")
print("╔══════════════════════════════════════════════════════════╗")
print("║              SHASHA_ACT — SYSTEM STATUS                  ║")
print("╚══════════════════════════════════════════════════════════╝")
print("")

# 1. PROCESS & CORE SERVICES
print(f"{C_BOLD}► CORE SERVICES{C_RESET}")

if is_running:
    perf_info = f"CPU: {cpu_pct} | MEM: {mem_pct} | Uptime: {uptime}"
    print(f"  Backend Process  :  {C_GREEN}● Running{C_RESET} (PID {pid}) [{C_DIM}{perf_info}{C_RESET}]")
elif port_occupied:
    print(f"  Backend Process  :  {C_YELLOW}▲ Port {port} occupied{C_RESET} (PID {','.join(map(str, port_pids))})")
else:
    print(f"  Backend Process  :  {C_RED}○ Stopped{C_RESET} (Run {C_CYAN}./start.sh{C_RESET} to start)")

# API & Readiness
if ready_data and ready_data.get("ready"):
    print(f"  API Gateway      :  {C_GREEN}● Ready{C_RESET} (Port {port})")
elif health_data:
    st = health_data.get("status", "UNKNOWN")
    col = C_GREEN if st == "OK" else C_YELLOW
    print(f"  API Gateway      :  {col}● {st}{C_RESET} (Port {port})")
else:
    print(f"  API Gateway      :  {C_RED}○ Unreachable{C_RESET}")

# Database
db_status_str = "Unknown"
if health_data and "components" in health_data:
    db_comp = health_data["components"].get("database", "UNKNOWN")
    if db_comp == "OK":
        db_status_str = f"{C_GREEN}● Online{C_RESET}"
    else:
        db_status_str = f"{C_RED}○ {db_comp}{C_RESET}"
elif pg_online:
    db_status_str = f"{C_GREEN}● Online{C_RESET} (Service active)"
else:
    db_status_str = f"{C_RED}○ Offline{C_RESET}"
print(f"  PostgreSQL DB    :  {db_status_str}")

# Components from /api/health
if health_data and "components" in health_data:
    comps = health_data["components"]
    worker = comps.get("worker", "UNKNOWN")
    w_col = C_GREEN if worker == "RUNNING" else (C_YELLOW if worker == "DISABLED" else C_RED)
    print(f"  Strategy Worker  :  {w_col}● {worker}{C_RESET}")

    ws = comps.get("websocket", "UNKNOWN")
    mkt = comps.get("market_data", "UNKNOWN")
    ws_col = C_GREEN if ws == "CONNECTED" else C_YELLOW
    print(f"  Shark WebSocket  :  {ws_col}● {ws}{C_RESET} (Feed: {mkt})")

    tg = comps.get("telegram", "UNKNOWN")
    tg_col = C_GREEN if tg == "CONFIGURED" else C_DIM
    print(f"  Telegram Bot     :  {tg_col}● {tg}{C_RESET}")

    # Outbox Queue
    outbox = health_data.get("outbox", {})
    pending = outbox.get("pending", 0) or 0
    processing = outbox.get("processing", 0) or 0
    failed = outbox.get("failed", 0) or 0
    q_col = C_RED if failed > 0 else (C_YELLOW if pending > 5 else C_GREEN)
    print(f"  Outbox Queue     :  {q_col}{pending} Pending | {processing} Processing | {failed} Failed{C_RESET}")

print("")

# 2. TRADING & BOT ACTIVITY
if overview_data:
    print(f"{C_BOLD}► TRADING ENGINE & PERFORMANCE{C_RESET}")
    mode = overview_data.get("mode", "UNKNOWN")
    mode_badge = f"{C_CYAN}[{mode}]{C_RESET}" if mode == "PAPER" else f"{C_MAGENTA}[{mode}]{C_RESET}"
    print(f"  Execution Mode   :  {mode_badge}")

    trades_today = overview_data.get("trades_today", 0)
    wins = overview_data.get("wins_today", 0)
    losses = overview_data.get("losses_today", 0)
    open_pos = overview_data.get("open_positions", 0)
    pending_ent = overview_data.get("pending_entries", 0)

    all_time = overview_data.get("all_time_stats", {})
    win_rate = all_time.get("win_rate")
    wr_str = f" | All-Time Win Rate: {win_rate:.1f}%" if win_rate is not None else ""

    print(f"  Positions / Orders: {C_BOLD}{open_pos}{C_RESET} Open Positions | {pending_ent} Pending Entries")
    print(f"  Today's Trades   :  {trades_today} Trades ({C_GREEN}{wins} Won{C_RESET} / {C_RED}{losses} Lost{C_RESET}){wr_str}")

    day_pnl = overview_data.get("day_pnl", {})
    if day_pnl:
        realized_net = day_pnl.get("realized_net", "0")
        try:
            r_val = float(realized_net)
            pnl_col = C_GREEN if r_val > 0 else (C_RED if r_val < 0 else C_DIM)
            sign = "+" if r_val > 0 else ""
            gross = day_pnl.get("gross", "0")
            fees = day_pnl.get("fees_today", "0")
            print(f"  Realized Net P&L :  {pnl_col}{C_BOLD}{sign}{r_val:.4f} USDT{C_RESET} (Gross: {gross} USDT, Fees: {fees} USDT)")
        except Exception:
            print(f"  Realized Net P&L :  {realized_net} USDT")

    if positions_data and "summary" in positions_data:
        summ = positions_data["summary"]
        u_pnl = summ.get("total_unrealized_pnl")
        if u_pnl is not None:
            try:
                u_val = float(u_pnl)
                u_col = C_GREEN if u_val > 0 else (C_RED if u_val < 0 else C_DIM)
                u_sign = "+" if u_val > 0 else ""
                margin = summ.get("total_margin", "0")
                print(f"  Unrealized P&L   :  {u_col}{u_sign}{u_val:.4f} USDT{C_RESET} (Total Margin: {margin} USDT)")
            except Exception:
                pass
    print("")

# 3. OPEN POSITIONS TABLE
items = []
if positions_data:
    items = positions_data.get("items") or positions_data.get("positions") or []

if items:
    print(f"{C_BOLD}► ACTIVE POSITIONS ({len(items)}){C_RESET}")
    print(f"  {C_DIM}{'SYMBOL':<14} {'DIR':<6} {'ENTRY':<10} {'LAST':<10} {'STOP':<10} {'TARGET 1':<10} {'uPnL (USDT)':<14} {'STAGE':<10}{C_RESET}")
    print("  " + "─" * 86)
    display_list = items if show_positions else items[:6]
    for p in display_list:
        sym = str(p.get("symbol") or "-")
        direction = str(p.get("direction") or "-")
        entry = str(p.get("actual_entry") or p.get("planned_entry") or "-")
        last = str(p.get("last_price") or "-")
        stop = str(p.get("stop_price") or "-")
        target = str(p.get("target1_price") or "-")
        stage = str(p.get("stop_stage") or "-")
        u_pnl_val = p.get("unrealized_pnl")

        if u_pnl_val is not None:
            try:
                u_f = float(u_pnl_val)
                u_col = C_GREEN if u_f > 0 else (C_RED if u_f < 0 else C_DIM)
                u_raw = f"{u_f:+.4f}"
                u_styled = color_text(f"{u_raw:<14}", u_col)
            except Exception:
                u_styled = f"{str(u_pnl_val):<14}"
        else:
            u_styled = f"{'-':<14}"

        dir_col = C_GREEN if direction == "LONG" else C_RED
        dir_styled = color_text(f"{direction:<6}", dir_col)

        print(f"  {sym:<14} {dir_styled} {entry:<10} {last:<10} {stop:<10} {target:<10} {u_styled} {stage:<10}")

    if len(items) > 6 and not show_positions:
        print(f"  {C_DIM}... and {len(items) - 6} more positions. Run './status.sh -p' to view all.{C_RESET}")
    print("")

# 4. NETWORK & WEBHOOKS
print(f"{C_BOLD}► ENDPOINTS & INGRESS{C_RESET}")
print(f"  Dashboard UI     :  {C_CYAN}http://localhost:{port}/{C_RESET}")
if auth_enabled:
    if dashboard_token:
        print(f"  Dashboard Auth   :  {C_GREEN}Enabled{C_RESET} (Token configured in .env)")
    else:
        print(f"  Dashboard Auth   :  {C_YELLOW}Enabled (Token missing in .env){C_RESET}")
else:
    print(f"  Dashboard Auth   :  {C_YELLOW}Disabled (Direct access){C_RESET}")

if ngrok_url:
    print(f"  Public Ngrok URL :  {C_GREEN}{ngrok_url}{C_RESET}")
    if webhook_token:
        print(f"  Public Webhook   :  {C_GREEN}{ngrok_url}/api/webhooks/tradingview/{webhook_token}{C_RESET}")
else:
    print(f"  Public Ngrok URL :  {C_DIM}Inactive (Run ngrok for public TradingView webhook){C_RESET}")

if webhook_token:
    print(f"  Local Webhook    :  {C_DIM}http://localhost:{port}/api/webhooks/tradingview/{webhook_token}{C_RESET}")

print("")

# 5. LOGS SUMMARY
print(f"{C_BOLD}► SYSTEM LOGS{C_RESET}")
if os.path.isfile(log_file):
    size_mb = os.path.getsize(log_file) / (1024 * 1024)
    mtime = datetime.fromtimestamp(os.path.getmtime(log_file)).strftime("%Y-%m-%d %H:%M:%S")
    print(f"  Log File         :  {log_file} ({size_mb:.2f} MB, modified {mtime})")
    print(f"  Tail Live Logs   :  {C_CYAN}tail -f {log_file}{C_RESET}")

    if show_logs:
        print("")
        print(f"  {C_DIM}── Last 15 log lines ──────────────────────────────────────{C_RESET}")
        try:
            tail_lines = subprocess.check_output(
                ["tail", "-n", "15", log_file],
                stderr=subprocess.DEVNULL
            ).decode().strip().split("\n")
            for line in tail_lines:
                print(f"  {C_DIM}{line}{C_RESET}")
        except Exception:
            pass
else:
    print(f"  Log File         :  {C_DIM}No log file found at {log_file}{C_RESET}")

print("")
PY_EOF
