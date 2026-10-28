"""Tests for :mod:`app.url_validator`.

These tests are pure standard library (DNS is injected) and therefore run in
any environment, including offline / dependency-free sandboxes. They exercise
the security-critical SSRF and URL-validation behaviour.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from app.exceptions import SSRFError, UnsupportedDomainError, UrlValidationError
from app.url_validator import URLValidator


def make_validator(dns_map: dict[str, list[str]] | None = None, **kwargs) -> URLValidator:
    """Build a validator with an in-memory DNS resolver."""

    dns_map = dns_map or {}

    def resolver(host: str):
        if host in dns_map:
            return dns_map[host]
        raise OSError(f"no DNS entry for {host}")

    kwargs.setdefault("dns_resolver", resolver)
    return URLValidator(**kwargs)


# --------------------------------------------------------------------------- #
# Syntactic validation                                                        #
# --------------------------------------------------------------------------- #
class TestSyntax:
    def test_valid_https_url(self):
        v = make_validator()
        parsed = v.validate_syntax("https://Arolinks.com/AbC?x=1#frag")
        assert parsed.scheme == "https"
        assert parsed.host == "arolinks.com"  # host lower-cased
        assert parsed.url == "https://arolinks.com/AbC?x=1#frag"  # path case kept
        assert parsed.is_ip_literal is False

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.com",
            "file:///etc/passwd",
            "gopher://example.com",
            "javascript:alert(1)",
        ],
    )
    def test_disallowed_schemes(self, url):
        v = make_validator()
        with pytest.raises(UrlValidationError):
            v.validate_syntax(url)

    @pytest.mark.parametrize("url", ["", "   ", "\t"])
    def test_empty(self, url):
        v = make_validator()
        with pytest.raises(UrlValidationError):
            v.validate_syntax(url)

    def test_non_string(self):
        v = make_validator()
        with pytest.raises(UrlValidationError):
            v.validate_syntax(None)  # type: ignore[arg-type]

    def test_missing_scheme(self):
        v = make_validator()
        with pytest.raises(UrlValidationError):
            v.validate_syntax("example.com/path")

    def test_missing_host(self):
        v = make_validator()
        with pytest.raises(UrlValidationError):
            v.validate_syntax("http:///just/a/path")

    def test_embedded_credentials_rejected(self):
        v = make_validator()
        with pytest.raises(UrlValidationError):
            v.validate_syntax("http://user:pass@example.com/")

    def test_whitespace_in_url_rejected(self):
        v = make_validator()
        with pytest.raises(UrlValidationError):
            v.validate_syntax("http://exa mple.com/")

    def test_too_long(self):
        v = make_validator(max_url_length=40)
        with pytest.raises(UrlValidationError):
            v.validate_syntax("https://example.com/" + "a" * 100)

    def test_invalid_port(self):
        v = make_validator()
        with pytest.raises(UrlValidationError):
            v.validate_syntax("http://example.com:not-a-port/")

    def test_port_allowlist(self):
        v = make_validator(allowed_ports={80, 443})
        with pytest.raises(UrlValidationError):
            v.validate_syntax("http://8.8.8.8:8080/")
        # Allowed ports pass syntactic validation.
        assert v.validate_syntax("http://8.8.8.8:80/").port == 80

    def test_ipv6_literal_bracketing_preserved(self):
        v = make_validator(allow_private=True)
        parsed = v.validate_syntax("http://[2001:4860:4860::8888]:8080/x")
        assert parsed.host == "2001:4860:4860::8888"
        assert parsed.port == 8080
        assert parsed.url == "http://[2001:4860:4860::8888]:8080/x"


# --------------------------------------------------------------------------- #
# Address classification (SSRF)                                               #
# --------------------------------------------------------------------------- #
class TestAddressClassification:
    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",
            "0.0.0.0",
            "10.0.0.5",
            "172.16.4.3",
            "192.168.1.10",
            "169.254.169.254",  # cloud metadata
            "169.254.1.1",  # link-local
            "::1",  # IPv6 loopback
            "fe80::1",  # IPv6 link-local
            "fc00::1",  # IPv6 unique-local (private)
            "fd00:ec2::254",  # IPv6 metadata
            "224.0.0.1",  # multicast
            "::ffff:127.0.0.1",  # IPv4-mapped loopback
            "::ffff:169.254.169.254",  # IPv4-mapped metadata
        ],
    )
    def test_blocked_addresses(self, ip):
        v = make_validator()
        assert v.classify_blocked(ip) is not None
        with pytest.raises(SSRFError):
            v.check_ip_allowed(ip)

    @pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2001:4860:4860::8888"])
    def test_allowed_public_addresses(self, ip):
        v = make_validator()
        assert v.classify_blocked(ip) is None
        v.check_ip_allowed(ip)  # does not raise

    def test_allow_private_flag_disables_checks(self):
        v = make_validator(allow_private=True)
        assert v.classify_blocked("127.0.0.1") is None
        v.check_ip_allowed("10.0.0.1")  # does not raise

    def test_garbage_ip_is_blocked(self):
        v = make_validator()
        assert v.classify_blocked("not-an-ip") is not None


# --------------------------------------------------------------------------- #
# Full validation (syntax + DNS + SSRF)                                       #
# --------------------------------------------------------------------------- #
class TestValidate:
    def test_valid_public_host(self):
        v = make_validator({"arolinks.com": ["8.8.8.8"]})
        result = v.validate("https://arolinks.com/abc123")
        assert result.parsed.host == "arolinks.com"
        assert result.ip_addresses == ("8.8.8.8",)

    def test_host_resolving_to_private_is_blocked(self):
        # Simulates a hostname pointing at an internal address (SSRF / rebinding).
        v = make_validator({"evil.example": ["10.0.0.5"]})
        with pytest.raises(SSRFError):
            v.validate("https://evil.example/path")

    def test_any_private_ip_among_many_blocks(self):
        v = make_validator({"mixed.example": ["8.8.8.8", "127.0.0.1"]})
        with pytest.raises(SSRFError):
            v.validate("https://mixed.example/")

    def test_ip_literal_loopback_blocked_without_dns(self):
        v = make_validator()  # empty DNS map; literal needs no DNS
        with pytest.raises(SSRFError):
            v.validate("http://127.0.0.1:8080/x")

    def test_ip_literal_metadata_blocked(self):
        v = make_validator()
        with pytest.raises(SSRFError):
            v.validate("http://169.254.169.254/latest/meta-data/")

    def test_dns_failure_is_validation_error(self):
        v = make_validator({})  # nothing resolves
        with pytest.raises(UrlValidationError):
            v.validate("https://nonexistent.invalid/")

    def test_empty_dns_result_is_error(self):
        v = make_validator({"empty.example": []})
        with pytest.raises(UrlValidationError):
            v.validate("https://empty.example/")

    def test_allow_private_permits_localhost(self):
        v = make_validator({"localhost": ["127.0.0.1"]}, allow_private=True)
        result = v.validate("http://localhost:8000/health")
        assert result.parsed.host == "localhost"


# --------------------------------------------------------------------------- #
# Redirect validation                                                         #
# --------------------------------------------------------------------------- #
class TestRedirectValidation:
    def test_relative_redirect_resolved_against_base(self):
        v = make_validator({"arolinks.com": ["8.8.8.8"]})
        result = v.validate_redirect("/step2?token=abc", base_url="https://arolinks.com/start")
        assert result.parsed.url == "https://arolinks.com/step2?token=abc"

    def test_absolute_redirect_to_private_blocked(self):
        v = make_validator({"arolinks.com": ["8.8.8.8"]})
        with pytest.raises(SSRFError):
            v.validate_redirect("http://192.168.0.1/admin", base_url="https://arolinks.com/x")

    def test_redirect_to_metadata_blocked(self):
        v = make_validator({"arolinks.com": ["8.8.8.8"]})
        with pytest.raises(SSRFError):
            v.validate_redirect(
                "http://169.254.169.254/latest/", base_url="https://arolinks.com/x"
            )

    def test_empty_redirect_location(self):
        v = make_validator()
        with pytest.raises(UrlValidationError):
            v.validate_redirect("   ", base_url="https://arolinks.com/x")


# --------------------------------------------------------------------------- #
# Supported-domain (authorisation) checks                                     #
# --------------------------------------------------------------------------- #
class TestSupportedDomain:
    SUPPORTED: ClassVar[list[str]] = ["arolinks.com", "example-shortener.net"]

    @pytest.mark.parametrize(
        "host,expected",
        [
            ("arolinks.com", True),
            ("Arolinks.com", True),
            ("www.arolinks.com", True),
            ("a.b.arolinks.com", True),
            ("arolinks.com.", True),  # trailing dot tolerated
            ("example-shortener.net", True),
            ("notarolinks.com", False),  # not a subdomain
            ("arolinks.com.evil.test", False),  # suffix-append attack
            ("evil.test", False),
        ],
    )
    def test_is_supported_domain(self, host, expected):
        v = make_validator()
        assert v.is_supported_domain(host, self.SUPPORTED) is expected

    def test_ensure_supported_raises_for_unknown(self):
        v = make_validator()
        with pytest.raises(UnsupportedDomainError):
            v.ensure_supported_domain("evil.test", self.SUPPORTED)

    def test_ensure_supported_passes_for_known(self):
        v = make_validator()
        v.ensure_supported_domain("www.arolinks.com", self.SUPPORTED)  # no raise
