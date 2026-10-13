"""CLI helpers: reset-paper, kill switch, status and trade resolution."""

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from app import main as app_main
from app.models import SignalStatus
from app.storage import Storage
from tests.helpers import make_settings

NOW = datetime(2026, 10, 13, 6, 0, tzinfo=timezone.utc)


def _trade_fields(client_order_id: str) -> dict:
    return dict(
        ref_id=client_order_id, client_order_id=None, order_id=None, symbol="CRVUSDT",
        market="USDT", margin_asset="USDT", direction="SHORT", entry_price=Decimal("0.3462"),
        target1=Decimal("0.3185"), stop_loss_reference=Decimal("0.3662"),
        telegram_leverage=Decimal("14"), leverage=Decimal("14"), contract_max_leverage=Decimal("20"),
        quantity=Decimal("1941"), notional=Decimal("671.7"), est_margin=Decimal("48"),
        budget=Decimal("48"), status="ENTRY_PENDING",
    )


def seed(tmp_path):
    settings = make_settings(tmp_path)
    storage = Storage(settings.database_file)
    paper_signal = storage.insert_signal(channel_id=-1, message_id=1, received_at=NOW,
                                         signal_time=NOW, raw_text="p",
                                         status=SignalStatus.RECEIVED.value)
    storage.create_trade(NOW, mode="PAPER", signal_id=paper_signal,
                         **{**_trade_fields("TG-1-paper"), "ref_id": "TG-1-paper"})
    real_signal = storage.insert_signal(channel_id=-1, message_id=2, received_at=NOW,
                                        signal_time=NOW, raw_text="r",
                                        status=SignalStatus.RECEIVED.value)
    storage.create_trade(NOW, mode="REAL", signal_id=real_signal,
                         **{**_trade_fields("TG-2-real"), "ref_id": "TG-2-real"})
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


def test_reset_paper_dry_run_changes_nothing(tmp_path):
    settings = seed(tmp_path)
    assert app_main.reset_paper(settings, confirmed=False) == 1
    assert counts(settings.database_file) == {"paper": 1, "real": 1, "signals": 2}
    assert not (settings.database_file.parent / "backups").exists()


def test_confirmed_reset_deletes_only_paper_trades_and_keeps_signals(tmp_path):
    settings = seed(tmp_path)
    assert app_main.reset_paper(settings, confirmed=True) == 0
    assert counts(settings.database_file) == {"paper": 0, "real": 1, "signals": 2}


def test_reset_writes_a_backup_with_the_original_rows(tmp_path):
    settings = seed(tmp_path)
    app_main.reset_paper(settings, confirmed=True)
    backups = list((settings.database_file.parent / "backups").glob("trading-*.db"))
    assert len(backups) == 1
    assert counts(backups[0]) == {"paper": 1, "real": 1, "signals": 2}


def test_reset_records_an_event(tmp_path):
    settings = seed(tmp_path)
    app_main.reset_paper(settings, confirmed=True)
    conn = sqlite3.connect(settings.database_file)
    try:
        events = [row[0] for row in conn.execute("SELECT event FROM events")]
    finally:
        conn.close()
    assert "paper.reset" in events


def test_reset_without_a_database_is_a_no_op(tmp_path):
    settings = make_settings(tmp_path)
    assert app_main.reset_paper(settings, confirmed=True) == 0


def test_kill_switch_is_created_and_removed(tmp_path):
    settings = make_settings(tmp_path)
    assert app_main.set_kill_switch(settings, True) == 0
    assert settings.kill_switch_file.exists()
    assert app_main.set_kill_switch(settings, False) == 0
    assert not settings.kill_switch_file.exists()


def test_status_renders_without_a_database(tmp_path, capsys):
    settings = make_settings(tmp_path)
    assert app_main.print_status(settings) == 0
    out = capsys.readouterr().out
    assert "Pi42 Signal Trader" in out and "PAPER" in out
    assert "stop-loss" in out.lower()


def test_resolve_trade_records_the_operator_decision(tmp_path, capsys):
    settings = seed(tmp_path)
    assert app_main.resolve_trade(settings, 1, "CANCELLED", "verified as never submitted") == 0
    storage = Storage(settings.database_file)
    trade = storage.get_trade(1)
    assert trade.trade_status.value == "CANCELLED"
    assert "verified as never submitted" in trade.status_reason
    events = [e["event"] for e in storage.recent_events(10)]
    storage.close()
    assert "operator.resolved_trade" in events
    assert "ENTRY_PENDING -> CANCELLED" in capsys.readouterr().out


def test_parse_file_never_needs_settings(tmp_path, capsys):
    message = tmp_path / "signal.txt"
    message.write_text("#CRV/USDT - Short\n\nEntry: 0.3462\nTarget 1: 0.3361\nLeverage: x14",
                       encoding="utf-8")
    assert app_main.parse_file(str(message)) == 0
    out = capsys.readouterr().out
    assert "CRVUSDT" in out and "SHORT" in out


def test_parse_file_reports_ordinary_chat(tmp_path, capsys):
    message = tmp_path / "chat.txt"
    message.write_text("good morning everyone", encoding="utf-8")
    assert app_main.parse_file(str(message)) == 1
    assert "not a signal" in capsys.readouterr().out.lower()


def test_check_returns_3_when_a_read_only_pi42_check_fails(tmp_path, monkeypatch, capsys):
    import asyncio

    import httpx

    from app import main as app_main
    from tests.helpers import FakePi42

    settings = make_settings(tmp_path)
    fake = FakePi42()
    fake.fail_next.append(lambda request: httpx.Response(500, json={"message": "down"}))
    monkeypatch.setattr(app_main, "_build_client",
                        lambda s, mode: fake.client(allow_mutations=False))
    assert asyncio.run(app_main.check_config(settings, telegram=False)) == 3
    out = capsys.readouterr().out
    assert "Read-only checks FAILED" in out and "exchangeInfo" in out


def test_check_passes_against_a_reachable_pi42(tmp_path, monkeypatch, capsys):
    import asyncio

    from app import main as app_main
    from tests.helpers import FakePi42

    settings = make_settings(tmp_path)
    fake = FakePi42()
    monkeypatch.setattr(app_main, "_build_client",
                        lambda s, mode: fake.client(allow_mutations=False))
    assert asyncio.run(app_main.check_config(settings, telegram=False)) == 0
    out = capsys.readouterr().out
    assert "Pi42 exchangeInfo OK: 2 USDT-margined perpetual contracts" in out
    assert "balance=500" in out


def test_check_never_builds_a_mutating_client(tmp_path, monkeypatch):
    import asyncio

    from app import main as app_main
    from tests.helpers import FakePi42

    settings = make_settings(tmp_path)
    fake = FakePi42()
    seen = {}

    def spy(s, mode):
        seen["mode"] = mode
        return fake.client(allow_mutations=False)

    monkeypatch.setattr(app_main, "_build_client", spy)
    asyncio.run(app_main.check_config(settings, telegram=False))
    assert seen["mode"].value == "PAPER" and fake.mutating_calls() == []
