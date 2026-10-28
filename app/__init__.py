"""AdLinkFly link-resolution API.

A production-oriented FastAPI service that resolves supported AdLinkFly-based
shortened URLs (for example ``arolinks.com``) to their final destination.

The package is organised into focused, independently testable modules:

* :mod:`app.config`        -- environment-based configuration.
* :mod:`app.schemas`       -- Pydantic request/response models.
* :mod:`app.status`        -- resolution status enum (pure stdlib).
* :mod:`app.exceptions`    -- typed domain exceptions (pure stdlib).
* :mod:`app.url_validator` -- URL + network (SSRF) validation (pure stdlib).
* :mod:`app.page_analyzer` -- HTML / page-state analysis.
* :mod:`app.http_client`   -- cloudscraper HTTP integration.
* :mod:`app.browser_agent` -- Playwright (Chromium) automation.
* :mod:`app.resolver`      -- orchestration of the resolution workflow.
* :mod:`app.security`      -- API-key authentication and rate limiting.
* :mod:`app.main`          -- FastAPI application and routes.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
