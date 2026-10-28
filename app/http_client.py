"""cloudscraper-based HTTP client with SSRF-safe redirect handling.

This component performs ordinary (non-JavaScript) HTTP fetches and transparently
handles the anti-bot challenges that cloudscraper supports. It deliberately does
**not** attempt to solve CAPTCHAs -- when a page needs JavaScript execution or
human verification, the resolver falls back to the browser agent or reports a
verification requirement.

Key safety properties
----------------------
* A **fresh** cloudscraper session is created for every :meth:`fetch` call, so
  cookies are never shared between unrelated requests/users.
* Redirects are followed **manually**, one hop at a time, and every hop is
  re-validated through :class:`~app.url_validator.URLValidator` (SSRF +
  DNS-rebinding protection applies to the whole chain, not just the first URL).
* Response bodies are capped at a configurable byte limit.
* Blocking I/O runs in a worker thread so the asyncio event loop stays free.

cloudscraper is imported lazily (only inside the default session factory) so
this module can be imported -- and the client can be unit-tested with an
injected fake session -- without the package installed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .exceptions import ResolverError, UpstreamError
from .url_validator import URLValidator, ValidationResult

logger = logging.getLogger(__name__)

#: A session factory returns an object exposing a ``requests``-like ``get`` and
#: ``close``. Injected to decouple from cloudscraper in tests.
SessionFactory = Callable[[], Any]

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class ChallengeError(UpstreamError):
    """cloudscraper could not satisfy an anti-bot challenge for this URL.

    Signals the resolver that a browser fallback should be attempted.
    """


@dataclass(slots=True)
class HttpFetchResult:
    """The outcome of a successful HTTP fetch (after following redirects)."""

    url: str
    status_code: int
    html: str
    headers: dict[str, str] = field(default_factory=dict)
    content_type: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False

    @property
    def is_html(self) -> bool:
        ct = self.content_type.lower()
        return "html" in ct or "xml" in ct or ct == ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


def _default_session_factory(user_agent: str) -> Callable[[], Any]:
    """Build the default cloudscraper session factory bound to ``user_agent``."""

    def factory() -> Any:
        # Lazy import: cloudscraper is only needed for the real (default) path.
        import cloudscraper

        return cloudscraper.create_scraper(
            browser={"custom": user_agent},
            delay=5,
        )

    return factory


class CloudscraperClient:
    """Fetch pages via cloudscraper with manual, validated redirect handling."""

    def __init__(
        self,
        validator: URLValidator,
        *,
        connect_timeout: float = 10.0,
        read_timeout: float = 20.0,
        max_redirects: int = 10,
        max_response_bytes: int = 3_000_000,
        user_agent: str = "Mozilla/5.0",
        session_factory: SessionFactory | None = None,
    ) -> None:
        self.validator = validator
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_redirects = max_redirects
        self.max_response_bytes = max_response_bytes
        self.user_agent = user_agent
        self._session_factory = session_factory or _default_session_factory(user_agent)

    async def fetch(
        self, url: str, *, validated: ValidationResult | None = None
    ) -> HttpFetchResult:
        """Fetch ``url`` (following validated redirects) off the event loop."""

        return await asyncio.to_thread(self._fetch_sync, url, validated)

    # ------------------------------------------------------------------ #
    # Blocking implementation (runs in a worker thread)                   #
    # ------------------------------------------------------------------ #
    def _fetch_sync(self, url: str, validated: ValidationResult | None) -> HttpFetchResult:
        session = self._session_factory()
        history: list[dict[str, Any]] = []
        current_url = validated.parsed.url if validated else url
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        try:
            for hop in range(self.max_redirects + 1):
                response = self._request(session, current_url, headers)
                status = response.status_code
                location = response.headers.get("Location") or response.headers.get("location")

                if status in _REDIRECT_STATUSES and location:
                    history.append({"status": status, "url": current_url})
                    # Re-validate every hop (SSRF + DNS-rebinding defence).
                    next_result = self.validator.validate_redirect(
                        location, base_url=current_url
                    )
                    current_url = next_result.parsed.url
                    self._drain(response)
                    if hop == self.max_redirects:
                        raise UpstreamError(
                            f"Exceeded the maximum of {self.max_redirects} redirects"
                        )
                    continue

                # Terminal response: read (capped) body and return.
                body, truncated = self._read_capped(response)
                content_type = (
                    response.headers.get("Content-Type")
                    or response.headers.get("content-type")
                    or ""
                )
                html = self._decode(response, body)
                return HttpFetchResult(
                    url=current_url,
                    status_code=status,
                    html=html,
                    headers=dict(response.headers),
                    content_type=content_type,
                    history=history,
                    truncated=truncated,
                )

            raise UpstreamError("Redirect handling terminated unexpectedly")  # pragma: no cover
        except ResolverError:
            # Validation / SSRF / upstream errors already carry the right type
            # and status -- never reclassify them as a generic transport error.
            raise
        except Exception as exc:
            raise self._classify_error(exc) from exc
        finally:
            self._safe_close(session)

    def _request(self, session: Any, url: str, headers: dict[str, str]) -> Any:
        return session.get(
            url,
            headers=headers,
            timeout=(self.connect_timeout, self.read_timeout),
            allow_redirects=False,
            stream=True,
        )

    def _read_capped(self, response: Any) -> tuple[bytes, bool]:
        """Read up to ``max_response_bytes`` from ``response``."""

        chunks: list[bytes] = []
        total = 0
        truncated = False
        try:
            for chunk in response.iter_content(chunk_size=16_384):
                if not chunk:
                    continue
                chunks.append(chunk)
                total += len(chunk)
                if total >= self.max_response_bytes:
                    truncated = True
                    break
        finally:
            self._drain(response)
        return b"".join(chunks)[: self.max_response_bytes], truncated

    @staticmethod
    def _decode(response: Any, body: bytes) -> str:
        encoding = getattr(response, "encoding", None) or "utf-8"
        try:
            return body.decode(encoding, errors="replace")
        except (LookupError, TypeError):
            return body.decode("utf-8", errors="replace")

    @staticmethod
    def _drain(response: Any) -> None:
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                logger.debug("Error closing HTTP response", exc_info=True)

    @staticmethod
    def _safe_close(session: Any) -> None:
        close = getattr(session, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                logger.debug("Error closing HTTP session", exc_info=True)

    @staticmethod
    def _classify_error(exc: Exception) -> UpstreamError:
        """Map a transport exception to a typed resolver error.

        Cloudflare/challenge failures become :class:`ChallengeError` (so the
        resolver tries the browser), everything else a generic
        :class:`UpstreamError`.
        """

        name = type(exc).__name__.lower()
        module = type(exc).__module__.lower()
        if "cloudflare" in name or "challenge" in name or "cloudscraper" in module:
            return ChallengeError(f"Anti-bot challenge could not be solved: {exc}")
        return UpstreamError(f"HTTP request failed: {exc}")
