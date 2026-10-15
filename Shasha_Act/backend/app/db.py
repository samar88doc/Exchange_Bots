"""Async SQLAlchemy setup. Runtime DDL is intentionally disabled; use Alembic."""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from fastapi import Request
from sqlalchemy import Numeric
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class ExactDecimal(Numeric):
    """NUMERIC-backed ``Decimal`` that survives SQLite's float-backed storage.

    SQLite has no native DECIMAL type, so a ``Decimal`` such as ``80515.2`` is stored as a
    binary float and read back through SQLAlchemy's decimal processor as
    ``80515.199999999997``. That noise breaks the exact level comparisons this bot relies on
    (protective stop price, target price and slice quantity) and leaks into dashboard
    payloads. Results are therefore re-quantized to nine decimal places on load, which is
    finer than any price or quantity step Shark publishes while removing binary float noise.
    PostgreSQL keeps its native exact ``NUMERIC`` storage.
    """

    cache_ok = True

    def __init__(self) -> None:
        super().__init__(28, 12, asdecimal=True)

    def result_processor(self, dialect: object, coltype: object) -> Callable[[object], object]:
        base = super().result_processor(dialect, coltype)

        def process(value: object) -> object:
            if value is None:
                return None
            parsed = base(value) if base is not None else value
            if not isinstance(parsed, Decimal):
                try:
                    parsed = Decimal(str(parsed))
                except (InvalidOperation, ValueError):
                    return value
            try:
                return parsed.quantize(Decimal("0.000000001"))
            except (InvalidOperation, ValueError):
                return parsed

        return process


def utcnow() -> datetime:
    return datetime.now(UTC)


def make_engine(database_url: str) -> AsyncEngine:
    kwargs: dict[str, object] = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"timeout": 30}
    return create_async_engine(database_url, **kwargs)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def session_dependency(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency, with the session factory attached by create_app()."""
    factory = request.app.state.session_factory
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
