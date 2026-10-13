"""Telethon (MTProto) listener for the single configured signal channel.

* Subscribes only to the resolved channel (NewMessage events filtered by chat).
* Edits (MessageEdited) are not subscribed, so an edit can never create a trade.
* First run sets a baseline at the newest message id; history is never executed.
* The last processed message id is persisted; on restart, messages newer than it
  are caught up. Duplicates are blocked by the storage unique constraint.
* Reconnects use bounded backoff; FloodWait honours Telegram's requested delay.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import timezone
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import ChatInviteAlready
from telethon.utils import get_peer_id

from app.config import Settings
from app.logging_config import log_event
from app.models import utc_now
from app.storage import Storage

logger = logging.getLogger("telegram")

META_CHANNEL_ID = "telegram_channel_id"
META_LAST_SEEN = "telegram_last_seen_message_id"
MAX_BACKOFF_SECONDS = 300


class TelegramSetupError(Exception):
    """Fatal Telegram problem: not authorised, not a member, or bad configuration."""


def parse_invite_hash(url: str) -> str:
    match = re.search(r"t\.me/(?:joinchat/|\+)([A-Za-z0-9_-]+)", url)
    if not match:
        raise TelegramSetupError("TELEGRAM_CHANNEL_INVITE_URL does not look like a t.me invite link")
    return match.group(1)


def ensure_session_dir(session_name: str) -> None:
    session_path = Path(session_name if session_name.endswith(".session") else f"{session_name}.session")
    parent = session_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(parent, 0o700)
    except OSError:
        pass


def build_client(settings: Settings) -> TelegramClient:
    if settings.telegram_api_id is None or not settings.telegram_api_hash:
        raise TelegramSetupError("TELEGRAM_API_ID and TELEGRAM_API_HASH are required")
    ensure_session_dir(settings.telegram_session_name)
    return TelegramClient(settings.telegram_session_name, settings.telegram_api_id, settings.telegram_api_hash)


def protect_session_file(session_name: str) -> None:
    path = Path(session_name if session_name.endswith(".session") else f"{session_name}.session")
    if path.exists():
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


async def resolve_channel(client: TelegramClient, settings: Settings, storage: Storage):
    """Return the channel entity: stored id first, otherwise the invite link (must already be joined)."""
    stored = storage.get_meta(META_CHANNEL_ID)
    if stored:
        try:
            return await client.get_entity(int(stored))
        except Exception as exc:  # noqa: BLE001
            logger.warning("event=telegram.stored_channel_unavailable error=%s; re-resolving invite link", type(exc).__name__)

    invite_hash = parse_invite_hash(settings.telegram_channel_invite_url)
    try:
        invite = await client(CheckChatInviteRequest(invite_hash))
    except Exception as exc:  # noqa: BLE001
        raise TelegramSetupError(
            f"could not resolve the channel invite link ({type(exc).__name__}). "
            "Join the channel in the Telegram app with this same account, then retry."
        ) from exc
    if not isinstance(invite, ChatInviteAlready):
        raise TelegramSetupError(
            "this Telegram account is not a member of the signal channel. Open the invite link in the "
            "Telegram app with this account, join, then restart the application. The bot never bypasses access."
        )
    entity = invite.chat
    channel_id = get_peer_id(entity)
    storage.set_meta(META_CHANNEL_ID, str(channel_id), utc_now())
    log_event(logger, "telegram.channel_resolved", channel_id=channel_id, title=getattr(entity, "title", ""))
    return entity


class TelegramSignalListener:
    def __init__(self, *, settings: Settings, storage: Storage, engine, client: TelegramClient | None = None) -> None:
        self._settings = settings
        self._storage = storage
        self._engine = engine
        self._client = client or build_client(settings)
        self._entity = None
        self._channel_id: int | None = None
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            try:
                await self._connect_and_listen()
                backoff = 5.0
                if not self._stop.is_set():
                    log_event(logger, "telegram.disconnected", level=logging.WARNING)
            except TelegramSetupError:
                raise
            except FloodWaitError as exc:
                log_event(logger, "telegram.flood_wait", level=logging.WARNING, seconds=exc.seconds)
                await self._sleep_or_stop(exc.seconds + 1)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("event=telegram.error error=%s", type(exc).__name__)
                await self._sleep_or_stop(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
            finally:
                if self._client.is_connected():
                    await self._client.disconnect()

    async def _connect_and_listen(self) -> None:
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise TelegramSetupError(
                "Telegram session is not authorised. Run `python -m app.main auth` interactively first."
            )
        protect_session_file(self._settings.telegram_session_name)
        if self._entity is None:
            self._entity = await resolve_channel(self._client, self._settings, self._storage)
            self._channel_id = get_peer_id(self._entity)
            self._client.add_event_handler(self._on_new_message, events.NewMessage(chats=self._entity))
        await self._establish_baseline()
        await self._catch_up()
        log_event(logger, "telegram.connected", channel_id=self._channel_id)

        async def poll_loop() -> None:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    pass
                if not self._stop.is_set():
                    try:
                        await self._catch_up()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("event=telegram.poll_catchup_error error=%s", exc)

        poll_task = asyncio.create_task(poll_loop(), name="telegram-poll-loop")
        try:
            await self._client.run_until_disconnected()
        finally:
            poll_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                pass

    async def _establish_baseline(self) -> None:
        if self._storage.get_meta(META_LAST_SEEN) is not None:
            return
        latest = await self._client.get_messages(self._entity, limit=1)
        baseline = latest[0].id if latest else 0
        self._storage.set_meta(META_LAST_SEEN, str(baseline), utc_now())
        log_event(logger, "telegram.baseline_set", baseline_message_id=baseline,
                  note="historical messages are NOT executed")

    async def _catch_up(self) -> None:
        last_seen = int(self._storage.get_meta(META_LAST_SEEN) or 0)
        count = 0
        async for message in self._client.iter_messages(self._entity, min_id=last_seen, reverse=True):
            await self._process(message)
            count += 1
        if count:
            log_event(logger, "telegram.catch_up_done", processed=count)

    async def _on_new_message(self, event) -> None:
        try:
            await self._process(event.message)
        except Exception:  # noqa: BLE001 - visible in logs, listener keeps running
            logger.exception("event=telegram.handler_error message_id=%s", getattr(event.message, "id", None))

    async def _process(self, message) -> None:
        async with self._lock:
            if self._channel_id is None or get_peer_id(message.peer_id) != self._channel_id:
                return  # unrelated chat (defence in depth)
            last_seen = int(self._storage.get_meta(META_LAST_SEEN) or 0)
            if message.id <= last_seen:
                return
            text = message.message or ""
            signal_time = message.date
            if signal_time is not None and signal_time.tzinfo is None:
                signal_time = signal_time.replace(tzinfo=timezone.utc)
            await self._engine.handle_message(
                channel_id=self._channel_id,
                message_id=message.id,
                text=text,
                signal_time=signal_time,
            )
            self._storage.set_meta(META_LAST_SEEN, str(max(last_seen, message.id)), utc_now())

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return
