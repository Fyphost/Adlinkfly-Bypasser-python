"""HTML and page-state analysis.

Given the HTML of an intermediate shortener page, this module determines:

* the current *page state* (direct destination, intermediate step, countdown,
  CAPTCHA, JS-required, unknown);
* candidate destination URLs (from meta-refresh, JavaScript redirects, known
  AdLinkFly markers, and outbound anchors);
* actionable buttons ("Get Link", "Continue", "Verify", "Proceed", ...);
* whether a CAPTCHA / human-verification challenge is present.

Design note
-----------
BeautifulSoup is imported lazily inside :func:`make_soup` so that the pure,
regex/string-based helpers in this module (URL extraction, button
classification, domain comparison) remain importable and unit-testable even in
environments where ``beautifulsoup4`` / ``lxml`` are not installed. The
higher-level :meth:`PageAnalyzer.analyze` naturally requires BeautifulSoup.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import urljoin, urlsplit

# --------------------------------------------------------------------------- #
# Enums and data structures                                                   #
# --------------------------------------------------------------------------- #


class PageState(StrEnum):
    """High-level classification of an intermediate page."""

    DIRECT_DESTINATION = "direct_destination"
    INTERMEDIATE = "intermediate"
    COUNTDOWN = "countdown"
    CAPTCHA = "captcha"
    REQUIRES_JS = "requires_js"
    UNKNOWN = "unknown"


class ButtonKind(StrEnum):
    """Semantic classification of an actionable control."""

    GET_LINK = "get_link"
    CONTINUE = "continue"
    VERIFY = "verify"
    SKIP = "skip"
    CLICK = "click"


#: Relative priority used when choosing which button to click next.
BUTTON_PRIORITY: dict[ButtonKind, int] = {
    ButtonKind.GET_LINK: 100,
    ButtonKind.CONTINUE: 80,
    ButtonKind.SKIP: 60,
    ButtonKind.CLICK: 50,
    ButtonKind.VERIFY: 40,
}


@dataclass(slots=True)
class ButtonCandidate:
    """An actionable element discovered on the page."""

    text: str
    kind: ButtonKind
    tag: str
    element_id: str | None = None
    classes: tuple[str, ...] = ()
    href: str | None = None
    disabled: bool = False

    @property
    def score(self) -> int:
        base = BUTTON_PRIORITY.get(self.kind, 0)
        if self.disabled:
            base -= 25
        if self.element_id:
            base += 5
        return base


@dataclass(slots=True)
class PageAnalysis:
    """The structured result of analysing a page."""

    state: PageState
    destination_url: str | None = None
    candidate_destinations: list[str] = field(default_factory=list)
    buttons: list[ButtonCandidate] = field(default_factory=list)
    has_captcha: bool = False
    captcha_type: str | None = None
    requires_javascript: bool = False
    has_countdown: bool = False
    title: str | None = None
    looks_like_adlinkfly: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def best_button(self) -> ButtonCandidate | None:
        actionable = [b for b in self.buttons if not b.disabled]
        pool = actionable or self.buttons
        return max(pool, key=lambda b: b.score) if pool else None


# --------------------------------------------------------------------------- #
# Pure helpers (no third-party dependency -- unit-testable anywhere)          #
# --------------------------------------------------------------------------- #

#: Multi-label public suffixes handled by the registrable-domain heuristic.
_TWO_LABEL_SUFFIXES: frozenset[str] = frozenset(
    {
        "co.uk",
        "org.uk",
        "ac.uk",
        "gov.uk",
        "co.in",
        "co.jp",
        "com.au",
        "com.br",
        "com.cn",
        "co.za",
        "co.nz",
        "com.mx",
        "com.tr",
    }
)

#: Hosts (by substring) that are advertising / social / analytics endpoints and
#: should never be treated as a resolution destination.
_AD_SOCIAL_SUBSTRINGS: tuple[str, ...] = (
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "google-analytics.com",
    "googletagmanager.com",
    "adservice.google",
    "facebook.com/sharer",
    "facebook.com/plugins",
    "twitter.com/intent",
    "t.co/",
    "pinterest.com/pin",
    "linkedin.com/share",
    "whatsapp.com/send",
    "api.whatsapp.com",
    "reddit.com/submit",
    "disqus.com",
    "amazon-adsystem.com",
    "adsterra",
    "propellerads",
    "popads",
    "media.net",
)

_JS_URL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"""(?:window\.)?(?:top\.)?location(?:\.href)?\s*=\s*["']([^"']+)["']""",
        re.IGNORECASE,
    ),
    re.compile(r"""location\.(?:replace|assign)\s*\(\s*["']([^"']+)["']\s*\)""", re.IGNORECASE),
    re.compile(
        r"""(?:var|let|const)\s+\w*(?:url|link|dest)\w*\s*=\s*["'](https?://[^"']+)["']""",
        re.IGNORECASE,
    ),
    re.compile(
        r"""["']?(?:destination|final_url|real_url)["']?\s*[:=]\s*["'](https?://[^"']+)["']""",
        re.IGNORECASE,
    ),
)

_META_REFRESH_URL_RE = re.compile(r"url\s*=\s*['\"]?([^'\";]+)", re.IGNORECASE)

_SECONDS_RE = re.compile(r"\b(\d{1,3})\s*(?:seconds?|secs?|s)\b", re.IGNORECASE)

#: Keyword -> button kind, evaluated in order (first match wins).
_BUTTON_KEYWORDS: tuple[tuple[ButtonKind, tuple[str, ...]], ...] = (
    (ButtonKind.GET_LINK, ("get link", "getlink", "get-link", "get your link", "generate link")),
    (ButtonKind.VERIFY, ("verify", "i'm not a robot", "i am not a robot", "human verification")),
    (ButtonKind.SKIP, ("skip ad", "skip this ad", "skip")),
    (
        ButtonKind.CONTINUE,
        ("continue", "proceed", "next step", "next", "go to link", "get started"),
    ),
    (ButtonKind.CLICK, ("click here", "click to continue", "click", "open link")),
)

_CAPTCHA_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("recaptcha", "recaptcha"),
    ("g-recaptcha", "recaptcha"),
    ("grecaptcha", "recaptcha"),
    ("google.com/recaptcha", "recaptcha"),
    ("gstatic.com/recaptcha", "recaptcha"),
    ("h-captcha", "hcaptcha"),
    ("hcaptcha.com", "hcaptcha"),
    ("cf-turnstile", "turnstile"),
    ("challenges.cloudflare.com/turnstile", "turnstile"),
)

_ADLINKFLY_SIGNATURES: tuple[str, ...] = (
    "adlinkfly",
    "/links/go",
    "shortener",
    "invisiblecaptchashortlink",
    "get-link-btn",
)

#: Element ids commonly used by AdLinkFly-style themes to hold the final link.
_KNOWN_DESTINATION_IDS: frozenset[str] = frozenset(
    {"btn6", "invisiblecaptchashortlink", "link-view", "generated-link", "final-link"}
)


def registrable_domain(host: str) -> str:
    """Return a best-effort registrable domain for ``host`` (heuristic)."""

    host = host.lower().strip().strip(".")
    if not host:
        return ""
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    last_two = ".".join(labels[-2:])
    last_three = ".".join(labels[-3:])
    if last_two in _TWO_LABEL_SUFFIXES:
        return last_three
    return last_two


def is_external(candidate_host: str, source_host: str) -> bool:
    """True if ``candidate_host`` is a different registrable domain than source."""

    if not candidate_host:
        return False
    return registrable_domain(candidate_host) != registrable_domain(source_host)


def is_ad_or_social(url: str) -> bool:
    """True if ``url`` points at a known ad/social/analytics endpoint."""

    lowered = url.lower()
    return any(sub in lowered for sub in _AD_SOCIAL_SUBSTRINGS)


def extract_urls_from_script(script_text: str) -> list[str]:
    """Extract candidate redirect URLs from inline JavaScript (regex-based)."""

    found: list[str] = []
    seen: set[str] = set()
    for pattern in _JS_URL_PATTERNS:
        for match in pattern.findall(script_text):
            url = match.strip()
            if url and url not in seen:
                seen.add(url)
                found.append(url)
    return found


def parse_meta_refresh_content(content: str) -> str | None:
    """Parse the ``url=`` part of a ``<meta http-equiv="refresh">`` content."""

    if not content:
        return None
    match = _META_REFRESH_URL_RE.search(content)
    if match:
        return match.group(1).strip()
    return None


def classify_button_text(text: str) -> ButtonKind | None:
    """Classify button/anchor text into a :class:`ButtonKind`, or ``None``."""

    if not text:
        return None
    lowered = " ".join(text.lower().split())
    for kind, keywords in _BUTTON_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return kind
    return None


def extract_countdown_seconds(text: str) -> int | None:
    """Return the first "N seconds" countdown value found in ``text``."""

    match = _SECONDS_RE.search(text)
    if match:
        try:
            return int(match.group(1))
        except ValueError:  # pragma: no cover - guarded by regex
            return None
    return None


# --------------------------------------------------------------------------- #
# BeautifulSoup-backed analyzer                                               #
# --------------------------------------------------------------------------- #


def make_soup(html: str) -> Any:
    """Build a BeautifulSoup tree, preferring the lxml parser.

    BeautifulSoup is imported here (lazily) so that importing this module does
    not require ``beautifulsoup4``/``lxml`` to be installed.
    """

    from bs4 import BeautifulSoup

    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # pragma: no cover - fallback when lxml unavailable
        return BeautifulSoup(html, "html.parser")


class PageAnalyzer:
    """Analyse shortener HTML into a :class:`PageAnalysis`."""

    def __init__(self, *, min_visible_text_for_static: int = 64) -> None:
        self.min_visible_text_for_static = min_visible_text_for_static

    # -- public API ----------------------------------------------------- #
    def analyze(self, html: str, *, base_url: str, source_host: str) -> PageAnalysis:
        soup = make_soup(html or "")

        title = None
        if soup.title and soup.title.string:
            title = soup.title.string.strip()

        has_captcha, captcha_type = self.detect_captcha(soup, html)
        buttons = self.find_buttons(soup)
        destination, candidates = self.extract_destinations(soup, base_url, source_host)
        has_countdown = self.detect_countdown(soup)
        requires_js = self.detect_requires_js(
            soup, html, has_destination=destination is not None, has_buttons=bool(buttons)
        )
        looks_adlinkfly = self.looks_like_adlinkfly(html)

        state = self._determine_state(
            has_captcha=has_captcha,
            destination=destination,
            buttons=buttons,
            has_countdown=has_countdown,
            requires_js=requires_js,
        )

        return PageAnalysis(
            state=state,
            destination_url=destination,
            candidate_destinations=candidates,
            buttons=buttons,
            has_captcha=has_captcha,
            captcha_type=captcha_type,
            requires_javascript=requires_js,
            has_countdown=has_countdown,
            title=title,
            looks_like_adlinkfly=looks_adlinkfly,
        )

    # -- detectors ------------------------------------------------------- #
    def detect_captcha(self, soup: Any, html: str) -> tuple[bool, str | None]:
        lowered = (html or "").lower()
        for signature, kind in _CAPTCHA_SIGNATURES:
            if signature in lowered:
                return True, kind
        # Fallback: a form field literally named/if labelled captcha.
        if soup.find(attrs={"name": re.compile("captcha", re.IGNORECASE)}):
            return True, "generic"
        return False, None

    def detect_countdown(self, soup: Any) -> bool:
        attr_re = re.compile(r"count|timer|wait|seconds", re.IGNORECASE)
        if soup.find(id=attr_re) or soup.find(class_=attr_re):
            return True
        text = soup.get_text(" ", strip=True)
        return extract_countdown_seconds(text) is not None

    def detect_requires_js(
        self, soup: Any, html: str, *, has_destination: bool, has_buttons: bool
    ) -> bool:
        if has_destination or has_buttons:
            return False
        scripts = soup.find_all("script")
        visible = soup.get_text(" ", strip=True)
        if soup.find("noscript") and len(visible) < self.min_visible_text_for_static:
            return True
        return bool(scripts) and len(visible) < self.min_visible_text_for_static

    def looks_like_adlinkfly(self, html: str) -> bool:
        lowered = (html or "").lower()
        return any(sig in lowered for sig in _ADLINKFLY_SIGNATURES)

    # -- extraction ------------------------------------------------------ #
    def find_buttons(self, soup: Any) -> list[ButtonCandidate]:
        buttons: list[ButtonCandidate] = []
        selectors = ["button", "a", "input", "[role=button]"]
        seen: set[tuple[str, str]] = set()
        for element in soup.select(",".join(selectors)):
            tag = element.name or ""
            text = self._element_label(element)
            kind = classify_button_text(text)
            if kind is None:
                continue
            element_id = element.get("id")
            dedupe_key = (text.lower(), element_id or "")
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            classes = tuple(element.get("class", []) or [])
            href = element.get("href") if tag == "a" else None
            disabled = element.has_attr("disabled") or element.get("aria-disabled") == "true"
            buttons.append(
                ButtonCandidate(
                    text=text,
                    kind=kind,
                    tag=tag,
                    element_id=element_id,
                    classes=classes,
                    href=href,
                    disabled=disabled,
                )
            )
        buttons.sort(key=lambda b: b.score, reverse=True)
        return buttons

    def extract_destinations(
        self, soup: Any, base_url: str, source_host: str
    ) -> tuple[str | None, list[str]]:
        """Return ``(strong_destination, all_candidates)``.

        ``strong_destination`` is only set when a high-confidence signal (meta
        refresh, JS redirect, or a known destination element) yields an
        external URL. Outbound anchors are returned as weaker candidates.
        """

        strong: list[str] = []
        weak: list[str] = []

        # 1. meta refresh (strong)
        meta = soup.find("meta", attrs={"http-equiv": re.compile("^refresh$", re.IGNORECASE)})
        if meta and meta.get("content"):
            url = parse_meta_refresh_content(meta["content"])
            if url:
                strong.append(urljoin(base_url, url))

        # 2. inline JS redirects (strong)
        for script in soup.find_all("script"):
            if script.string:
                for url in extract_urls_from_script(script.string):
                    strong.append(urljoin(base_url, url))

        # 3. known AdLinkFly destination elements / data attributes (strong)
        for element in soup.find_all(attrs={"id": True}):
            if (element.get("id") or "").lower() in _KNOWN_DESTINATION_IDS:
                href = element.get("href") or element.get("data-url") or element.get("data-href")
                if href:
                    strong.append(urljoin(base_url, href))
        for element in soup.find_all(attrs={"data-url": True}):
            strong.append(urljoin(base_url, element["data-url"]))
        for element in soup.find_all(attrs={"data-href": True}):
            strong.append(urljoin(base_url, element["data-href"]))

        # 4. outbound anchors (weak)
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            absolute = urljoin(base_url, href)
            weak.append(absolute)

        strong_ext = self._filter_external(strong, source_host)
        weak_ext = self._filter_external(weak, source_host)

        all_candidates = self._dedupe(strong_ext + weak_ext)
        destination = strong_ext[0] if strong_ext else None
        return destination, all_candidates

    # -- internals ------------------------------------------------------- #
    @staticmethod
    def _element_label(element: Any) -> str:
        text = element.get_text(" ", strip=True) if hasattr(element, "get_text") else ""
        if not text:
            for attr in ("value", "aria-label", "title", "alt"):
                value = element.get(attr)
                if value:
                    text = str(value).strip()
                    break
        return text

    @staticmethod
    def _filter_external(urls: list[str], source_host: str) -> list[str]:
        result: list[str] = []
        for url in urls:
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https"):
                continue
            host = parts.hostname or ""
            if not host:
                continue
            if not is_external(host, source_host):
                continue
            if is_ad_or_social(url):
                continue
            result.append(url)
        return result

    @staticmethod
    def _dedupe(urls: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for url in urls:
            if url not in seen:
                seen.add(url)
                result.append(url)
        return result

    @staticmethod
    def _determine_state(
        *,
        has_captcha: bool,
        destination: str | None,
        buttons: list[ButtonCandidate],
        has_countdown: bool,
        requires_js: bool,
    ) -> PageState:
        if has_captcha:
            return PageState.CAPTCHA
        if destination:
            return PageState.DIRECT_DESTINATION
        if has_countdown:
            return PageState.COUNTDOWN
        if buttons:
            return PageState.INTERMEDIATE
        if requires_js:
            return PageState.REQUIRES_JS
        return PageState.UNKNOWN
