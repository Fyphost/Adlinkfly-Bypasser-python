"""FastAPI application: wiring, middleware, error handling and routes.

Exposes ``POST /api/resolve`` (authenticated + rate-limited), ``GET /health``,
and a root redirect to the interactive docs. All responses -- successful,
verification-required, or error -- share a consistent JSON shape.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from .browser_agent import BrowserAgent
from .config import Settings, get_settings
from .exceptions import APIError, BrowserUnavailableError
from .http_client import CloudscraperClient
from .logging_config import configure_logging, get_logger, request_id_var
from .page_analyzer import PageAnalyzer
from .ratelimit import SlidingWindowRateLimiter
from .resolver import LinkResolver
from .schemas import ErrorResponse, HealthResponse, ResolveRequest, ResolveResponse
from .security import enforce_rate_limit
from .status import ResolveStatus

logger = get_logger(__name__)

#: HTTP status code per resolution status. Resolution *outcomes* (we ran the
#: resolver and produced a verdict) return 200; problems with the *request*
#: itself return a 4xx.
STATUS_CODE_MAP: dict[ResolveStatus, int] = {
    ResolveStatus.RESOLVED: 200,
    ResolveStatus.VERIFICATION_REQUIRED: 200,
    ResolveStatus.FAILED: 200,
    ResolveStatus.TIMEOUT: 200,
    ResolveStatus.INVALID_URL: 400,
    ResolveStatus.UNSUPPORTED_DOMAIN: 422,
}


def build_components(
    settings: Settings,
) -> tuple[LinkResolver, BrowserAgent, SlidingWindowRateLimiter]:
    """Construct the resolver, browser agent and rate limiter from settings."""

    validator = _build_validator(settings)
    analyzer = PageAnalyzer()
    http_client = CloudscraperClient(
        validator,
        connect_timeout=settings.http_connect_timeout,
        read_timeout=settings.http_read_timeout,
        max_redirects=settings.max_redirects,
        max_response_bytes=settings.max_response_bytes,
        user_agent=settings.user_agent,
    )
    browser_agent = BrowserAgent(
        analyzer,
        validator,
        headless=settings.browser_headless,
        navigation_timeout_ms=settings.browser_navigation_timeout_ms,
        max_steps=settings.browser_max_steps,
        countdown_max_seconds=settings.browser_countdown_max_seconds,
        user_agent=settings.user_agent,
        slow_mo_ms=settings.browser_slow_mo_ms,
    )
    resolver = LinkResolver(settings, validator, analyzer, http_client, browser_agent)
    rate_limiter = SlidingWindowRateLimiter(
        settings.rate_limit_requests, settings.rate_limit_window_seconds
    )
    return resolver, browser_agent, rate_limiter


def _build_validator(settings: Settings):
    from .url_validator import URLValidator

    return URLValidator(
        allow_private=settings.allow_private_networks,
        allowed_schemes=settings.allowed_schemes,
        max_url_length=settings.max_url_length,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    configure_logging(settings.log_level, json_output=settings.log_json)

    for warning in settings.validate_runtime():
        logger.warning(warning)

    resolver, browser_agent, rate_limiter = build_components(settings)
    app.state.resolver = resolver
    app.state.browser_agent = browser_agent
    app.state.rate_limiter = rate_limiter

    # Best-effort eager browser launch. Failure here must not crash startup:
    # the agent retries lazily per request and degrades gracefully.
    if settings.enable_browser:
        try:
            await browser_agent.start()
        except BrowserUnavailableError as exc:
            logger.warning("Browser could not be started eagerly: %s", exc)

    logger.info(
        "application started",
        extra={"app": settings.app_name, "version": settings.app_version},
    )
    try:
        yield
    finally:
        if settings.enable_browser:
            await browser_agent.stop()
        logger.info("application stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory."""

    settings = settings or get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Resolve supported AdLinkFly-based shortened URLs to their final "
            "destination using cloudscraper with a Playwright browser fallback."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings

    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    _register_middleware(app)
    _register_exception_handlers(app)
    _register_routes(app)
    return app


# --------------------------------------------------------------------------- #
# Middleware                                                                  #
# --------------------------------------------------------------------------- #
def _register_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable]
    ):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        start = time.monotonic()
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        duration_ms = round((time.monotonic() - start) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request handled",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response


# --------------------------------------------------------------------------- #
# Exception handlers (uniform error bodies)                                   #
# --------------------------------------------------------------------------- #
def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def handle_api_error(_: Request, exc: APIError) -> JSONResponse:
        body = ErrorResponse(status=exc.status, message=exc.message)
        return JSONResponse(
            status_code=exc.status_code, content=body.model_dump(), headers=exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        detail = "; ".join(
            f"{'.'.join(str(p) for p in err.get('loc', []))}: {err.get('msg', '')}".strip(": ")
            for err in exc.errors()
        )
        body = ErrorResponse(
            status="invalid_request",
            message=detail or "The request body is invalid.",
        )
        return JSONResponse(status_code=422, content=body.model_dump())

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled exception: %s", exc)
        body = ErrorResponse(
            status="internal_error", message="An unexpected internal error occurred."
        )
        return JSONResponse(status_code=500, content=body.model_dump())


# --------------------------------------------------------------------------- #
# Routes                                                                      #
# --------------------------------------------------------------------------- #
def _register_routes(app: FastAPI) -> None:
    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/docs")

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    async def health(request: Request) -> HealthResponse:
        settings: Settings = request.app.state.settings
        browser_agent: BrowserAgent | None = getattr(request.app.state, "browser_agent", None)
        return HealthResponse(
            app=settings.app_name,
            version=settings.app_version,
            environment=settings.environment,
            browser_enabled=settings.enable_browser,
            browser_ready=bool(browser_agent and browser_agent.is_ready),
        )

    @app.post(
        "/api/resolve",
        response_model=ResolveResponse,
        tags=["resolver"],
        dependencies=[Depends(enforce_rate_limit)],
        responses={
            200: {"model": ResolveResponse},
            400: {"model": ErrorResponse},
            401: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
        },
    )
    async def resolve_endpoint(payload: ResolveRequest, request: Request) -> JSONResponse:
        resolver: LinkResolver = request.app.state.resolver
        result = await resolver.resolve(payload.url, include_meta=payload.include_meta)
        status_code = STATUS_CODE_MAP.get(result.status, 200)
        data = result.model_dump(mode="json")
        if data.get("meta") is None:
            data.pop("meta", None)  # omit meta unless populated
        return JSONResponse(status_code=status_code, content=data)


# Module-level ASGI app for ``uvicorn app.main:app``.
app = create_app()
