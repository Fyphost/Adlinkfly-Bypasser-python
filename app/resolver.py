"""Link-resolution orchestration.

:class:`LinkResolver` ties the components together into the end-to-end workflow:

1.  Validate the URL (syntax + DNS + SSRF) and confirm it is a supported,
    authorised shortener domain.
2.  Try the fast path with cloudscraper (:class:`~app.http_client.CloudscraperClient`):
    follow validated redirects and analyse the resulting page. If the response
    already *is* an external destination, or the page exposes a strong
    destination signal, return it.
3.  Fall back to the Playwright browser agent when the page needs JavaScript,
    presents intermediate steps/countdowns, or cloudscraper hit an anti-bot
    challenge.
4.  Honestly report ``verification_required`` when a CAPTCHA / human step blocks
    progress, and ``failed``/``timeout`` when automated resolution cannot
    legitimately proceed.

The resolver is constructed via dependency injection so every collaborator can
be mocked in tests. Concurrency is bounded by two semaphores (overall resolves
and, more tightly, browser contexts), and the whole operation is wrapped in an
overall time budget.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from .browser_agent import BrowserAgent, is_external_destination
from .config import Settings
from .exceptions import ResolverError, UpstreamError
from .http_client import ChallengeError, CloudscraperClient
from .page_analyzer import PageAnalysis, PageAnalyzer, PageState
from .schemas import ResolveMeta, ResolveResponse
from .status import ResolveStatus, default_message
from .url_validator import URLValidator

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Resolution:
    """Internal result passed back from the resolution pipeline."""

    status: ResolveStatus
    destination: str | None = None
    message: str | None = None
    via: str | None = None
    steps: int | None = None


@dataclass(slots=True)
class _Trace:
    """Mutable progress tracker so outcomes (incl. timeouts) are diagnosable.

    Updated as the pipeline advances; read by :meth:`LinkResolver.resolve` even
    when the pipeline is cancelled by the overall timeout, so the response can
    report which phase was active and how far the browser got.
    """

    via: str | None = None
    steps: int | None = None


class LinkResolver:
    """Resolve supported AdLinkFly shortened URLs to their destination."""

    def __init__(
        self,
        settings: Settings,
        validator: URLValidator,
        analyzer: PageAnalyzer,
        http_client: CloudscraperClient,
        browser_agent: BrowserAgent,
    ) -> None:
        self.settings = settings
        self.validator = validator
        self.analyzer = analyzer
        self.http_client = http_client
        self.browser_agent = browser_agent
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_resolves)
        self._browser_semaphore = asyncio.Semaphore(settings.max_concurrent_browsers)

    # ------------------------------------------------------------------ #
    # Public entry point                                                  #
    # ------------------------------------------------------------------ #
    async def resolve(self, url: str, *, include_meta: bool = False) -> ResolveResponse:
        """Resolve ``url`` and return a :class:`ResolveResponse`."""

        start = time.monotonic()
        trace = _Trace()
        try:
            async with self._semaphore:
                resolution = await asyncio.wait_for(
                    self._pipeline(url, trace, start),
                    timeout=self.settings.resolve_total_timeout,
                )
        except TimeoutError:
            logger.info(
                "resolution timed out",
                extra={"url": _safe(url), "phase": trace.via, "steps": trace.steps},
            )
            phase = f" during the {trace.via} phase" if trace.via else ""
            resolution = _Resolution(
                ResolveStatus.TIMEOUT,
                message=f"Resolution timed out{phase} before a destination could be determined",
                via=trace.via,
                steps=trace.steps,
            )
        except ResolverError as exc:
            logger.info(
                "resolution rejected",
                extra={"url": _safe(url), "status": exc.status, "reason": exc.message},
            )
            resolution = _Resolution(exc.status, message=exc.message)
        except Exception:
            logger.exception("unexpected resolver error", extra={"url": _safe(url)})
            resolution = _Resolution(
                ResolveStatus.FAILED, message="An unexpected error occurred during resolution"
            )

        return self._to_response(url, resolution, include_meta=include_meta, start=start)

    # ------------------------------------------------------------------ #
    # Pipeline                                                            #
    # ------------------------------------------------------------------ #
    async def _pipeline(self, url: str, trace: _Trace, start: float) -> _Resolution:
        # 1. Validate (syntax + DNS + SSRF). DNS is blocking -> run in a thread.
        validated = await asyncio.to_thread(self.validator.validate, url)
        source_host = validated.parsed.host

        # 2. Authorisation: only supported shortener domains may be automated.
        self.validator.ensure_supported_domain(source_host, self.settings.supported_domains)

        # 3. Fast path: cloudscraper, bounded by its own phase budget so a slow
        #    upstream cannot consume the entire request.
        trace.via = "http"
        try:
            http_outcome = await asyncio.wait_for(
                self._try_http(validated.parsed.url, validated, source_host),
                timeout=self.settings.http_phase_timeout,
            )
        except TimeoutError:
            logger.info(
                "cloudscraper phase exceeded %.1fs; falling back to browser",
                self.settings.http_phase_timeout,
            )
            http_outcome = None
        if http_outcome is not None:
            return http_outcome

        # 4. Browser fallback, given whatever time remains in the budget.
        if self.settings.enable_browser:
            trace.via = "browser"
            budget = self._remaining_budget(start)
            return await self._try_browser(validated.parsed.url, source_host, trace, budget)

        return _Resolution(
            ResolveStatus.FAILED,
            message="Static resolution failed and browser automation is disabled",
            via="http",
        )

    def _remaining_budget(self, start: float) -> float:
        """Seconds left for the browser phase, leaving a safety margin."""

        elapsed = time.monotonic() - start
        remaining = self.settings.resolve_total_timeout - elapsed
        remaining -= self.settings.browser_time_margin_seconds
        return max(1.0, remaining)

    async def _try_http(self, url: str, validated, source_host: str) -> _Resolution | None:
        """Attempt HTTP resolution. Return a terminal result, or ``None`` to fall back."""

        try:
            result = await self.http_client.fetch(url, validated=validated)
        except ChallengeError as exc:
            logger.info("cloudscraper challenge; will try browser: %s", exc)
            return None  # fall back to browser
        except UpstreamError as exc:
            logger.info("cloudscraper upstream error; will try browser: %s", exc)
            return None

        # (a) The response itself landed on an external destination.
        if is_external_destination(result.url, source_host):
            dest = await self._validate_destination(result.url)
            if dest:
                return _Resolution(ResolveStatus.RESOLVED, destination=dest, via="http")

        # (b) Only analyse HTML bodies.
        analysis: PageAnalysis | None = None
        if result.is_html and result.html:
            analysis = self.analyzer.analyze(
                result.html, base_url=result.url, source_host=source_host
            )
            if analysis.destination_url:
                dest = await self._validate_destination(analysis.destination_url)
                if dest:
                    return _Resolution(ResolveStatus.RESOLVED, destination=dest, via="http")

        # (c) A CAPTCHA with nothing else to click/execute -> honest report.
        if analysis is not None and analysis.has_captcha and not self._browser_could_help(analysis):
            return _Resolution(
                ResolveStatus.VERIFICATION_REQUIRED,
                message=f"Human verification is required ({analysis.captcha_type})",
                via="http",
            )

        # (d) If the browser could plausibly make progress, fall back.
        if self.settings.enable_browser and (
            analysis is None or self._browser_could_help(analysis)
        ):
            return None

        # (e) Browser disabled or won't help.
        if analysis is not None and analysis.has_captcha:
            return _Resolution(
                ResolveStatus.VERIFICATION_REQUIRED,
                message=f"Human verification is required ({analysis.captcha_type})",
                via="http",
            )
        return _Resolution(
            ResolveStatus.FAILED,
            message="Could not resolve the destination from the page",
            via="http",
        )

    async def _try_browser(
        self, url: str, source_host: str, trace: _Trace, budget: float
    ) -> _Resolution:
        async with self._browser_semaphore:
            try:
                result = await self.browser_agent.resolve(
                    url, source_host=source_host, budget=budget
                )
            except ResolverError as exc:
                logger.info("browser resolution error: %s", exc.message)
                return _Resolution(exc.status, message=exc.message, via="browser")

        trace.steps = result.steps
        if result.status is ResolveStatus.RESOLVED and result.destination_url:
            dest = await self._validate_destination(result.destination_url)
            if dest:
                return _Resolution(
                    ResolveStatus.RESOLVED, destination=dest, via="browser", steps=result.steps
                )
            return _Resolution(
                ResolveStatus.FAILED,
                message="Resolved destination failed security validation",
                via="browser",
                steps=result.steps,
            )

        return _Resolution(
            result.status,
            message=result.message or default_message(result.status),
            via="browser",
            steps=result.steps,
        )

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _browser_could_help(analysis: PageAnalysis) -> bool:
        """Whether browser navigation could plausibly advance this page."""

        if analysis.best_button is not None:
            return True
        if analysis.requires_javascript:
            return True
        return analysis.state in {
            PageState.INTERMEDIATE,
            PageState.COUNTDOWN,
            PageState.REQUIRES_JS,
            PageState.UNKNOWN,
        }

    async def _validate_destination(self, url: str) -> str | None:
        """Validate a candidate destination (SSRF-safe). Return normalised URL or None."""

        try:
            result = await asyncio.to_thread(self.validator.validate, url)
            return result.parsed.url
        except ResolverError as exc:
            logger.warning(
                "discarding unsafe/invalid destination candidate: %s", exc.message
            )
            return None

    def _to_response(
        self, original_url: str, resolution: _Resolution, *, include_meta: bool, start: float
    ) -> ResolveResponse:
        meta: ResolveMeta | None = None
        if include_meta:
            meta = ResolveMeta(
                resolved_via=resolution.via,
                steps=resolution.steps,
                elapsed_ms=round((time.monotonic() - start) * 1000, 2),
            )

        if resolution.status is ResolveStatus.RESOLVED and resolution.destination:
            return ResolveResponse.resolved(
                original_url=original_url,
                destination_url=resolution.destination,
                message=resolution.message,
                meta=meta,
            )
        return ResolveResponse.failure(
            original_url=original_url,
            status=resolution.status,
            message=resolution.message,
            meta=meta,
        )


def _safe(url: str) -> str:
    """Trim a URL for log output (avoid logging very long values)."""

    return url if len(url) <= 256 else url[:253] + "..."
