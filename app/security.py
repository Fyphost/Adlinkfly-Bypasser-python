"""API-key authentication and rate-limiting dependencies.

These FastAPI dependencies read the active :class:`~app.config.Settings` and the
shared :class:`~app.ratelimit.SlidingWindowRateLimiter` from ``request.app.state``
(wired up in :mod:`app.main`), which keeps them easy to override in tests.

Authentication fails closed: when ``require_api_key`` is set but no keys are
configured, every request is rejected (except in development, where auth is
treated as disabled -- see :meth:`Settings.auth_required`).
"""

from __future__ import annotations

import hmac
import logging

from fastapi import Depends, Request, Security
from fastapi.security import APIKeyHeader

from .config import Settings, get_settings
from .exceptions import APIError
from .ratelimit import SlidingWindowRateLimiter

logger = logging.getLogger(__name__)

# The header name is fixed at import time for a stable OpenAPI security scheme.
_settings_for_header = get_settings()
api_key_header = APIKeyHeader(name=_settings_for_header.api_key_header, auto_error=False)


def _settings_from(request: Request) -> Settings:
    return getattr(request.app.state, "settings", None) or get_settings()


def _is_valid_key(candidate: str, allowed: list[str]) -> bool:
    """Constant-time comparison against every configured key."""

    return any(hmac.compare_digest(candidate, key) for key in allowed)


async def require_api_key(
    request: Request, api_key: str | None = Security(api_key_header)
) -> str | None:
    """Authenticate the caller, returning the validated key (or ``None``)."""

    settings = _settings_from(request)
    if not settings.auth_required():
        return None
    # Enforced but misconfigured (production with no keys): fail closed.
    if not settings.api_keys:
        logger.error("API-key auth is enforced but no API keys are configured")
        raise APIError(
            status_code=401,
            status="unauthorized",
            message="Server authentication is not configured. Contact the administrator.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    if api_key and _is_valid_key(api_key, settings.api_keys):
        return api_key
    raise APIError(
        status_code=401,
        status="unauthorized",
        message="A valid API key is required.",
        headers={"WWW-Authenticate": "ApiKey"},
    )


async def enforce_rate_limit(
    request: Request, api_key: str | None = Depends(require_api_key)
) -> str | None:
    """Apply the per-identifier rate limit after successful authentication."""

    settings = _settings_from(request)
    if not settings.rate_limit_enabled:
        return api_key

    limiter: SlidingWindowRateLimiter | None = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        return api_key

    identifier = api_key or (request.client.host if request.client else "anonymous")
    result = await limiter.check(identifier)
    if not result.allowed:
        retry_after = int(result.retry_after) + 1
        logger.info("rate limit exceeded", extra={"retry_after": retry_after})
        raise APIError(
            status_code=429,
            status="rate_limited",
            message="Rate limit exceeded. Please retry later.",
            headers={"Retry-After": str(retry_after)},
        )
    return api_key
