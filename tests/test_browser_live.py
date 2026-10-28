"""Opt-in live browser tests using controlled local pages.

These require a real Playwright + Chromium install and are **skipped by
default**. Enable them with::

    RUN_BROWSER_TESTS=1 pytest -m browser

A tiny local HTTP server serves ``tests/pages/`` on 127.0.0.1 so no third-party
shortener is involved -- the navigation policy and destination extraction are
validated against stable, self-contained fixtures.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import http.server
import os
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_BROWSER_TESTS"),
    reason="set RUN_BROWSER_TESTS=1 to run live browser tests",
)

pytest.importorskip("playwright")
pytest.importorskip("bs4")

from app.browser_agent import BrowserAgent
from app.page_analyzer import PageAnalyzer
from app.status import ResolveStatus
from app.url_validator import URLValidator

PAGES_DIR = Path(__file__).parent / "pages"


@contextlib.contextmanager
def serve_pages():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(PAGES_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _host, port = server.server_address
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def make_agent() -> BrowserAgent:
    # allow_private so the in-browser guard permits the 127.0.0.1 test server.
    validator = URLValidator(allow_private=True)
    return BrowserAgent(
        PageAnalyzer(),
        validator,
        headless=True,
        navigation_timeout_ms=15_000,
        max_steps=6,
        countdown_max_seconds=5,
    )


async def _run(url: str):
    agent = make_agent()
    try:
        return await agent.resolve(url, source_host="127.0.0.1")
    finally:
        await agent.stop()


@pytest.mark.browser
def test_countdown_then_final_link():
    with serve_pages() as base:
        result = asyncio.run(_run(f"{base}/step1_countdown.html"))
    assert result.status is ResolveStatus.RESOLVED
    assert result.destination_url == "https://example.com/final-destination"


@pytest.mark.browser
def test_direct_meta_refresh():
    with serve_pages() as base:
        result = asyncio.run(_run(f"{base}/direct_meta_refresh.html"))
    assert result.status is ResolveStatus.RESOLVED
    assert result.destination_url == "https://example.com/meta-destination"
