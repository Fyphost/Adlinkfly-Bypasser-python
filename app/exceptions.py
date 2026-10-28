"""Typed domain exceptions for the resolver.

All exceptions carry a :class:`~app.status.ResolveStatus` and a human-readable
message so that higher layers (the resolver and the API) can translate them
into a consistent response body without inspecting exception types ad hoc.

This module is standard-library only so that :mod:`app.url_validator` remains
importable and testable without any third-party dependency.
"""

from __future__ import annotations

from .status import ResolveStatus, default_message


class ResolverError(Exception):
    """Base class for all resolver-domain errors.

    Parameters
    ----------
    message:
        Human-readable description. When omitted, the default message for the
        associated status is used.
    status:
        The :class:`ResolveStatus` this error maps to. Subclasses provide a
        sensible default.
    """

    #: Default status for the exception class; overridden by subclasses.
    default_status: ResolveStatus = ResolveStatus.FAILED

    def __init__(
        self,
        message: str | None = None,
        *,
        status: ResolveStatus | None = None,
    ) -> None:
        self.status: ResolveStatus = status or self.default_status
        self.message: str = message or default_message(self.status)
        super().__init__(self.message)


class UrlValidationError(ResolverError):
    """The supplied URL is malformed or uses a disallowed scheme/shape."""

    default_status = ResolveStatus.INVALID_URL


class SSRFError(UrlValidationError):
    """The URL resolves to a blocked (private/internal/metadata) address.

    Raised both for the initial URL and for any redirect hop whose target
    resolves to a non-public address, providing redirect-by-redirect
    protection against SSRF and DNS-rebinding.
    """

    default_status = ResolveStatus.INVALID_URL


class UnsupportedDomainError(ResolverError):
    """The host is well-formed but is not an authorised shortener domain."""

    default_status = ResolveStatus.UNSUPPORTED_DOMAIN


class VerificationRequiredError(ResolverError):
    """A CAPTCHA or human-verification step blocks automated resolution."""

    default_status = ResolveStatus.VERIFICATION_REQUIRED


class ResolutionTimeoutError(ResolverError):
    """Resolution exceeded the configured overall time budget."""

    default_status = ResolveStatus.TIMEOUT


class ResolutionFailedError(ResolverError):
    """Automated resolution completed without yielding a destination URL."""

    default_status = ResolveStatus.FAILED


class UpstreamError(ResolverError):
    """A transport/upstream error (HTTP error, connection failure) occurred."""

    default_status = ResolveStatus.FAILED


class BrowserUnavailableError(ResolverError):
    """The Playwright browser could not be launched or is disabled.

    This is treated as a resolution failure rather than a crash so that a
    missing/unhealthy browser degrades gracefully into a structured response.
    """

    default_status = ResolveStatus.FAILED


class APIError(Exception):
    """A transport-level API error (auth, rate limit, internal) to render.

    Unlike :class:`ResolverError`, this carries an explicit HTTP status code and
    a free-form ``status`` string (e.g. ``"unauthorized"``) so the API layer can
    emit a uniform error body with optional response headers.
    """

    def __init__(
        self,
        *,
        status_code: int,
        status: str,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.status = status
        self.message = message
        self.headers = headers or {}
        super().__init__(message)
