"""Tests for :mod:`app.config` (requires pydantic-settings)."""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic_settings")

from app.config import Settings


def make(**kwargs) -> Settings:
    # _env_file=None isolates tests from any local .env file.
    return Settings(_env_file=None, **kwargs)


def test_csv_api_keys():
    assert make(api_keys="a, b ,c").api_keys == ["a", "b", "c"]


def test_json_list_api_keys():
    assert make(api_keys='["x","y"]').api_keys == ["x", "y"]


def test_supported_domains_lowercased_and_stripped():
    settings = make(supported_domains="Arolinks.COM, Foo.NET.")
    assert settings.supported_domains == {"arolinks.com", "foo.net"}


def test_empty_csv_is_empty_list():
    assert make(api_keys="").api_keys == []


def test_log_level_normalised():
    assert make(log_level="debug").log_level == "DEBUG"


def test_invalid_log_level_rejected():
    with pytest.raises(ValueError):
        make(log_level="verbose")


class TestAuthRequired:
    def test_disabled_when_require_false(self):
        assert make(require_api_key=False).auth_required() is False

    def test_enforced_in_production_without_keys(self):
        # Fail-closed: production + no keys still enforces (rejects everything).
        assert make(environment="production", require_api_key=True, api_keys=[]).auth_required()

    def test_disabled_in_development_without_keys(self):
        assert (
            make(environment="development", require_api_key=True, api_keys=[]).auth_required()
            is False
        )

    def test_enforced_with_keys(self):
        assert make(require_api_key=True, api_keys=["k"]).auth_required() is True


class TestValidateRuntime:
    def test_warns_for_prod_without_keys(self):
        cfg = make(environment="production", require_api_key=True, api_keys=[])
        assert any("rejected" in w for w in cfg.validate_runtime())

    def test_warns_for_allow_private(self):
        warnings = make(allow_private_networks=True).validate_runtime()
        assert any("SSRF" in w for w in warnings)

    def test_no_warnings_for_healthy_config(self):
        cfg = make(require_api_key=True, api_keys=["k"], allow_private_networks=False)
        assert cfg.validate_runtime() == []
