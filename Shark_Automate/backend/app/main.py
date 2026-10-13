"""FastAPI application entrypoint: middleware, lifespan, routers."""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router as api_router
from app.api.webhooks import router as webhook_router
from app.core.config import get_settings
from app.core.logging import correlation_id_var, setup_logging
from app.db.session import dispose_engine, init_engine, session_scope
from app.services.bootstrap import recover_state, seed_reference_data
from app.services.runtime import build_context
from app.workers.monitor import WorkerManager

log = logging.getLogger("trading.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_dir)
    init_engine(settings)
    ctx = build_context(settings)
    app.state.ctx = ctx
    app.state.workers = WorkerManager(ctx)

    # Reference data + restart recovery (DB is the source of truth).
    async with session_scope() as session:
        await seed_reference_data(session, settings)
        report = await recover_state(session, settings)
    log.info("restart recovery: %s", report)

    app.state.workers.start()
    yield
    await app.state.workers.stop()
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def security_and_logging(request: Request, call_next):
        cid = request.headers.get("x-correlation-id") or uuid.uuid4().hex[:16]
        correlation_id_var.set(cid)
        started = time.perf_counter()
        if int(request.headers.get("content-length") or 0) > settings.webhook_max_body_bytes:
            return JSONResponse({"detail": "payload too large"}, status_code=413)
        response: Response = await call_next(request)
        response.headers["X-Correlation-Id"] = cid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        elapsed = int((time.perf_counter() - started) * 1000)
        log.info("%s %s -> %s (%dms)", request.method, request.url.path, response.status_code, elapsed)
        return response

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):  # noqa: ANN001
        log.exception("unhandled error")
        return JSONResponse(
            {"detail": "internal error", "code": "INTERNAL_ERROR"},
            status_code=500,
        )

    app.include_router(webhook_router)
    app.include_router(api_router)
    return app


app = create_app()
