"""reset-paper must delete only PAPER trades, keep signals/REAL trades, and back up first."""

import sqlite3
from datetime import timedelta

from app import main as app_main
from app.models import SignalStatus
from app.storage import Storage
from tests.helpers import make_settings, utc
from tests.test_paper_broker import make_trade


def seed(tmp_path):
    settings = make_settings(tmp_path)
    storage = Storage(settings.database_file)
    now = utc()
    fields = {k: v for k, v in make_trade("SHORT").__dict__.items()
              if k not in ("id", "signal_id", "created_at", "updated_at", "mode")}
    sig_p = storage.insert_signal(channel_id=-1, message_id=1, received_at=now, signal_time=now,
                                  raw_text="p", status=SignalStatus.RECEIVED.value)
    storage.create_trade(now, mode="PAPER", signal_id=sig_p, **{**fields, "client_order_id": "TG-1-paper"})
    sig_r = storage.insert_signal(channel_id=-1, message_id=2, received_at=now + timedelta(seconds=1),
                                  signal_time=now, raw_text="r", status=SignalStatus.RECEIVED.value)
    storage.create_trade(now, mode="REAL", signal_id=sig_r, **{**fields, "client_order_id": "TG-2-real"})
    storage.close()
    return settings


def counts(path):
    conn = sqlite3.connect(path)
    try:
        return {
            "paper": conn.execute("SELECT COUNT(*) FROM trades WHERE mode='PAPER'").fetchone()[0],
            "real": conn.execute("SELECT COUNT(*) FROM trades WHERE mode='REAL'").fetchone()[0],
            "signals": conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0],
        }
    finally:
        conn.close()


def test_dry_run_changes_nothing(tmp_path):
    settings = seed(tmp_path)
    assert app_main.reset_paper(settings, confirmed=False) == 1
    assert counts(settings.database_file) == {"paper": 1, "real": 1, "signals": 2}
    assert not (settings.database_file.parent / "backups").exists()


def test_confirmed_reset_deletes_only_paper_and_keeps_signals(tmp_path):
    settings = seed(tmp_path)
    assert app_main.reset_paper(settings, confirmed=True) == 0
    assert counts(settings.database_file) == {"paper": 0, "real": 1, "signals": 2}


def test_reset_writes_backup_with_original_rows(tmp_path):
    settings = seed(tmp_path)
    app_main.reset_paper(settings, confirmed=True)
    backups = list((settings.database_file.parent / "backups").glob("trading-*.db"))
    assert len(backups) == 1
    assert counts(backups[0]) == {"paper": 1, "real": 1, "signals": 2}


def test_reset_records_event(tmp_path):
    settings = seed(tmp_path)
    app_main.reset_paper(settings, confirmed=True)
    conn = sqlite3.connect(settings.database_file)
    try:
        events = [r[0] for r in conn.execute("SELECT event FROM events")]
    finally:
        conn.close()
    assert "paper.reset" in events
