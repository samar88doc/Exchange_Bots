"""Mode safety: PAPER can never mutate Mudrex; REAL needs every gate; no transfer code exists."""

import asyncio
import re
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import REAL_ACK_PHRASE, ConfigError
from app.engine_factory import build_engine
from app.main import run_bot
from app.models import Mode, TradeStatus
from app.mudrex_client import ReadOnlyViolation
from app.position_monitor import PositionMonitor
from tests.helpers import ASSET_CRV, SIGNAL_CRV, FakeMudrex, make_settings, utc

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------ config gates
def test_default_mode_is_paper(tmp_path):
    s = make_settings(tmp_path, TRADING_MODE="")
    assert s.trading_mode == "PAPER" and s.enable_real_trading is False


def test_invalid_mode_falls_back_to_paper_and_warns(tmp_path):
    s = make_settings(tmp_path, TRADING_MODE="LIVE!!")
    assert s.trading_mode == "PAPER"
    assert "not a valid mode" in (s.mode_warning or "")


def test_real_without_any_flags_has_all_gate_problems(tmp_path):
    s = make_settings(tmp_path, TRADING_MODE="REAL")
    problems = " | ".join(s.real_gate_problems())
    assert "ENABLE_REAL_TRADING" in problems and "LIVE_TRADING_ACKNOWLEDGEMENT" in problems


def test_real_requires_exact_acknowledgement_phrase(tmp_path):
    s = make_settings(tmp_path, TRADING_MODE="REAL", ENABLE_REAL_TRADING="true",
                      LIVE_TRADING_ACKNOWLEDGEMENT="yes please")
    assert any("LIVE_TRADING_ACKNOWLEDGEMENT" in p for p in s.real_gate_problems())


def test_real_gates_satisfied_only_when_everything_is_explicit(tmp_path):
    s = make_settings(tmp_path, TRADING_MODE="REAL", ENABLE_REAL_TRADING="true",
                      LIVE_TRADING_ACKNOWLEDGEMENT=REAL_ACK_PHRASE)
    assert s.real_gate_problems() == []


def test_real_mode_needs_api_key_even_when_flags_set(tmp_path):
    s = make_settings(tmp_path, TRADING_MODE="REAL", ENABLE_REAL_TRADING="true",
                      LIVE_TRADING_ACKNOWLEDGEMENT=REAL_ACK_PHRASE, MUDREX_API_SECRET="")
    assert any("MUDREX_API_SECRET" in p for p in s.real_gate_problems())


def test_unsupported_currency_aborts_startup(tmp_path):
    with pytest.raises(ConfigError, match="not supported"):
        make_settings(tmp_path, TRADE_CURRENCY="INR")


@pytest.mark.parametrize("env", [
    {"MAX_MARGIN_USDT_PER_TRADE": "-5"},
    {"MAX_MARGIN_USDT_PER_TRADE": "abc"},
    {"MARGIN_RESERVE_USDT": "60"},
    {"MAX_CONCURRENT_POSITIONS": "0"},
    {"MAX_SIGNAL_AGE_MINUTES": "0"},
    {"LIMIT_ORDER_TTL_MINUTES": "-1"},
    {"MAX_ENTRY_DEVIATION_PCT": "-1"},
    {"MAX_SIGNAL_AGE_MINUTES": "2.5"},
])
def test_invalid_numeric_limits_abort(tmp_path, env):
    with pytest.raises(ConfigError):
        make_settings(tmp_path, **env)


def test_secrets_are_redacted_from_repr_and_summary(tmp_path):
    s = make_settings(tmp_path)
    assert "TEST-ONLY-SECRET" not in repr(s)
    assert "TEST-ONLY-SECRET" not in " ".join(s.redacted_summary().values())


def test_run_refuses_real_without_gates_and_places_nothing(tmp_path):
    s = make_settings(tmp_path, TRADING_MODE="REAL")
    assert asyncio.run(run_bot(s)) == 2  # refused before any network or DB writes


def test_build_engine_refuses_mutating_client_in_paper(tmp_path):
    s = make_settings(tmp_path)
    fake = FakeMudrex()
    with pytest.raises(RuntimeError, match="safety violation"):
        build_engine(s, Mode.PAPER, client=fake.client(allow_mutations=True))


# ---------------------------------------------------------- PAPER lifecycle
def test_paper_full_lifecycle_sends_only_read_requests(tmp_path):
    """Signal -> pending -> fill -> take-profit, with ONLY GET requests reaching Mudrex."""
    from app.execution_engine import ExecutionEngine
    from app.paper_broker import PaperBroker
    from app.storage import Storage
    from app.symbol_mapper import SymbolMapper

    settings = make_settings(tmp_path, TRADING_MODE="PAPER")
    fake = FakeMudrex()
    client = fake.client(allow_mutations=False)
    storage = Storage(settings.database_file)
    now = utc()
    clock = {"now": now}
    paper = PaperBroker(storage, initial_balance=settings.paper_initial_balance_usdt,
                        fee_rate=settings.paper_fee_rate,
                        maintenance_margin_rate=settings.paper_maintenance_margin_rate,
                        ttl_minutes=settings.limit_order_ttl_minutes)
    engine = ExecutionEngine(settings=settings, storage=storage, client=client,
                             mapper=SymbolMapper(client, 600), mode=Mode.PAPER, paper_broker=paper,
                             now=lambda: clock["now"])
    monitor = PositionMonitor(storage=storage, client=client, mode=Mode.PAPER, currency="USDT",
                              paper_broker=paper, ttl_minutes=120)

    async def scenario():
        assert await engine.handle_message(channel_id=-100, message_id=1, text=SIGNAL_CRV,
                                           signal_time=now) == "ACCEPTED"
        # SHORT limit at 0.3462: market below the limit -> still pending.
        fake.assets["CRVUSDT"]["price"] = "0.3400"
        await monitor.monitor_once()
        assert storage.trades()[0].trade_status is TradeStatus.ENTRY_PENDING
        # Market rises to the limit -> simulated fill at the limit price.
        fake.assets["CRVUSDT"]["price"] = "0.3470"
        await monitor.monitor_once()
        assert storage.trades()[0].trade_status is TradeStatus.ENTRY_FILLED
        # Market falls to Target 1 (8% ROI) -> simulated take-profit.
        fake.assets["CRVUSDT"]["price"] = "0.3185"
        await monitor.monitor_once()

    asyncio.run(scenario())
    trade = storage.trades()[0]
    assert trade.trade_status is TradeStatus.CLOSED
    assert trade.exit_reason == "TAKE_PROFIT_1"
    assert trade.fill_price == Decimal("0.3462")
    assert trade.realized_pnl > 0
    assert trade.mode == "PAPER"

    mutating = [c for c in fake.calls if c[0] != "GET"]
    assert mutating == [], f"PAPER mode sent mutating requests: {mutating}"
    storage.close()


def test_paper_read_only_client_blocks_every_mutation_without_network(tmp_path):
    fake = FakeMudrex()
    client = fake.client(allow_mutations=False)

    async def attempt_all():
        results = []
        for coro in (
            client.place_limit_order(symbol="CRVUSDT", side="SHORT", quantity=Decimal("1"),
                                     price=Decimal("0.3462"), takeprofit_price=Decimal("0.3361"),
                                     client_order_id="TG-1-x"),
            client.set_leverage("CRVUSDT", Decimal("5")),
            client.cancel_order("order-1"),
        ):
            try:
                await coro
            except ReadOnlyViolation:
                results.append("blocked")
        return results

    assert asyncio.run(attempt_all()) == ["blocked", "blocked", "blocked"]
    assert fake.calls == [], "blocked mutations must never reach the HTTP layer"


def test_paper_monitor_never_calls_a_mutating_endpoint_even_when_stuck_pending(tmp_path):
    from app.paper_broker import PaperBroker
    from app.storage import Storage

    fake = FakeMudrex(assets=[{**ASSET_CRV, "price": "0.1"}])
    client = fake.client(allow_mutations=False)
    storage = Storage(tmp_path / "x.db")
    broker = PaperBroker(storage, initial_balance=Decimal("1000"), fee_rate=Decimal("0"),
                         maintenance_margin_rate=Decimal("0.005"), ttl_minutes=1)
    monitor = PositionMonitor(storage=storage, client=client, mode=Mode.PAPER, currency="USDT",
                              paper_broker=broker, ttl_minutes=1)
    asyncio.run(monitor.monitor_once())
    assert [c for c in fake.calls if c[0] != "GET"] == []
    storage.close()


# ---------------------------------------------------------- code-level guards
def test_no_wallet_transfer_or_withdrawal_code_exists():
    forbidden = re.compile(r"withdraw|/wallet/futures/transfer|transfers/inr|deposit\s*\(", re.IGNORECASE)
    offenders = []
    for path in list((ROOT / "app").rglob("*.py")):
        if forbidden.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path))
    assert offenders == [], f"forbidden transfer/withdrawal reference in: {offenders}"


def test_entry_order_never_sends_stop_loss():
    """The entry order method has a hard guard that stop-loss request fields are never sent at entry."""
    src = (ROOT / "app" / "mudrex_client.py").read_text(encoding="utf-8")
    assert 'if "is_stoploss" in body or "stoploss_price" in body:  # hard invariant, survives -O' in src


def test_entry_order_is_always_limit():
    """Entry orders are strictly LIMIT orders."""
    src = (ROOT / "app" / "mudrex_client.py").read_text(encoding="utf-8")
    assert '"trigger_type": "LIMIT"' in src


def test_real_confirmation_refuses_non_tty(monkeypatch):
    import io

    from app import main as app_main

    monkeypatch.setattr(app_main.sys, "stdin", io.StringIO("I ACCEPT NO-STOP-LOSS LIVE TRADING RISK\n"))
    assert app_main._confirm_real_interactively() is False


def test_real_confirmation_requires_exact_phrase_in_tty(monkeypatch):
    import io

    from app import main as app_main

    class TTY(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(app_main.sys, "stdin", TTY())
    monkeypatch.setattr("builtins.input", lambda *_: "yes please")
    assert app_main._confirm_real_interactively() is False
    monkeypatch.setattr("builtins.input", lambda *_: app_main.REAL_CONFIRM_PHRASE)
    assert app_main._confirm_real_interactively() is True


def test_real_run_bot_respects_skip_interactive(tmp_path):
    from app.main import run_bot

    # Even with skip_interactive=True, gates MUST still be verified
    s_invalid = make_settings(tmp_path, TRADING_MODE="REAL")
    assert asyncio.run(run_bot(s_invalid, skip_interactive=True)) == 2
