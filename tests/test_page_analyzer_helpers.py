"""Tests for the pure, dependency-free helpers in :mod:`app.page_analyzer`.

These do not require BeautifulSoup and therefore run in any environment. The
BeautifulSoup-backed :meth:`PageAnalyzer.analyze` is covered separately in
``test_page_analyzer.py`` (which skips when bs4 is unavailable).
"""

from __future__ import annotations

import pytest

from app.page_analyzer import (
    ButtonKind,
    classify_button_text,
    extract_countdown_seconds,
    extract_urls_from_script,
    is_ad_or_social,
    is_external,
    parse_meta_refresh_content,
    registrable_domain,
)


class TestRegistrableDomain:
    @pytest.mark.parametrize(
        "host,expected",
        [
            ("arolinks.com", "arolinks.com"),
            ("www.arolinks.com", "arolinks.com"),
            ("a.b.c.arolinks.com", "arolinks.com"),
            ("example.co.uk", "example.co.uk"),
            ("www.example.co.uk", "example.co.uk"),
            ("shop.example.com.au", "example.com.au"),
            ("single", "single"),
        ],
    )
    def test_registrable_domain(self, host, expected):
        assert registrable_domain(host) == expected


class TestIsExternal:
    def test_same_domain_not_external(self):
        assert is_external("www.arolinks.com", "arolinks.com") is False

    def test_subdomain_not_external(self):
        assert is_external("go.arolinks.com", "arolinks.com") is False

    def test_different_domain_external(self):
        assert is_external("example.com", "arolinks.com") is True

    def test_empty_not_external(self):
        assert is_external("", "arolinks.com") is False


class TestAdSocial:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.facebook.com/sharer/sharer.php?u=x",
            "https://pagead2.googlesyndication.com/x",
            "https://twitter.com/intent/tweet?url=x",
            "https://api.whatsapp.com/send?text=x",
        ],
    )
    def test_detects_ads_social(self, url):
        assert is_ad_or_social(url) is True

    def test_regular_url_not_ad(self):
        assert is_ad_or_social("https://example.com/page") is False


class TestExtractUrlsFromScript:
    def test_window_location_href(self):
        js = 'setTimeout(function(){ window.location.href = "https://example.com/final"; }, 5000);'
        assert "https://example.com/final" in extract_urls_from_script(js)

    def test_location_replace(self):
        js = 'location.replace("https://dest.example/go");'
        assert extract_urls_from_script(js) == ["https://dest.example/go"]

    def test_var_url_assignment(self):
        js = 'var desturl = "https://target.example/abc"; console.log(desturl);'
        assert "https://target.example/abc" in extract_urls_from_script(js)

    def test_no_urls(self):
        assert extract_urls_from_script("var x = 1; doThing();") == []

    def test_dedupes(self):
        js = 'location.href="https://a.example/x"; window.location = "https://a.example/x";'
        assert extract_urls_from_script(js) == ["https://a.example/x"]


class TestMetaRefresh:
    @pytest.mark.parametrize(
        "content,expected",
        [
            ("5; url=https://example.com/final", "https://example.com/final"),
            ("0;URL='https://example.com/x'", "https://example.com/x"),
            ('3; url="https://example.com/y"', "https://example.com/y"),
            ("10", None),
        ],
    )
    def test_parse_meta_refresh(self, content, expected):
        assert parse_meta_refresh_content(content) == expected


class TestButtonClassification:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Get Link", ButtonKind.GET_LINK),
            ("GET YOUR LINK", ButtonKind.GET_LINK),
            ("Continue", ButtonKind.CONTINUE),
            ("Proceed to link", ButtonKind.CONTINUE),
            ("Verify", ButtonKind.VERIFY),
            ("I'm not a robot", ButtonKind.VERIFY),
            ("Skip Ad", ButtonKind.SKIP),
            ("Click here", ButtonKind.CLICK),
            ("Random label", None),
            ("", None),
        ],
    )
    def test_classify(self, text, expected):
        assert classify_button_text(text) == expected


class TestCountdown:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Please wait 10 seconds", 10),
            ("5 sec remaining", 5),
            ("wait 3s", 3),
            ("no timer here", None),
        ],
    )
    def test_extract_countdown(self, text, expected):
        assert extract_countdown_seconds(text) == expected
