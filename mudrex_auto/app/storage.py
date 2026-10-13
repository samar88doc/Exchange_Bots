"""SQLite persistence behind a small repository API.

Tables: signals (unique on channel_id + message_id), trades (one per signal,
unique client_order_id), events (audit trail, secrets never stored), meta
(key/value state such as the resolved channel id and last processed message).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.models import (
    ACTIVE_TRADE_STATUSES,
    PENDING_LIKE_STATUSES,
    TradeRecord,
    TradeStatus,
    trade_from_row,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    signal_time TEXT,
    raw_text TEXT NOT NULL,
    symbol TEXT,
    direction TEXT,
    entry TEXT,
    stop_loss TEXT,
    target1 TEXT,
    target2 TEXT,
    target3 TEXT,
    target4 TEXT,
    target5 TEXT,
    telegram_leverage TEXT,
    status TEXT NOT NULL,
    decision_reason TEXT,
    mudrex_symbol TEXT,
    mudrex_asset_id TEXT,
    updated_at TEXT,
    UNIQUE (channel_id, message_id)
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL UNIQUE REFERENCES signals(id),
    mode TEXT NOT NULL,
    client_order_id TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    mudrex_asset_id TEXT,
    direction TEXT NOT NULL,
    entry_price TEXT NOT NULL,
    target1 TEXT NOT NULL,
    target2 TEXT,
    target3 TEXT,
    target4 TEXT,
    target5 TEXT,
    stop_loss_reference TEXT,
    telegram_leverage TEXT NOT NULL,
    leverage TEXT NOT NULL,
    asset_min_leverage TEXT,
    asset_max_leverage TEXT,
    leverage_step TEXT,
    quantity TEXT NOT NULL,
    notional TEXT NOT NULL,
    est_margin TEXT NOT NULL,
    budget TEXT NOT NULL,
    status TEXT NOT NULL,
    status_reason TEXT,
    order_id TEXT,
    position_id TEXT,
    fill_price TEXT,
    filled_quantity TEXT,
    exit_price TEXT,
    exit_reason TEXT,
    entry_fee TEXT,
    exit_fee TEXT,
    realized_pnl TEXT,
    liquidation_price TEXT,
    tp_confirmed INTEGER NOT NULL DEFAULT 0,
    tp_order_id TEXT,
    tp_order_ids TEXT,
    targets_hit TEXT,
    sl_order_id TEXT,
    sl_price TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    filled_at TEXT,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status, mode);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol, mode);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    event TEXT NOT NULL,
    signal_id INTEGER,
    trade_id INTEGER,
    detail TEXT
);
"""

_SIGNAL_COLUMNS = {
    "received_at", "signal_time", "raw_text", "symbol", "direction", "entry", "stop_loss",
    "target1", "target2", "target3", "target4", "target5", "telegram_leverage", "status", "decision_reason",
    "mudrex_symbol", "mudrex_asset_id", "updated_at", "channel_id", "message_id",
}
_TRADE_COLUMNS = {
    "signal_id", "mode", "client_order_id", "symbol", "mudrex_asset_id", "direction", "entry_price",
    "target1", "target2", "target3", "target4", "target5",
    "stop_loss_reference", "telegram_leverage", "leverage", "asset_min_leverage",
    "asset_max_leverage", "leverage_step", "quantity", "notional", "est_margin", "budget", "status",
    "status_reason", "order_id", "position_id", "fill_price", "filled_quantity", "exit_price",
    "exit_reason", "entry_fee", "exit_fee", "realized_pnl", "liquidation_price", "tp_confirmed",
    "tp_order_id", "tp_order_ids", "targets_hit", "sl_order_id", "sl_price",
    "created_at", "updated_at", "filled_at", "closed_at",
}


def _db(value: Any) -> Any:
    """Serialise Python values for SQLite."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bool):
        return int(value)
    if hasattr(value, "value") and isinstance(value.value, str):  # Enum
        return value.value
    return value


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class Storage:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    # ---------------------------------------------------------------- meta
    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else row["value"]

    def set_meta(self, key: str, value: str, now: datetime) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, _iso(now)),
        )

    # ------------------------------------------------------------- signals
    def insert_signal(
        self,
        *,
        channel_id: int,
        message_id: int,
        received_at: datetime,
        signal_time: datetime | None,
        raw_text: str,
        status: str,
    ) -> int | None:
        """Insert a message. Returns None when (channel_id, message_id) already exists."""
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO signals(channel_id, message_id, received_at, signal_time, raw_text, status, updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                channel_id,
                message_id,
                _iso(received_at),
                _iso(signal_time) if signal_time else None,
                raw_text,
                status,
                _iso(received_at),
            ),
        )
        if cursor.rowcount == 0:
            return None
        return int(cursor.lastrowid)

    def update_signal(self, signal_id: int, now: datetime, **fields: Any) -> None:
        self._update("signals", signal_id, fields, _SIGNAL_COLUMNS, now)

    def get_signal(self, signal_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
        return None if row is None else dict(row)

    def signal_by_message(self, channel_id: int, message_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM signals WHERE channel_id=? AND message_id=?", (channel_id, message_id)
        ).fetchone()
        return None if row is None else dict(row)

    def recent_signals(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def signal_counts(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM signals GROUP BY status").fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    def accepted_duplicate_exists(self, symbol: str, direction: str, entry: str, since: datetime) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM signals WHERE symbol=? AND direction=? AND entry=? AND status='ACCEPTED' "
            "AND received_at>=? LIMIT 1",
            (symbol, direction, entry, _iso(since)),
        ).fetchone()
        return row is not None

    # -------------------------------------------------------------- trades
    def create_trade(self, now: datetime, **fields: Any) -> int:
        unknown = set(fields) - _TRADE_COLUMNS
        if unknown:
            raise ValueError(f"unknown trade columns: {sorted(unknown)}")
        fields.setdefault("created_at", now)
        fields["updated_at"] = now
        columns = list(fields)
        placeholders = ",".join("?" for _ in columns)
        sql = f"INSERT INTO trades({','.join(columns)}) VALUES({placeholders})"
        cursor = self._conn.execute(sql, [_db(fields[c]) for c in columns])
        return int(cursor.lastrowid)

    def update_trade(self, trade_id: int, now: datetime, **fields: Any) -> None:
        self._update("trades", trade_id, fields, _TRADE_COLUMNS, now)

    def get_trade(self, trade_id: int) -> TradeRecord | None:
        row = self._conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        return None if row is None else trade_from_row(row)

    def trade_for_signal(self, signal_id: int) -> TradeRecord | None:
        row = self._conn.execute("SELECT * FROM trades WHERE signal_id=?", (signal_id,)).fetchone()
        return None if row is None else trade_from_row(row)

    def trades(self, *, mode: str | None = None, statuses: Iterable[TradeStatus] | None = None) -> list[TradeRecord]:
        sql = "SELECT * FROM trades WHERE 1=1"
        params: list[Any] = []
        if mode is not None:
            sql += " AND mode=?"
            params.append(mode)
        if statuses is not None:
            status_list = [s.value for s in statuses]
            sql += f" AND status IN ({','.join('?' for _ in status_list)})"
            params.extend(status_list)
        sql += " ORDER BY id"
        return [trade_from_row(r) for r in self._conn.execute(sql, params).fetchall()]

    def active_trades(self, mode: str | None = None) -> list[TradeRecord]:
        return self.trades(mode=mode, statuses=ACTIVE_TRADE_STATUSES)

    def pending_like_count(self, mode: str) -> int:
        statuses = [s.value for s in PENDING_LIKE_STATUSES]
        row = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM trades WHERE mode=? AND status IN ({','.join('?' for _ in statuses)})",
            [mode, *statuses],
        ).fetchone()
        return int(row["n"])

    def active_trade_for_symbol(self, symbol: str, mode: str) -> TradeRecord | None:
        statuses = [s.value for s in ACTIVE_TRADE_STATUSES]
        row = self._conn.execute(
            f"SELECT * FROM trades WHERE symbol=? AND mode=? AND status IN ({','.join('?' for _ in statuses)}) LIMIT 1",
            [symbol, mode, *statuses],
        ).fetchone()
        return None if row is None else trade_from_row(row)

    def trade_counts(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT mode || ':' || status AS k, COUNT(*) AS n FROM trades GROUP BY k").fetchall()
        return {r["k"]: int(r["n"]) for r in rows}

    # -------------------------------------------------------------- events
    def add_event(
        self,
        now: datetime,
        event: str,
        *,
        level: str = "INFO",
        signal_id: int | None = None,
        trade_id: int | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        payload = json.dumps(detail, default=str, sort_keys=True) if detail else None
        self._conn.execute(
            "INSERT INTO events(ts, level, event, signal_id, trade_id, detail) VALUES(?,?,?,?,?,?)",
            (_iso(now), level, event, signal_id, trade_id, payload),
        )

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ internals
    def _migrate(self) -> None:
        """Idempotently add new columns to existing databases (ALTER TABLE is safe to call repeatedly)."""
        new_columns = [
            ("target2", "TEXT"),
            ("target3", "TEXT"),
            ("target4", "TEXT"),
            ("target5", "TEXT"),
            ("tp_order_ids", "TEXT"),
            ("targets_hit", "TEXT"),
            ("sl_order_id", "TEXT"),
            ("sl_price", "TEXT"),
        ]
        existing = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(trades)").fetchall()
        }
        for col, col_type in new_columns:
            if col not in existing:
                self._conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {col_type}")

        existing_signals = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(signals)").fetchall()
        }
        for col in ("target4", "target5"):
            if col not in existing_signals:
                self._conn.execute(f"ALTER TABLE signals ADD COLUMN {col} TEXT")

    def transaction(self):
        """Context manager: BEGIN IMMEDIATE ... COMMIT/ROLLBACK."""
        return _Transaction(self._conn)

    def _update(self, table: str, row_id: int, fields: Mapping[str, Any], allowed: set[str], now: datetime) -> None:
        if not fields:
            return
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown {table} columns: {sorted(unknown)}")
        assignments = ",".join(f"{c}=?" for c in fields)
        values = [_db(v) for v in fields.values()]
        if table == "trades" or "updated_at" in allowed:
            assignments += ",updated_at=?"
            values.append(_iso(now))
        values.append(row_id)
        self._conn.execute(f"UPDATE {table} SET {assignments} WHERE id=?", values)


class _Transaction:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self._conn.execute("BEGIN IMMEDIATE")
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self._conn.execute("COMMIT")
        else:
            self._conn.execute("ROLLBACK")
        return False
