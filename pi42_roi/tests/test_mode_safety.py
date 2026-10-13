"""Mode safety: PAPER can never mutate Pi42; REAL needs every gate; no money movement."""

import asyncio
import io
import re
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import REAL_ACK_PHRASE, ConfigError
from app.engine_factory import build_engine
from app.main import run_bot
from app.models import Mode, TradeStatus
from app.paper_broker import PaperBroker
from app.pi42_client import ReadOnlyViolation
from app.storage import Storage
from tests.helpers import CHANNEL_ID, SIGNAL_CRV, FakePi42, make_settings, utc

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------- config gates
def test_default_mode_is_paper(tmp_path):
    s = make_settings(tmp_path, TRADING_MODE="")
    assert s.trading_mode == "PAPER" and s.enable_real_trading is False


def test_invalid_mode_falls_back_to_paper_with_a_warning(tmp_path):
    s = make_settings(tmp_path, TRADING_MODE="LIVE!!")
    assert s.trading_mode == "PAPER"
    assert "not a valid mode" in (s.mode_warning or "")


def test_real_without_flags_lists_every_gate_problem(tmp_path):
    s = make_settings(tmp_path, TRADING_MODE="REAL")
    problems = " | ".join(s.real_gate_problems())
    assert "ENABLE_REAL_TRADING" in problems and "LIVE_TRADING_ACKNOWLEDGEMENT" in problems


def test_real_requires_the_exact_acknowledgement_phrase(tmp_path):
    s = make_settings(tmp_path, TRADING_MODE="REAL", ENABLE_REAL_TRADING="true",
                      LIVE_TRADING_ACKNOWLEDGEMENT="yes please")
    assert any("LIVE_TRADING_ACKNOWLEDGEMENT" in p for p in s.real_gate_problems())


def test_real_gates_pass_only_when_everything_is_explicit(tmp_path):
    s = make_settings(tmp_path, TRADING_MODE="REAL", ENABLE_REAL_TRADING="true",
                      LIVE_TRADING_ACKNOWLEDGEMENT=REAL_ACK_PHRASE)
    assert s.real_gate_problems() == []


def test_real_mode_needs_api_credentials_even_with_the_flags_set(tmp_path):
    s = make_settings(tmp_path, TRADING_MODE="REAL", ENABLE_REAL_TRADING="true",
                      LIVE_TRADING_ACKNOWLEDGEMENT=REAL_ACK_PHRASE, PI42_API_KEY="")
    assert any("PI42_API_KEY" in p for p in s.real_gate_problems())


def test_real_mode_needs_telegram_configuration(tmp_path):
    s = make_settings(tmp_path, TRADING_MODE="REAL", ENABLE_REAL_TRADING="true",
                      LIVE_TRADING_ACKNOWLEDGEMENT=REAL_ACK_PHRASE, TELEGRAM_PHONE="")
    assert any("TELEGRAM_PHONE" in p for p in s.real_gate_problems())


@pytest.mark.parametrize("env", [
    {"PI42_MARKET": "EUR"},
    {"PI42_MARGIN_ASSET": "EUR"},
    {"PI42_MARGIN_MODE": "HEDGE"},
    {"PI42_BASE_URL": "http://fapi.pi42.com"},
    {"PI42_PUBLIC_BASE_URL": "http://api.pi42.com"},
    {"TELEGRAM_API_ID": "abc"},
    {"TELEGRAM_CHANNEL_INVITE_URL": "https://example.com/join"},
    {"MAX_MARGIN_PER_TRADE": "-5"},
    {"MAX_MARGIN_PER_TRADE": "abc"},
    {"MARGIN_RESERVE": "60"},
    {"MAX_CONCURRENT_POSITIONS": "0"},
    {"MAX_SIGNAL_AGE_MINUTES": "0"},
    {"LIMIT_ORDER_TTL_MINUTES": "-1"},
    {"MAX_ENTRY_DEVIATION_PCT": "-1"},
    {"MAX_SIGNAL_AGE_MINUTES": "2.5"},
    {"TARGET_ROI_PCT": "0"},
    {"TARGET_ROI_PCT": "-3"},
    {"PAPER_FEE_RATE": "0.5"},
])
def test_invalid_settings_abort_startup(tmp_path, env):
    with pytest.raises(ConfigError):
        make_settings(tmp_path, **env)


def test_margin_reserve_must_be_smaller_than_the_per_trade_margin(tmp_path):
    with pytest.raises(ConfigError, match="MARGIN_RESERVE"):
        make_settings(tmp_path, MAX_MARGIN_PER_TRADE="2", MARGIN_RESERVE="2")


def test_secrets_never_appear_in_repr_or_the_redacted_summary(tmp_path):
    s = make_settings(tmp_path)
    assert "TEST-ONLY-SECRET" not in repr(s)
    assert "TEST-ONLY-SECRET" not in " ".join(s.redacted_summary().values())
    assert s.redacted_summary()["PI42_API_SECRET"] == "set"


def test_defaults_are_conservative(tmp_path):
    s = make_settings(tmp_path)
    assert s.market == "USDT" and s.margin_asset == "USDT"
    assert s.margin_mode == "ISOLATED"
    assert s.paper_initial_balance == Decimal("1000")
    assert s.max_margin_per_trade == Decimal("50") and s.margin_reserve == Decimal("2")
    assert s.target_roi_pct == Decimal("8.0")
    assert s.send_ref_id is True


# ------------------------------------------------------------ engine factory
def test_paper_with_a_mutating_client_is_a_safety_violation(tmp_path):
    settings = make_settings(tmp_path)
    fake = FakePi42()
    with pytest.raises(RuntimeError, match="safety violation"):
        build_engine(settings, Mode.PAPER, client=fake.client(allow_mutations=True))


def test_real_mode_enables_mutations_and_paper_does_not(tmp_path):
    settings = make_settings(tmp_path)
    fake = FakePi42()
    paper_runtime = build_engine(settings, Mode.PAPER, client=fake.client(allow_mutations=False))
    assert paper_runtime.client.allow_mutations is False
    assert paper_runtime.paper is not None
    real_settings = make_settings(tmp_path, TRADING_MODE="REAL", ENABLE_REAL_TRADING="true",
                                  LIVE_TRADING_ACKNOWLEDGEMENT=REAL_ACK_PHRASE)
    real_runtime = build_engine(real_settings, Mode.REAL, client=fake.client(allow_mutations=True))
    assert real_runtime.client.allow_mutations is True
    assert real_runtime.paper is None
    real_runtime.storage.close()
    paper_runtime.storage.close()


def test_factory_refuses_to_start_without_credentials(tmp_path):
    settings = make_settings(tmp_path, PI42_API_KEY="", PI42_API_SECRET="")
    with pytest.raises(ConfigError, match="PI42_API_KEY"):
        build_engine(settings, Mode.PAPER)


def test_factory_refuses_to_start_without_telegram_configuration(tmp_path):
    settings = make_settings(tmp_path, TELEGRAM_API_ID="", TELEGRAM_PHONE="")
    with pytest.raises(ConfigError, match="Telegram configuration incomplete"):
        build_engine(settings, Mode.PAPER)


def test_real_run_is_refused_without_the_gates_before_any_network_or_db_write(tmp_path):
    settings = make_settings(tmp_path, TRADING_MODE="REAL")
    assert asyncio.run(run_bot(settings)) == 2
    assert not settings.database_file.exists()


def test_real_run_with_skip_interactive_still_enforces_the_gates(tmp_path):
    settings = make_settings(tmp_path, TRADING_MODE="REAL")
    assert asyncio.run(run_bot(settings, skip_interactive=True)) == 2


# --------------------------------------------------------------- TTY prompt
def test_real_confirmation_refuses_a_non_tty_start(monkeypatch):
    from app import main as app_main

    monkeypatch.setattr(app_main.sys, "stdin", io.StringIO(REAL_ACK_PHRASE + "\n"))
    assert app_main._confirm_real_interactively() is False


def test_real_confirmation_requires_the_exact_phrase_in_a_tty(monkeypatch):
    from app import main as app_main

    class TTY(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(app_main.sys, "stdin", TTY())
    monkeypatch.setattr("builtins.input", lambda *_: "yes please")
    assert app_main._confirm_real_interactively() is False
    monkeypatch.setattr("builtins.input", lambda *_: app_main.REAL_CONFIRM_PHRASE)
    assert app_main._confirm_real_interactively() is True


# ------------------------------------------------------------ source guards
def test_no_money_movement_code_exists():
    """The bot never transfers, withdraws or deposits: funding is a human action."""
    forbidden = re.compile(
        r"def\s+\w*(withdraw|transfer|deposit)\w*|\.(withdraw|transfer|deposit)\s*\(|"
        r"[\"'](POST|PUT|PATCH)[\"'],\s*[\"']/v1/wallet/",
        re.IGNORECASE,
    )
    offenders = [str(path) for path in (ROOT / "app").rglob("*.py")
                 if forbidden.search(path.read_text(encoding="utf-8"))]
    assert offenders == [], f"forbidden money-movement code in: {offenders}"
    # Wallet access is read-only: exactly one wallet endpoint, always a GET.
    source = (ROOT / "app" / "pi42_client.py").read_text(encoding="utf-8")
    assert source.count("/v1/wallet/") == 1
    assert '"/v1/wallet/futures-wallet/details"' in source


def test_the_client_never_assigns_a_stop_loss_field():
    source = (ROOT / "app" / "pi42_client.py").read_text(encoding="utf-8")
    assert 'body["stopLossPrice"]' not in source and "body['stopLossPrice']" not in source
    assert 'if "stopLossPrice" in body' in source, "the hard guard must stay"


def test_the_entry_order_is_always_a_limit_order():
    source = (ROOT / "app" / "pi42_client.py").read_text(encoding="utf-8")
    assert '"type": "LIMIT",' in source
    assert '"type": "MARKET"' not in source.split("def close_position")[0]


def test_paper_never_places_an_order_even_through_a_full_signal_lifecycle(tmp_path):
    settings = make_settings(tmp_path, TRADING_MODE="PAPER")
    fake = FakePi42(tickers={"CRVUSDT": "0.3470"})
    client = fake.client(allow_mutations=False)
    storage = Storage(settings.database_file)
    PaperBroker(storage, initial_balance=settings.paper_initial_balance,
                fee_rate=settings.paper_fee_rate,
                maintenance_margin_rate=settings.paper_maintenance_margin_rate,
                ttl_minutes=settings.limit_order_ttl_minutes)
    engine = build_engine(settings, Mode.PAPER, client=client)
    runtime_paper = engine.paper
    assert runtime_paper is not None
    asyncio.run(engine.engine.handle_message(channel_id=CHANNEL_ID, message_id=1,
                                             text=SIGNAL_CRV, signal_time=utc()))
    asyncio.run(engine.monitor.monitor_once())
    assert storage.trades(mode="PAPER")[0].trade_status is TradeStatus.ENTRY_FILLED
    assert [c for c in fake.calls if c[0] != "GET"] == []
    engine.storage.close()
    storage.close()


def test_read_only_client_blocks_every_mutation_without_touching_the_network():
    fake = FakePi42()
    client = fake.client(allow_mutations=False)

    async def attempt():
        blocked = 0
        for coro in (
            client.place_limit_order(symbol="CRVUSDT", side="SELL", quantity=Decimal("1"),
                                     price=Decimal("0.3462"), margin_asset="USDT"),
            client.set_preference("CRVUSDT", Decimal("5"), "ISOLATED"),
            client.close_position(symbol="CRVUSDT", position_id="p", side_of_position="SHORT",
                                  quantity=Decimal("1"), margin_asset="USDT"),
            client.cancel_order("cli-1"),
        ):
            try:
                await coro
            except ReadOnlyViolation:
                blocked += 1
        return blocked

    assert asyncio.run(attempt()) == 4
    assert fake.calls == []
