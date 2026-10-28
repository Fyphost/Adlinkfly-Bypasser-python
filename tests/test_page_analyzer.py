"""Tests for :meth:`app.page_analyzer.PageAnalyzer.analyze` (requires bs4)."""

from __future__ import annotations

import pytest

pytest.importorskip("bs4")

from app.page_analyzer import ButtonKind, PageAnalyzer, PageState

ANALYZER = PageAnalyzer()
BASE = "https://arolinks.com/abc123"
SOURCE = "arolinks.com"


def analyze(html: str):
    return ANALYZER.analyze(html, base_url=BASE, source_host=SOURCE)


def test_meta_refresh_destination():
    html = """
    <html><head>
      <meta http-equiv="refresh" content="3; url=https://example.com/final">
    </head><body>Redirecting...</body></html>
    """
    result = analyze(html)
    assert result.state is PageState.DIRECT_DESTINATION
    assert result.destination_url == "https://example.com/final"


def test_javascript_redirect_destination():
    html = """
    <html><body>
      <script>
        setTimeout(function(){ window.location.href = "https://example.com/final"; }, 10);
      </script>
    </body></html>
    """
    result = analyze(html)
    assert result.state is PageState.DIRECT_DESTINATION
    assert result.destination_url == "https://example.com/final"


def test_recaptcha_detected():
    html = """
    <html><body>
      <div class="g-recaptcha" data-sitekey="abc"></div>
      <script src="https://www.google.com/recaptcha/api.js"></script>
    </body></html>
    """
    result = analyze(html)
    assert result.state is PageState.CAPTCHA
    assert result.has_captcha is True
    assert result.captcha_type == "recaptcha"


def test_countdown_with_disabled_button():
    html = """
    <html><body>
      <p>Please wait <span id="timer">10</span> seconds</p>
      <button id="continue" disabled>Continue</button>
    </body></html>
    """
    result = analyze(html)
    assert result.has_countdown is True
    assert result.state is PageState.COUNTDOWN
    assert result.best_button is not None
    assert result.best_button.kind is ButtonKind.CONTINUE


def test_get_link_button_intermediate():
    html = """
    <html><body>
      <p>Your link is ready. Lots of descriptive text here to avoid the JS heuristic.</p>
      <a id="getlink" href="/go/next">Get Link</a>
    </body></html>
    """
    result = analyze(html)
    assert result.state is PageState.INTERMEDIATE
    assert result.best_button is not None
    assert result.best_button.kind is ButtonKind.GET_LINK


def test_requires_javascript_when_empty():
    html = "<html><head><script>var x = 1;</script></head><body></body></html>"
    result = analyze(html)
    assert result.requires_javascript is True
    assert result.state is PageState.REQUIRES_JS


def test_outbound_anchor_is_candidate_not_destination():
    html = """
    <html><body>
      <p>Plenty of visible text so the page is not treated as JS-only content.</p>
      <a href="https://example.com/dest">External destination</a>
      <a href="https://pagead2.googlesyndication.com/ad">sponsored</a>
    </body></html>
    """
    result = analyze(html)
    # Weak signal only: present as a candidate but not a strong destination.
    assert result.destination_url is None
    assert "https://example.com/dest" in result.candidate_destinations
    # Ad/social links are filtered out.
    assert all("googlesyndication" not in c for c in result.candidate_destinations)


def test_title_extracted():
    html = "<html><head><title>  My Page  </title></head><body>hello there friends</body></html>"
    assert analyze(html).title == "My Page"
