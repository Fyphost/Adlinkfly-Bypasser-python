"""URL and network-security validation (SSRF protection).

This module is deliberately implemented using **only the Python standard
library** so that the security-critical validation logic can be unit-tested in
any environment, without installing FastAPI, cloudscraper, Playwright, or even
an internet connection (DNS is injected and therefore mockable).

Responsibilities
----------------
* Strict syntactic validation of user-supplied URLs (scheme, host, length,
  rejection of embedded credentials).
* DNS resolution of the host to every A/AAAA address.
* Rejection of any address that is loopback, private, link-local (including the
  cloud metadata endpoint ``169.254.169.254``), multicast, reserved or
  otherwise non-global -- protecting against SSRF.
* IPv4-mapped IPv6 unwrapping so that e.g. ``::ffff:169.254.169.254`` cannot be
  used to smuggle a blocked address past the checks.
* Redirect-by-redirect validation: every hop's absolute target is revalidated,
  which also mitigates DNS-rebinding when the caller re-resolves per hop.
* Authorised-domain (allowlist) checks for supported shorteners.

The validator does **not** perform any network I/O other than DNS resolution,
and that resolver is injectable.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from urllib.parse import SplitResult, urljoin, urlsplit

from .exceptions import SSRFError, UnsupportedDomainError, UrlValidationError

#: Addresses that must always be rejected, independent of the general
#: classification rules, as defence in depth. The IMDS endpoint is the most
#: important entry here.
_EXPLICIT_METADATA_ADDRESSES: frozenset[str] = frozenset(
    {
        "169.254.169.254",  # AWS / GCP / Azure / OpenStack IMDS (IPv4)
        "fd00:ec2::254",  # AWS IMDS (IPv6)
    }
)

#: Default schemes permitted for both the initial URL and every redirect hop.
DEFAULT_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})


DnsResolver = Callable[[str], Sequence[str]]
"""A callable mapping a hostname to a sequence of resolved IP address strings.

Injected to make DNS mockable in tests and to allow custom resolvers (e.g. one
that caches, or one that pins results) in production.
"""


@dataclass(frozen=True, slots=True)
class ParsedURL:
    """A validated, normalised URL."""

    url: str
    scheme: str
    host: str
    port: int | None
    is_ip_literal: bool


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """The result of a successful full validation."""

    parsed: ParsedURL
    ip_addresses: tuple[str, ...] = field(default_factory=tuple)


def default_dns_resolver(host: str, *, timeout: float = 5.0) -> list[str]:
    """Resolve ``host`` to a de-duplicated list of IP address strings.

    Uses :func:`socket.getaddrinfo` under a temporarily-set default socket
    timeout. Raises :class:`socket.gaierror` / :class:`OSError` on failure; the
    caller is responsible for translating those into a validation error.
    """

    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    finally:
        socket.setdefaulttimeout(previous)

    addresses: list[str] = []
    seen: set[str] = set()
    for info in infos:
        sockaddr = info[4]
        ip = str(sockaddr[0])
        if ip not in seen:
            seen.add(ip)
            addresses.append(ip)
    return addresses


class URLValidator:
    """Validate URLs and guard against SSRF / unsafe network targets.

    Parameters
    ----------
    allow_private:
        When ``True`` (intended for local development and tests only) the
        address-classification checks are skipped, permitting loopback/private
        targets such as a local test server. Defaults to ``False``.
    allowed_schemes:
        The set of permitted URL schemes.
    max_url_length:
        Reject URLs longer than this many characters.
    allowed_ports:
        When provided, only these explicit ports (plus the scheme default) are
        permitted. ``None`` allows any port.
    dns_resolver:
        Injectable hostname -> IP-list callable. Defaults to
        :func:`default_dns_resolver`.
    """

    def __init__(
        self,
        *,
        allow_private: bool = False,
        allowed_schemes: Iterable[str] = DEFAULT_ALLOWED_SCHEMES,
        max_url_length: int = 2048,
        allowed_ports: Iterable[int] | None = None,
        dns_resolver: DnsResolver | None = None,
    ) -> None:
        self.allow_private = allow_private
        self.allowed_schemes = frozenset(s.lower() for s in allowed_schemes)
        self.max_url_length = max_url_length
        self.allowed_ports = frozenset(allowed_ports) if allowed_ports is not None else None
        self._dns_resolver = dns_resolver or default_dns_resolver

    # ------------------------------------------------------------------ #
    # Syntactic validation                                               #
    # ------------------------------------------------------------------ #
    def validate_syntax(self, url: str) -> ParsedURL:
        """Validate the structure of ``url`` without any network I/O.

        Raises
        ------
        UrlValidationError
            If the URL is not a string, is empty, too long, uses a disallowed
            scheme, embeds credentials, has no host, or has an invalid/for-
            bidden port.
        """

        if not isinstance(url, str):
            raise UrlValidationError("URL must be a string")

        candidate = url.strip()
        if not candidate:
            raise UrlValidationError("URL must not be empty")
        if len(candidate) > self.max_url_length:
            raise UrlValidationError(
                f"URL exceeds the maximum allowed length of {self.max_url_length} characters"
            )
        if any(ch.isspace() for ch in candidate):
            raise UrlValidationError("URL must not contain whitespace")

        try:
            split = urlsplit(candidate)
        except ValueError as exc:  # pragma: no cover - urlsplit rarely raises
            raise UrlValidationError(f"URL could not be parsed: {exc}") from exc

        scheme = split.scheme.lower()
        if not scheme:
            raise UrlValidationError("URL must include a scheme (http or https)")
        if scheme not in self.allowed_schemes:
            raise UrlValidationError(f"URL scheme '{scheme}' is not permitted")

        # Reject embedded credentials (user:pass@host) which are a common SSRF
        # obfuscation vector and ambiguous to parse safely.
        if split.username or split.password:
            raise UrlValidationError("URLs with embedded credentials are not permitted")

        host = split.hostname
        if not host:
            raise UrlValidationError("URL must include a host")
        host = host.lower().rstrip(".")
        if not host:
            raise UrlValidationError("URL has an empty host")

        try:
            port = split.port
        except ValueError as exc:
            raise UrlValidationError("URL has an invalid port") from exc

        if self.allowed_ports is not None and port is not None and port not in self.allowed_ports:
            raise UrlValidationError(f"Port {port} is not permitted")

        is_ip_literal = self._is_ip_literal(host)
        normalized = self._normalize(split, host)
        return ParsedURL(
            url=normalized,
            scheme=scheme,
            host=host,
            port=port,
            is_ip_literal=is_ip_literal,
        )

    @staticmethod
    def _normalize(split: SplitResult, host: str) -> str:
        """Rebuild a normalised absolute URL, preserving path/query/fragment.

        Called only after :meth:`validate_syntax` has confirmed the port parses,
        so ``split.port`` is safe to access here.
        """

        netloc = host
        if split.port is not None:
            # Re-wrap IPv6 literals in brackets for the netloc.
            host_part = f"[{host}]" if ":" in host else host
            netloc = f"{host_part}:{split.port}"
        elif ":" in host:
            netloc = f"[{host}]"
        path = split.path or ""
        query = f"?{split.query}" if split.query else ""
        fragment = f"#{split.fragment}" if split.fragment else ""
        return f"{split.scheme.lower()}://{netloc}{path}{query}{fragment}"

    @staticmethod
    def _is_ip_literal(host: str) -> bool:
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return False

    # ------------------------------------------------------------------ #
    # Address-level (SSRF) validation                                     #
    # ------------------------------------------------------------------ #
    def classify_blocked(self, ip: str) -> str | None:
        """Return a human-readable reason if ``ip`` is blocked, else ``None``.

        An IPv4-mapped IPv6 address is unwrapped and judged on its embedded
        IPv4 value so a mapped address cannot smuggle a blocked target.
        """

        if self.allow_private:
            return None

        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return f"'{ip}' is not a valid IP address"

        # Unwrap IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1) to the real IPv4.
        mapped = getattr(addr, "ipv4_mapped", None)
        if mapped is not None:
            addr = mapped

        if str(addr) in _EXPLICIT_METADATA_ADDRESSES:
            return "address is a cloud metadata endpoint"

        checks: tuple[tuple[bool, str], ...] = (
            (addr.is_unspecified, "address is unspecified (0.0.0.0/::)"),
            (addr.is_loopback, "address is loopback"),
            (addr.is_link_local, "address is link-local"),
            (addr.is_multicast, "address is multicast"),
            (addr.is_reserved, "address is reserved"),
            (addr.is_private, "address is private/internal"),
        )
        for is_bad, reason in checks:
            if is_bad:
                return reason

        if not addr.is_global:
            return "address is not globally routable"

        return None

    def check_ip_allowed(self, ip: str) -> None:
        """Raise :class:`SSRFError` if ``ip`` is not an allowed public address."""

        reason = self.classify_blocked(ip)
        if reason is not None:
            raise SSRFError(f"Blocked network target {ip!r}: {reason}")

    def resolve_host(self, host: str) -> list[str]:
        """Resolve ``host`` to IP strings, raising on DNS failure.

        If ``host`` is already an IP literal it is returned as-is (no DNS).
        """

        if self._is_ip_literal(host):
            return [host]
        try:
            addresses = list(self._dns_resolver(host))
        except (OSError, socket.gaierror) as exc:
            raise UrlValidationError(f"DNS resolution failed for host '{host}'") from exc
        if not addresses:
            raise UrlValidationError(f"Host '{host}' did not resolve to any address")
        return addresses

    # ------------------------------------------------------------------ #
    # Composed, high-level entry points                                   #
    # ------------------------------------------------------------------ #
    def validate(self, url: str) -> ValidationResult:
        """Fully validate ``url``: syntax, DNS resolution and SSRF checks.

        Returns the parsed URL together with every resolved IP address so the
        caller may pin connections to those addresses (DNS-rebinding defence).
        """

        parsed = self.validate_syntax(url)
        addresses = self.resolve_host(parsed.host)
        for ip in addresses:
            self.check_ip_allowed(ip)
        return ValidationResult(parsed=parsed, ip_addresses=tuple(addresses))

    def validate_redirect(self, location: str, *, base_url: str) -> ValidationResult:
        """Validate a redirect ``location`` (possibly relative) against ``base_url``.

        The location is resolved to an absolute URL first, then fully validated.
        Re-resolving here (every hop) is what provides redirect-by-redirect SSRF
        protection and DNS-rebinding mitigation.
        """

        if not location or not location.strip():
            raise UrlValidationError("Redirect location is empty")
        absolute = urljoin(base_url, location.strip())
        return self.validate(absolute)

    # ------------------------------------------------------------------ #
    # Authorisation / supported-domain checks                             #
    # ------------------------------------------------------------------ #
    @staticmethod
    def is_supported_domain(host: str, supported_domains: Iterable[str]) -> bool:
        """Return ``True`` if ``host`` matches an entry in ``supported_domains``.

        Matching is case-insensitive and includes subdomains: an entry
        ``arolinks.com`` matches ``arolinks.com`` and ``www.arolinks.com`` but
        not ``notarolinks.com`` or ``arolinks.com.evil.test``.
        """

        host = host.lower().rstrip(".")
        for raw in supported_domains:
            domain = raw.lower().strip().lstrip(".").rstrip(".")
            if not domain:
                continue
            if host == domain or host.endswith("." + domain):
                return True
        return False

    def ensure_supported_domain(self, host: str, supported_domains: Iterable[str]) -> None:
        """Raise :class:`UnsupportedDomainError` if ``host`` is not supported."""

        if not self.is_supported_domain(host, supported_domains):
            raise UnsupportedDomainError(
                f"Domain '{host}' is not in the list of supported shorteners"
            )
