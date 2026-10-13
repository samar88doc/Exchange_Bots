"""Lightweight, standalone, production-grade web dashboard for Mudrex Signal Trader.

Serves an interactive dark-mode terminal at http://localhost:8500 with zero
external dependencies (pure Python standard library: http.server).
Provides clear, real-time details of:
1. Live Positions (multi-targets, hit statuses, live PnL, margin risk, stop loss)
2. Open & Scheduled Orders (exchange orders and multi-target partial close legs)
3. Telegram Messages (full raw text, parsed trade parameters, latency, decision reasons)
4. Account Ledger & Closed Trades History
5. Activity Event Logs
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from app.config import Settings, load_env_file
from app.models import ACTIVE_TRADE_STATUSES, Mode, TradeStatus
from app.paper_broker import PaperBroker
from app.storage import Storage

logger = logging.getLogger("dashboard")

# Caches to avoid throttling Mudrex endpoints
_LIVE_PRICE_CACHE: dict[str, tuple[float, Decimal]] = {}
_PRICE_CACHE_TTL = 2.0  # seconds

_LIVE_FUNDS_CACHE: tuple[float, dict[str, Any]] | None = None
_FUNDS_CACHE_TTL = 3.0  # seconds

_LIVE_ORDERS_CACHE: tuple[float, list[dict[str, Any]]] | None = None
_ORDERS_CACHE_TTL = 2.5  # seconds

_LIVE_POSITIONS_CACHE: tuple[float, list[dict[str, Any]]] | None = None
_POSITIONS_CACHE_TTL = 2.5  # seconds


def _get_live_price(symbol: str, base_url: str, secret: str) -> Decimal | None:
    now_ts = time.time()
    cached = _LIVE_PRICE_CACHE.get(symbol)
    if cached and (now_ts - cached[0] < _PRICE_CACHE_TTL):
        return cached[1]
    try:
        url = f"{base_url}/fapi/v1/futures/{symbol}?is_symbol"
        headers = {"X-Authentication": secret} if secret else {}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            raw_p = data.get("data", {}).get("price")
            if raw_p is not None:
                val = Decimal(str(raw_p))
                _LIVE_PRICE_CACHE[symbol] = (now_ts, val)
                return val
    except Exception as exc:
        logger.debug("Failed to get live price for %s: %s", symbol, exc)
    return cached[1] if cached else None


def _get_live_funds(base_url: str, secret: str, currency: str = "USDT") -> dict[str, Any] | None:
    global _LIVE_FUNDS_CACHE
    now_ts = time.time()
    if _LIVE_FUNDS_CACHE and (now_ts - _LIVE_FUNDS_CACHE[0] < _FUNDS_CACHE_TTL):
        return _LIVE_FUNDS_CACHE[1]
    try:
        url = f"{base_url}/fapi/v1/futures/funds?trade_currency={currency}"
        headers = {"X-Authentication": secret} if secret else {}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            funds = data.get("data", {})
            if funds:
                res = {
                    "balance": float(funds.get("balance", 0)),
                    "locked": float(funds.get("locked_amount", 0)),
                }
                _LIVE_FUNDS_CACHE = (now_ts, res)
                return res
    except Exception as exc:
        logger.debug("Failed to get live funds: %s", exc)
    return _LIVE_FUNDS_CACHE[1] if _LIVE_FUNDS_CACHE else None


def _get_live_orders(base_url: str, secret: str, currency: str = "USDT") -> list[dict[str, Any]]:
    global _LIVE_ORDERS_CACHE
    now_ts = time.time()
    if _LIVE_ORDERS_CACHE and (now_ts - _LIVE_ORDERS_CACHE[0] < _ORDERS_CACHE_TTL):
        return _LIVE_ORDERS_CACHE[1]
    try:
        url = f"{base_url}/fapi/v1/futures/orders?trade_currency={currency}&limit=100"
        headers = {"X-Authentication": secret} if secret else {}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            orders = data.get("data", [])
            if isinstance(orders, list):
                _LIVE_ORDERS_CACHE = (now_ts, orders)
                return orders
    except Exception as exc:
        logger.debug("Failed to get live orders: %s", exc)
    return _LIVE_ORDERS_CACHE[1] if _LIVE_ORDERS_CACHE else []


def _get_live_positions(base_url: str, secret: str, currency: str = "USDT") -> list[dict[str, Any]]:
    global _LIVE_POSITIONS_CACHE
    now_ts = time.time()
    if _LIVE_POSITIONS_CACHE and (now_ts - _LIVE_POSITIONS_CACHE[0] < _POSITIONS_CACHE_TTL):
        return _LIVE_POSITIONS_CACHE[1]
    try:
        url = f"{base_url}/fapi/v1/futures/positions?trade_currency={currency}&limit=100"
        headers = {"X-Authentication": secret} if secret else {}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            positions = data.get("data", [])
            if isinstance(positions, list):
                _LIVE_POSITIONS_CACHE = (now_ts, positions)
                return positions
    except Exception as exc:
        logger.debug("Failed to get live positions: %s", exc)
    return _LIVE_POSITIONS_CACHE[1] if _LIVE_POSITIONS_CACHE else []


def _is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _get_bot_status(data_dir: Path) -> dict[str, Any]:
    pid_file = data_dir / "bot.pid"
    if not pid_file.is_file():
        return {"running": False, "pid": None}
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        if _is_process_alive(pid):
            return {"running": True, "pid": pid}
        return {"running": False, "pid": None, "stale_pid": pid}
    except Exception:
        return {"running": False, "pid": None}


def _calc_emergency_sl_price(entry: Decimal, direction: str, leverage: Decimal) -> Decimal:
    """Calculate the price at which unrealized loss reaches -75% of margin."""
    if leverage <= Decimal("0"):
        leverage = Decimal("1")
    ratio = Decimal("0.75") / leverage
    if direction.upper() == "LONG":
        return entry * (Decimal("1") - ratio)
    return entry * (Decimal("1") + ratio)


def _format_latency(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 0:
        return "0s"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"


def _fetch_dashboard_data(settings: Settings) -> dict[str, Any]:
    data_dir = Path(settings.database_path).parent
    bot_state = _get_bot_status(data_dir)
    kill_switch_active = settings.kill_switch_file.exists() or not settings.trading_enabled

    storage = Storage(settings.database_file)
    try:
        # 1. Signals
        signals_rows = storage.recent_signals(limit=50)
        signal_counts = storage.signal_counts()

        formatted_signals = []
        for s in signals_rows:
            lat_sec: float | None = None
            try:
                r_at = s.get("received_at")
                s_at = s.get("signal_time")
                if r_at and s_at:
                    r_dt = datetime.fromisoformat(str(r_at).replace("Z", "+00:00")) if isinstance(r_at, str) else r_at
                    s_dt = datetime.fromisoformat(str(s_at).replace("Z", "+00:00")) if isinstance(s_at, str) else s_at
                    lat_sec = max(0.0, (r_dt - s_dt).total_seconds())
            except Exception:
                lat_sec = None

            # Collect targets
            all_targets = []
            for k in ("target1", "target2", "target3", "target4", "target5"):
                v = s.get(k)
                if v is not None and str(v).strip():
                    all_targets.append(str(v))

            formatted_signals.append({
                "id": s.get("id"),
                "channel_id": s.get("channel_id"),
                "message_id": s.get("message_id"),
                "received_at": str(s.get("received_at")),
                "signal_time": str(s.get("signal_time")),
                "raw_text": s.get("raw_text") or "",
                "symbol": s.get("symbol") or "-",
                "direction": s.get("direction") or "-",
                "entry": str(s.get("entry")) if s.get("entry") else "-",
                "target1": str(s.get("target1")) if s.get("target1") else None,
                "target2": str(s.get("target2")) if s.get("target2") else None,
                "target3": str(s.get("target3")) if s.get("target3") else None,
                "target4": str(s.get("target4")) if s.get("target4") else None,
                "target5": str(s.get("target5")) if s.get("target5") else None,
                "targets": all_targets,
                "stop_loss": str(s.get("stop_loss")) if s.get("stop_loss") else None,
                "telegram_leverage": str(s.get("telegram_leverage")) if s.get("telegram_leverage") else None,
                "status": s.get("status") or "UNKNOWN",
                "decision_reason": s.get("decision_reason") or "",
                "latency_seconds": lat_sec,
                "formatted_latency": _format_latency(lat_sec),
            })

        # 2. Trades
        active_trades_objs = storage.active_trades()
        all_trades_objs = storage.trades()

        # Query Mudrex positions if in REAL mode
        mudrex_positions_by_id: dict[str, dict[str, Any]] = {}
        if settings.trading_mode == "REAL":
            for p in _get_live_positions(settings.mudrex_base_url, settings.mudrex_api_secret, settings.trade_currency):
                pid = str(p.get("id") or p.get("position_id") or "")
                if pid:
                    mudrex_positions_by_id[pid] = p

        active_trades = []
        total_unrealized_pnl = Decimal("0")

        for t in active_trades_objs:
            live_price = _get_live_price(t.symbol, settings.mudrex_base_url, settings.mudrex_api_secret)

            # Targets list with status
            hits: set[int] = set()
            if t.targets_hit:
                try:
                    hits = set(json.loads(t.targets_hit))
                except Exception:
                    hits = set()

            raw_targets = [t.target1, t.target2, t.target3, t.target4, t.target5]
            valid_targets = [(i + 1, tgt) for i, tgt in enumerate(raw_targets) if tgt is not None and tgt > Decimal("0")]

            targets_detail = []
            for num, tgt in valid_targets:
                diff = (tgt - t.entry_price) * (Decimal("1") if t.direction == "LONG" else Decimal("-1"))
                pct = round(float((diff / t.entry_price) * Decimal("100")), 2) if t.entry_price else 0.0
                targets_detail.append({
                    "target_num": num,
                    "price": str(tgt),
                    "hit": num in hits,
                    "target_pct": pct,
                })

            # Calculate live PnL & Margin risk
            pnl_usdt = Decimal("0")
            roe_pct = 0.0
            margin_loss_pct = 0.0
            if live_price is not None and t.entry_price > Decimal("0"):
                diff = (live_price - t.entry_price) * (Decimal("1") if t.direction == "LONG" else Decimal("-1"))
                if t.quantity > Decimal("0"):
                    pnl_usdt = diff * t.quantity
                elif t.est_margin > Decimal("0") and t.leverage > Decimal("0"):
                    pnl_usdt = (diff / t.entry_price) * t.est_margin * t.leverage

                roe_pct = round(float((diff / t.entry_price) * t.leverage * Decimal("100")), 2)
                if t.est_margin > Decimal("0"):
                    # positive number means loss
                    margin_loss_pct = round(float((-pnl_usdt / t.est_margin) * Decimal("100")), 2)

                total_unrealized_pnl += pnl_usdt

            # Emergency SL price at -75% margin loss
            em_sl = _calc_emergency_sl_price(t.entry_price, t.direction, t.leverage)
            em_sl_dist: float | None = None
            if live_price is not None and live_price > Decimal("0"):
                if t.direction == "LONG":
                    em_sl_dist = round(float(((live_price - em_sl) / live_price) * Decimal("100")), 2)
                else:
                    em_sl_dist = round(float(((em_sl - live_price) / live_price) * Decimal("100")), 2)

            # Determine active SL type
            if 2 in hits:
                sl_mode = "TRAILING"
                sl_display_text = f"Trailing SL (Target 1: ${t.target1})"
            elif 1 in hits:
                sl_mode = "BREAK_EVEN"
                sl_display_text = f"Break-Even SL (Entry: ${t.entry_price})"
            else:
                sl_mode = "EMERGENCY_75_PCT"
                sl_display_text = f"Emergency SL at -75% Risk (${em_sl:.4f})"

            active_trades.append({
                "id": t.id,
                "signal_id": t.signal_id,
                "mode": t.mode,
                "symbol": t.symbol,
                "direction": t.direction,
                "entry_price": str(t.entry_price),
                "current_price": str(live_price) if live_price is not None else None,
                "target1": str(t.target1),
                "target2": str(t.target2) if t.target2 else None,
                "target3": str(t.target3) if t.target3 else None,
                "target4": str(t.target4) if t.target4 else None,
                "target5": str(t.target5) if t.target5 else None,
                "targets_detail": targets_detail,
                "targets_hit": sorted(list(hits)),
                "leverage": str(t.leverage),
                "quantity": str(t.quantity),
                "notional": str(t.notional),
                "est_margin": str(t.est_margin),
                "liquidation_price": str(t.liquidation_price) if t.liquidation_price else None,
                "status": t.status,
                "status_reason": t.status_reason or "",
                "position_id": t.position_id,
                "order_id": t.order_id,
                "client_order_id": t.client_order_id,
                "sl_price": str(t.sl_price) if t.sl_price else None,
                "stop_loss_reference": str(t.stop_loss_reference) if t.stop_loss_reference else None,
                "emergency_sl_price": f"{em_sl:.4f}",
                "emergency_sl_distance_pct": em_sl_dist,
                "margin_loss_pct": margin_loss_pct,
                "sl_mode": sl_mode,
                "sl_display_text": sl_display_text,
                "unrealized_pnl": round(float(pnl_usdt), 2),
                "roe_pct": roe_pct,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "filled_at": t.filled_at.isoformat() if t.filled_at else None,
                "closed_at": t.closed_at.isoformat() if t.closed_at else None,
            })

        # 3. Orders: Combine Live Mudrex Orders + Scheduled Bot Exit Legs
        combined_orders: list[dict[str, Any]] = []

        # 3a. Live open exchange orders from Mudrex
        if settings.trading_mode == "REAL":
            raw_mudrex_orders = _get_live_orders(settings.mudrex_base_url, settings.mudrex_api_secret, settings.trade_currency)
            for ro in raw_mudrex_orders:
                combined_orders.append({
                    "id": str(ro.get("id") or ro.get("order_id") or ""),
                    "client_order_id": str(ro.get("client_order_id") or ""),
                    "source": "EXCHANGE",
                    "symbol": str(ro.get("symbol") or "").upper(),
                    "side": str(ro.get("order_type") or "").upper(),
                    "order_type": str(ro.get("trigger_type") or ro.get("order_type") or "LIMIT").upper(),
                    "milestone": "Exchange Working Order",
                    "price": str(ro.get("price") or ro.get("order_price") or "-"),
                    "quantity": str(ro.get("quantity") or "0"),
                    "filled_quantity": str(ro.get("filled_quantity") or "0"),
                    "status": str(ro.get("status") or "OPEN").upper(),
                    "created_at": str(ro.get("created_at") or "-"),
                })

        # 3b. Scheduled bot target exit legs & pending entry orders
        for t in active_trades_objs:
            hits = set(json.loads(t.targets_hit)) if t.targets_hit else set()
            valid_targets = [(i + 1, tgt) for i, tgt in enumerate([t.target1, t.target2, t.target3, t.target4, t.target5]) if tgt is not None and tgt > Decimal("0")]
            n_tgts = len(valid_targets)

            if t.status == TradeStatus.ENTRY_PENDING.value:
                combined_orders.append({
                    "id": t.order_id or f"ENTRY-{t.id}",
                    "client_order_id": t.client_order_id,
                    "source": "BOT_ENTRY",
                    "symbol": t.symbol,
                    "side": t.direction,
                    "order_type": "LIMIT ENTRY",
                    "milestone": "Limit Entry Fill",
                    "price": str(t.entry_price),
                    "quantity": str(t.quantity),
                    "filled_quantity": "0",
                    "status": "PENDING_FILL",
                    "created_at": t.created_at.isoformat() if t.created_at else "-",
                })

            if n_tgts > 0 and t.status in (TradeStatus.ENTRY_FILLED.value, TradeStatus.SUBMITTING.value):
                base_qty = t.quantity / Decimal(n_tgts)
                for num, tgt in valid_targets:
                    is_hit = num in hits
                    leg_qty = base_qty if num < n_tgts else (t.quantity - base_qty * Decimal(n_tgts - 1))
                    is_last = (num == n_tgts)
                    milestone_text = f"Target {num} {'Final Square-off' if is_last else 'Partial Exit'} ({int(100/n_tgts)}%)"
                    combined_orders.append({
                        "id": f"LEG-T{num}-{t.id}",
                        "client_order_id": f"TG-{t.id}-T{num}",
                        "source": "BOT_TARGET_LEG",
                        "symbol": t.symbol,
                        "side": "BUY" if t.direction == "SHORT" else "SELL",
                        "order_type": f"TAKE_PROFIT ({settings.target_order_type})",
                        "milestone": milestone_text,
                        "price": str(tgt),
                        "quantity": str(leg_qty),
                        "filled_quantity": str(leg_qty) if is_hit else "0",
                        "status": "FILLED" if is_hit else "SCHEDULED",
                        "created_at": t.filled_at.isoformat() if t.filled_at else t.created_at.isoformat(),
                    })

        # 4. Closed history
        closed_trades = []
        for t in reversed(all_trades_objs):
            if t.trade_status not in ACTIVE_TRADE_STATUSES:
                closed_trades.append({
                    "id": t.id,
                    "signal_id": t.signal_id,
                    "mode": t.mode,
                    "symbol": t.symbol,
                    "direction": t.direction,
                    "entry_price": str(t.entry_price),
                    "exit_price": str(t.exit_price) if t.exit_price else None,
                    "exit_reason": t.exit_reason or t.status_reason or t.status,
                    "realized_pnl": float(t.realized_pnl) if t.realized_pnl is not None else None,
                    "status": t.status,
                    "leverage": str(t.leverage),
                    "est_margin": str(t.est_margin),
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                    "closed_at": t.closed_at.isoformat() if t.closed_at else None,
                })

        # 5. Events
        events = storage.recent_events(limit=50)

        # 6. Performance & Ledger
        closed_trades_list = [
            t for t in all_trades_objs
            if t.status in (TradeStatus.CLOSED.value, TradeStatus.LIQUIDATED.value)
        ]
        wins = [t for t in closed_trades_list if (t.realized_pnl or Decimal(0)) > Decimal(0)]
        losses = [t for t in closed_trades_list if (t.realized_pnl or Decimal(0)) < Decimal(0)]
        total_pnl = sum((t.realized_pnl or Decimal(0)) for t in closed_trades_list)
        win_rate = (len(wins) / len(closed_trades_list) * 100.0) if closed_trades_list else 0.0

        ledger_info: dict[str, Any] = {}
        if settings.trading_mode == "PAPER":
            broker = PaperBroker(
                storage,
                initial_balance=settings.paper_initial_balance_usdt,
                fee_rate=settings.paper_fee_rate,
                maintenance_margin_rate=settings.paper_maintenance_margin_rate,
                ttl_minutes=settings.limit_order_ttl_minutes,
            )
            raw_ledger = broker.ledger()
            ledger_info = {
                "is_real": False,
                "title": "Simulated Balance",
                "initial": float(raw_ledger["initial"]),
                "realized_pnl": float(raw_ledger["realized_pnl"]),
                "reserved_margin": float(raw_ledger["locked_margin"]),
                "available": float(raw_ledger["available"]),
                "equity": float(raw_ledger["initial"] + raw_ledger["realized_pnl"] - raw_ledger["open_entry_fees"]),
            }
        else:
            live_funds = _get_live_funds(settings.mudrex_base_url, settings.mudrex_api_secret, settings.trade_currency)
            bal = float(live_funds["balance"]) if live_funds else 0.0
            locked = float(live_funds["locked"]) if live_funds else float(len(active_trades) * settings.max_margin_usdt_per_trade)
            avail = max(0.0, bal - locked)
            ledger_info = {
                "is_real": True,
                "title": "Real Futures Wallet",
                "initial": bal,
                "realized_pnl": float(total_pnl),
                "reserved_margin": locked,
                "available": avail,
                "equity": bal,
            }

        return {
            "server_time": datetime.now(timezone.utc).isoformat(),
            "bot": {
                "running": bot_state["running"],
                "pid": bot_state["pid"],
                "mode": settings.trading_mode,
                "kill_switch_active": kill_switch_active,
                "trading_enabled": settings.trading_enabled,
                "max_concurrent_positions": settings.max_concurrent_positions,
                "max_margin_per_trade": float(settings.max_margin_usdt_per_trade),
                "margin_reserve": float(settings.margin_reserve_usdt),
                "trade_currency": settings.trade_currency,
                "channel_url": settings.telegram_channel_invite_url,
                "database_path": str(settings.database_file),
                "target_order_type": settings.target_order_type,
            },
            "metrics": {
                "active_count": len(active_trades),
                "max_positions": settings.max_concurrent_positions,
                "total_trades": len(all_trades_objs),
                "closed_count": len(closed_trades_list),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": round(win_rate, 1),
                "total_realized_pnl": float(total_pnl),
                "total_unrealized_pnl": round(float(total_unrealized_pnl), 2),
                "signals_total": len(signals_rows),
                "signals_accepted": signal_counts.get("ACCEPTED", 0),
                "signals_rejected": sum(v for k, v in signal_counts.items() if k != "ACCEPTED"),
                "open_orders_count": len(combined_orders),
            },
            "ledger": ledger_info,
            "active_trades": active_trades,
            "positions": active_trades,
            "orders": combined_orders,
            "recent_signals": formatted_signals,
            "closed_trades": closed_trades[:50],
            "events": events,
        }
    finally:
        storage.close()


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Mudrex Signal Trader — Professional Futures Terminal</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Outfit:wght@500;600;700;800&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-base: #06090F;
      --bg-surface: #0B101D;
      --bg-card: rgba(14, 21, 38, 0.76);
      --bg-card-hover: rgba(22, 32, 58, 0.88);
      --border-subtle: rgba(255, 255, 255, 0.08);
      --border-focus: rgba(0, 245, 155, 0.4);

      --accent-green: #00F59B;
      --accent-green-glow: rgba(0, 245, 155, 0.35);
      --accent-green-bg: rgba(0, 245, 155, 0.12);

      --accent-red: #FF3B5C;
      --accent-red-glow: rgba(255, 59, 92, 0.35);
      --accent-red-bg: rgba(255, 59, 92, 0.12);

      --accent-blue: #00D2FF;
      --accent-blue-bg: rgba(0, 210, 255, 0.12);

      --accent-amber: #FFAE00;
      --accent-amber-bg: rgba(255, 174, 0, 0.12);

      --accent-purple: #A855F7;
      --accent-purple-bg: rgba(168, 85, 247, 0.14);

      --accent-tg: #229ED9;
      --accent-tg-bg: rgba(34, 158, 217, 0.14);

      --text-main: #F3F6FC;
      --text-sub: #94A3B8;
      --text-muted: #5A6A85;

      --font-display: 'Outfit', sans-serif;
      --font-body: 'Plus Jakarta Sans', sans-serif;
      --font-mono: 'JetBrains Mono', monospace;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      background: radial-gradient(circle at 50% -15%, #132238 0%, var(--bg-base) 70%);
      color: var(--text-main);
      font-family: var(--font-body);
      min-height: 100vh;
      line-height: 1.5;
      overflow-x: hidden;
    }

    /* Glow backdrop points */
    .glow-point {
      position: fixed;
      width: 520px;
      height: 520px;
      border-radius: 50%;
      pointer-events: none;
      z-index: 0;
      opacity: 0.14;
      filter: blur(140px);
    }
    .glow-1 { top: -120px; left: 10%; background: #00F59B; }
    .glow-2 { top: 180px; right: 5%; background: #00D2FF; }

    .container {
      max-width: 1540px;
      margin: 0 auto;
      padding: 20px 28px 60px;
      position: relative;
      z-index: 1;
    }

    /* Header */
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 16px 24px;
      background: var(--bg-card);
      backdrop-filter: blur(18px);
      border: 1px solid var(--border-subtle);
      border-radius: 18px;
      margin-bottom: 22px;
      box-shadow: 0 14px 34px rgba(0, 0, 0, 0.45);
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 14px;
    }

    .logo-badge {
      width: 44px;
      height: 44px;
      background: linear-gradient(135deg, #00F59B 0%, #0077B6 100%);
      border-radius: 12px;
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 0 22px rgba(0, 245, 155, 0.35);
    }

    .logo-badge svg {
      width: 24px;
      height: 24px;
      fill: #06090F;
    }

    .brand-title h1 {
      font-family: var(--font-display);
      font-size: 1.35rem;
      font-weight: 700;
      letter-spacing: -0.02em;
      color: #FFF;
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .brand-title p {
      font-size: 0.78rem;
      color: var(--text-sub);
    }

    .header-status {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }

    /* Badges & Pills */
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      padding: 5px 13px;
      border-radius: 999px;
      font-size: 0.74rem;
      font-weight: 600;
      letter-spacing: 0.03em;
      text-transform: uppercase;
      border: 1px solid transparent;
      transition: all 0.2s ease;
    }

    .pill-running {
      background: var(--accent-green-bg);
      color: var(--accent-green);
      border-color: rgba(0, 245, 155, 0.3);
      box-shadow: 0 0 14px rgba(0, 245, 155, 0.2);
    }

    .pill-stopped {
      background: var(--accent-red-bg);
      color: var(--accent-red);
      border-color: rgba(255, 59, 92, 0.3);
    }

    .pill-mode {
      background: var(--accent-blue-bg);
      color: var(--accent-blue);
      border-color: rgba(0, 210, 255, 0.25);
    }

    .pill-real {
      background: rgba(0, 245, 155, 0.16);
      color: #00F59B;
      border-color: rgba(0, 245, 155, 0.45);
      font-weight: 700;
    }

    .pill-purple {
      background: var(--accent-purple-bg);
      color: var(--accent-purple);
      border-color: rgba(168, 85, 247, 0.35);
    }

    .pill-tg {
      background: var(--accent-tg-bg);
      color: var(--accent-tg);
      border-color: rgba(34, 158, 217, 0.35);
    }

    .pill-kill-on {
      background: var(--accent-amber-bg);
      color: var(--accent-amber);
      border-color: rgba(255, 174, 0, 0.3);
    }

    .pulse-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: currentColor;
      box-shadow: 0 0 8px currentColor;
      animation: pulse 1.6s infinite ease-in-out;
    }

    @keyframes pulse {
      0%, 100% { transform: scale(1); opacity: 1; }
      50% { transform: scale(1.4); opacity: 0.6; }
    }

    .refresh-control {
      display: flex;
      align-items: center;
      gap: 8px;
      padding-left: 8px;
      border-left: 1px solid var(--border-subtle);
    }

    .refresh-select {
      background: rgba(255, 255, 255, 0.05);
      color: var(--text-main);
      border: 1px solid var(--border-subtle);
      border-radius: 8px;
      padding: 5px 8px;
      font-size: 0.75rem;
      outline: none;
      cursor: pointer;
    }

    .btn-refresh {
      background: rgba(255, 255, 255, 0.05);
      color: var(--text-main);
      border: 1px solid var(--border-subtle);
      padding: 6px 12px;
      border-radius: 8px;
      cursor: pointer;
      font-size: 0.78rem;
      font-weight: 600;
      display: flex;
      align-items: center;
      gap: 6px;
      transition: all 0.2s;
    }

    .btn-refresh:hover {
      background: rgba(255, 255, 255, 0.1);
      border-color: rgba(255, 255, 255, 0.2);
    }

    .btn-refresh svg {
      width: 14px;
      height: 14px;
      transition: transform 0.4s ease;
    }
    .btn-refresh.spin svg {
      transform: rotate(360deg);
    }

    /* KPI Grid */
    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 16px;
      margin-bottom: 22px;
    }

    .kpi-card {
      background: var(--bg-card);
      backdrop-filter: blur(14px);
      border: 1px solid var(--border-subtle);
      border-radius: 16px;
      padding: 18px 20px;
      position: relative;
      overflow: hidden;
      transition: transform 0.2s, border-color 0.2s;
    }

    .kpi-card:hover {
      transform: translateY(-2px);
      border-color: rgba(255, 255, 255, 0.15);
    }

    .kpi-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 10px;
    }

    .kpi-title {
      font-size: 0.78rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--text-sub);
    }

    .kpi-icon {
      width: 30px;
      height: 30px;
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.05);
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--text-sub);
    }

    .kpi-value {
      font-family: var(--font-display);
      font-size: 1.85rem;
      font-weight: 700;
      letter-spacing: -0.02em;
      line-height: 1.1;
      margin-bottom: 6px;
      color: #FFF;
    }

    .kpi-sub {
      font-size: 0.77rem;
      color: var(--text-muted);
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }

    .badge-pos { color: var(--accent-green); font-weight: 600; }
    .badge-neg { color: var(--accent-red); font-weight: 600; }

    .capacity-bar {
      width: 100%;
      height: 6px;
      background: rgba(255, 255, 255, 0.06);
      border-radius: 99px;
      margin-top: 10px;
      overflow: hidden;
    }

    .capacity-fill {
      height: 100%;
      background: linear-gradient(90deg, #00D2FF, #00F59B);
      border-radius: 99px;
      width: 0%;
      transition: width 0.4s ease;
    }

    /* Navigation Tabs */
    .tabs-nav {
      display: flex;
      gap: 8px;
      background: rgba(11, 16, 29, 0.85);
      padding: 6px;
      border-radius: 14px;
      border: 1px solid var(--border-subtle);
      margin-bottom: 20px;
      overflow-x: auto;
    }

    .tab-btn {
      background: transparent;
      color: var(--text-sub);
      border: none;
      padding: 10px 18px;
      border-radius: 9px;
      font-size: 0.88rem;
      font-weight: 600;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 8px;
      transition: all 0.2s;
      white-space: nowrap;
    }

    .tab-btn:hover {
      color: #FFF;
      background: rgba(255, 255, 255, 0.05);
    }

    .tab-btn.active {
      color: #FFF;
      background: rgba(255, 255, 255, 0.12);
      box-shadow: 0 4px 14px rgba(0, 0, 0, 0.35);
    }

    .tab-badge {
      padding: 2px 7px;
      border-radius: 99px;
      font-size: 0.72rem;
      background: rgba(255, 255, 255, 0.1);
      color: #FFF;
      font-weight: 700;
    }

    /* Table & Card Panels */
    .panel {
      background: var(--bg-card);
      backdrop-filter: blur(14px);
      border: 1px solid var(--border-subtle);
      border-radius: 18px;
      overflow: hidden;
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.35);
      display: none;
    }

    .panel.active {
      display: block;
      animation: fadeIn 0.2s ease-in;
    }

    @keyframes fadeIn {
      from { opacity: 0; transform: translateY(4px); }
      to { opacity: 1; transform: translateY(0); }
    }

    .panel-header {
      padding: 18px 24px;
      border-bottom: 1px solid var(--border-subtle);
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 12px;
    }

    .panel-header h2 {
      font-family: var(--font-display);
      font-size: 1.15rem;
      font-weight: 700;
      color: #FFF;
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .panel-controls {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .search-input {
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid var(--border-subtle);
      border-radius: 8px;
      padding: 6px 12px;
      color: #FFF;
      font-size: 0.82rem;
      outline: none;
      min-width: 180px;
      transition: border-color 0.2s;
    }
    .search-input:focus {
      border-color: var(--accent-green);
    }

    .filter-btn-group {
      display: flex;
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid var(--border-subtle);
      border-radius: 8px;
      padding: 2px;
      gap: 2px;
    }

    .filter-btn {
      background: transparent;
      border: none;
      color: var(--text-sub);
      font-size: 0.76rem;
      font-weight: 600;
      padding: 5px 10px;
      border-radius: 6px;
      cursor: pointer;
      transition: all 0.15s;
    }
    .filter-btn.active {
      background: rgba(255, 255, 255, 0.12);
      color: #FFF;
    }

    /* POSITIONS: Grid Cards View */
    .positions-cards-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(460px, 1fr));
      gap: 20px;
      padding: 24px;
    }

    .pos-card {
      background: rgba(11, 17, 32, 0.85);
      border: 1px solid var(--border-subtle);
      border-radius: 16px;
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 16px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.3);
      position: relative;
      overflow: hidden;
      transition: transform 0.2s, border-color 0.2s;
    }
    .pos-card:hover {
      transform: translateY(-2px);
      border-color: rgba(255, 255, 255, 0.18);
    }

    .pos-card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    .pos-card-title {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .pos-symbol {
      font-family: var(--font-display);
      font-size: 1.25rem;
      font-weight: 800;
      letter-spacing: -0.01em;
      color: #FFF;
    }

    .pos-main-metrics {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 12px;
      background: rgba(255, 255, 255, 0.025);
      border: 1px solid var(--border-subtle);
      border-radius: 12px;
      padding: 12px 14px;
    }

    .pos-metric-box {
      display: flex;
      flex-direction: column;
      gap: 2px;
    }
    .pos-metric-lbl {
      font-size: 0.72rem;
      color: var(--text-sub);
      text-transform: uppercase;
      letter-spacing: 0.04em;
      font-weight: 600;
    }
    .pos-metric-val {
      font-family: var(--font-mono);
      font-size: 0.98rem;
      font-weight: 700;
      color: #FFF;
    }

    /* Target Stepper Pipeline */
    .stepper-container {
      background: rgba(255, 255, 255, 0.02);
      border: 1px solid var(--border-subtle);
      border-radius: 12px;
      padding: 14px 16px;
    }
    .stepper-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
      font-size: 0.76rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-sub);
    }
    .stepper-track {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      position: relative;
      gap: 8px;
    }
    .stepper-track::before {
      content: '';
      position: absolute;
      top: 14px;
      left: 20px;
      right: 20px;
      height: 2px;
      background: rgba(255, 255, 255, 0.08);
      z-index: 0;
    }

    .stepper-node {
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 6px;
      z-index: 1;
      flex: 1;
      text-align: center;
    }

    .node-icon {
      width: 28px;
      height: 28px;
      border-radius: 50%;
      background: #0E1527;
      border: 2px solid rgba(255, 255, 255, 0.18);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 0.7rem;
      font-weight: 700;
      color: var(--text-sub);
      transition: all 0.2s;
    }
    .stepper-node.hit .node-icon {
      background: var(--accent-green);
      border-color: #00F59B;
      color: #06090F;
      box-shadow: 0 0 12px rgba(0, 245, 155, 0.45);
    }
    .stepper-node.active-next .node-icon {
      border-color: var(--accent-blue);
      color: var(--accent-blue);
      box-shadow: 0 0 10px rgba(0, 210, 255, 0.35);
      animation: pulse 1.5s infinite;
    }

    .node-label {
      font-size: 0.72rem;
      font-weight: 700;
      color: var(--text-main);
    }
    .node-price {
      font-family: var(--font-mono);
      font-size: 0.75rem;
      color: var(--text-sub);
    }
    .node-badge {
      font-size: 0.65rem;
      padding: 1px 6px;
      border-radius: 99px;
      font-weight: 700;
      text-transform: uppercase;
    }
    .node-badge-hit {
      background: var(--accent-green-bg);
      color: var(--accent-green);
      border: 1px solid rgba(0, 245, 155, 0.3);
    }
    .node-badge-pending {
      background: rgba(255, 255, 255, 0.05);
      color: var(--text-muted);
    }

    /* Risk / Stop-loss shield box */
    .risk-shield-box {
      background: rgba(255, 255, 255, 0.02);
      border: 1px solid var(--border-subtle);
      border-radius: 12px;
      padding: 12px 14px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .risk-shield-top {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 0.76rem;
    }
    .risk-meter-bar {
      width: 100%;
      height: 6px;
      background: rgba(255, 255, 255, 0.06);
      border-radius: 99px;
      overflow: hidden;
      position: relative;
    }
    .risk-meter-fill {
      height: 100%;
      border-radius: 99px;
      transition: width 0.3s;
    }

    .pos-card-footer {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-top: 10px;
      border-top: 1px solid rgba(255, 255, 255, 0.05);
    }

    /* Table View styles */
    .table-container {
      width: 100%;
      overflow-x: auto;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      text-align: left;
      font-size: 0.84rem;
    }

    th {
      background: rgba(11, 16, 29, 0.95);
      color: var(--text-muted);
      font-weight: 600;
      text-transform: uppercase;
      font-size: 0.72rem;
      letter-spacing: 0.05em;
      padding: 12px 18px;
      border-bottom: 1px solid var(--border-subtle);
      white-space: nowrap;
    }

    td {
      padding: 13px 18px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.035);
      color: var(--text-main);
      vertical-align: middle;
      white-space: nowrap;
    }

    tr:hover td {
      background: var(--bg-card-hover);
    }

    .mono {
      font-family: var(--font-mono);
    }

    /* Tags */
    .tag-dir {
      font-weight: 700;
      font-size: 0.72rem;
      padding: 3px 8px;
      border-radius: 6px;
      text-transform: uppercase;
      letter-spacing: 0.03em;
    }
    .tag-long {
      background: var(--accent-green-bg);
      color: var(--accent-green);
      border: 1px solid rgba(0, 245, 155, 0.3);
    }
    .tag-short {
      background: var(--accent-red-bg);
      color: var(--accent-red);
      border: 1px solid rgba(255, 59, 92, 0.3);
    }

    .tag-status {
      font-size: 0.74rem;
      font-weight: 600;
      padding: 3px 9px;
      border-radius: 99px;
      display: inline-flex;
      align-items: center;
      gap: 5px;
    }
    .tag-status-open { background: var(--accent-blue-bg); color: var(--accent-blue); }
    .tag-status-pending { background: var(--accent-amber-bg); color: var(--accent-amber); }
    .tag-status-filled { background: var(--accent-green-bg); color: var(--accent-green); }
    .tag-status-cancelled { background: rgba(255, 255, 255, 0.08); color: var(--text-sub); }
    .tag-status-scheduled { background: var(--accent-purple-bg); color: var(--accent-purple); }

    .tag-decision-accepted { background: var(--accent-green-bg); color: var(--accent-green); font-weight: 700; }
    .tag-decision-rejected { background: var(--accent-red-bg); color: var(--accent-red); font-weight: 600; }

    /* TELEGRAM FEED: Message Cards */
    .telegram-feed {
      display: flex;
      flex-direction: column;
      gap: 12px;
      padding: 20px 24px;
    }

    .tg-msg-card {
      background: rgba(11, 17, 32, 0.85);
      border: 1px solid var(--border-subtle);
      border-radius: 14px;
      padding: 16px 20px;
      display: grid;
      grid-template-columns: 240px 1fr 280px 130px;
      align-items: center;
      gap: 18px;
      transition: all 0.15s ease;
    }
    .tg-msg-card:hover {
      background: rgba(18, 27, 48, 0.95);
      border-color: rgba(255, 255, 255, 0.15);
    }

    .tg-col-meta {
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .tg-msg-id {
      font-family: var(--font-mono);
      font-size: 0.88rem;
      font-weight: 700;
      color: #FFF;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .tg-time {
      font-size: 0.73rem;
      color: var(--text-muted);
    }
    .tg-latency-badge {
      font-size: 0.68rem;
      font-weight: 700;
      padding: 2px 7px;
      border-radius: 99px;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      width: fit-content;
    }
    .lat-fast { background: var(--accent-green-bg); color: var(--accent-green); }
    .lat-stale { background: var(--accent-amber-bg); color: var(--accent-amber); }

    .tg-col-trade {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .tg-symbol-row {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .tg-symbol-name {
      font-family: var(--font-display);
      font-size: 1.05rem;
      font-weight: 700;
      color: #FFF;
    }
    .tg-params-row {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      font-size: 0.76rem;
    }
    .param-chip {
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid var(--border-subtle);
      border-radius: 6px;
      padding: 2px 8px;
      font-family: var(--font-mono);
      color: var(--text-sub);
    }
    .param-chip strong {
      color: #FFF;
    }

    .tg-col-decision {
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .tg-reason-text {
      font-size: 0.76rem;
      color: var(--text-sub);
      line-height: 1.35;
      font-family: var(--font-mono);
      background: rgba(0, 0, 0, 0.25);
      border-radius: 6px;
      padding: 6px 10px;
      border-left: 3px solid rgba(255, 255, 255, 0.1);
    }
    .tg-decision-accepted .tg-reason-text {
      border-left-color: var(--accent-green);
    }
    .tg-decision-rejected .tg-reason-text {
      border-left-color: var(--accent-red);
    }

    .tg-col-actions {
      display: flex;
      justify-content: flex-end;
    }

    /* Buttons */
    .btn-sm {
      padding: 6px 12px;
      font-size: 0.76rem;
      font-weight: 600;
      border-radius: 8px;
      border: 1px solid var(--border-subtle);
      background: rgba(255, 255, 255, 0.06);
      color: var(--text-main);
      cursor: pointer;
      transition: all 0.2s ease;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      white-space: nowrap;
    }
    .btn-sm:hover {
      background: rgba(255, 255, 255, 0.12);
      border-color: rgba(255, 255, 255, 0.25);
      color: #FFF;
    }
    .btn-sm-primary {
      background: var(--accent-green-bg);
      border-color: rgba(0, 245, 155, 0.3);
      color: var(--accent-green);
    }
    .btn-sm-primary:hover {
      background: var(--accent-green);
      color: #06090F;
      box-shadow: 0 0 12px rgba(0, 245, 155, 0.35);
    }
    .btn-sm-danger {
      background: var(--accent-red-bg);
      border-color: rgba(255, 59, 92, 0.3);
      color: var(--accent-red);
    }
    .btn-sm-danger:hover {
      background: var(--accent-red);
      color: #FFF;
      box-shadow: 0 0 12px rgba(255, 59, 92, 0.35);
    }

    /* Empty state */
    .empty-state {
      padding: 60px 20px;
      text-align: center;
      color: var(--text-muted);
    }
    .empty-state svg {
      width: 48px;
      height: 48px;
      margin-bottom: 12px;
      opacity: 0.4;
    }
    .empty-state p {
      font-size: 0.95rem;
      color: var(--text-sub);
    }

    /* Event rows */
    .event-row {
      display: flex;
      align-items: center;
      gap: 16px;
      padding: 12px 24px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.03);
      font-size: 0.82rem;
    }
    .event-row:hover {
      background: var(--bg-card-hover);
    }
    .event-ts {
      font-family: var(--font-mono);
      color: var(--text-muted);
      min-width: 145px;
      font-size: 0.75rem;
    }
    .event-level {
      font-size: 0.7rem;
      font-weight: 700;
      padding: 2px 7px;
      border-radius: 4px;
      text-transform: uppercase;
      min-width: 52px;
      text-align: center;
    }
    .level-info { background: rgba(0, 210, 255, 0.1); color: var(--accent-blue); }
    .level-warning { background: var(--accent-amber-bg); color: var(--accent-amber); }
    .level-error { background: var(--accent-red-bg); color: var(--accent-red); }
    .event-name {
      font-weight: 600;
      color: var(--text-main);
      min-width: 190px;
    }
    .event-detail {
      color: var(--text-sub);
      font-family: var(--font-mono);
      font-size: 0.78rem;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    /* Modals */
    .modal-overlay {
      position: fixed;
      inset: 0;
      background: rgba(4, 7, 13, 0.85);
      backdrop-filter: blur(8px);
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 1000;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.2s ease;
    }
    .modal-overlay.active {
      opacity: 1;
      pointer-events: auto;
    }
    .modal-card {
      width: 90%;
      max-width: 500px;
      background: #0B101D;
      border: 1px solid rgba(255, 255, 255, 0.14);
      border-radius: 16px;
      box-shadow: 0 24px 50px rgba(0, 0, 0, 0.7);
      padding: 24px;
      transform: scale(0.96);
      transition: transform 0.2s cubic-bezier(0.16, 1, 0.3, 1);
    }
    .modal-overlay.active .modal-card {
      transform: scale(1);
    }
    .modal-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 16px;
    }
    .modal-header h3 {
      font-family: var(--font-display);
      font-size: 1.2rem;
      font-weight: 700;
      color: #FFF;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .btn-close {
      background: none;
      border: none;
      color: var(--text-sub);
      font-size: 1.4rem;
      cursor: pointer;
      line-height: 1;
      padding: 0 4px;
    }
    .btn-close:hover { color: #FFF; }

    /* Telegram post preview bubble inside modal */
    .tg-bubble {
      background: #182533;
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 14px;
      padding: 16px;
      position: relative;
      margin-bottom: 16px;
    }
    .tg-bubble-header {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 12px;
      padding-bottom: 10px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    }
    .tg-avatar {
      width: 32px;
      height: 32px;
      border-radius: 50%;
      background: #229ED9;
      display: flex;
      align-items: center;
      justify-content: center;
      color: #FFF;
      font-weight: 700;
      font-size: 0.85rem;
    }
    .tg-bubble-text {
      font-family: var(--font-mono);
      font-size: 0.85rem;
      line-height: 1.6;
      white-space: pre-wrap;
      color: #E2E8F0;
    }

    .modal-input {
      width: 100%;
      background: rgba(255, 255, 255, 0.06);
      border: 1px solid rgba(255, 255, 255, 0.14);
      border-radius: 8px;
      padding: 10px 14px;
      color: #FFF;
      font-size: 0.95rem;
      font-family: var(--font-mono);
      outline: none;
      transition: border-color 0.2s, box-shadow 0.2s;
    }
    .modal-input:focus {
      border-color: var(--accent-green);
      box-shadow: 0 0 0 3px rgba(0, 245, 155, 0.18);
    }
    .modal-footer {
      display: flex;
      justify-content: flex-end;
      gap: 10px;
      margin-top: 22px;
    }

    footer {
      margin-top: 40px;
      text-align: center;
      font-size: 0.78rem;
      color: var(--text-muted);
    }

    @media (max-width: 900px) {
      .container { padding: 12px; }
      header { flex-direction: column; align-items: flex-start; gap: 14px; }
      .header-status { width: 100%; }
      .tg-msg-card { grid-template-columns: 1fr; gap: 12px; }
      .positions-cards-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="glow-point glow-1"></div>
  <div class="glow-point glow-2"></div>

  <div class="container">
    <!-- Header -->
    <header>
      <div class="brand">
        <div class="logo-badge">
          <svg viewBox="0 0 24 24"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>
        </div>
        <div class="brand-title">
          <h1>Mudrex Signal Trader</h1>
          <p id="channel-name">Telegram Channel: Vip Crypto Signal Jemal</p>
        </div>
      </div>

      <div class="header-status">
        <div id="bot-status-pill" class="pill pill-stopped">
          <div class="pulse-dot"></div>
          <span id="bot-status-text">Checking...</span>
        </div>
        <div id="mode-pill" class="pill pill-mode">PAPER MODE</div>
        <div id="target-type-pill" class="pill pill-purple">LIMIT EXITS</div>
        <div id="kill-pill" class="pill pill-running">TRADING ACTIVE</div>

        <div class="refresh-control">
          <select id="auto-refresh-select" class="refresh-select" onchange="changeRefreshRate(this.value)">
            <option value="2">Auto: 2s</option>
            <option value="5" selected>Auto: 5s</option>
            <option value="10">Auto: 10s</option>
            <option value="0">Paused</option>
          </select>
          <button class="btn-refresh" id="btn-refresh" onclick="fetchData()">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21.5 2v6h-6M2.5 22v-6h6M2 11.5a10 10 0 0 1 18.8-4.3M22 12.5a10 10 0 0 1-18.8 4.2"/></svg>
            <span id="refresh-timer">Sync</span>
          </button>
        </div>
      </div>
    </header>

    <!-- KPI Metric Cards -->
    <div class="kpi-grid">
      <!-- Balance Card -->
      <div class="kpi-card">
        <div class="kpi-header">
          <span class="kpi-title" id="kpi-balance-title">Account Balance</span>
          <div class="kpi-icon">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="4" width="20" height="16" rx="2"/><line x1="2" y1="10" x2="22" y2="10"/></svg>
          </div>
        </div>
        <div class="kpi-value mono" id="kpi-balance">$0.00</div>
        <div class="kpi-sub" id="kpi-balance-sub">
          <span>Available: <strong class="mono" id="kpi-avail">$0.00</strong></span>
          <span>• In Trades: <strong class="mono" id="kpi-reserved">$0.00</strong></span>
        </div>
      </div>

      <!-- Unrealized PnL Card -->
      <div class="kpi-card">
        <div class="kpi-header">
          <span class="kpi-title">Unrealized PnL</span>
          <div class="kpi-icon">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg>
          </div>
        </div>
        <div class="kpi-value mono" id="kpi-unrealized-pnl">$0.00</div>
        <div class="kpi-sub">
          <span>Across <strong class="mono" id="kpi-unrealized-count">0</strong> open futures positions</span>
        </div>
      </div>

      <!-- Realized PnL Card -->
      <div class="kpi-card">
        <div class="kpi-header">
          <span class="kpi-title">Realized PnL</span>
          <div class="kpi-icon">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M16 8l-8 8M8 8l8 8"/></svg>
          </div>
        </div>
        <div class="kpi-value mono" id="kpi-pnl">+$0.00</div>
        <div class="kpi-sub">
          <span id="kpi-winloss">0 Wins • 0 Losses</span>
          <span>• Win Rate: <strong class="mono" id="kpi-winrate">0.0%</strong></span>
        </div>
      </div>

      <!-- Active Capacity Card -->
      <div class="kpi-card">
        <div class="kpi-header">
          <span class="kpi-title">Position Capacity</span>
          <div class="kpi-icon">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 20V10M12 20V4M6 20v-6"/></svg>
          </div>
        </div>
        <div class="kpi-value mono" id="kpi-capacity">0 / 3</div>
        <div class="kpi-sub">
          <span>Risk Cap: <strong class="mono" id="kpi-margin-per-trade">$48/trade</strong></span>
        </div>
        <div class="capacity-bar">
          <div class="capacity-fill" id="capacity-bar-fill"></div>
        </div>
      </div>

      <!-- Signals Feed Card -->
      <div class="kpi-card">
        <div class="kpi-header">
          <span class="kpi-title">Telegram Signals</span>
          <div class="kpi-icon">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
          </div>
        </div>
        <div class="kpi-value mono" id="kpi-signals-total">0</div>
        <div class="kpi-sub">
          <span class="badge-pos" id="kpi-signals-accepted">0 Accepted</span>
          <span>•</span>
          <span class="badge-neg" id="kpi-signals-rejected">0 Rejected</span>
        </div>
      </div>
    </div>

    <!-- Navigation Tabs -->
    <div class="tabs-nav">
      <button class="tab-btn active" id="btn-tab-positions" onclick="switchTab('positions-tab', this)">
        <span>📊 Live Positions</span>
        <span class="tab-badge" id="tab-count-positions">0</span>
      </button>
      <button class="tab-btn" id="btn-tab-orders" onclick="switchTab('orders-tab', this)">
        <span>📋 Orders & Executions</span>
        <span class="tab-badge" id="tab-count-orders">0</span>
      </button>
      <button class="tab-btn" id="btn-tab-signals" onclick="switchTab('signals-tab', this)">
        <span>💬 Telegram Messages</span>
        <span class="tab-badge" id="tab-count-signals">0</span>
      </button>
      <button class="tab-btn" id="btn-tab-history" onclick="switchTab('history-tab', this)">
        <span>📈 Trade History</span>
        <span class="tab-badge" id="tab-count-history">0</span>
      </button>
      <button class="tab-btn" id="btn-tab-events" onclick="switchTab('events-tab', this)">
        <span>⚡ System Events</span>
        <span class="tab-badge" id="tab-count-events">0</span>
      </button>
    </div>

    <!-- TAB 1: POSITIONS -->
    <div id="positions-tab" class="panel active">
      <div class="panel-header">
        <h2>Live Positions & Multi-Target Pipeline</h2>
        <div class="panel-controls">
          <div class="filter-btn-group">
            <button class="filter-btn active" id="pos-view-cards" onclick="setPositionsView('cards')">Cards</button>
            <button class="filter-btn" id="pos-view-table" onclick="setPositionsView('table')">Table</button>
          </div>
        </div>
      </div>

      <!-- View A: Graphical Cards -->
      <div id="positions-cards-container" class="positions-cards-grid">
        <div class="empty-state">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
          <p>No open positions right now.</p>
        </div>
      </div>

      <!-- View B: Dense Table -->
      <div id="positions-table-container" class="table-container" style="display:none;">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Symbol</th>
              <th>Side</th>
              <th>Status</th>
              <th>Entry</th>
              <th>Mark Price</th>
              <th>Unrealized PnL</th>
              <th>Targets Pipeline</th>
              <th>Active Stop Loss</th>
              <th>Liq. Price</th>
              <th>Margin</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody id="positions-table-body">
            <tr><td colspan="12"><div class="empty-state"><p>No open positions right now.</p></div></td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- TAB 2: ORDERS -->
    <div id="orders-tab" class="panel">
      <div class="panel-header">
        <h2>Exchange Orders & Scheduled Target Exits</h2>
        <div class="panel-controls">
          <input type="text" class="search-input" id="orders-search" placeholder="Filter by symbol..." oninput="filterOrders()">
          <div class="filter-btn-group">
            <button class="filter-btn active" onclick="setOrderFilter('ALL', this)">All</button>
            <button class="filter-btn" onclick="setOrderFilter('EXCHANGE', this)">Mudrex Exchange</button>
            <button class="filter-btn" onclick="setOrderFilter('SCHEDULED', this)">Target Legs</button>
          </div>
        </div>
      </div>
      <div class="table-container">
        <table>
          <thead>
            <tr>
              <th>ID / Ref</th>
              <th>Source</th>
              <th>Symbol</th>
              <th>Side</th>
              <th>Order Type</th>
              <th>Purpose / Milestone</th>
              <th>Limit Price</th>
              <th>Qty</th>
              <th>Filled</th>
              <th>Status</th>
              <th>Created</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody id="orders-body">
            <tr><td colspan="12"><div class="empty-state"><p>No open orders or scheduled target legs right now.</p></div></td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- TAB 3: TELEGRAM SIGNALS -->
    <div id="signals-tab" class="panel">
      <div class="panel-header">
        <h2>Telegram Channel Signals & Diagnostics</h2>
        <div class="panel-controls">
          <input type="text" class="search-input" id="signals-search" placeholder="Search symbol or text..." oninput="filterSignals()">
          <div class="filter-btn-group">
            <button class="filter-btn active" onclick="setSignalFilter('ALL', this)">All</button>
            <button class="filter-btn" onclick="setSignalFilter('ACCEPTED', this)">Accepted</button>
            <button class="filter-btn" onclick="setSignalFilter('REJECTED', this)">Rejected</button>
          </div>
        </div>
      </div>
      <div class="telegram-feed" id="signals-feed">
        <div class="empty-state"><p>No signals received from Telegram yet.</p></div>
      </div>
    </div>

    <!-- TAB 4: TRADE HISTORY -->
    <div id="history-tab" class="panel">
      <div class="panel-header">
        <h2>Closed Trades & Outcomes</h2>
      </div>
      <div class="table-container">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Symbol</th>
              <th>Side</th>
              <th>Entry</th>
              <th>Exit</th>
              <th>Realized PnL</th>
              <th>Outcome Reason</th>
              <th>Closed At</th>
            </tr>
          </thead>
          <tbody id="history-body">
            <tr><td colspan="8"><div class="empty-state"><p>No closed trades in history yet.</p></div></td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- TAB 5: EVENTS -->
    <div id="events-tab" class="panel">
      <div class="panel-header">
        <h2>Bot Event Audit Trail</h2>
        <div class="panel-controls">
          <button class="btn-sm" onclick="clearEventLogs()">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
            Clear Log
          </button>
        </div>
      </div>
      <div id="events-list">
        <div class="empty-state"><p>No events recorded yet.</p></div>
      </div>
    </div>

    <footer>
      Mudrex Signal Trader • Real-Time Futures Terminal • Standalone Local Edition
    </footer>
  </div>

  <!-- MODAL 1: Telegram Raw Post Modal -->
  <div id="tg-modal" class="modal-overlay">
    <div class="modal-card">
      <div class="modal-header">
        <h3>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="#229ED9"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm4.64 6.8c-.15 1.58-.8 5.42-1.13 7.19-.14.75-.42 1-.68 1.03-.58.05-1.02-.38-1.58-.75-.88-.58-1.38-.94-2.23-1.5-.99-.65-.35-1.01.22-1.59.15-.15 2.71-2.48 2.76-2.69a.2.2 0 00-.05-.18c-.06-.05-.14-.03-.21-.02-.09.02-1.49.95-4.22 2.79-.4.27-.76.41-1.08.4-.36-.01-1.04-.2-1.55-.37-.63-.2-1.12-.31-1.08-.66.02-.18.27-.36.74-.55 2.92-1.27 4.86-2.11 5.83-2.51 2.78-1.16 3.35-1.36 3.73-1.36.08 0 .27.02.39.12.1.08.13.19.14.27-.01.06.01.24 0 .37z"/></svg>
          Telegram Signal Post
        </h3>
        <button class="btn-close" onclick="closeTgModal()">&times;</button>
      </div>
      <div class="modal-body">
        <div class="tg-bubble">
          <div class="tg-bubble-header">
            <div class="tg-avatar">J</div>
            <div>
              <div style="font-weight:700; font-size:0.85rem; color:#FFF;">Vip Crypto Signal Jemal</div>
              <div id="modal-tg-meta" style="font-size:0.72rem; color:var(--text-sub);">Message #66666</div>
            </div>
          </div>
          <div id="modal-tg-body" class="tg-bubble-text">-</div>
        </div>
        <div id="modal-tg-diagnostics" style="font-size:0.8rem; background:rgba(255,255,255,0.03); border:1px solid var(--border-subtle); border-radius:8px; padding:12px;"></div>
      </div>
      <div class="modal-footer">
        <button class="btn-sm" onclick="copyModalTgText()">Copy Raw Text</button>
        <button class="btn-sm btn-sm-primary" onclick="closeTgModal()">Close</button>
      </div>
    </div>
  </div>

  <!-- MODAL 2: Edit Target Modal -->
  <div id="edit-target-modal" class="modal-overlay">
    <div class="modal-card">
      <div class="modal-header">
        <h3>Edit Multi-Target Take Profit</h3>
        <button class="btn-close" onclick="closeEditTargetModal()">&times;</button>
      </div>
      <div class="modal-body">
        <div style="background:rgba(255,255,255,0.03); border:1px solid var(--border-subtle); border-radius:8px; padding:12px 14px; margin-bottom:14px;">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <span id="modal-edit-symbol" style="font-size:1.1rem; font-weight:700;">-</span>
            <span id="modal-edit-dir" class="tag-dir tag-long">LONG</span>
          </div>
          <div style="margin-top:6px; font-size:0.82rem; color:var(--text-sub);">
            Entry Price: <strong class="mono" id="modal-edit-entry" style="color:#FFF;">-</strong>
          </div>
        </div>

        <div style="display:flex; flex-direction:column; gap:12px;">
          <div>
            <label style="display:block; font-size:0.78rem; font-weight:600; color:var(--text-sub); margin-bottom:4px;">Target 1 (USDT)</label>
            <input type="number" step="any" id="modal-t1-input" class="modal-input" placeholder="e.g. 516.35">
          </div>
          <div>
            <label style="display:block; font-size:0.78rem; font-weight:600; color:var(--text-sub); margin-bottom:4px;">Target 2 (USDT)</label>
            <input type="number" step="any" id="modal-t2-input" class="modal-input" placeholder="e.g. 509.97">
          </div>
          <div>
            <label style="display:block; font-size:0.78rem; font-weight:600; color:var(--text-sub); margin-bottom:4px;">Target 3 (USDT)</label>
            <input type="number" step="any" id="modal-t3-input" class="modal-input" placeholder="e.g. 473.14">
          </div>
        </div>

        <div id="modal-edit-error" style="display:none; margin-top:12px; padding:8px 12px; border-radius:6px; background:var(--accent-red-bg); color:var(--accent-red); font-size:0.8rem; border:1px solid rgba(255,59,92,0.3);"></div>
      </div>
      <div class="modal-footer">
        <button class="btn-sm" onclick="closeEditTargetModal()">Cancel</button>
        <button id="modal-btn-save-target" class="btn-sm btn-sm-primary" onclick="submitEditTargets()">Save Targets</button>
      </div>
    </div>
  </div>

  <!-- MODAL 3: Close Position Confirmation -->
  <div id="close-pos-modal" class="modal-overlay">
    <div class="modal-card">
      <div class="modal-header">
        <h3 style="color:var(--accent-red)">Confirm Market Close</h3>
        <button class="btn-close" onclick="closePosModal()">&times;</button>
      </div>
      <div class="modal-body">
        <p style="font-size:0.88rem; color:var(--text-sub); margin-bottom:14px;">
          Are you sure you want to execute an immediate <strong>MARKET CLOSE</strong> for this position?
        </p>
        <div style="background:rgba(255,59,92,0.08); border:1px solid rgba(255,59,92,0.25); border-radius:8px; padding:12px; font-size:0.82rem;">
          <div>Symbol: <strong id="modal-close-sym">-</strong></div>
          <div>Size: <strong id="modal-close-qty" class="mono">-</strong></div>
          <div>Unrealized PnL: <strong id="modal-close-pnl" class="mono">-</strong></div>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn-sm" onclick="closePosModal()">Cancel</button>
        <button id="modal-btn-confirm-close" class="btn-sm btn-sm-danger" onclick="submitClosePosition()">Market Close Now</button>
      </div>
    </div>
  </div>

  <script>
    let globalData = null;
    let refreshSeconds = 5;
    let countdown = 5;
    let currentSignalFilter = 'ALL';
    let currentOrderFilter = 'ALL';
    let positionsViewMode = 'cards';
    let activeEditTrade = null;
    let activeCloseTrade = null;

    function switchTab(panelId, btn) {
      document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.getElementById(panelId).classList.add('active');
      btn.classList.add('active');
      localStorage.setItem('active_dashboard_tab', panelId);
    }

    function setPositionsView(mode) {
      positionsViewMode = mode;
      document.getElementById('pos-view-cards').classList.toggle('active', mode === 'cards');
      document.getElementById('pos-view-table').classList.toggle('active', mode === 'table');
      document.getElementById('positions-cards-container').style.display = mode === 'cards' ? 'grid' : 'none';
      document.getElementById('positions-table-container').style.display = mode === 'table' ? 'block' : 'none';
    }

    function changeRefreshRate(val) {
      refreshSeconds = Number(val);
      countdown = refreshSeconds;
      document.getElementById('refresh-timer').textContent = refreshSeconds > 0 ? `${countdown}s` : 'Paused';
    }

    function formatTime(isoStr) {
      if (!isoStr) return '-';
      try {
        const d = new Date(isoStr);
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) +
               ' ' + d.toLocaleDateString([], { month: 'short', day: 'numeric' });
      } catch (e) {
        return isoStr;
      }
    }

    async function fetchData() {
      const btn = document.getElementById('btn-refresh');
      btn.classList.add('spin');
      try {
        const res = await fetch('/api/status');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        globalData = await res.json();
        renderDashboard(globalData);
      } catch (err) {
        console.error('Fetch error:', err);
      } finally {
        setTimeout(() => btn.classList.remove('spin'), 350);
      }
    }

    function renderDashboard(data) {
      // 1. Header Bot Status
      const botPill = document.getElementById('bot-status-pill');
      const botText = document.getElementById('bot-status-text');
      if (data.bot.running) {
        botPill.className = 'pill pill-running';
        botText.textContent = `RUNNING (PID ${data.bot.pid})`;
      } else {
        botPill.className = 'pill pill-stopped';
        botText.textContent = 'STOPPED';
      }

      // Mode & Type
      const modePill = document.getElementById('mode-pill');
      modePill.textContent = data.bot.mode + ' MODE';
      modePill.className = data.bot.mode === 'REAL' ? 'pill pill-real' : 'pill pill-mode';

      const typePill = document.getElementById('target-type-pill');
      typePill.textContent = (data.bot.target_order_type || 'LIMIT') + ' EXITS';

      const killPill = document.getElementById('kill-pill');
      if (data.bot.kill_switch_active) {
        killPill.className = 'pill pill-kill-on';
        killPill.textContent = 'KILL SWITCH ON';
      } else {
        killPill.className = 'pill pill-running';
        killPill.textContent = 'TRADING ACTIVE';
      }

      // 2. KPIs
      if (data.ledger && data.ledger.available !== null && data.ledger.available !== undefined) {
        const balTitle = document.getElementById('kpi-balance-title');
        if (balTitle && data.ledger.title) balTitle.textContent = data.ledger.title;
        document.getElementById('kpi-balance').textContent = '$' + Number(data.ledger.equity || data.ledger.initial || 0).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
        document.getElementById('kpi-avail').textContent = '$' + Number(data.ledger.available || 0).toFixed(2);
        document.getElementById('kpi-reserved').textContent = '$' + Number(data.ledger.reserved_margin || 0).toFixed(2);
      }

      // Unrealized PnL
      const uPnl = data.metrics.total_unrealized_pnl || 0;
      const uPnlEl = document.getElementById('kpi-unrealized-pnl');
      uPnlEl.textContent = (uPnl >= 0 ? '+$' : '-$') + Math.abs(uPnl).toFixed(2);
      uPnlEl.style.color = uPnl > 0 ? 'var(--accent-green)' : (uPnl < 0 ? 'var(--accent-red)' : '#FFF');
      document.getElementById('kpi-unrealized-count').textContent = data.metrics.active_count;

      // Realized PnL
      const pnlVal = data.metrics.total_realized_pnl || 0;
      const pnlEl = document.getElementById('kpi-pnl');
      pnlEl.textContent = (pnlVal >= 0 ? '+$' : '-$') + Math.abs(pnlVal).toFixed(2);
      pnlEl.style.color = pnlVal > 0 ? 'var(--accent-green)' : (pnlVal < 0 ? 'var(--accent-red)' : '#FFF');
      document.getElementById('kpi-winloss').textContent = `${data.metrics.wins} Wins • ${data.metrics.losses} Losses`;
      document.getElementById('kpi-winrate').textContent = `${data.metrics.win_rate}%`;

      // Capacity
      const activeCount = data.metrics.active_count;
      const maxCount = data.metrics.max_positions;
      document.getElementById('kpi-capacity').textContent = `${activeCount} / ${maxCount}`;
      const pct = Math.min(100, Math.round((activeCount / maxCount) * 100));
      document.getElementById('capacity-bar-fill').style.width = pct + '%';
      document.getElementById('kpi-margin-per-trade').textContent = `$${data.bot.max_margin_per_trade}/trade`;

      // Signals KPI
      document.getElementById('kpi-signals-total').textContent = data.metrics.signals_total;
      document.getElementById('kpi-signals-accepted').textContent = `${data.metrics.signals_accepted} Accepted`;
      document.getElementById('kpi-signals-rejected').textContent = `${data.metrics.signals_rejected} Rejected`;

      // Badges
      document.getElementById('tab-count-positions').textContent = activeCount;
      document.getElementById('tab-count-orders').textContent = (data.orders || []).length;
      document.getElementById('tab-count-signals').textContent = (data.recent_signals || []).length;
      document.getElementById('tab-count-history').textContent = (data.closed_trades || []).length;
      document.getElementById('tab-count-events').textContent = (data.events || []).length;

      // Render tab bodies
      renderPositions(data.active_trades || []);
      renderOrders(data.orders || []);
      renderSignals(data.recent_signals || []);
      renderHistory(data.closed_trades || []);
      renderEvents(data.events || []);
    }

    // -------------------------------------------------------------
    // RENDER POSITIONS
    // -------------------------------------------------------------
    function renderPositions(trades) {
      const cardsContainer = document.getElementById('positions-cards-container');
      const tableBody = document.getElementById('positions-table-body');

      if (!trades || trades.length === 0) {
        cardsContainer.innerHTML = `<div class="empty-state" style="grid-column: 1/-1;"><p>No active positions or pending orders right now.</p></div>`;
        tableBody.innerHTML = `<tr><td colspan="12"><div class="empty-state"><p>No open positions right now.</p></div></td></tr>`;
        return;
      }

      // Render Cards View
      cardsContainer.innerHTML = trades.map(t => {
        const isLong = t.direction === 'LONG';
        const pnl = Number(t.unrealized_pnl || 0);
        const roe = Number(t.roe_pct || 0);
        const pnlColor = pnl >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';
        const pnlSign = pnl >= 0 ? '+' : '';

        // Stepper nodes
        const stepperHtml = (t.targets_detail || []).map((tgt, idx) => {
          const isHit = tgt.hit;
          const isNext = !isHit && (idx === 0 || (t.targets_detail[idx - 1] && t.targets_detail[idx - 1].hit));
          const nodeCls = isHit ? 'stepper-node hit' : (isNext ? 'stepper-node active-next' : 'stepper-node');
          const badgeHtml = isHit
            ? `<span class="node-badge node-badge-hit">HIT ✅</span>`
            : `<span class="node-badge node-badge-pending">Pending</span>`;

          return `
            <div class="${nodeCls}">
              <div class="node-icon">${isHit ? '✓' : 'T' + tgt.target_num}</div>
              <div class="node-label">Target ${tgt.target_num}</div>
              <div class="node-price">$${tgt.price}</div>
              <div style="font-size:0.68rem; color:${pnlColor}; font-weight:600;">+${tgt.target_pct}%</div>
              ${badgeHtml}
            </div>
          `;
        }).join('');

        // Risk Meter
        const marginLoss = Number(t.margin_loss_pct || 0);
        const meterPct = Math.min(100, Math.max(0, (marginLoss / 75.0) * 100));
        const meterColor = meterPct > 70 ? 'var(--accent-red)' : (meterPct > 40 ? 'var(--accent-amber)' : 'var(--accent-green)');

        return `
          <div class="pos-card">
            <div class="pos-card-header">
              <div class="pos-card-title">
                <span class="pos-symbol">${t.symbol}</span>
                <span class="tag-dir ${isLong ? 'tag-long' : 'tag-short'}">${t.direction}</span>
                <span class="pill pill-mode" style="font-size:0.68rem; padding:2px 8px;">${t.leverage}x ISOLATED</span>
              </div>
              <span class="tag-status tag-status-filled">${t.status}</span>
            </div>

            <div class="pos-main-metrics">
              <div class="pos-metric-box">
                <span class="pos-metric-lbl">Entry Price</span>
                <span class="pos-metric-val">$${t.entry_price}</span>
              </div>
              <div class="pos-metric-box">
                <span class="pos-metric-lbl">Live Mark Price</span>
                <span class="pos-metric-val" style="color:var(--accent-blue)">${t.current_price ? '$' + t.current_price : '-'}</span>
              </div>
              <div class="pos-metric-box">
                <span class="pos-metric-lbl">Unrealized PnL</span>
                <span class="pos-metric-val" style="color:${pnlColor}">${pnlSign}$${pnl.toFixed(2)} (${pnlSign}${roe.toFixed(2)}%)</span>
              </div>
            </div>

            <div style="display:grid; grid-template-columns:repeat(3, 1fr); font-size:0.78rem; color:var(--text-sub); padding:0 4px;">
              <div>Quantity: <strong class="mono" style="color:#FFF">${t.quantity}</strong></div>
              <div>Invested Margin: <strong class="mono" style="color:#FFF">$${Number(t.est_margin || 0).toFixed(2)}</strong></div>
              <div>Liquidation: <strong class="mono" style="color:var(--accent-red)">${t.liquidation_price ? '$' + t.liquidation_price : '-'}</strong></div>
            </div>

            <!-- Multi-Target Stepper -->
            <div class="stepper-container">
              <div class="stepper-header">
                <span>Multi-Target Pipeline (Limit Exits)</span>
                <span style="color:var(--accent-purple)">${t.targets_hit.length} / ${(t.targets_detail || []).length} Hit</span>
              </div>
              <div class="stepper-track">
                ${stepperHtml}
              </div>
            </div>

            <!-- Stop-Loss & Risk Shield -->
            <div class="risk-shield-box">
              <div class="risk-shield-top">
                <span>Active Protection: <strong style="color:#FFF">${t.sl_display_text}</strong></span>
                <span class="mono" style="font-size:0.73rem; color:${meterColor}">Risk: ${marginLoss > 0 ? '-' + marginLoss.toFixed(1) + '%' : '0.0%'} / -75.0%</span>
              </div>
              <div class="risk-meter-bar">
                <div class="risk-meter-fill" style="width:${meterPct}%; background:${meterColor};"></div>
              </div>
              <div style="font-size:0.72rem; color:var(--text-muted); display:flex; justify-content:space-between;">
                <span>Emergency SL Trigger: <strong class="mono" style="color:#FFF">$${t.emergency_sl_price}</strong></span>
                <span>Buffer to Trigger: <strong class="mono" style="color:#FFF">${t.emergency_sl_distance_pct !== null ? t.emergency_sl_distance_pct + '%' : '-'}</strong></span>
              </div>
            </div>

            <div class="pos-card-footer">
              <span style="font-size:0.72rem; color:var(--text-muted)">Created: ${formatTime(t.created_at)}</span>
              <div style="display:flex; gap:8px;">
                <button class="btn-sm" onclick="openEditTargetModal(${t.id}, '${t.symbol}', '${t.direction}', '${t.entry_price}', '${t.target1 || ''}', '${t.target2 || ''}', '${t.target3 || ''}')">
                  Edit Targets
                </button>
                <button class="btn-sm btn-sm-danger" onclick="openClosePosModal(${t.id}, '${t.symbol}', '${t.quantity}', '${pnlSign}$${pnl.toFixed(2)}')">
                  Close Position
                </button>
              </div>
            </div>
          </div>
        `;
      }).join('');

      // Render Table View
      tableBody.innerHTML = trades.map(t => {
        const isLong = t.direction === 'LONG';
        const pnl = Number(t.unrealized_pnl || 0);
        const pnlColor = pnl >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';
        const pnlSign = pnl >= 0 ? '+' : '';
        const hitsText = `${t.targets_hit.length}/${(t.targets_detail || []).length} Hit`;

        return `
          <tr>
            <td class="mono">#${t.id}</td>
            <td><strong>${t.symbol}</strong></td>
            <td><span class="tag-dir ${isLong ? 'tag-long' : 'tag-short'}">${t.direction}</span></td>
            <td><span class="tag-status tag-status-filled">${t.status}</span></td>
            <td class="mono">$${t.entry_price}</td>
            <td class="mono" style="color:var(--accent-blue)">${t.current_price ? '$' + t.current_price : '-'}</td>
            <td class="mono" style="color:${pnlColor}; font-weight:700;">${pnlSign}$${pnl.toFixed(2)} (${pnlSign}${Number(t.roe_pct || 0).toFixed(1)}%)</td>
            <td><span class="pill pill-purple" style="font-size:0.7rem; padding:2px 8px;">${hitsText}</span></td>
            <td style="font-size:0.75rem;">${t.sl_display_text}</td>
            <td class="mono" style="color:var(--accent-red)">${t.liquidation_price ? '$' + t.liquidation_price : '-'}</td>
            <td class="mono">$${Number(t.est_margin || 0).toFixed(2)}</td>
            <td>
              <button class="btn-sm" onclick="openEditTargetModal(${t.id}, '${t.symbol}', '${t.direction}', '${t.entry_price}', '${t.target1 || ''}', '${t.target2 || ''}', '${t.target3 || ''}')">Edit</button>
            </td>
          </tr>
        `;
      }).join('');
    }

    // -------------------------------------------------------------
    // RENDER ORDERS
    // -------------------------------------------------------------
    function renderOrders(orders) {
      const tbody = document.getElementById('orders-body');
      const searchVal = (document.getElementById('orders-search').value || '').trim().toUpperCase();

      let filtered = orders || [];
      if (currentOrderFilter === 'EXCHANGE') {
        filtered = filtered.filter(o => o.source === 'EXCHANGE');
      } else if (currentOrderFilter === 'SCHEDULED') {
        filtered = filtered.filter(o => o.source === 'BOT_TARGET_LEG');
      }

      if (searchVal) {
        filtered = filtered.filter(o => o.symbol.includes(searchVal) || o.id.includes(searchVal));
      }

      if (filtered.length === 0) {
        tbody.innerHTML = `<tr><td colspan="12"><div class="empty-state"><p>No orders matching filter criteria.</p></div></td></tr>`;
        return;
      }

      tbody.innerHTML = filtered.map(o => {
        const isExchange = o.source === 'EXCHANGE';
        const sourceBadge = isExchange
          ? `<span class="pill pill-mode" style="font-size:0.68rem; padding:2px 8px;">Mudrex Exchange</span>`
          : `<span class="pill pill-purple" style="font-size:0.68rem; padding:2px 8px;">Bot Scheduled</span>`;

        const isFilled = o.status === 'FILLED';
        const isScheduled = o.status === 'SCHEDULED';
        const statusBadge = isFilled
          ? `<span class="tag-status tag-status-filled">FILLED</span>`
          : (isScheduled ? `<span class="tag-status tag-status-scheduled">SCHEDULED</span>` : `<span class="tag-status tag-status-open">${o.status}</span>`);

        const cancelBtn = (isExchange && o.status === 'OPEN')
          ? `<button class="btn-sm btn-sm-danger" onclick="cancelExchangeOrder('${o.id}')">Cancel</button>`
          : `<span style="color:var(--text-muted); font-size:0.75rem;">-</span>`;

        return `
          <tr>
            <td class="mono" style="font-size:0.76rem;">${o.id}</td>
            <td>${sourceBadge}</td>
            <td><strong>${o.symbol}</strong></td>
            <td><span class="tag-dir ${o.side === 'LONG' || o.side === 'BUY' ? 'tag-long' : 'tag-short'}">${o.side}</span></td>
            <td><span class="pill pill-purple" style="font-size:0.68rem; padding:2px 8px;">${o.order_type}</span></td>
            <td style="font-size:0.78rem;">${o.milestone || '-'}</td>
            <td class="mono">$${o.price}</td>
            <td class="mono">${o.quantity}</td>
            <td class="mono">${o.filled_quantity || '0'}</td>
            <td>${statusBadge}</td>
            <td class="mono" style="font-size:0.72rem;">${formatTime(o.created_at)}</td>
            <td>${cancelBtn}</td>
          </tr>
        `;
      }).join('');
    }

    function setOrderFilter(filter, btn) {
      currentOrderFilter = filter;
      document.querySelectorAll('#orders-tab .filter-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      if (globalData) renderOrders(globalData.orders);
    }

    function filterOrders() {
      if (globalData) renderOrders(globalData.orders);
    }

    // -------------------------------------------------------------
    // RENDER TELEGRAM SIGNALS
    // -------------------------------------------------------------
    function renderSignals(signals) {
      const feed = document.getElementById('signals-feed');
      const searchVal = (document.getElementById('signals-search').value || '').trim().toUpperCase();

      let filtered = signals || [];
      if (currentSignalFilter === 'ACCEPTED') {
        filtered = filtered.filter(s => s.status === 'ACCEPTED');
      } else if (currentSignalFilter === 'REJECTED') {
        filtered = filtered.filter(s => s.status !== 'ACCEPTED');
      }

      if (searchVal) {
        filtered = filtered.filter(s =>
          (s.symbol && s.symbol.includes(searchVal)) ||
          (s.raw_text && s.raw_text.toUpperCase().includes(searchVal)) ||
          String(s.message_id).includes(searchVal)
        );
      }

      if (filtered.length === 0) {
        feed.innerHTML = `<div class="empty-state"><p>No Telegram signals matching filter.</p></div>`;
        return;
      }

      feed.innerHTML = filtered.map(s => {
        const isAccepted = s.status === 'ACCEPTED';
        const cardCls = isAccepted ? 'tg-msg-card tg-decision-accepted' : 'tg-msg-card tg-decision-rejected';
        const statusBadge = isAccepted
          ? `<span class="tag-status tag-decision-accepted">ACCEPTED ✅</span>`
          : `<span class="tag-status tag-decision-rejected">REJECTED ❌</span>`;

        const isLong = s.direction === 'LONG';
        const latSec = s.latency_seconds;
        const latCls = (latSec !== null && latSec < 180) ? 'lat-fast' : 'lat-stale';

        // Targets badges
        const tgtsChips = (s.targets || []).map((t, idx) => `
          <span class="param-chip">T${idx + 1}: <strong>${t}</strong></span>
        `).join('');

        return `
          <div class="${cardCls}">
            <div class="tg-col-meta">
              <span class="tg-msg-id">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="#229ED9"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm4.64 6.8c-.15 1.58-.8 5.42-1.13 7.19-.14.75-.42 1-.68 1.03-.58.05-1.02-.38-1.58-.75-.88-.58-1.38-.94-2.23-1.5-.99-.65-.35-1.01.22-1.59.15-.15 2.71-2.48 2.76-2.69a.2.2 0 00-.05-.18c-.06-.05-.14-.03-.21-.02-.09.02-1.49.95-4.22 2.79-.4.27-.76.41-1.08.4-.36-.01-1.04-.2-1.55-.37-.63-.2-1.12-.31-1.08-.66.02-.18.27-.36.74-.55 2.92-1.27 4.86-2.11 5.83-2.51 2.78-1.16 3.35-1.36 3.73-1.36.08 0 .27.02.39.12.1.08.13.19.14.27-.01.06.01.24 0 .37z"/></svg>
                Msg #${s.message_id}
              </span>
              <span class="tg-time">Post: ${formatTime(s.signal_time)}</span>
              <span class="tg-time">Recv: ${formatTime(s.received_at)}</span>
              <span class="tg-latency-badge ${latCls}">Latency: ${s.formatted_latency}</span>
            </div>

            <div class="tg-col-trade">
              <div class="tg-symbol-row">
                <span class="tg-symbol-name">${s.symbol}</span>
                <span class="tag-dir ${isLong ? 'tag-long' : 'tag-short'}">${s.direction}</span>
                ${s.telegram_leverage ? `<span class="pill pill-mode" style="font-size:0.68rem; padding:2px 7px;">${s.telegram_leverage}x Lev</span>` : ''}
              </div>
              <div class="tg-params-row">
                <span class="param-chip">Entry: <strong>${s.entry}</strong></span>
                ${tgtsChips}
                ${s.stop_loss ? `<span class="param-chip" style="color:var(--accent-red)">SL: <strong>${s.stop_loss}</strong></span>` : ''}
              </div>
            </div>

            <div class="tg-col-decision">
              <div>${statusBadge}</div>
              <div class="tg-reason-text">${s.decision_reason || 'Normal processing'}</div>
            </div>

            <div class="tg-col-actions">
              <button class="btn-sm" onclick="openTgModal(${s.id})">
                View Post
              </button>
            </div>
          </div>
        `;
      }).join('');
    }

    function setSignalFilter(filter, btn) {
      currentSignalFilter = filter;
      document.querySelectorAll('#signals-tab .filter-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      if (globalData) renderSignals(globalData.recent_signals);
    }

    function filterSignals() {
      if (globalData) renderSignals(globalData.recent_signals);
    }

    // -------------------------------------------------------------
    // RENDER HISTORY & EVENTS
    // -------------------------------------------------------------
    function renderHistory(closed) {
      const tbody = document.getElementById('history-body');
      if (!closed || closed.length === 0) {
        tbody.innerHTML = `<tr><td colspan="8"><div class="empty-state"><p>No closed trades in history yet.</p></div></td></tr>`;
        return;
      }
      tbody.innerHTML = closed.map(t => {
        const pnl = t.realized_pnl;
        const pnlStr = pnl !== null && pnl !== undefined ? (pnl >= 0 ? '+$' + pnl.toFixed(2) : '-$' + Math.abs(pnl).toFixed(2)) : '-';
        const pnlColor = pnl > 0 ? 'var(--accent-green)' : (pnl < 0 ? 'var(--accent-red)' : 'var(--text-sub)');
        return `
          <tr>
            <td class="mono">#${t.id}</td>
            <td><strong>${t.symbol}</strong></td>
            <td><span class="tag-dir ${t.direction === 'LONG' ? 'tag-long' : 'tag-short'}">${t.direction}</span></td>
            <td class="mono">$${t.entry_price}</td>
            <td class="mono">${t.exit_price ? '$' + t.exit_price : '-'}</td>
            <td class="mono" style="font-weight:700; color:${pnlColor}">${pnlStr}</td>
            <td><span class="tag-status tag-status-filled">${t.exit_reason || t.status}</span></td>
            <td class="mono" style="font-size:0.75rem">${formatTime(t.closed_at)}</td>
          </tr>
        `;
      }).join('');
    }

    function renderEvents(events) {
      const list = document.getElementById('events-list');
      if (!events || events.length === 0) {
        list.innerHTML = `<div class="empty-state"><p>No events recorded yet.</p></div>`;
        return;
      }
      list.innerHTML = events.map(e => `
        <div class="event-row">
          <span class="event-ts">${formatTime(e.ts)}</span>
          <span class="event-level ${e.level === 'INFO' ? 'level-info' : (e.level === 'WARNING' ? 'level-warning' : 'level-error')}">${e.level}</span>
          <span class="event-name">${e.event}</span>
          <span class="event-detail">${e.detail || ''}</span>
        </div>
      `).join('');
    }

    // -------------------------------------------------------------
    // MODAL ACTIONS
    // -------------------------------------------------------------
    function openTgModal(signalId) {
      if (!globalData) return;
      const sig = (globalData.recent_signals || []).find(s => s.id === signalId);
      if (!sig) return;

      document.getElementById('modal-tg-meta').textContent = `Message #${sig.message_id} • Received ${formatTime(sig.received_at)}`;
      document.getElementById('modal-tg-body').textContent = sig.raw_text;

      const diag = document.getElementById('modal-tg-diagnostics');
      diag.innerHTML = `
        <div style="display:flex; justify-content:space-between; margin-bottom:6px;">
          <span>Status: <strong>${sig.status}</strong></span>
          <span>Latency: <strong>${sig.formatted_latency}</strong></span>
        </div>
        <div style="color:var(--text-sub);">Decision Reason: <span style="color:#FFF;">${sig.decision_reason || 'Accepted for execution'}</span></div>
      `;

      document.getElementById('tg-modal').classList.add('active');
    }

    function closeTgModal() {
      document.getElementById('tg-modal').classList.remove('active');
    }

    function copyModalTgText() {
      const text = document.getElementById('modal-tg-body').textContent;
      navigator.clipboard.writeText(text).then(() => {
        alert('Telegram message text copied to clipboard!');
      });
    }

    function openEditTargetModal(id, symbol, dir, entry, t1, t2, t3) {
      activeEditTrade = { id, symbol, dir, entry: Number(entry) };
      document.getElementById('modal-edit-symbol').textContent = symbol;
      const dTag = document.getElementById('modal-edit-dir');
      dTag.textContent = dir;
      dTag.className = `tag-dir ${dir === 'LONG' ? 'tag-long' : 'tag-short'}`;
      document.getElementById('modal-edit-entry').textContent = '$' + entry;

      document.getElementById('modal-t1-input').value = t1 || '';
      document.getElementById('modal-t2-input').value = t2 || '';
      document.getElementById('modal-t3-input').value = t3 || '';
      document.getElementById('modal-edit-error').style.display = 'none';

      document.getElementById('edit-target-modal').classList.add('active');
    }

    function closeEditTargetModal() {
      document.getElementById('edit-target-modal').classList.remove('active');
      activeEditTrade = null;
    }

    async function submitEditTargets() {
      if (!activeEditTrade) return;
      const t1 = document.getElementById('modal-t1-input').value.trim();
      const t2 = document.getElementById('modal-t2-input').value.trim();
      const t3 = document.getElementById('modal-t3-input').value.trim();
      const errBox = document.getElementById('modal-edit-error');
      const btn = document.getElementById('modal-btn-save-target');

      if (!t1) {
        errBox.textContent = 'Target 1 is required.';
        errBox.style.display = 'block';
        return;
      }

      btn.disabled = true;
      btn.textContent = 'Saving...';
      try {
        const resp = await fetch('/api/trades/edit-target', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            trade_id: activeEditTrade.id,
            target_price: t1,
            target1: t1,
            target2: t2 || null,
            target3: t3 || null,
          }),
        });
        const res = await resp.json();
        if (!resp.ok || !res.success) throw new Error(res.error || 'Failed to update targets');
        closeEditTargetModal();
        fetchData();
      } catch (err) {
        errBox.textContent = err.message;
        errBox.style.display = 'block';
      } finally {
        btn.disabled = false;
        btn.textContent = 'Save Targets';
      }
    }

    function openClosePosModal(id, symbol, qty, pnl) {
      activeCloseTrade = { id, symbol };
      document.getElementById('modal-close-sym').textContent = symbol;
      document.getElementById('modal-close-qty').textContent = qty;
      document.getElementById('modal-close-pnl').textContent = pnl;
      document.getElementById('close-pos-modal').classList.add('active');
    }

    function closePosModal() {
      document.getElementById('close-pos-modal').classList.remove('active');
      activeCloseTrade = null;
    }

    async function submitClosePosition() {
      if (!activeCloseTrade) return;
      const btn = document.getElementById('modal-btn-confirm-close');
      btn.disabled = true;
      btn.textContent = 'Closing...';
      try {
        const resp = await fetch('/api/trades/close', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ trade_id: activeCloseTrade.id }),
        });
        const res = await resp.json();
        if (!resp.ok || !res.success) throw new Error(res.error || 'Failed to close position');
        closePosModal();
        fetchData();
      } catch (err) {
        alert('Error closing position: ' + err.message);
      } finally {
        btn.disabled = false;
        btn.textContent = 'Market Close Now';
      }
    }

    async function cancelExchangeOrder(orderId) {
      if (!confirm(`Cancel exchange order ${orderId}?`)) return;
      try {
        const resp = await fetch('/api/orders/cancel', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ order_id: orderId }),
        });
        const res = await resp.json();
        if (!resp.ok || !res.success) throw new Error(res.error || 'Failed to cancel order');
        fetchData();
      } catch (e) {
        alert('Error cancelling order: ' + e.message);
      }
    }

    async function clearEventLogs() {
      if (!confirm('Clear all event log entries?')) return;
      try {
        const resp = await fetch('/api/logs/clear', { method: 'POST' });
        const res = await resp.json();
        if (res.success) fetchData();
      } catch (e) {
        console.error(e);
      }
    }

    // Auto-refresh countdown loop
    setInterval(() => {
      if (refreshSeconds <= 0) return;
      countdown -= 1;
      if (countdown <= 0) {
        countdown = refreshSeconds;
        fetchData();
      }
      document.getElementById('refresh-timer').textContent = `${countdown}s`;
    }, 1000);

    // Restore tab
    const savedTab = localStorage.getItem('active_dashboard_tab');
    if (savedTab && document.getElementById(savedTab)) {
      const btnId = savedTab.replace('-tab', '');
      const targetBtn = document.getElementById(`btn-tab-${btnId}`);
      if (targetBtn) switchTab(savedTab, targetBtn);
    }

    // Initial load
    fetchData();
  </script>
</body>
</html>
"""


class DashboardRequestHandler(BaseHTTPRequestHandler):
    settings: Settings

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        parsed_path = self.path.split("?")[0]

        if parsed_path == "/":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))
            return

        if parsed_path == "/api/status":
            try:
                data = _fetch_dashboard_data(self.settings)
                body = json.dumps(data, indent=2, default=str).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                logger.exception("Error in /api/status")
                err_body = json.dumps({"error": str(exc)}).encode("utf-8")
                self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(err_body)
            return

        if parsed_path == "/api/health":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return

        self.send_response(HTTPStatus.NOT_FOUND)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Not Found")

    def do_POST(self) -> None:  # noqa: N802
        parsed_path = self.path.split("?")[0]

        if parsed_path == "/api/trades/edit-target":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                req_body = self.rfile.read(content_length).decode("utf-8")
                payload = json.loads(req_body) if req_body else {}

                trade_id = int(payload.get("trade_id"))
                raw_new_target = str(payload.get("target_price", "")).strip()
                if not raw_new_target and not payload.get("target1"):
                    raise ValueError("Target price is required.")

                new_target1 = Decimal(str(payload.get("target1") or raw_new_target))
                if new_target1 <= Decimal(0):
                    raise ValueError("Target 1 must be positive.")

                new_target2 = Decimal(str(payload.get("target2"))) if payload.get("target2") else None
                new_target3 = Decimal(str(payload.get("target3"))) if payload.get("target3") else None

                storage = Storage(self.settings.database_file)
                try:
                    trade = storage.get_trade(trade_id)
                    if not trade:
                        raise ValueError(f"Trade #{trade_id} not found.")
                    if trade.trade_status not in ACTIVE_TRADE_STATUSES:
                        raise ValueError(f"Trade #{trade_id} is {trade.status}; only active trades can be modified.")

                    if trade.direction == "LONG" and new_target1 <= trade.entry_price:
                        raise ValueError(f"For LONG trades, Target 1 ({new_target1}) must be higher than entry ({trade.entry_price}).")
                    if trade.direction == "SHORT" and new_target1 >= trade.entry_price:
                        raise ValueError(f"For SHORT trades, Target 1 ({new_target1}) must be lower than entry ({trade.entry_price}).")

                    old_target = trade.target1
                    now = datetime.now(timezone.utc)
                    update_kwargs: dict[str, Any] = {"target1": new_target1}
                    if new_target2 is not None:
                        update_kwargs["target2"] = new_target2
                    if new_target3 is not None:
                        update_kwargs["target3"] = new_target3

                    storage.update_trade(trade.id, now, **update_kwargs)
                    storage.add_event(
                        now,
                        "trade.target_updated",
                        level="INFO",
                        trade_id=trade.id,
                        signal_id=trade.signal_id,
                        detail={
                            "old_target": str(old_target),
                            "new_target": str(new_target1),
                            "target2": str(new_target2) if new_target2 else None,
                            "target3": str(new_target3) if new_target3 else None,
                            "source": "dashboard",
                        },
                    )

                    exchange_note = "Target updated in database."
                    if (
                        trade.mode == Mode.REAL.value
                        and trade.trade_status == TradeStatus.ENTRY_FILLED
                        and trade.position_id
                        and trade.tp_order_id
                    ):
                        try:
                            url = f"{self.settings.mudrex_base_url}/fapi/v1/futures/positions/{trade.position_id}/riskorder"
                            patch_data = json.dumps({
                                "takeprofit_order_id": trade.tp_order_id,
                                "takeprofit_price": str(new_target1),
                                "trade_currency": self.settings.trade_currency,
                            }).encode("utf-8")
                            p_req = urllib.request.Request(
                                url,
                                data=patch_data,
                                headers={
                                    "X-Authentication": self.settings.mudrex_api_secret,
                                    "Content-Type": "application/json",
                                },
                                method="PATCH",
                            )
                            with urllib.request.urlopen(p_req, timeout=4) as p_resp:
                                if p_resp.status in (200, 201):
                                    exchange_note = "Target synced to Mudrex position."
                        except Exception as m_exc:
                            exchange_note = f"Target updated in database (Mudrex sync note: {m_exc})"

                    resp_payload = {
                        "success": True,
                        "trade_id": trade.id,
                        "old_target": str(old_target),
                        "new_target": str(new_target1),
                        "message": f"Target 1 updated to {new_target1}. {exchange_note}",
                    }
                    body = json.dumps(resp_payload).encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                finally:
                    storage.close()
            except Exception as exc:
                err_body = json.dumps({"success": False, "error": str(exc)}).encode("utf-8")
                self.send_response(HTTPStatus.BAD_REQUEST)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(err_body)
                return

        if parsed_path == "/api/trades/close":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                req_body = self.rfile.read(content_length).decode("utf-8")
                payload = json.loads(req_body) if req_body else {}

                trade_id = int(payload.get("trade_id"))
                storage = Storage(self.settings.database_file)
                try:
                    trade = storage.get_trade(trade_id)
                    if not trade:
                        raise ValueError(f"Trade #{trade_id} not found.")

                    now = datetime.now(timezone.utc)
                    if trade.mode == Mode.REAL.value and trade.position_id:
                        url = f"{self.settings.mudrex_base_url}/fapi/v1/futures/positions/{trade.position_id}/close"
                        p_data = json.dumps({"order_type": "MARKET"}).encode("utf-8")
                        req = urllib.request.Request(
                            url,
                            data=p_data,
                            headers={
                                "X-Authentication": self.settings.mudrex_api_secret,
                                "Content-Type": "application/json",
                            },
                            method="POST",
                        )
                        with urllib.request.urlopen(req, timeout=5) as resp:
                            logger.info("Market close response: %s", resp.status)

                    live_p = _get_live_price(trade.symbol, self.settings.mudrex_base_url, self.settings.mudrex_api_secret)
                    storage.update_trade(
                        trade.id,
                        now,
                        status=TradeStatus.CLOSED.value,
                        exit_price=live_p or trade.entry_price,
                        exit_reason="manual_dashboard_close",
                        closed_at=now,
                    )
                    storage.add_event(
                        now,
                        "trade.manual_close",
                        level="WARNING",
                        trade_id=trade.id,
                        detail={"symbol": trade.symbol, "exit_price": str(live_p)},
                    )
                    body = json.dumps({"success": True, "message": f"Position #{trade.id} closed."}).encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                finally:
                    storage.close()
            except Exception as exc:
                err_body = json.dumps({"success": False, "error": str(exc)}).encode("utf-8")
                self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(err_body)
                return

        if parsed_path == "/api/orders/cancel":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                req_body = self.rfile.read(content_length).decode("utf-8")
                payload = json.loads(req_body) if req_body else {}
                order_id = str(payload.get("order_id", "")).strip()
                if not order_id:
                    raise ValueError("order_id is required")

                url = f"{self.settings.mudrex_base_url}/fapi/v1/futures/orders/{order_id}"
                req = urllib.request.Request(
                    url,
                    headers={"X-Authentication": self.settings.mudrex_api_secret},
                    method="DELETE",
                )
                with urllib.request.urlopen(req, timeout=4) as resp:
                    logger.info("Cancel order response: %s", resp.status)

                body = json.dumps({"success": True, "message": f"Order {order_id} cancelled."}).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
                return
            except Exception as exc:
                err_body = json.dumps({"success": False, "error": str(exc)}).encode("utf-8")
                self.send_response(HTTPStatus.BAD_REQUEST)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(err_body)
                return

        if parsed_path == "/api/logs/clear":
            try:
                storage = Storage(self.settings.database_file)
                try:
                    with storage.transaction() as conn:
                        conn.execute("DELETE FROM events")
                finally:
                    storage.close()
                body = json.dumps({"success": True, "message": "Activity log events cleared."}).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
                return
            except Exception as exc:
                err_body = json.dumps({"success": False, "error": str(exc)}).encode("utf-8")
                self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(err_body)
                return

        self.send_response(HTTPStatus.NOT_FOUND)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Not Found")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


def run_dashboard_server(settings: Settings, host: str = "0.0.0.0", port: int = 8500) -> int:
    DashboardRequestHandler.settings = settings
    server = ThreadingHTTPServer((host, port), DashboardRequestHandler)
    print("========================================================================")
    print(f"Mudrex Signal Trader — Dashboard running at http://localhost:{port}")
    print(f"Mode: {settings.trading_mode}  |  Press Ctrl+C to stop the dashboard server")
    print("========================================================================")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard server...")
    finally:
        server.server_close()
    return 0


def main() -> int:
    raw_env = load_env_file(".env")
    settings = Settings.from_env(raw_env)
    port = int(os.environ.get("DASHBOARD_PORT", "8500"))
    host = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
    return run_dashboard_server(settings, host=host, port=port)


if __name__ == "__main__":
    sys.exit(main())
