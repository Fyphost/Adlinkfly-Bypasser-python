"""Resolution status vocabulary.

This module is intentionally dependency-free (standard library only) so that it
can be imported by both the Pydantic schemas and the pure-stdlib
:mod:`app.url_validator` / :mod:`app.exceptions` modules without pulling in any
third-party package. Keeping it light keeps the security-critical validator
independently unit-testable.
"""

from __future__ import annotations

from enum import StrEnum


class ResolveStatus(StrEnum):
    """Canonical outcome of a resolve request.

    The string values form part of the public API contract and must remain
    stable. They are returned verbatim in the ``status`` field of every
    response.
    """

    #: A final destination URL was obtained and validated.
    RESOLVED = "resolved"
    #: Human verification (CAPTCHA / "I am not a robot" / manual step) is
    #: required and cannot be completed by automated navigation.
    VERIFICATION_REQUIRED = "verification_required"
    #: The host is well-formed but not an authorised/supported shortener.
    UNSUPPORTED_DOMAIN = "unsupported_domain"
    #: The input URL is malformed, uses a forbidden scheme, or was blocked by
    #: SSRF / network-security validation.
    INVALID_URL = "invalid_url"
    #: Resolution exceeded the configured time budget.
    TIMEOUT = "timeout"
    #: Automated resolution ran to completion without finding a destination
    #: (or an upstream/unexpected error prevented resolution).
    FAILED = "failed"


#: Human-readable default messages for each status. Call sites may override
#: these with a more specific message.
STATUS_MESSAGES: dict[ResolveStatus, str] = {
    ResolveStatus.RESOLVED: "Destination URL resolved successfully",
    ResolveStatus.VERIFICATION_REQUIRED: "Human verification is required to continue",
    ResolveStatus.UNSUPPORTED_DOMAIN: "The provided domain is not a supported shortener",
    ResolveStatus.INVALID_URL: "The provided URL is invalid or not permitted",
    ResolveStatus.TIMEOUT: "Resolution timed out before a destination could be determined",
    ResolveStatus.FAILED: "The destination URL could not be resolved automatically",
}


def default_message(status: ResolveStatus) -> str:
    """Return the default human-readable message for ``status``."""

    return STATUS_MESSAGES.get(status, "Unknown resolution status")
