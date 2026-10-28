"""Tests for the browser navigation loop ``BrowserAgent._drive``.

These use a fake page and a fake analyzer, so they run without Playwright or
BeautifulSoup. They cover the new time-budget / deadline handling and the
bounded (no ``networkidle``) waiting strategy. The event loop is driven with
``asyncio.run``.
"""

from __future__ import annotations

import asyncio

from app.browser_agent import BrowserAgent
from app.page_analyzer import ButtonCandidate, ButtonKind, PageAnalysis, PageState
from app.status import ResolveStatus
from app.url_validator import URLValidator


# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #
class TimeoutLike(Exception):
    """Exception whose class name contains 'timeout' (matches _is_timeout_error)."""


class FakeLocator:
    def __init__(self, page: FakePage):
        self.page = page
        self.first = self

    async def click(self, *, timeout=None):
        self.page.clicks += 1
        if self.page.nav_on_click:
            self.page.url = self.page.nav_on_click.pop(0)


class FakePage:
    def __init__(self, url="https://arolinks.com/x", nav_on_click=None, goto_exc=None):
        self.url = url
        self.nav_on_click = list(nav_on_click or [])
        self.goto_exc = goto_exc
        self.clicks = 0
        self.content_calls = 0

    def set_default_navigation_timeout(self, timeout):  # pragma: no cover - noop
        pass

    def set_default_timeout(self, timeout):  # pragma: no cover - noop
        pass

    async def goto(self, url, *, wait_until=None, timeout=None):
        if self.goto_exc is not None:
            raise self.goto_exc
        self.url = url

    async def content(self):
        self.content_calls += 1
        return "<html></html>"

    async def wait_for_url(self, predicate, *, timeout=None):
        if predicate(self.url):
            return
        raise TimeoutLike("no url change")

    async def wait_for_timeout(self, ms):
        return

    def locator(self, selector):
        return FakeLocator(self)

    def get_by_role(self, role, *, name=None, exact=None):
        return FakeLocator(self)

    def get_by_text(self, text, *, exact=None):  # pragma: no cover - fallback path
        return FakeLocator(self)


class FakeAnalyzer:
    def __init__(self, analysis: PageAnalysis):
        self.analysis = analysis

    def analyze(self, html, *, base_url, source_host) -> PageAnalysis:
        return self.analysis


def intermediate_analysis() -> PageAnalysis:
    button = ButtonCandidate(text="Get Link", kind=ButtonKind.GET_LINK, tag="button")
    return PageAnalysis(state=PageState.INTERMEDIATE, buttons=[button])


def captcha_analysis() -> PageAnalysis:
    return PageAnalysis(state=PageState.CAPTCHA, has_captcha=True, captcha_type="recaptcha")


def empty_analysis() -> PageAnalysis:
    return PageAnalysis(state=PageState.UNKNOWN)


def make_agent(analysis: PageAnalysis, *, max_steps=4) -> BrowserAgent:
    return BrowserAgent(
        FakeAnalyzer(analysis),
        URLValidator(allow_private=True),
        navigation_timeout_ms=1000,
        countdown_max_seconds=1,
        settle_timeout_ms=10,
        max_steps=max_steps,
    )


def run_drive(agent, page, url, source_host, *, deadline_offset=None):
    async def main():
        deadline = None
        if deadline_offset is not None:
            deadline = asyncio.get_running_loop().time() + deadline_offset
        return await agent._drive(page, url, source_host=source_host, deadline=deadline)

    return asyncio.run(main())


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #
def test_drive_direct_external_destination():
    page = FakePage(url="https://arolinks.com/x")
    agent = make_agent(empty_analysis())
    result = run_drive(agent, page, "https://example.com/final", "arolinks.com")
    assert result.status is ResolveStatus.RESOLVED
    assert result.destination_url == "https://example.com/final"
    assert result.steps == 1


def test_drive_click_through_to_destination():
    page = FakePage(url="https://arolinks.com/x", nav_on_click=["https://example.com/final"])
    agent = make_agent(intermediate_analysis())
    result = run_drive(agent, page, "https://arolinks.com/x", "arolinks.com")
    assert result.status is ResolveStatus.RESOLVED
    assert result.destination_url == "https://example.com/final"
    assert result.steps == 2
    assert page.clicks == 1


def test_drive_captcha_requires_verification():
    page = FakePage(url="https://arolinks.com/x")
    agent = make_agent(captcha_analysis())
    result = run_drive(agent, page, "https://arolinks.com/x", "arolinks.com")
    assert result.status is ResolveStatus.VERIFICATION_REQUIRED


def test_drive_budget_exhausted_returns_failed():
    page = FakePage(url="https://arolinks.com/x")
    agent = make_agent(intermediate_analysis())
    result = run_drive(
        agent, page, "https://arolinks.com/x", "arolinks.com", deadline_offset=-1.0
    )
    assert result.status is ResolveStatus.FAILED
    assert "budget" in result.message


def test_drive_navigation_limit_reached():
    # Clicks never change the URL, so no progress is made.
    page = FakePage(url="https://arolinks.com/x", nav_on_click=[])
    agent = make_agent(intermediate_analysis(), max_steps=2)
    result = run_drive(agent, page, "https://arolinks.com/x", "arolinks.com")
    assert result.status is ResolveStatus.FAILED
    assert "navigation limit" in result.message


def test_drive_goto_hard_error_is_failure():
    page = FakePage(url="https://arolinks.com/x", goto_exc=ValueError("connection refused"))
    agent = make_agent(empty_analysis())
    result = run_drive(agent, page, "https://arolinks.com/x", "arolinks.com")
    assert result.status is ResolveStatus.FAILED
    assert "navigation failed" in result.message


def test_drive_goto_timeout_is_nonfatal():
    # The initial goto times out but the page already shows an external URL.
    page = FakePage(url="https://example.com/final", goto_exc=TimeoutLike("slow"))
    agent = make_agent(empty_analysis())
    result = run_drive(agent, page, "https://arolinks.com/x", "arolinks.com")
    assert result.status is ResolveStatus.RESOLVED
    assert result.destination_url == "https://example.com/final"


def test_wait_ms_clamps_to_remaining_budget():
    agent = make_agent(empty_analysis())

    async def main():
        deadline = asyncio.get_running_loop().time() + 0.5  # ~500ms remaining
        return agent._wait_ms(10_000, deadline)

    clamped = asyncio.run(main())
    assert 0 < clamped <= 500


def test_wait_ms_without_deadline_returns_desired():
    agent = make_agent(empty_analysis())

    async def main():
        return agent._wait_ms(1234, None)

    assert asyncio.run(main()) == 1234
