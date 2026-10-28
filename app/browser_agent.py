"""Playwright (Chromium) browser automation for JavaScript-heavy pages.

When the HTTP (cloudscraper) path cannot resolve a link -- because the page
renders its destination via JavaScript, uses a countdown, or requires clicking
through intermediate steps -- the resolver falls back to this agent.

Design
------
* A single Chromium instance is launched lazily and shared; **each resolve uses
  its own browser context** (``new_context``) so cookies/state never leak
  between users. Contexts are always closed in a ``finally`` block.
* The per-step decision (*what should we do on this page?*) is implemented as a
  pure function, :func:`decide_browser_action`, operating on a
  :class:`~app.page_analyzer.PageAnalysis`. This keeps the navigation policy
  fully unit-testable without a real browser.
* Interactions prefer Playwright's auto-waiting locators and explicit
  conditions (``wait_for_url``, ``wait_for_load_state``) over arbitrary sleeps.
  Countdowns are handled by letting ``click`` auto-wait for the control to
  become actionable (enabled), bounded by a configurable timeout.
* A request router blocks navigations/subresources whose host is a private or
  metadata IP literal, as a lightweight in-browser SSRF guard. The final
  destination is always re-validated by the resolver.

Playwright is imported lazily so this module (and the pure decision logic) can
be imported and tested without the package installed.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from .exceptions import BrowserUnavailableError
from .page_analyzer import (
    ButtonCandidate,
    PageAnalysis,
    PageAnalyzer,
    is_ad_or_social,
    is_external,
)
from .status import ResolveStatus
from .url_validator import URLValidator

logger = logging.getLogger(__name__)


class BrowserActionType(StrEnum):
    """The kind of action the agent should take on the current page."""

    RETURN_DESTINATION = "return_destination"
    VERIFICATION_REQUIRED = "verification_required"
    CLICK_BUTTON = "click_button"
    WAIT_COUNTDOWN = "wait_countdown"
    WAIT_AND_RETRY = "wait_and_retry"
    GIVE_UP = "give_up"


@dataclass(slots=True)
class ActionDecision:
    """A decision produced by :func:`decide_browser_action`."""

    action: BrowserActionType
    destination: str | None = None
    button: ButtonCandidate | None = None
    reason: str = ""


@dataclass(slots=True)
class BrowserResult:
    """The outcome of a browser-based resolution."""

    status: ResolveStatus
    destination_url: str | None = None
    steps: int = 0
    final_url: str | None = None
    message: str = ""


def is_external_destination(current_url: str, source_host: str) -> bool:
    """True if ``current_url`` looks like a real destination off the shortener.

    The URL must be http(s), on a different registrable domain than the
    shortener, and not a known ad/social endpoint.
    """

    parts = urlsplit(current_url)
    if parts.scheme not in ("http", "https"):
        return False
    host = parts.hostname or ""
    if not host:
        return False
    if not is_external(host, source_host):
        return False
    return not is_ad_or_social(current_url)


def decide_browser_action(
    analysis: PageAnalysis, *, current_url: str, source_host: str
) -> ActionDecision:
    """Decide the next action for the current page (pure policy function)."""

    # 1. We have already navigated off the shortener onto a real destination.
    if is_external_destination(current_url, source_host):
        return ActionDecision(
            BrowserActionType.RETURN_DESTINATION,
            destination=current_url,
            reason="navigated to an external destination",
        )

    # 2. A CAPTCHA / human-verification challenge blocks automation.
    if analysis.has_captcha:
        return ActionDecision(
            BrowserActionType.VERIFICATION_REQUIRED,
            reason=f"captcha detected ({analysis.captcha_type})",
        )

    # 3. A strong destination signal was extracted from the page.
    if analysis.destination_url:
        return ActionDecision(
            BrowserActionType.RETURN_DESTINATION,
            destination=analysis.destination_url,
            reason="destination extracted from page",
        )

    # 4/5. There is an actionable control -- click it (auto-waiting through any
    # countdown that merely disables the button).
    button = analysis.best_button
    if button is not None:
        action = (
            BrowserActionType.WAIT_COUNTDOWN
            if (analysis.has_countdown or button.disabled)
            else BrowserActionType.CLICK_BUTTON
        )
        return ActionDecision(action, button=button, reason=f"click '{button.text}'")

    # 6. Countdown with no button yet, or a JS-rendered/unknown page: wait and
    # re-examine.
    if analysis.has_countdown or analysis.requires_javascript:
        return ActionDecision(
            BrowserActionType.WAIT_AND_RETRY, reason="waiting for dynamic content"
        )

    # 7. Nothing actionable.
    return ActionDecision(BrowserActionType.GIVE_UP, reason="no actionable element found")


def _is_timeout_error(exc: BaseException) -> bool:
    """Heuristically detect a Playwright timeout without importing its types.

    Keeping :class:`BrowserAgent._drive` free of any ``playwright`` import makes
    the navigation loop unit-testable with a fake page object. Playwright raises
    ``playwright.async_api.TimeoutError`` whose class name contains "Timeout".
    """

    return "timeout" in type(exc).__name__.lower()


class BrowserAgent:
    """Manage a shared Chromium instance and resolve links via navigation."""

    def __init__(
        self,
        analyzer: PageAnalyzer,
        validator: URLValidator,
        *,
        headless: bool = True,
        navigation_timeout_ms: int = 20_000,
        max_steps: int = 8,
        countdown_max_seconds: float = 20.0,
        user_agent: str = "Mozilla/5.0",
        slow_mo_ms: int = 0,
        max_wait_retries: int = 3,
        settle_timeout_ms: int = 2_500,
    ) -> None:
        self.analyzer = analyzer
        self.validator = validator
        self.headless = headless
        self.navigation_timeout_ms = navigation_timeout_ms
        self.max_steps = max_steps
        self.countdown_max_seconds = countdown_max_seconds
        self.user_agent = user_agent
        self.slow_mo_ms = slow_mo_ms
        self.max_wait_retries = max_wait_retries
        self.settle_timeout_ms = settle_timeout_ms

        self._playwright: Any = None
        self._browser: Any = None
        self._start_lock = asyncio.Lock()

    @property
    def is_ready(self) -> bool:
        return self._browser is not None and getattr(self._browser, "is_connected", lambda: True)()

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Launch Chromium (idempotent). Raises :class:`BrowserUnavailableError`."""

        if self.is_ready:
            return
        async with self._start_lock:
            if self.is_ready:
                return
            try:
                from playwright.async_api import async_playwright

                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=self.headless,
                    slow_mo=self.slow_mo_ms,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                logger.info("Chromium browser launched", extra={"headless": self.headless})
            except Exception as exc:
                await self._cleanup_partial()
                raise BrowserUnavailableError(
                    f"Failed to launch the Playwright browser: {exc}"
                ) from exc

    async def stop(self) -> None:
        """Close the browser and Playwright, swallowing shutdown errors."""

        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:
            logger.debug("Error closing browser", exc_info=True)
        finally:
            self._browser = None
        await self._cleanup_partial()

    async def _cleanup_partial(self) -> None:
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:
            logger.debug("Error stopping playwright", exc_info=True)
        finally:
            self._playwright = None

    # ------------------------------------------------------------------ #
    # Resolution                                                          #
    # ------------------------------------------------------------------ #
    async def resolve(
        self, url: str, *, source_host: str, budget: float | None = None
    ) -> BrowserResult:
        """Resolve ``url`` by driving a fresh, isolated browser context.

        ``budget`` (seconds), when provided, bounds the entire browser phase:
        the agent returns a structured result just before the budget elapses,
        instead of letting an outer timeout cancel it mid-navigation.
        """

        await self.start()
        if self._browser is None:  # pragma: no cover - start() raises otherwise
            raise BrowserUnavailableError("Browser is not available")

        deadline = (self._now() + budget) if budget is not None else None
        context = await self._browser.new_context(
            user_agent=self.user_agent,
            java_script_enabled=True,
            ignore_https_errors=False,
        )
        try:
            await context.route("**/*", self._route_handler)
            page = await context.new_page()
            page.set_default_navigation_timeout(self.navigation_timeout_ms)
            page.set_default_timeout(self.navigation_timeout_ms)
            return await self._drive(page, url, source_host=source_host, deadline=deadline)
        finally:
            # Per-request context is always torn down to free resources & state.
            try:
                await context.close()
            except Exception:
                logger.debug("Error closing browser context", exc_info=True)

    async def _drive(
        self, page: Any, url: str, *, source_host: str, deadline: float | None = None
    ) -> BrowserResult:
        steps = 0
        wait_retries = 0

        # Initial navigation (bounded). A timeout is non-fatal -- we still try to
        # analyse whatever rendered; a hard navigation error is terminal.
        goto_timeout = self._wait_ms(self.navigation_timeout_ms, deadline)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=goto_timeout)
        except Exception as exc:
            if not _is_timeout_error(exc):
                return BrowserResult(
                    status=ResolveStatus.FAILED,
                    steps=0,
                    final_url=url,
                    message=f"initial navigation failed: {exc}",
                )
            logger.debug("initial navigation timed out; analysing partial page")

        for step in range(1, self.max_steps + 1):
            if self._deadline_exceeded(deadline):
                return BrowserResult(
                    status=ResolveStatus.FAILED,
                    steps=steps,
                    final_url=page.url,
                    message="browser time budget exhausted before resolution",
                )
            steps = step
            current_url = page.url
            try:
                html = await page.content()
            except Exception as exc:
                logger.debug("Could not read page content: %s", exc)
                html = ""

            analysis = self.analyzer.analyze(html, base_url=current_url, source_host=source_host)
            decision = decide_browser_action(
                analysis, current_url=current_url, source_host=source_host
            )
            logger.debug(
                "browser step",
                extra={"step": step, "url": current_url, "action": decision.action},
            )

            if decision.action is BrowserActionType.RETURN_DESTINATION:
                return BrowserResult(
                    status=ResolveStatus.RESOLVED,
                    destination_url=decision.destination,
                    steps=step,
                    final_url=current_url,
                    message=decision.reason,
                )
            if decision.action is BrowserActionType.VERIFICATION_REQUIRED:
                return BrowserResult(
                    status=ResolveStatus.VERIFICATION_REQUIRED,
                    steps=step,
                    final_url=current_url,
                    message=decision.reason,
                )
            if decision.action is BrowserActionType.GIVE_UP:
                return BrowserResult(
                    status=ResolveStatus.FAILED,
                    steps=step,
                    final_url=current_url,
                    message=decision.reason,
                )
            if decision.action is BrowserActionType.WAIT_AND_RETRY:
                wait_retries += 1
                if wait_retries > self.max_wait_retries:
                    return BrowserResult(
                        status=ResolveStatus.FAILED,
                        steps=step,
                        final_url=current_url,
                        message="no progress after waiting for dynamic content",
                    )
                await self._settle(page, deadline)
                continue

            # CLICK_BUTTON or WAIT_COUNTDOWN
            wait_retries = 0
            assert decision.button is not None
            await self._click_and_wait(page, decision.button, deadline)

        return BrowserResult(
            status=ResolveStatus.FAILED,
            steps=steps,
            final_url=page.url,
            message=f"navigation limit ({self.max_steps} steps) reached",
        )

    # ------------------------------------------------------------------ #
    # Time-budget helpers                                                 #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _now() -> float:
        return asyncio.get_running_loop().time()

    def _remaining_ms(self, deadline: float | None) -> int:
        if deadline is None:
            return self.navigation_timeout_ms
        return int(max(0.0, (deadline - self._now()) * 1000))

    def _wait_ms(self, desired_ms: int, deadline: float | None) -> int:
        """Clamp ``desired_ms`` to the time remaining before ``deadline``."""

        if deadline is None:
            return desired_ms
        return max(1, min(desired_ms, self._remaining_ms(deadline)))

    def _deadline_exceeded(self, deadline: float | None) -> bool:
        return deadline is not None and self._now() >= deadline

    async def _settle(self, page: Any, deadline: float | None) -> None:
        """Briefly, boundedly wait for dynamic content to settle.

        A short explicit wait -- not an open-ended ``networkidle`` wait, which
        never settles on ad-heavy shortener pages.
        """

        timeout = self._wait_ms(self.settle_timeout_ms, deadline)
        try:
            await page.wait_for_timeout(timeout)
        except Exception as exc:
            logger.debug("settle wait interrupted: %s", exc)

    async def _click_and_wait(
        self, page: Any, button: ButtonCandidate, deadline: float | None
    ) -> None:
        """Click the chosen control and wait (boundedly) for navigation.

        ``click`` auto-waits for the element to become actionable (visible,
        enabled, stable), transparently handling a countdown that only disables
        the button. Every wait is clamped to the remaining time budget, and no
        open-ended network waits are used.
        """

        click_timeout = self._wait_ms(int(self.countdown_max_seconds * 1000), deadline)
        locator = self._locate(page, button)
        before = page.url
        try:
            await locator.click(timeout=click_timeout)
        except Exception as exc:
            logger.debug("click did not complete for %r: %s", button.text, exc)
            return

        # The real success signal is a URL change; fall back to a short settle
        # for in-page (SPA) updates that do not change the URL.
        url_timeout = self._wait_ms(self.navigation_timeout_ms, deadline)
        try:
            await page.wait_for_url(lambda candidate: candidate != before, timeout=url_timeout)
        except Exception as exc:
            logger.debug("no URL change after click: %s", exc)
            await self._settle(page, deadline)

    def _locate(self, page: Any, button: ButtonCandidate) -> Any:
        """Build a Playwright locator for ``button`` (id first, then role/text)."""

        if button.element_id:
            return page.locator(f"#{button.element_id}").first
        role = "link" if button.tag == "a" else "button"
        try:
            return page.get_by_role(role, name=button.text, exact=False).first
        except Exception:  # pragma: no cover - defensive
            return page.get_by_text(button.text, exact=False).first

    # ------------------------------------------------------------------ #
    # In-browser SSRF guard                                               #
    # ------------------------------------------------------------------ #
    def should_block_request(self, url: str) -> bool:
        """Return True if an in-browser request to ``url`` must be blocked.

        Blocks http(s) requests to ``localhost`` and to private/metadata IP
        literals. Hostnames that require DNS are allowed here (the resolver
        performs full DNS-based validation on the final destination); this
        method is a cheap, synchronous guard suitable for a route handler.
        """

        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return False
        host = (parts.hostname or "").lower()
        if not host:
            return False
        if host == "localhost":
            return True
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        return self.validator.classify_blocked(str(ip)) is not None

    async def _route_handler(self, route: Any) -> None:
        try:
            if self.should_block_request(route.request.url):
                logger.debug("blocking in-browser request to %s", route.request.url)
                await route.abort()
            else:
                await route.continue_()
        except Exception:
            logger.debug("route handler error", exc_info=True)
            try:
                await route.continue_()
            except Exception:
                logger.debug("route continue failed", exc_info=True)
