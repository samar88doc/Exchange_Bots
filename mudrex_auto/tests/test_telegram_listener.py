"""Telegram listener logic without a network connection."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from telethon.tl.types import PeerChannel

from app.storage import Storage
from app.telegram_listener import (
    META_LAST_SEEN,
    TelegramSetupError,
    TelegramSignalListener,
    parse_invite_hash,
)
from tests.helpers import make_settings

CHANNEL = -1001234567890


@dataclass
class FakeEngine:
    calls: list

    async def handle_message(self, **kwargs):
        self.calls.append(kwargs)
        return "ACCEPTED"


class FakeTelegram:
    def is_connected(self):
        return True


def msg(message_id, text="x", chat=CHANNEL):
    # -100 prefixed marked id -> bare channel id for PeerChannel
    bare = abs(chat) - 1000000000000
    return SimpleNamespace(id=message_id, message=text, date=datetime.now(timezone.utc),
                           peer_id=PeerChannel(channel_id=bare))


def make_listener(tmp_path):
    settings = make_settings(tmp_path)
    storage = Storage(settings.database_file)
    engine = FakeEngine(calls=[])
    listener = TelegramSignalListener(settings=settings, storage=storage, engine=engine, client=FakeTelegram())
    listener._channel_id = CHANNEL
    return listener, storage, engine


def test_invite_hash_parsed():
    assert parse_invite_hash("https://t.me/+Xh7OTE_G_jcxMDE0") == "Xh7OTE_G_jcxMDE0"
    with pytest.raises(TelegramSetupError):
        parse_invite_hash("https://example.com/nothing")


def test_messages_from_unrelated_chats_are_ignored(tmp_path):
    listener, storage, engine = make_listener(tmp_path)
    storage.set_meta(META_LAST_SEEN, "0", datetime.now(timezone.utc))
    asyncio.run(listener._process(msg(5, chat=-1009999999999)))
    assert engine.calls == []


def test_messages_at_or_below_last_seen_are_not_reprocessed(tmp_path):
    listener, storage, engine = make_listener(tmp_path)
    storage.set_meta(META_LAST_SEEN, "10", datetime.now(timezone.utc))
    asyncio.run(listener._process(msg(10)))
    asyncio.run(listener._process(msg(9)))
    assert engine.calls == []


def test_new_message_is_handed_to_engine_and_watermark_advances(tmp_path):
    listener, storage, engine = make_listener(tmp_path)
    storage.set_meta(META_LAST_SEEN, "10", datetime.now(timezone.utc))
    asyncio.run(listener._process(msg(11, text="hello")))
    assert len(engine.calls) == 1 and engine.calls[0]["message_id"] == 11
    assert storage.get_meta(META_LAST_SEEN) == "11"


def test_first_run_baseline_prevents_historical_execution(tmp_path):
    listener, storage, engine = make_listener(tmp_path)

    class History:
        async def get_messages(self, entity, limit):
            return [msg(500)]

    listener._client = History()
    asyncio.run(listener._establish_baseline())
    assert storage.get_meta(META_LAST_SEEN) == "500"
    # Old history (<=500) never reaches the engine.
    asyncio.run(listener._process(msg(499)))
    assert engine.calls == []


def test_handler_exceptions_are_visible_not_silent(tmp_path, caplog):
    listener, storage, engine = make_listener(tmp_path)

    class Boom(FakeEngine):
        async def handle_message(self, **kwargs):
            raise RuntimeError("engine exploded")

    listener._engine = Boom(calls=[])
    storage.set_meta(META_LAST_SEEN, "1", datetime.now(timezone.utc))
    event = SimpleNamespace(message=msg(2, text="x"))
    with caplog.at_level("ERROR"):
        asyncio.run(listener._on_new_message(event))
    assert "telegram.handler_error" in caplog.text
