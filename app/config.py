"""Environment-based application configuration.

All runtime tunables are defined here as a single :class:`Settings` object,
populated from environment variables and/or a ``.env`` file. Use
:func:`get_settings` (cached) to obtain the active configuration.

List/set-valued settings accept either a JSON array or a simple
comma-separated string in the environment, e.g.::

    SUPPORTED_DOMAINS="arolinks.com,example-shortener.net"
    API_KEYS="key-one,key-two"
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from . import __version__


def _split_csv(value: Any) -> Any:
    """Permit comma-separated strings for list/set-valued settings."""

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        # Leave JSON arrays to pydantic's native parser.
        if stripped.startswith("["):
            return value
        return [item.strip() for item in stripped.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    """Strongly-typed application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ----- Application -------------------------------------------------- #
    app_name: str = "AdLinkFly Resolver API"
    app_version: str = __version__
    environment: Literal["development", "staging", "production"] = "production"

    # ----- Logging ------------------------------------------------------ #
    log_level: str = "INFO"
    log_json: bool = True

    # ----- Authentication ---------------------------------------------- #
    #: Accepted API keys. When empty and ``require_api_key`` is True the app
    #: refuses to start (fail closed) unless ``environment`` is development.
    api_keys: list[str] = Field(default_factory=list)
    require_api_key: bool = True
    api_key_header: str = "X-API-Key"

    # ----- Rate limiting ----------------------------------------------- #
    rate_limit_enabled: bool = True
    rate_limit_requests: int = 60
    rate_limit_window_seconds: int = 60

    # ----- HTTP client (cloudscraper) ---------------------------------- #
    http_connect_timeout: float = 10.0
    http_read_timeout: float = 20.0
    max_redirects: int = 10
    max_response_bytes: int = 3_000_000
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    # ----- Browser (Playwright) ---------------------------------------- #
    enable_browser: bool = True
    browser_headless: bool = True
    browser_navigation_timeout_ms: int = 20_000
    browser_max_steps: int = 8
    browser_countdown_max_seconds: float = 30.0
    browser_slow_mo_ms: int = 0

    # ----- Resolver ----------------------------------------------------- #
    resolve_total_timeout: float = 60.0
    max_concurrent_resolves: int = 10
    max_concurrent_browsers: int = 2
    supported_domains: set[str] = Field(default_factory=lambda: {"arolinks.com"})

    # ----- Security / network ------------------------------------------ #
    #: When True (development/testing only) SSRF address checks are disabled so
    #: that a local test server (127.0.0.1) can be resolved. NEVER enable in a
    #: deployment reachable by untrusted callers.
    allow_private_networks: bool = False
    allowed_schemes: set[str] = Field(default_factory=lambda: {"http", "https"})
    max_url_length: int = 2048

    # ----- CORS --------------------------------------------------------- #
    cors_allow_origins: list[str] = Field(default_factory=list)

    # ------------------------------------------------------------------ #
    # Validators                                                          #
    # ------------------------------------------------------------------ #
    @field_validator(
        "api_keys",
        "supported_domains",
        "allowed_schemes",
        "cors_allow_origins",
        mode="before",
    )
    @classmethod
    def _parse_csv(cls, value: Any) -> Any:
        return _split_csv(value)

    @field_validator("supported_domains", "allowed_schemes", mode="after")
    @classmethod
    def _lowercase_set(cls, value: set[str]) -> set[str]:
        return {item.lower().strip().strip(".") for item in value if item.strip()}

    @field_validator("log_level", mode="after")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        normalized = value.upper()
        valid = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        if normalized not in valid:
            raise ValueError(f"log_level must be one of {sorted(valid)}")
        return normalized

    # ------------------------------------------------------------------ #
    # Convenience                                                         #
    # ------------------------------------------------------------------ #
    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    def auth_required(self) -> bool:
        """Whether API-key auth should be enforced for incoming requests.

        Auth is enforced whenever ``require_api_key`` is set, with a single
        convenience exception: development mode with no keys configured. In
        production, enforcement stays on even when no keys are configured, so
        the service fails *closed* (every request is rejected) rather than
        silently accepting unauthenticated traffic.
        """

        if not self.require_api_key:
            return False
        return not (self.is_development and not self.api_keys)

    def validate_runtime(self) -> list[str]:
        """Return a list of configuration warnings for logging at startup."""

        warnings: list[str] = []
        if self.require_api_key and not self.api_keys:
            if self.is_development:
                warnings.append(
                    "require_api_key is set but no API_KEYS configured; "
                    "authentication is DISABLED (development mode)."
                )
            else:
                warnings.append(
                    "require_api_key is set but no API_KEYS configured; "
                    "all requests will be rejected until keys are provided."
                )
        if self.allow_private_networks:
            warnings.append(
                "allow_private_networks is ENABLED; SSRF protection is relaxed. "
                "Do not use this in production."
            )
        return warnings


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings instance."""

    return Settings()
