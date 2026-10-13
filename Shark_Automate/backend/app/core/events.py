"""In-process pub/sub used to fan out real-time UI updates over SSE.

Single broadcaster per process; subscribers are asyncio queues with bounded size
so a slow client can never stall the trading engine.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._history: list[str] = []
        self._history_limit = 200

    def publish(self, topic: str, data: dict[str, Any]) -> None:
        message = json.dumps({"topic": topic, "ts": time.time(), "data": data}, default=str)
        self._history.append(message)
        if len(self._history) > self._history_limit:
            self._history = self._history[-self._history_limit:]
        dead: list[asyncio.Queue[str]] = []
        for q in self._subscribers:
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                dead.append(q)  # slow consumer: drop rather than block trading
        for q in dead:
            self._subscribers.discard(q)

    def subscribe(self, maxsize: int = 500) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subscribers.discard(q)

    async def stream(self) -> AsyncIterator[str]:
        q = self.subscribe()
        try:
            while True:
                yield await q.get()
        finally:
            self.unsubscribe(q)


event_bus = EventBus()
