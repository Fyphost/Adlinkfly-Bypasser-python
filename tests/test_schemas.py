"""Tests for :mod:`app.schemas` (requires pydantic)."""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from pydantic import ValidationError

from app.schemas import ResolveMeta, ResolveRequest, ResolveResponse
from app.status import ResolveStatus


class TestResolveRequest:
    def test_strips_whitespace(self):
        assert ResolveRequest(url="  https://arolinks.com/x  ").url == "https://arolinks.com/x"

    def test_empty_url_rejected(self):
        with pytest.raises(ValidationError):
            ResolveRequest(url="   ")

    def test_missing_url_rejected(self):
        with pytest.raises(ValidationError):
            ResolveRequest()  # type: ignore[call-arg]

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            ResolveRequest(url="https://arolinks.com/x", unexpected=True)  # type: ignore[call-arg]

    def test_include_meta_default_false(self):
        assert ResolveRequest(url="https://arolinks.com/x").include_meta is False


class TestResolveResponse:
    def test_resolved_builder(self):
        resp = ResolveResponse.resolved(
            original_url="https://arolinks.com/x", destination_url="https://example.com"
        )
        assert resp.success is True
        assert resp.status is ResolveStatus.RESOLVED
        assert resp.destination_url == "https://example.com"
        assert resp.message == "Destination URL resolved successfully"

    def test_failure_builder(self):
        resp = ResolveResponse.failure(
            original_url="https://arolinks.com/x",
            status=ResolveStatus.VERIFICATION_REQUIRED,
        )
        assert resp.success is False
        assert resp.destination_url is None
        assert resp.status is ResolveStatus.VERIFICATION_REQUIRED
        assert resp.message == "Human verification is required to continue"

    def test_custom_message(self):
        resp = ResolveResponse.failure(
            original_url="u", status=ResolveStatus.FAILED, message="custom"
        )
        assert resp.message == "custom"

    def test_meta_round_trips(self):
        meta = ResolveMeta(resolved_via="browser", steps=3, elapsed_ms=12.5)
        resp = ResolveResponse.resolved(
            original_url="u", destination_url="d", meta=meta
        )
        assert resp.meta is not None
        assert resp.meta.resolved_via == "browser"
        assert resp.meta.steps == 3
