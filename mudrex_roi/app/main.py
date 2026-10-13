"""Command-line entry point.

    python -m app.main run          # start (PAPER by default)
    python -m app.main check        # validate config; read-only API + optional Telegram check
    python -m app.main auth         # interactive Telegram login (first run)
    python -m app.main status       # signals, trades, rejections, paper results
    python -m app.main parse-file F # parse a saved message offline
    python -m app.main kill|resume  # create/remove the KILL_SWITCH file (no new trades)
    python -m app.main resolve-trade ID --status REJECTED --note "..."
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from pathlib import Path

from app.config import (
    REAL_CONFIRM_PHRASE,
    ConfigError,
    Settings,
    load_env_file,
)
from app.logging_config import log_event, register_secret, setup_logging
from app.models import Mode, TradeStatus, utc_now
from app.mudrex_client import MudrexClient
from app.notifications import Notifier, NullNotifier
from app.paper_broker import PaperBroker
from app.signal_parser import SignalParseError, parse_signal
from app.storage import Storage

logger = logging.getLogger("main")

PAPER_BANNER = "PAPER MODE — NO LIVE ORDERS WILL BE PLACED"
REAL_BANNER = "REAL MODE — LIVE MUDREX FUTURES ORDERS WILL BE PLACED"
RISK_WARNING = "NO STOP-LOSS ENABLED — liquidation and substantial loss of allocated margin are possible."
BUDGET_NOTE = (
    "The margin budget is an allocation, not a maximum loss. Without a stop-loss, losses can exceed "
    "the allocated margin in adverse moves (see README: Risk and liquidation)."
)


def load_settings() -> Settings:
    env: dict[str, str] = {}
    env.update(load_env_file(Path(".env")))
    import os

    env.update(os.environ)  # real environment variables win over .env
    return Settings.from_env(env)


def _register_secrets(settings: Settings) -> None:
    for value in (settings.mudrex_api_secret, settings.telegram_api_hash, settings.notify_bot_token):
        register_secret(value)
    if settings.telegram_phone:
        register_secret(settings.telegram_phone)


def _build_client(settings: Settings, mode: Mode) -> MudrexClient:
    if not settings.mudrex_api_secret:
        raise ConfigError("MUDREX_API_SECRET is required (read-only key is enough in PAPER mode)")
    return MudrexClient(
        base_url=settings.mudrex_base_url,
        api_secret=settings.mudrex_api_secret,
        connect_timeout=settings.http_connect_timeout,
        read_timeout=settings.http_read_timeout,
        allow_mutations=mode is Mode.REAL,
    )


def _notifier(settings: Settings):
    if settings.notify_bot_token and settings.notify_chat_id:
        return Notifier(settings.notify_bot_token, settings.notify_chat_id)
    return NullNotifier()


# ------------------------------------------------------------------ run
def _confirm_real_interactively() -> bool:
    if not sys.stdin.isatty():
        # REAL requires a human at a terminal. Unattended/service starts are refused.
        print("REAL trading needs an interactive terminal to confirm; refusing non-TTY start.")
        return False
    print(RISK_WARNING)
    print(BUDGET_NOTE)
    print(f"Type exactly '{REAL_CONFIRM_PHRASE}' to start REAL trading, anything else aborts:")
    return input("> ").strip() == REAL_CONFIRM_PHRASE


async def run_bot(settings: Settings, skip_interactive: bool = False) -> int:
    from app.engine_factory import build_engine  # local import keeps CLI import light

    mode = Mode(settings.trading_mode)
    if mode is Mode.REAL:
        problems = settings.real_gate_problems()
        if problems:
            log_event(logger, "startup.refused", level=logging.CRITICAL, mode="REAL", problems=" | ".join(problems))
            return 2
        if not skip_interactive and not _confirm_real_interactively():
            log_event(logger, "startup.refused", level=logging.CRITICAL, reason="interactive confirmation not given")
            return 2
        banner = REAL_BANNER
    else:
        banner = PAPER_BANNER

    print("=" * 72)
    print(banner)
    print(RISK_WARNING)
    print("=" * 72)
    logger.info("event=startup mode=%s trading_enabled=%s currency=%s", mode.value,
                settings.trading_enabled, settings.trade_currency)
    if settings.mode_warning:
        logger.warning("event=startup.mode_warning %s", settings.mode_warning)

    try:
        runtime = build_engine(settings, mode)
    except ConfigError as exc:
        log_event(logger, "startup.refused", level=logging.CRITICAL, reason=str(exc))
        return 2
    storage, client = runtime.storage, runtime.client

    try:
        if mode is Mode.REAL:
            funds = await client.get_futures_funds(settings.trade_currency)
            log_event(logger, "real.readonly_check_ok", currency=settings.trade_currency,
                      futures_balance=funds.balance, locked=funds.locked)
        assets = await client.list_futures_assets()
        log_event(logger, "mudrex.metadata_loaded", instruments=len(assets))
        if runtime.paper is not None:
            ledger = runtime.paper.ledger()
            log_event(logger, "paper.ledger", simulated_initial=ledger["initial"],
                      simulated_available=ledger["available"], note="SIMULATED, not the real wallet")
    except Exception as exc:  # noqa: BLE001
        log_event(logger, "startup.readonly_check_failed", level=logging.CRITICAL, error=type(exc).__name__,
                  detail=str(exc))
        await client.aclose()
        storage.close()
        return 3

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    async def monitor_loop() -> None:
        while not stop.is_set():
            try:
                await runtime.monitor.monitor_once()
            except Exception:  # noqa: BLE001
                logger.exception("event=monitor.cycle_error")
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.monitor_poll_seconds)
            except asyncio.TimeoutError:
                pass

    listener = runtime.listener
    listener_task = asyncio.create_task(listener.run(), name="telegram-listener")
    monitor_task = asyncio.create_task(monitor_loop(), name="monitor")

    async def watch_listener() -> None:
        await listener_task  # fatal listener errors (auth/channel) stop the process

    watcher = asyncio.create_task(watch_listener(), name="listener-watch")
    stopper = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait({watcher, stopper}, return_when=asyncio.FIRST_COMPLETED)
    exit_code = 0
    if watcher in done and not watcher.cancelled() and watcher.exception() is not None:
        exc = watcher.exception()
        log_event(logger, "telegram.fatal", level=logging.CRITICAL, error=type(exc).__name__, detail=str(exc))
        exit_code = 4

    log_event(logger, "shutdown.begin")
    listener.request_stop()
    for task in (listener_task, monitor_task, watcher, stopper):
        task.cancel()
    await asyncio.gather(listener_task, monitor_task, watcher, stopper, return_exceptions=True)
    await runtime.notifier.aclose()
    await client.aclose()
    storage.close()
    log_event(logger, "shutdown.complete", exit_code=exit_code)
    return exit_code


# ------------------------------------------------------------------ check
async def check_config(settings: Settings, *, telegram: bool) -> int:
    print("Configuration (secrets redacted):")
    for key, value in settings.redacted_summary().items():
        print(f"  {key}: {value}")
    print()
    problems = settings.real_gate_problems() if settings.trading_mode == "REAL" else []
    if settings.trading_mode == "PAPER":
        print("Mode: PAPER — REAL trading is OFF. Gate problems (informational):")
        for p in settings.real_gate_problems():
            print(f"  - {p}")
    else:
        print("Mode: REAL requested. Gate problems:" if problems else "Mode: REAL gates satisfied.")
        for p in problems:
            print(f"  - {p}")

    storage = Storage(settings.database_file)
    print(f"Database OK: {settings.database_file}")
    storage.close()

    if settings.mudrex_api_secret:
        client = _build_client(settings, Mode.PAPER)
        try:
            assets = await client.list_futures_assets()
            print(f"Mudrex read-only check OK: {len(assets)} futures instruments listed")
            funds = await client.get_futures_funds(settings.trade_currency)
            print(f"Mudrex futures wallet ({settings.trade_currency}): balance={funds.balance} locked={funds.locked}")
        except Exception as exc:  # noqa: BLE001
            print(f"Mudrex read-only check FAILED: {type(exc).__name__}: {exc}")
            problems.append("mudrex read-only check failed")
        finally:
            await client.aclose()
    else:
        print("Mudrex check skipped: MUDREX_API_SECRET is not set")

    if telegram:
        from app.telegram_listener import build_client, resolve_channel

        if settings.telegram_problems():
            print("Telegram check skipped: " + "; ".join(settings.telegram_problems()))
        else:
            tg = build_client(settings)
            await tg.connect()
            try:
                if not await tg.is_user_authorized():
                    print("Telegram: NOT AUTHORISED. Run `python -m app.main auth`.")
                else:
                    storage = Storage(settings.database_file)
                    entity = await resolve_channel(tg, settings, storage)
                    print(f"Telegram OK: channel resolved ({getattr(entity, 'title', 'untitled')})")
                    storage.close()
            except Exception as exc:  # noqa: BLE001
                print(f"Telegram check FAILED: {type(exc).__name__}: {exc}")
            finally:
                await tg.disconnect()
    return 0


# ------------------------------------------------------------------- auth
async def run_auth(settings: Settings) -> int:
    from typing import Any, cast

    from app.telegram_listener import build_client, resolve_channel

    client = build_client(settings)
    # Telethon's start() returns a coroutine when an event loop is running,
    # but Telethon statically annotates its return type as -> 'TelegramClient'.
    await cast(Any, client.start(phone=settings.telegram_phone))
    print("Telegram session authorised.")
    storage = Storage(settings.database_file)
    try:
        entity = await resolve_channel(client, settings, storage)
        print(f"Signal channel resolved: {getattr(entity, 'title', 'untitled')}")
    except Exception as exc:  # noqa: BLE001
        print(f"Channel resolution failed: {exc}")
        return 1
    finally:
        storage.close()
        await client.disconnect()
    return 0


# ----------------------------------------------------------------- status
def print_status(settings: Settings) -> int:
    storage = Storage(settings.database_file)
    try:
        print(f"Database: {settings.database_file}")
        print(f"Mode configured: {settings.trading_mode}  TRADING_ENABLED={settings.trading_enabled}  "
              f"kill_switch={'ON' if settings.kill_switch_file.exists() else 'off'}")
        print("\nSignal decisions:", storage.signal_counts() or "none")
        print("Trades by mode:status:", storage.trade_counts() or "none")

        active = storage.active_trades()
        print(f"\nActive trades ({len(active)}):")
        for t in active:
            print(f"  #{t.id} {t.mode} {t.symbol} {t.direction} {t.status} limit={t.entry_price} "
                  f"tp1={t.target1} lev={t.leverage} qty={t.quantity} margin~{t.est_margin} "
                  f"tp_confirmed={t.tp_confirmed} | {t.status_reason or ''}")

        print("\nRecent signals:")
        for s in storage.recent_signals(15):
            print(f"  msg={s['message_id']} {s['status']:<10} {s['symbol'] or '-':<12} "
                  f"{s['direction'] or '-':<6} | {s['decision_reason'] or ''}")

        paper_trades = storage.trades(mode=Mode.PAPER.value)
        if paper_trades:
            closed = [t for t in paper_trades if t.status in (TradeStatus.CLOSED.value, TradeStatus.LIQUIDATED.value)]
            wins = [t for t in closed if (t.realized_pnl or 0) > 0]
            total = sum((t.realized_pnl or 0) for t in closed)
            print(f"\nPAPER results (SIMULATED): closed={len(closed)} wins={len(wins)} "
                  f"realized_pnl={total}")
            if settings.paper_initial_balance_usdt:
                ledger = PaperBroker(
                    storage,
                    initial_balance=settings.paper_initial_balance_usdt,
                    fee_rate=settings.paper_fee_rate,
                    maintenance_margin_rate=settings.paper_maintenance_margin_rate,
                    ttl_minutes=settings.limit_order_ttl_minutes,
                ).ledger()
                print("  simulated balance:", {k: str(v) for k, v in ledger.items()})

        print("\nRecent events:")
        for e in storage.recent_events(10):
            print(f"  {e['ts']} {e['level']:<7} {e['event']} trade={e['trade_id']} signal={e['signal_id']}")
    finally:
        storage.close()
    return 0


def resolve_trade(settings: Settings, trade_id: int, status: str, note: str) -> int:
    allowed = {"REJECTED", "CANCELLED", "CLOSED", "ENTRY_PENDING", "ENTRY_FILLED"}
    if status not in allowed:
        print(f"status must be one of {sorted(allowed)}")
        return 1
    storage = Storage(settings.database_file)
    try:
        trade = storage.get_trade(trade_id)
        if trade is None:
            print("no such trade")
            return 1
        now = utc_now()
        storage.update_trade(trade_id, now, status=status, status_reason=f"manual resolution: {note}")
        storage.add_event(now, "operator.resolved_trade", trade_id=trade_id, signal_id=trade.signal_id,
                          detail={"from": trade.status, "to": status, "note": note})
        print(f"trade #{trade_id}: {trade.status} -> {status}")
    finally:
        storage.close()
    return 0


def parse_file(path: str) -> int:
    from datetime import datetime, timezone

    text = Path(path).read_text(encoding="utf-8")
    try:
        signal = parse_signal(text, channel_id=0, message_id=0, signal_time=datetime.now(timezone.utc))
    except SignalParseError as exc:
        print(json.dumps({"ok": False, "ignorable": exc.ignorable, "reason": exc.reason}, indent=2))
        return 1
    print(json.dumps({
        "ok": True,
        "symbol": signal.symbol,
        "direction": signal.direction.value,
        "entry": str(signal.entry),
        "stop_loss_reference": str(signal.stop_loss) if signal.stop_loss else None,
        "target1": str(signal.target1),
        "target2": str(signal.target2) if signal.target2 else None,
        "target3": str(signal.target3) if signal.target3 else None,
        "telegram_leverage": str(signal.telegram_leverage),
    }, indent=2))
    return 0


def set_kill_switch(settings: Settings, enable: bool) -> int:
    path = settings.kill_switch_file
    if enable:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("kill switch active: no new trades\n", encoding="utf-8")
        print(f"Kill switch ON. No new trades will be opened. File: {path}")
    else:
        if path.exists():
            path.unlink()
        print("Kill switch OFF. New trades allowed again (if TRADING_ENABLED=true).")
    return 0


def reset_paper(settings: Settings, confirmed: bool) -> int:
    """Delete PAPER trades only. Signals, events, REAL trades and Telegram state are kept.

    Keeping signals is deliberate: a signal already seen stays recorded, so it can never execute again.
    A timestamped backup of the database is written before any deletion.
    """
    import sqlite3
    from datetime import datetime, timezone

    db_path = settings.database_file
    if not db_path.exists():
        print("No database yet; nothing to reset.")
        return 0
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = db_path.parent / "backups" / f"trading-{stamp}.db"

    storage = Storage(db_path)
    try:
        with storage.transaction() as conn:
            count = conn.execute("SELECT COUNT(*) FROM trades WHERE mode = ?", ("PAPER",)).fetchone()[0]
            real_count = conn.execute("SELECT COUNT(*) FROM trades WHERE mode = ?", ("REAL",)).fetchone()[0]
        print(f"PAPER trades to delete: {count}  (REAL trades kept: {real_count})")
        print(f"Signals and events are kept. Backup will be written to: {backup}")
        if not confirmed:
            print("Nothing changed. Re-run with --yes to delete the PAPER trades.")
            return 1
        backup.parent.mkdir(parents=True, exist_ok=True)
        # Consistent backup using SQLite's online backup API (safe even if a writer exists).
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(backup))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        now = datetime.now(timezone.utc)
        with storage.transaction() as conn:
            conn.execute("DELETE FROM trades WHERE mode = ?", ("PAPER",))
        storage.add_event(now, "paper.reset", level="WARNING",
                          detail={"deleted_paper_trades": count, "backup": backup.name})
        print(f"Deleted {count} PAPER trades. Simulated balance restored to PAPER_INITIAL_BALANCE_USDT.")
        return 0
    finally:
        storage.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mudrex-signal-trader")
    sub = parser.add_subparsers(dest="command", required=True)
    run_cmd = sub.add_parser("run", help="start the listener, monitor and execution engine")
    run_cmd.add_argument("--yes", action="store_true", help="skip interactive TTY prompt if all REAL safety gates are satisfied")
    check = sub.add_parser("check", help="validate configuration and read-only connectivity")
    check.add_argument("--telegram", action="store_true", help="also resolve the Telegram channel")
    sub.add_parser("auth", help="interactive Telegram login and channel resolution")
    sub.add_parser("status", help="show signals, trades and paper results")
    sub.add_parser("init-db", help="create the SQLite schema")
    parse_cmd = sub.add_parser("parse-file", help="parse a saved message text file offline")
    parse_cmd.add_argument("path")
    sub.add_parser("kill", help="stop opening new trades immediately")
    sub.add_parser("resume", help="allow new trades again")
    reset = sub.add_parser("reset-paper", help="delete PAPER trades (keeps signals, REAL trades, Telegram state)")
    reset.add_argument("--yes", action="store_true", help="actually delete (otherwise dry run)")
    resolve = sub.add_parser("resolve-trade", help="manually resolve a NEEDS_REVIEW trade")
    resolve.add_argument("trade_id", type=int)
    resolve.add_argument("--status", required=True)
    resolve.add_argument("--note", required=True)
    dash = sub.add_parser("dashboard", help="run the lightweight web dashboard")
    dash.add_argument("--port", type=int, default=8500, help="port to listen on (default: 8500)")
    dash.add_argument("--host", default="0.0.0.0", help="host to listen on (default: 0.0.0.0)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "parse-file":
        return parse_file(args.path)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"CONFIGURATION ERROR: {exc}", file=sys.stderr)
        return 2
    setup_logging(settings.log_level)
    _register_secrets(settings)

    if args.command == "run":
        return asyncio.run(run_bot(settings, skip_interactive=args.yes))
    if args.command == "check":
        return asyncio.run(check_config(settings, telegram=args.telegram))
    if args.command == "auth":
        return asyncio.run(run_auth(settings))
    if args.command == "status":
        return print_status(settings)
    if args.command == "dashboard":
        from app.dashboard import run_dashboard_server
        return run_dashboard_server(settings, host=args.host, port=args.port)
    if args.command == "init-db":
        Storage(settings.database_file).close()
        print(f"Schema ready: {settings.database_file}")
        return 0
    if args.command == "kill":
        return set_kill_switch(settings, True)
    if args.command == "resume":
        return set_kill_switch(settings, False)
    if args.command == "reset-paper":
        return reset_paper(settings, args.yes)
    if args.command == "resolve-trade":
        return resolve_trade(settings, args.trade_id, args.status, args.note)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
