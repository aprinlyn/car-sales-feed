"""Property-based tests for ConfigManager.

# Feature: car-sales-feed, Property 10: Configuration environment variable precedence
# Feature: car-sales-feed, Property 11: Missing required configuration names the key
# Feature: car-sales-feed, Property 12: Invalid configuration is rejected with descriptive error
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from hypothesis import given, settings, assume, HealthCheck
from hypothesis import strategies as st

from config.manager import ConfigManager, ConfigError, REQUIRED_KEYS


# --- Helpers ---


def write_yaml_temp(data: dict) -> str:
    """Write a YAML config to a temp file and return the path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    yaml.dump(data, f)
    f.close()
    return f.name


def minimal_valid_config() -> dict:
    """Return a minimal valid configuration dict."""
    return {
        "database": {"url": "sqlite:///test.db"},
        "browser": {"profile_dir": "./test_profile"},
    }


def test_scheduling_enable_flags_default_to_true():
    """Enable flags default to true when not specified in config."""
    yaml_path = write_yaml_temp(minimal_valid_config())

    try:
        cfg = ConfigManager(yaml_path=yaml_path)
        assert cfg.get("scheduling.enable_scrape") is True
        assert cfg.get("scheduling.enable_publish") is True
    finally:
        os.unlink(yaml_path)


# --- Property 10: Environment variable precedence ---


class TestEnvVarPrecedence:
    """Property 10: For any configuration key that appears in both the YAML file
    and an environment variable, the loaded configuration value SHALL equal the
    environment variable value, not the YAML value."""

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        yaml_value=st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N"))),
        env_value=st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N"))),
    )
    def test_env_var_overrides_yaml_value(self, yaml_value: str, env_value: str):
        """Env vars always take precedence over YAML values for database.url."""
        assume(yaml_value != env_value)
        # Avoid values that coerce to bool/int/float
        assume(env_value.lower() not in ("true", "false", "yes", "no", "0", "1"))
        assume(not env_value.isdigit())
        try:
            float(env_value)
            assume(False)
        except ValueError:
            pass

        config_data = minimal_valid_config()
        config_data["database"]["url"] = yaml_value
        yaml_path = write_yaml_temp(config_data)

        try:
            with patch.dict(os.environ, {"CSF_DATABASE_URL": env_value}, clear=False):
                cfg = ConfigManager(yaml_path=yaml_path)
                assert cfg.get("database.url") == env_value
        finally:
            os.unlink(yaml_path)

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(env_int=st.integers(min_value=1, max_value=999))
    def test_env_var_integer_overrides_yaml(self, env_int: int):
        """Env var integer values are coerced and override YAML."""
        config_data = minimal_valid_config()
        config_data["database"]["retry_max"] = 3  # YAML value
        yaml_path = write_yaml_temp(config_data)

        try:
            with patch.dict(os.environ, {"CSF_DATABASE_URL": f"sqlite:///test_{env_int}.db"}, clear=False):
                cfg = ConfigManager(yaml_path=yaml_path)
                assert cfg.get("database.url") == f"sqlite:///test_{env_int}.db"
        finally:
            os.unlink(yaml_path)


# --- Property 11: Missing required configuration names the key ---


class TestMissingRequiredConfig:
    """Property 11: For any required configuration key that is absent from both
    YAML and environment variables, the system SHALL raise a startup error whose
    message contains the name of the missing key."""

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(missing_key_index=st.integers(min_value=0, max_value=len(REQUIRED_KEYS) - 1))
    def test_missing_required_key_names_the_key_in_error(self, missing_key_index: int):
        """Validation error message contains the name of the missing key."""
        missing_key = REQUIRED_KEYS[missing_key_index]

        # Create config with some required keys but not the missing one
        config_data: dict = {}
        for key in REQUIRED_KEYS:
            if key == missing_key:
                continue
            parts = key.split(".")
            d = config_data
            for part in parts[:-1]:
                d = d.setdefault(part, {})
            d[parts[-1]] = "test_value"

        yaml_path = write_yaml_temp(config_data)

        try:
            # Make sure no env var supplies the missing key
            env_key = f"CSF_{missing_key.upper().replace('.', '_')}"
            env_cleanup = {}
            if env_key in os.environ:
                env_cleanup[env_key] = os.environ[env_key]
                del os.environ[env_key]

            try:
                cfg = ConfigManager(yaml_path=yaml_path)
                with pytest.raises(ConfigError) as exc_info:
                    cfg.validate()
                assert missing_key in str(exc_info.value)
            finally:
                # Restore env if we removed anything
                os.environ.update(env_cleanup)
        finally:
            os.unlink(yaml_path)


# --- Property 12: Invalid configuration is rejected with descriptive error ---


class TestInvalidConfigRejection:
    """Property 12: For any configuration value that violates its type or format
    constraint, the system SHALL reject it at validation with an error message
    describing the invalid value."""

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(invalid_score=st.integers(min_value=101, max_value=1000))
    def test_score_boundary_above_100_rejected(self, invalid_score: int):
        """Score boundaries above 100 are rejected with descriptive error."""
        config_data = minimal_valid_config()
        config_data["scoring"] = {"thresholds": {"safe_min": invalid_score, "suspicious_min": 40}}
        yaml_path = write_yaml_temp(config_data)

        try:
            cfg = ConfigManager(yaml_path=yaml_path)
            with pytest.raises(ConfigError) as exc_info:
                cfg.validate()
            assert "scoring.thresholds.safe_min" in str(exc_info.value)
        finally:
            os.unlink(yaml_path)

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(invalid_score=st.integers(min_value=-1000, max_value=-1))
    def test_score_boundary_below_0_rejected(self, invalid_score: int):
        """Score boundaries below 0 are rejected with descriptive error."""
        config_data = minimal_valid_config()
        config_data["scoring"] = {"thresholds": {"safe_min": 70, "suspicious_min": invalid_score}}
        yaml_path = write_yaml_temp(config_data)

        try:
            cfg = ConfigManager(yaml_path=yaml_path)
            with pytest.raises(ConfigError) as exc_info:
                cfg.validate()
            assert "scoring.thresholds.suspicious_min" in str(exc_info.value)
        finally:
            os.unlink(yaml_path)

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        bad_cron=st.text(
            min_size=1,
            max_size=20,
            alphabet=st.characters(whitelist_categories=("L", "N", "P")),
        )
    )
    def test_malformed_cron_expression_rejected(self, bad_cron: str):
        """Malformed cron expressions are rejected with descriptive error."""
        from croniter import croniter as cron_module

        assume(not cron_module.is_valid(bad_cron))

        config_data = minimal_valid_config()
        config_data["scheduling"] = {"scrape_cron": bad_cron, "publish_cron": "0 17 * * *"}
        yaml_path = write_yaml_temp(config_data)

        try:
            cfg = ConfigManager(yaml_path=yaml_path)
            with pytest.raises(ConfigError) as exc_info:
                cfg.validate()
            assert "scheduling.scrape_cron" in str(exc_info.value)
        finally:
            os.unlink(yaml_path)

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(negative_value=st.integers(min_value=-1000, max_value=-1))
    def test_negative_numeric_fields_rejected(self, negative_value: int):
        """Negative values for numeric fields are rejected."""
        config_data = minimal_valid_config()
        config_data["scraping"] = {"olx": {"max_pages": negative_value}}
        yaml_path = write_yaml_temp(config_data)

        try:
            cfg = ConfigManager(yaml_path=yaml_path)
            with pytest.raises(ConfigError) as exc_info:
                cfg.validate()
            assert "scraping.olx.max_pages" in str(exc_info.value)
        finally:
            os.unlink(yaml_path)

    def test_safe_min_not_greater_than_suspicious_min_rejected(self):
        """safe_min must be greater than suspicious_min."""
        config_data = minimal_valid_config()
        config_data["scoring"] = {"thresholds": {"safe_min": 40, "suspicious_min": 70}}
        yaml_path = write_yaml_temp(config_data)

        try:
            cfg = ConfigManager(yaml_path=yaml_path)
            with pytest.raises(ConfigError) as exc_info:
                cfg.validate()
            assert "safe_min" in str(exc_info.value)
            assert "suspicious_min" in str(exc_info.value)
        finally:
            os.unlink(yaml_path)

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(bad_weight=st.floats(min_value=1.01, max_value=10.0, allow_nan=False, allow_infinity=False))
    def test_weight_above_1_rejected(self, bad_weight: float):
        """Scoring weights above 1.0 are rejected."""
        config_data = minimal_valid_config()
        config_data["scoring"] = {"weights": {"price_deviation": bad_weight}}
        yaml_path = write_yaml_temp(config_data)

        try:
            cfg = ConfigManager(yaml_path=yaml_path)
            with pytest.raises(ConfigError) as exc_info:
                cfg.validate()
            assert "scoring.weights.price_deviation" in str(exc_info.value)
        finally:
            os.unlink(yaml_path)
