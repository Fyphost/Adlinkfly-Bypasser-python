"""Tests for :class:`app.resolver.LinkResolver` (requires pydantic + bs4).

Collaborators are faked: a fake HTTP client, a fake browser agent, a real
:class:`~app.url_validator.URLValidator` with injected DNS, and the real
:class:`~app.page_analyzer.PageAnalyzer`. The event loop is driven with
``asyncio.run`` so ``pytest-asyncio`` is not needed.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("bs4")

from app.browser_agent import BrowserResult
from app.config import Settings
from app.exceptions import BrowserUnavailableError
from app.http_client import ChallengeError, HttpFetchResult
from app.page_analyzer import PageAnalyzer
from app.resolver import LinkResolver
from app.status import ResolveStatus
from app.url_validator import URLValidator

DNS = {
    "arolinks.com": ["8.8.8.8"],
    "example.com": ["1.1.1.1"],
    "notsupported.test": ["8.8.8.8"],
    "evil.test": ["10.0.0.1"],  # resolves to a private address
}

# Reusable page fixtures (kept multi-line to stay within the line length).
HTML_META = (
    '<html><head>'
    '<meta http-equiv="refresh" content="2; url=https://example.com/final">'
    "</head><body>wait</body></html>"
)
HTML_CAPTCHA = (
    '<html><body><p>verify you are human</p>'
    '<div class="g-recaptcha"></div></body></html>'
)
HTML_NO_SIGNAL = (
    "<html><body><p>Some unrelated content without any actionable links.</p></body></html>"
)
HTML_GETLINK = (
    "<html><body><p>Proceed to your destination link, almost there now.</p>"
    '<a id="getlink" href="/go">Get Link</a></body></html>'
)


def dns_resolver(host):
    if host in DNS:
        return DNS[host]
    raise OSError(f"no dns for {host}")


class FakeHttp:
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls = 0

    async def fetch(self, url, *, validated=None):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.result


class SlowHttp:
    def __init__(self, delay=0.3):
        self.delay = delay

    async def fetch(self, url, *, validated=None):
        await asyncio.sleep(self.delay)
        return HttpFetchResult(url=url, status_code=200, html="", content_type="text/html")


class FakeBrowser:
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls = 0
        self.last_budget = None

    async def resolve(self, url, *, source_host, budget=None):
        self.calls += 1
        self.last_budget = budget
        if self.exc is not None:
            raise self.exc
        return self.result


class SlowBrowser:
    def __init__(self, delay=0.3):
        self.delay = delay

    async def resolve(self, url, *, source_host, budget=None):
        await asyncio.sleep(self.delay)
        return BrowserResult(
            status=ResolveStatus.RESOLVED, destination_url="https://example.com/final", steps=1
        )


def build_resolver(
    *, http, browser, enable_browser=True, total_timeout=30.0, http_phase_timeout=20.0
):
    validator = URLValidator(dns_resolver=dns_resolver, allow_private=False)
    settings = Settings(
        _env_file=None,
        supported_domains={"arolinks.com"},
        enable_browser=enable_browser,
        resolve_total_timeout=total_timeout,
        http_phase_timeout=http_phase_timeout,
    )
    return LinkResolver(settings, validator, PageAnalyzer(), http, browser)


def html_result(html: str, url: str = "https://arolinks.com/abc") -> HttpFetchResult:
    return HttpFetchResult(url=url, status_code=200, html=html, content_type="text/html")


def resolver_for(
    *, html=None, http_exc=None, browser=None, enable_browser=False
) -> LinkResolver:
    if http_exc is not None:
        http = FakeHttp(exc=http_exc)
    else:
        http = FakeHttp(result=html_result(html) if html is not None else None)
    return build_resolver(
        http=http, browser=browser or FakeBrowser(), enable_browser=enable_browser
    )


def resolve(resolver, url, **kwargs):
    return asyncio.run(resolver.resolve(url, **kwargs))


# --------------------------------------------------------------------------- #
# Validation / authorisation                                                  #
# --------------------------------------------------------------------------- #
def test_invalid_scheme():
    resp = resolve(resolver_for(), "ftp://arolinks.com/x")
    assert resp.success is False
    assert resp.status is ResolveStatus.INVALID_URL


def test_unsupported_domain():
    resp = resolve(resolver_for(), "https://notsupported.test/x")
    assert resp.status is ResolveStatus.UNSUPPORTED_DOMAIN


def test_ssrf_blocked_before_supported_check():
    resp = resolve(resolver_for(), "https://evil.test/x")
    assert resp.status is ResolveStatus.INVALID_URL


# --------------------------------------------------------------------------- #
# HTTP fast path                                                              #
# --------------------------------------------------------------------------- #
def test_http_direct_external_redirect():
    http = FakeHttp(
        result=HttpFetchResult(
            url="https://example.com/final", status_code=200, html="", content_type="text/html"
        )
    )
    resolver = build_resolver(http=http, browser=FakeBrowser(), enable_browser=False)
    resp = resolve(resolver, "https://arolinks.com/abc")
    assert resp.success is True
    assert resp.status is ResolveStatus.RESOLVED
    assert resp.destination_url == "https://example.com/final"


def test_http_meta_refresh_destination():
    resp = resolve(resolver_for(html=HTML_META), "https://arolinks.com/abc")
    assert resp.status is ResolveStatus.RESOLVED
    assert resp.destination_url == "https://example.com/final"


def test_http_captcha_no_browser():
    resp = resolve(resolver_for(html=HTML_CAPTCHA), "https://arolinks.com/abc")
    assert resp.status is ResolveStatus.VERIFICATION_REQUIRED


def test_http_failed_when_browser_disabled_and_no_signal():
    resp = resolve(resolver_for(html=HTML_NO_SIGNAL), "https://arolinks.com/abc")
    assert resp.status is ResolveStatus.FAILED


# --------------------------------------------------------------------------- #
# Browser fallback                                                            #
# --------------------------------------------------------------------------- #
def test_browser_fallback_resolves():
    browser = FakeBrowser(
        result=BrowserResult(
            status=ResolveStatus.RESOLVED, destination_url="https://example.com/final", steps=3
        )
    )
    resolver = resolver_for(html=HTML_GETLINK, browser=browser, enable_browser=True)
    resp = resolve(resolver, "https://arolinks.com/abc", include_meta=True)
    assert resp.status is ResolveStatus.RESOLVED
    assert resp.destination_url == "https://example.com/final"
    assert browser.calls == 1
    assert resp.meta is not None
    assert resp.meta.resolved_via == "browser"
    assert resp.meta.steps == 3


def test_browser_verification_required():
    browser = FakeBrowser(
        result=BrowserResult(
            status=ResolveStatus.VERIFICATION_REQUIRED, message="captcha", steps=2
        )
    )
    resolver = resolver_for(html=HTML_GETLINK, browser=browser, enable_browser=True)
    resp = resolve(resolver, "https://arolinks.com/abc")
    assert resp.status is ResolveStatus.VERIFICATION_REQUIRED


def test_browser_destination_ssrf_blocked():
    browser = FakeBrowser(
        result=BrowserResult(
            status=ResolveStatus.RESOLVED, destination_url="https://evil.test/x", steps=1
        )
    )
    resolver = resolver_for(html=HTML_GETLINK, browser=browser, enable_browser=True)
    resp = resolve(resolver, "https://arolinks.com/abc")
    assert resp.status is ResolveStatus.FAILED
    assert resp.destination_url is None


def test_challenge_falls_back_to_browser():
    browser = FakeBrowser(
        result=BrowserResult(
            status=ResolveStatus.RESOLVED, destination_url="https://example.com/final", steps=1
        )
    )
    resolver = resolver_for(
        http_exc=ChallengeError("blocked"), browser=browser, enable_browser=True
    )
    resp = resolve(resolver, "https://arolinks.com/abc")
    assert resp.status is ResolveStatus.RESOLVED
    assert browser.calls == 1


def test_challenge_without_browser_fails():
    resolver = resolver_for(http_exc=ChallengeError("blocked"), enable_browser=False)
    resp = resolve(resolver, "https://arolinks.com/abc")
    assert resp.status is ResolveStatus.FAILED


def test_browser_unavailable_degrades():
    browser = FakeBrowser(exc=BrowserUnavailableError("no chromium"))
    resolver = resolver_for(html=HTML_GETLINK, browser=browser, enable_browser=True)
    resp = resolve(resolver, "https://arolinks.com/abc")
    assert resp.status is ResolveStatus.FAILED


# --------------------------------------------------------------------------- #
# Timeout                                                                     #
# --------------------------------------------------------------------------- #
def test_overall_timeout():
    resolver = build_resolver(http=SlowHttp(), browser=FakeBrowser(), total_timeout=0.05)
    resp = resolve(resolver, "https://arolinks.com/abc")
    assert resp.status is ResolveStatus.TIMEOUT


def test_http_phase_timeout_falls_back_to_browser():
    # cloudscraper is slow; the HTTP phase budget elapses and we use the browser.
    browser = FakeBrowser(
        result=BrowserResult(
            status=ResolveStatus.RESOLVED, destination_url="https://example.com/final", steps=1
        )
    )
    resolver = build_resolver(
        http=SlowHttp(delay=0.3),
        browser=browser,
        enable_browser=True,
        total_timeout=5.0,
        http_phase_timeout=0.05,
    )
    resp = resolve(resolver, "https://arolinks.com/abc")
    assert resp.status is ResolveStatus.RESOLVED
    assert browser.calls == 1
    # The browser was handed a positive remaining budget.
    assert browser.last_budget is not None and browser.last_budget > 0


def test_timeout_reports_phase_in_meta():
    # HTTP falls back immediately (challenge); the browser then runs past the
    # overall budget. The timeout must report that it happened in the browser.
    resolver = build_resolver(
        http=FakeHttp(exc=ChallengeError("blocked")),
        browser=SlowBrowser(delay=0.3),
        enable_browser=True,
        total_timeout=0.1,
    )
    resp = resolve(resolver, "https://arolinks.com/abc", include_meta=True)
    assert resp.status is ResolveStatus.TIMEOUT
    assert resp.meta is not None
    assert resp.meta.resolved_via == "browser"
