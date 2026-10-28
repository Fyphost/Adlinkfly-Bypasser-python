"""Pydantic request and response models for the public API.

The response contract is intentionally small and stable. Every resolve call --
whether it succeeds, requires verification, or fails -- returns a
:class:`ResolveResponse` with the same five canonical fields. Optional
diagnostic ``meta`` is only included when the client opts in.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .status import ResolveStatus, default_message


class ResolveRequest(BaseModel):
    """Body for ``POST /api/resolve``."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(
        ...,
        min_length=1,
        max_length=4096,
        description="The shortened AdLinkFly URL to resolve.",
        examples=["https://arolinks.com/example"],
    )
    include_meta: bool = Field(
        default=False,
        description="When true, include optional diagnostic metadata in the response.",
    )

    @field_validator("url", mode="before")
    @classmethod
    def _strip_url(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError("url must not be empty")
            return stripped
        return value


class ResolveMeta(BaseModel):
    """Optional, non-sensitive diagnostic metadata about a resolution."""

    resolved_via: str | None = Field(
        default=None,
        description="Which engine produced the result: 'http', 'browser', or None.",
    )
    steps: int | None = Field(
        default=None,
        description="Number of navigation steps performed by the browser agent.",
    )
    elapsed_ms: float | None = Field(
        default=None, description="Total wall-clock time spent resolving, in milliseconds."
    )


class ResolveResponse(BaseModel):
    """Canonical response for every resolution outcome."""

    success: bool = Field(description="True only when a destination URL was resolved.")
    original_url: str = Field(description="The URL that was submitted for resolution.")
    destination_url: str | None = Field(
        default=None, description="The resolved final destination, or null when unavailable."
    )
    status: ResolveStatus = Field(description="Machine-readable resolution status.")
    message: str = Field(description="Human-readable explanation of the outcome.")
    meta: ResolveMeta | None = Field(
        default=None, description="Optional diagnostics (only when requested)."
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "success": True,
                    "original_url": "https://arolinks.com/example",
                    "destination_url": "https://example.com",
                    "status": "resolved",
                    "message": "Destination URL resolved successfully",
                },
                {
                    "success": False,
                    "original_url": "https://arolinks.com/example",
                    "destination_url": None,
                    "status": "verification_required",
                    "message": "Human verification is required to continue",
                },
            ]
        }
    )

    @classmethod
    def resolved(
        cls,
        *,
        original_url: str,
        destination_url: str,
        message: str | None = None,
        meta: ResolveMeta | None = None,
    ) -> ResolveResponse:
        return cls(
            success=True,
            original_url=original_url,
            destination_url=destination_url,
            status=ResolveStatus.RESOLVED,
            message=message or default_message(ResolveStatus.RESOLVED),
            meta=meta,
        )

    @classmethod
    def failure(
        cls,
        *,
        original_url: str,
        status: ResolveStatus,
        message: str | None = None,
        meta: ResolveMeta | None = None,
    ) -> ResolveResponse:
        return cls(
            success=False,
            original_url=original_url,
            destination_url=None,
            status=status,
            message=message or default_message(status),
            meta=meta,
        )


class HealthResponse(BaseModel):
    """Response for ``GET /health``."""

    status: str = "ok"
    app: str
    version: str
    environment: str
    browser_enabled: bool
    browser_ready: bool


class ErrorResponse(BaseModel):
    """Uniform error body for authentication / rate-limit / internal errors.

    Mirrors the shape of :class:`ResolveResponse` so clients can parse a single
    structure regardless of outcome.
    """

    success: bool = False
    original_url: str | None = None
    destination_url: str | None = None
    status: str
    message: str
