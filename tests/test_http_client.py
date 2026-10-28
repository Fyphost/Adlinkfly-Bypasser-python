"""Tests for :mod:`app.http_client`.

A fake ``requests``-like session is injected, so these tests run without the
``cloudscraper`` package and without any network. The event loop is driven with
``asyncio.run`` so ``pytest-asyncio`` is not required.
"""

from __future__ import annotations

import asyncio

import pytest

from app.exceptions import SSRFError, UpstreamError
from app.http_client import ChallengeError, CloudscraperClient
from app.url_validator import URLValidator


class FakeResponse:
    def __init__(self, status_code, headers=None, body=b"", encoding="utf-8"):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        self.encoding = encoding
        self.closed = False

    def iter_content(self, chunk_size=16_384):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requested: list[str] = []
        self.closed = False

    def get(self, url, **kwargs):
        self.requested.append(url)
        if not self._responses:
            raise AssertionError("unexpected extra request")
        return self._responses.pop(0)

    def close(self):
        self.closed = True


class RaisingSession:
    def __init__(self, exc):
        self._exc = exc
        self.closed = False

    def get(self, *args, **kwargs):
        raise self._exc

    def close(self):
        self.closed = True


def make_validator():
    dns = {"example.com": ["8.8.8.8"], "dest.example": ["1.1.1.1"]}

    def resolver(host):
        if host in dns:
            return dns[host]
        raise OSError(f"no dns for {host}")

    return URLValidator(dns_resolver=resolver)


def make_client(session, **kwargs):
    calls = {"count": 0}

    def factory():
        calls["count"] += 1
        return session

    client = CloudscraperClient(
        make_validator(),
        session_factory=factory,
        **kwargs,
    )
    client._factory_calls = calls  # type: ignore[attr-defined]
    return client


def run(coro):
    return asyncio.run(coro)


class TestBasicFetch:
    def test_simple_200(self):
        session = FakeSession(
            [FakeResponse(200, {"Content-Type": "text/html"}, b"<html>hi</html>")]
        )
        client = make_client(session)
        result = run(client.fetch("https://example.com/start"))
        assert result.status_code == 200
        assert result.html == "<html>hi</html>"
        assert result.is_html
        assert result.ok
        assert session.closed  # session closed after fetch

    def test_fresh_session_per_fetch(self):
        session = FakeSession([FakeResponse(200, {}, b"a"), FakeResponse(200, {}, b"b")])
        client = make_client(session)
        run(client.fetch("https://example.com/1"))
        # Re-queue a response and fetch again.
        session._responses.append(FakeResponse(200, {}, b"c"))
        run(client.fetch("https://example.com/2"))
        assert client._factory_calls["count"] == 2  # type: ignore[attr-defined]

    def test_non_html_content_type(self):
        session = FakeSession(
            [FakeResponse(200, {"Content-Type": "application/json"}, b'{"a":1}')]
        )
        client = make_client(session)
        result = run(client.fetch("https://example.com/data"))
        assert result.is_html is False
        assert result.content_type == "application/json"


class TestRedirects:
    def test_follows_redirect_chain(self):
        session = FakeSession(
            [
                FakeResponse(302, {"Location": "https://example.com/step2"}),
                FakeResponse(200, {"Content-Type": "text/html"}, b"<html>done</html>"),
            ]
        )
        client = make_client(session)
        result = run(client.fetch("https://example.com/start"))
        assert result.status_code == 200
        assert result.url == "https://example.com/step2"
        assert len(result.history) == 1
        assert result.history[0]["status"] == 302

    def test_relative_redirect_resolved(self):
        session = FakeSession(
            [
                FakeResponse(301, {"Location": "/step2"}),
                FakeResponse(200, {"Content-Type": "text/html"}, b"ok"),
            ]
        )
        client = make_client(session)
        result = run(client.fetch("https://example.com/start"))
        assert result.url == "https://example.com/step2"

    def test_redirect_to_private_ip_blocked(self):
        session = FakeSession([FakeResponse(302, {"Location": "http://10.0.0.1/admin"})])
        client = make_client(session)
        with pytest.raises(SSRFError):
            run(client.fetch("https://example.com/start"))

    def test_redirect_to_metadata_blocked(self):
        session = FakeSession(
            [FakeResponse(302, {"Location": "http://169.254.169.254/latest/meta-data/"})]
        )
        client = make_client(session)
        with pytest.raises(SSRFError):
            run(client.fetch("https://example.com/start"))

    def test_too_many_redirects(self):
        session = FakeSession(
            [FakeResponse(302, {"Location": "https://example.com/loop"}) for _ in range(5)]
        )
        client = make_client(session, max_redirects=2)
        with pytest.raises(UpstreamError):
            run(client.fetch("https://example.com/start"))


class TestBodyCap:
    def test_truncation(self):
        session = FakeSession([FakeResponse(200, {"Content-Type": "text/html"}, b"x" * 100)])
        client = make_client(session, max_response_bytes=10)
        result = run(client.fetch("https://example.com/big"))
        assert result.truncated is True
        assert len(result.html) == 10


class TestErrorClassification:
    def test_cloudflare_error_becomes_challenge(self):
        class CloudflareChallengeError(Exception):
            pass

        client = make_client(RaisingSession(CloudflareChallengeError("blocked")))
        with pytest.raises(ChallengeError):
            run(client.fetch("https://example.com/x"))

    def test_generic_error_becomes_upstream(self):
        client = make_client(RaisingSession(RuntimeError("boom")))
        with pytest.raises(UpstreamError) as excinfo:
            run(client.fetch("https://example.com/x"))
        assert not isinstance(excinfo.value, ChallengeError)
