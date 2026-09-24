"""Application factory."""
from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import Settings, get_settings
from app.deps import AuthRequired, protect
from app.logging_setup import setup_logging
from app.routers import admin, auth, dev, guests, health, livekit, local_storage, meetings, recordings
from app.security import csrf, sessions
from app.security.headers import security_headers
from app.templating import templates

log = logging.getLogger("app")
BASE_DIR = Path(__file__).parent

_ERROR_TEXT = {
    403: ("Not allowed", "You do not have permission to do that."),
    404: ("Page not found", "We could not find what you were looking for."),
    429: ("Slow down", "Too many requests. Please wait a moment and try again."),
    500: ("Something went wrong", "It is not your fault. Please try again in a moment."),
}


def _init_sentry(settings: Settings) -> None:
    if not settings.sentry_dsn:
        return
    import sentry_sdk

    from app.sentry_scrub import scrub_event

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.app_env,
        send_default_pii=False,
        max_request_body_size="never",
        before_send=scrub_event,  # type: ignore[arg-type]
    )


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging()
    _init_sentry(settings)

    app = FastAPI(
        title="Company Meeting Platform",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(protect)],
    )
    headers = security_headers(settings)

    @app.middleware("http")
    async def base_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = uuid.uuid4().hex[:12]
        anon_name = sessions.anon_cookie_name()
        seed = request.cookies.get(anon_name)
        new_seed = seed is None or len(seed) > 100
        request.state.anon_seed = csrf.new_anon_seed() if new_seed else seed
        response = await call_next(request)
        for key, value in headers.items():
            response.headers.setdefault(key, value)
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        if new_seed:
            sessions.set_cookie(response, anon_name, request.state.anon_seed, max_age=None)
        return response

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(meetings.router)
    app.include_router(livekit.router)
    app.include_router(guests.router)
    app.include_router(recordings.router)
    app.include_router(admin.router)
    app.include_router(dev.router)
    app.include_router(local_storage.router)

    _register_error_handlers(app)
    return app


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api/") or request.headers.get("accept", "").startswith(
        "application/json"
    )


def _error_response(request: Request, status: int, detail: Any = None) -> Response:
    title, message = _ERROR_TEXT.get(status, _ERROR_TEXT[500])
    if _wants_json(request):
        text = detail if isinstance(detail, str) else message
        return JSONResponse({"error": text}, status_code=status)
    if request.headers.get("hx-request"):
        return Response(message, status_code=status)
    return templates.TemplateResponse(
        request, "errors/error.html", {"status": status, "title": title, "message": message}, status_code=status
    )


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AuthRequired)
    async def _auth_required(request: Request, exc: AuthRequired) -> Response:
        if _wants_json(request):
            return JSONResponse({"error": "Please log in."}, status_code=401)
        if request.headers.get("hx-request"):
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code in (301, 302, 303, 307, 308):
            return Response(status_code=exc.status_code, headers=dict(exc.headers or {}))
        return _error_response(request, exc.status_code, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> Response:
        if _wants_json(request):
            return JSONResponse({"error": "Invalid request."}, status_code=422)
        return _error_response(request, 404)

    @app.exception_handler(Exception)
    async def _server_error(request: Request, exc: Exception) -> Response:
        log.exception("unhandled error", extra={"request_id": getattr(request.state, "request_id", None)})
        return _error_response(request, 500)


app = create_app()
