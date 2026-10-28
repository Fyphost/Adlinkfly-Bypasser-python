"""Tests for the pure, browser-free logic in :mod:`app.browser_agent`.

The navigation *policy* (:func:`decide_browser_action`) and the in-browser SSRF
request guard (:meth:`BrowserAgent.should_block_request`) are exercised without
launching a real browser. :class:`~app.page_analyzer.PageAnalysis` and
:class:`~app.page_analyzer.ButtonCandidate` are constructed directly, so these
tests need neither Playwright nor BeautifulSoup.
"""

from __future__ import annotations

from app.browser_agent import (
    BrowserActionType,
    BrowserAgent,
    decide_browser_action,
    is_external_destination,
)
from app.page_analyzer import ButtonCandidate, ButtonKind, PageAnalysis, PageState
from app.url_validator import URLValidator


def analysis(**kwargs) -> PageAnalysis:
    defaults: dict = {"state": PageState.UNKNOWN}
    defaults.update(kwargs)
    return PageAnalysis(**defaults)


SOURCE = "arolinks.com"


class TestIsExternalDestination:
    def test_external_public(self):
        assert is_external_destination("https://example.com/final", SOURCE) is True

    def test_same_domain_not_destination(self):
        assert is_external_destination("https://www.arolinks.com/step2", SOURCE) is False

    def test_ad_not_destination(self):
        assert (
            is_external_destination("https://googlesyndication.com/x", SOURCE) is False
        )

    def test_non_http_not_destination(self):
        assert is_external_destination("about:blank", SOURCE) is False


class TestDecideBrowserAction:
    def test_external_url_returns_destination(self):
        decision = decide_browser_action(
            analysis(), current_url="https://example.com/final", source_host=SOURCE
        )
        assert decision.action is BrowserActionType.RETURN_DESTINATION
        assert decision.destination == "https://example.com/final"

    def test_captcha_requires_verification(self):
        a = analysis(state=PageState.CAPTCHA, has_captcha=True, captcha_type="recaptcha")
        decision = decide_browser_action(
            a, current_url="https://arolinks.com/x", source_host=SOURCE
        )
        assert decision.action is BrowserActionType.VERIFICATION_REQUIRED

    def test_destination_in_page(self):
        a = analysis(
            state=PageState.DIRECT_DESTINATION, destination_url="https://dest.example/y"
        )
        decision = decide_browser_action(
            a, current_url="https://arolinks.com/x", source_host=SOURCE
        )
        assert decision.action is BrowserActionType.RETURN_DESTINATION
        assert decision.destination == "https://dest.example/y"

    def test_external_takes_priority_over_captcha(self):
        # A captcha widget on the real destination must not mask success.
        a = analysis(has_captcha=True, captcha_type="recaptcha")
        decision = decide_browser_action(
            a, current_url="https://example.com/final", source_host=SOURCE
        )
        assert decision.action is BrowserActionType.RETURN_DESTINATION

    def test_enabled_button_click(self):
        button = ButtonCandidate(text="Get Link", kind=ButtonKind.GET_LINK, tag="button")
        a = analysis(state=PageState.INTERMEDIATE, buttons=[button])
        decision = decide_browser_action(
            a, current_url="https://arolinks.com/x", source_host=SOURCE
        )
        assert decision.action is BrowserActionType.CLICK_BUTTON
        assert decision.button is button

    def test_countdown_button_waits(self):
        button = ButtonCandidate(
            text="Continue", kind=ButtonKind.CONTINUE, tag="button", disabled=True
        )
        a = analysis(state=PageState.COUNTDOWN, buttons=[button], has_countdown=True)
        decision = decide_browser_action(
            a, current_url="https://arolinks.com/x", source_host=SOURCE
        )
        assert decision.action is BrowserActionType.WAIT_COUNTDOWN
        assert decision.button is button

    def test_requires_js_waits(self):
        a = analysis(state=PageState.REQUIRES_JS, requires_javascript=True)
        decision = decide_browser_action(
            a, current_url="https://arolinks.com/x", source_host=SOURCE
        )
        assert decision.action is BrowserActionType.WAIT_AND_RETRY

    def test_nothing_actionable_gives_up(self):
        decision = decide_browser_action(
            analysis(), current_url="https://arolinks.com/x", source_host=SOURCE
        )
        assert decision.action is BrowserActionType.GIVE_UP


class TestShouldBlockRequest:
    def make_agent(self) -> BrowserAgent:
        # analyzer is unused by should_block_request; pass a sentinel.
        return BrowserAgent(analyzer=object(), validator=URLValidator())  # type: ignore[arg-type]

    def test_blocks_localhost(self):
        assert self.make_agent().should_block_request("http://localhost:8000/") is True

    def test_blocks_private_ip_literal(self):
        assert self.make_agent().should_block_request("http://10.0.0.1/admin") is True

    def test_blocks_metadata_ip(self):
        agent = self.make_agent()
        assert agent.should_block_request("http://169.254.169.254/latest/") is True

    def test_allows_public_ip(self):
        assert self.make_agent().should_block_request("http://8.8.8.8/") is False

    def test_allows_hostname(self):
        # Hostnames are allowed here (resolver does full DNS validation later).
        assert self.make_agent().should_block_request("https://example.com/x") is False

    def test_allows_data_uri(self):
        assert self.make_agent().should_block_request("data:text/html,<p>x</p>") is False
