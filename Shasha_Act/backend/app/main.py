"""FastAPI application factory and ASGI lifespan for Shasha_Act."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.middleware.cors import CORSMiddleware

from app.api import public_router, router
from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory
from app.runtime import build_context
from app.security import SlidingWindowRateLimiter
from app.services.risk import ensure_risk_control
from app.webhooks import router as webhook_router

log = logging.getLogger("shasha_act.api")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = PROJECT_ROOT / "frontend"


async def _market_poll_loop(context: Any, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await context.market.rest_bootstrap()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            context.market.last_error = type(exc).__name__
        try:
            await asyncio.wait_for(stop.wait(), timeout=context.settings.market_data_poll_seconds)
        except TimeoutError:
            pass


def create_app(
    settings: Settings | None = None,
    *,
    engine: AsyncEngine | None = None,
    shark_http_client: Any | None = None,
    telegram_http_client: Any | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()
    db_engine = engine or make_engine(app_settings.database_url)
    session_factory = make_session_factory(db_engine)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if app_settings.environment == "production":
            if app_settings.dashboard_auth_enabled and not app_settings.dashboard_api_token:
                raise RuntimeError("DASHBOARD_API_TOKEN must be configured in production when DASHBOARD_AUTH_ENABLED=true")
            if not app_settings.tradingview_webhook_token:
                raise RuntimeError("TRADINGVIEW_WEBHOOK_TOKEN must be configured in production")
            if app_settings.database_url.startswith("sqlite"):
                raise RuntimeError("Production requires PostgreSQL; SQLite is not supported")
        application.state.settings = app_settings
        application.state.engine = db_engine
        application.state.session_factory = session_factory
        application.state.risk_lock = asyncio.Lock()
        application.state.webhook_limiter = SlidingWindowRateLimiter(
            app_settings.webhook_rate_limit_per_minute
        )
        application.state.wallet_cache = None
        context = build_context(
            app_settings,
            session_factory,
            shark_http_client=shark_http_client,
            telegram_http_client=telegram_http_client,
        )
        application.state.context = context

        async with session_factory() as session:
            await ensure_risk_control(session, app_settings)
            await session.commit()

        stop = asyncio.Event()
        background: list[asyncio.Task[Any]] = []
        if app_settings.run_workers:
            context.worker.start()
            background.append(asyncio.create_task(context.reconciler.run(stop), name="shasha-act-reconciler"))
        if app_settings.run_market_stream:
            background.append(asyncio.create_task(context.market.run_public_stream(stop), name="shasha-act-market-ws"))
        if app_settings.run_market_poll:
            background.append(asyncio.create_task(_market_poll_loop(context, stop), name="shasha-act-market-rest"))

        try:
            yield
        finally:
            stop.set()
            for task in background:
                task.cancel()
            if background:
                await asyncio.gather(*background, return_exceptions=True)
            await context.worker.stop()
            await context.close()
            await db_engine.dispose()

    application = FastAPI(
        title="Shasha_Act Trading Operations",
        description="PAPER-first TradingView SHASHA signal processing and Shark Exchange operations API.",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    application.state.settings = app_settings
    application.state.engine = db_engine
    application.state.session_factory = session_factory
    application.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.cors_origins_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
        allow_headers=["Content-Type", "X-Dashboard-Token"],
        max_age=600,
    )

    @application.middleware("http")
    async def secure_response_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.url.path.startswith("/api/") or request.url.path.startswith("/assets/") or request.url.path in ("/", "/index.html"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; "
            "base-uri 'self'; frame-ancestors 'none'",
        )
        if app_settings.environment == "production" and request.url.scheme == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    @application.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        # Avoid logging request paths (the webhook token is part of that path) and
        # exception representations from clients that may include tokenized URLs.
        log.error("unhandled request failure (%s)", type(exc).__name__)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    application.include_router(public_router)
    application.include_router(router)
    application.include_router(webhook_router)
    if FRONTEND_DIR.is_dir():
        application.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets", check_dir=False), name="assets")

    @application.get("/", include_in_schema=False)
    async def dashboard_index() -> FileResponse:
        path = FRONTEND_DIR / "index.html"
        if not path.is_file():
            return FileResponse(FRONTEND_DIR / "unavailable.html", status_code=503)
        return FileResponse(path)

    return application


app = create_app()
