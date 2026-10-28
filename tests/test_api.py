"""End-to-end API tests (requires fastapi + httpx).

The resolver is replaced with a fake on ``app.state`` after startup, so these
tests exercise routing, auth, rate limiting, status-code mapping and error
shaping without any real network or browser.
"""

from __future__ import annotations

import contextlib

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.schemas import ResolveMeta, ResolveResponse
from app.status import ResolveStatus

API_KEY = "test-key"


def settings(**overrides) -> Settings:
    base = {
        "_env_file": None,
        "environment": "production",
        "api_keys": [API_KEY],
        "require_api_key": True,
        "enable_browser": False,
        "rate_limit_requests": 1000,
        "rate_limit_window_seconds": 60,
        "supported_domains": {"arolinks.com"},
        "log_json": False,
    }
    base.update(overrides)
    return Settings(**base)


class FakeResolver:
    def __init__(self, response: ResolveResponse):
        self.response = response
        self.calls = 0

    async def resolve(self, url: str, *, include_meta: bool = False) -> ResolveResponse:
        self.calls += 1
        return self.response


@contextlib.contextmanager
def client_for(cfg: Settings, resolver: FakeResolver | None = None):
    app = create_app(cfg)
    with TestClient(app) as client:
        if resolver is not None:
            client.app.state.resolver = resolver
        yield client


def auth(headers: dict | None = None) -> dict:
    headers = headers or {}
    headers["X-API-Key"] = API_KEY
    return headers


# --------------------------------------------------------------------------- #
# Health / docs                                                               #
# --------------------------------------------------------------------------- #
def test_health_ok():
    with client_for(settings()) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["browser_enabled"] is False


def test_root_redirects_to_docs():
    with client_for(settings()) as client:
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code in (307, 308)
        assert resp.headers["location"] == "/docs"


# --------------------------------------------------------------------------- #
# Resolve outcomes                                                            #
# --------------------------------------------------------------------------- #
def test_resolve_success():
    resolver = FakeResolver(
        ResolveResponse.resolved(
            original_url="https://arolinks.com/x", destination_url="https://example.com"
        )
    )
    with client_for(settings(), resolver) as client:
        resp = client.post(
            "/api/resolve", json={"url": "https://arolinks.com/x"}, headers=auth()
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["destination_url"] == "https://example.com"
        assert body["status"] == "resolved"
        assert "meta" not in body  # omitted when not populated


def test_resolve_verification_required_is_200():
    resolver = FakeResolver(
        ResolveResponse.failure(
            original_url="https://arolinks.com/x", status=ResolveStatus.VERIFICATION_REQUIRED
        )
    )
    with client_for(settings(), resolver) as client:
        resp = client.post(
            "/api/resolve", json={"url": "https://arolinks.com/x"}, headers=auth()
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert body["destination_url"] is None
        assert body["status"] == "verification_required"


def test_resolve_invalid_url_is_400():
    resolver = FakeResolver(
        ResolveResponse.failure(original_url="notaurl", status=ResolveStatus.INVALID_URL)
    )
    with client_for(settings(), resolver) as client:
        resp = client.post("/api/resolve", json={"url": "notaurl"}, headers=auth())
        assert resp.status_code == 400
        assert resp.json()["status"] == "invalid_url"


def test_resolve_unsupported_domain_is_422():
    resolver = FakeResolver(
        ResolveResponse.failure(
            original_url="https://other.test/x", status=ResolveStatus.UNSUPPORTED_DOMAIN
        )
    )
    with client_for(settings(), resolver) as client:
        resp = client.post("/api/resolve", json={"url": "https://other.test/x"}, headers=auth())
        assert resp.status_code == 422
        assert resp.json()["status"] == "unsupported_domain"


def test_resolve_includes_meta_when_present():
    resolver = FakeResolver(
        ResolveResponse.resolved(
            original_url="https://arolinks.com/x",
            destination_url="https://example.com",
            meta=ResolveMeta(resolved_via="http", elapsed_ms=5.0),
        )
    )
    with client_for(settings(), resolver) as client:
        resp = client.post(
            "/api/resolve",
            json={"url": "https://arolinks.com/x", "include_meta": True},
            headers=auth(),
        )
        assert resp.json()["meta"]["resolved_via"] == "http"


# --------------------------------------------------------------------------- #
# Authentication                                                              #
# --------------------------------------------------------------------------- #
def test_missing_api_key_is_401():
    with client_for(settings(), FakeResolver(_any_response())) as client:
        resp = client.post("/api/resolve", json={"url": "https://arolinks.com/x"})
        assert resp.status_code == 401
        assert resp.json()["status"] == "unauthorized"


def test_wrong_api_key_is_401():
    with client_for(settings(), FakeResolver(_any_response())) as client:
        resp = client.post(
            "/api/resolve",
            json={"url": "https://arolinks.com/x"},
            headers={"X-API-Key": "nope"},
        )
        assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# Request validation                                                          #
# --------------------------------------------------------------------------- #
def test_missing_url_is_422_uniform():
    with client_for(settings(), FakeResolver(_any_response())) as client:
        resp = client.post("/api/resolve", json={}, headers=auth())
        assert resp.status_code == 422
        body = resp.json()
        assert body["success"] is False
        assert body["status"] == "invalid_request"


def test_empty_url_is_422():
    with client_for(settings(), FakeResolver(_any_response())) as client:
        resp = client.post("/api/resolve", json={"url": "   "}, headers=auth())
        assert resp.status_code == 422


def test_unknown_field_is_422():
    with client_for(settings(), FakeResolver(_any_response())) as client:
        resp = client.post(
            "/api/resolve",
            json={"url": "https://arolinks.com/x", "evil": 1},
            headers=auth(),
        )
        assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Rate limiting                                                               #
# --------------------------------------------------------------------------- #
def test_rate_limit_exceeded_is_429():
    resolver = FakeResolver(
        ResolveResponse.resolved(
            original_url="https://arolinks.com/x", destination_url="https://example.com"
        )
    )
    cfg = settings(rate_limit_requests=2, rate_limit_window_seconds=60)
    with client_for(cfg, resolver) as client:
        for _ in range(2):
            ok = client.post("/api/resolve", json={"url": "https://arolinks.com/x"}, headers=auth())
            assert ok.status_code == 200
        blocked = client.post(
            "/api/resolve", json={"url": "https://arolinks.com/x"}, headers=auth()
        )
        assert blocked.status_code == 429
        assert blocked.json()["status"] == "rate_limited"
        assert "retry-after" in {k.lower() for k in blocked.headers}


def _any_response() -> ResolveResponse:
    return ResolveResponse.resolved(
        original_url="https://arolinks.com/x", destination_url="https://example.com"
    )
