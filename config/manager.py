"""Configuration manager with YAML loading, env var override, and validation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from croniter import croniter


class ConfigError(Exception):
    """Raised when configuration is invalid or missing required keys."""

    pass


# Required keys that must be present (from YAML or env vars)
REQUIRED_KEYS = [
    "database.url",
    "browser.profile_dir",
]

# Default values for optional configuration parameters
DEFAULTS: dict[str, Any] = {
    "scraping.olx.max_pages": 10,
    "scraping.olx.page_delay_seconds": 3,
    "scraping.olx.timeout_seconds": 30,
    "scraping.olx.captcha_pause_seconds": 60,
    "scraping.facebook.max_listings": 100,
    "scraping.facebook.timeout_seconds": 30,
    "scraping.facebook.captcha_pause_seconds": 60,
    "scoring.thresholds.safe_min": 70,
    "scoring.thresholds.suspicious_min": 40,
    "scoring.weights.price_deviation": 0.20,
    "scoring.weights.image_count": 0.10,
    "scoring.weights.description_length": 0.10,
    "scoring.weights.seller_age": 0.15,
    "scoring.weights.seller_listing_history": 0.10,
    "scoring.weights.contact_info": 0.10,
    "scoring.weights.mileage_consistency": 0.10,
    "scoring.weights.location_specificity": 0.15,
    "publishing.twitter.max_posts_per_run": 30,
    "publishing.twitter.post_interval_seconds": 120,
    "publishing.threads.max_posts_per_run": 25,
    "publishing.threads.post_interval_seconds": 120,
    "scheduling.scrape_cron": "0 7 * * *",
    "scheduling.publish_cron": "0 17 * * *",
    "scheduling.enable_scrape": True,
    "scheduling.enable_publish": True,
    "database.retry_max": 3,
    "database.dead_letter_path": "dead_letter.jsonl",
}


class ConfigManager:
    """Loads configuration from YAML with environment variable overrides.

    Environment variables override YAML values using the naming convention:
    CSF_<SECTION>_<KEY> (uppercase, dots replaced with underscores).
    Example: CSF_DATABASE_URL overrides database.url
    """

    ENV_PREFIX = "CSF_"

    def __init__(self, yaml_path: str = "config.yaml"):
        """Load config from YAML, override with env vars."""
        self._yaml_path = yaml_path
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        """Load YAML file and apply environment variable overrides."""
        yaml_data: dict[str, Any] = {}

        yaml_file = Path(self._yaml_path)
        if yaml_file.exists():
            try:
                with open(yaml_file) as f:
                    parsed = yaml.safe_load(f)
                    if parsed and isinstance(parsed, dict):
                        yaml_data = parsed
            except yaml.YAMLError as e:
                raise ConfigError(f"Failed to parse YAML configuration: {e}")

        # Flatten YAML into dot-notation keys
        self._data = self._flatten(yaml_data)

        # Apply defaults for any missing optional keys
        for key, default_value in DEFAULTS.items():
            if key not in self._data:
                self._data[key] = default_value

        # Apply environment variable overrides
        self._apply_env_overrides()

    def _flatten(self, data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
        """Flatten a nested dict into dot-notation keys."""
        result: dict[str, Any] = {}
        for key, value in data.items():
            full_key = f"{prefix}{key}" if prefix else key
            if isinstance(value, dict):
                result.update(self._flatten(value, f"{full_key}."))
            else:
                result[full_key] = value
        return result

    def _apply_env_overrides(self) -> None:
        """Override config values with environment variables.

        Convention: CSF_DATABASE_URL -> database.url
        """
        for env_key, env_value in os.environ.items():
            if env_key.startswith(self.ENV_PREFIX):
                # Convert CSF_DATABASE_URL -> database.url
                config_key = env_key[len(self.ENV_PREFIX) :].lower().replace("_", ".")
                self._data[config_key] = self._coerce_value(env_value)

    def _coerce_value(self, value: str) -> Any:
        """Attempt to coerce string env var values to appropriate types."""
        # Boolean
        if value.lower() in ("true", "yes", "1"):
            return True
        if value.lower() in ("false", "no", "0"):
            return False

        # Integer
        try:
            return int(value)
        except ValueError:
            pass

        # Float
        try:
            return float(value)
        except ValueError:
            pass

        return value

    def get(self, key: str, default: Any = None) -> Any:
        """Get a config value by dot-notation key."""
        return self._data.get(key, default)

    def validate(self) -> None:
        """Validate all required keys and value formats.

        Raises ConfigError on failure with a descriptive message naming
        the invalid or missing key.
        """
        # Check required keys
        for key in REQUIRED_KEYS:
            if key not in self._data or self._data[key] is None:
                raise ConfigError(
                    f"Missing required configuration key: '{key}'. "
                    f"Provide it in the YAML file or as environment variable "
                    f"{self.ENV_PREFIX}{key.upper().replace('.', '_')}"
                )

        # Validate score boundaries
        self._validate_score_boundaries()

        # Validate cron expressions
        self._validate_cron_expressions()

        # Validate numeric fields
        self._validate_numeric_fields()

        # Validate boolean fields
        self._validate_boolean_fields()

        # Validate browser profile directory path
        self._validate_browser_profile()

    def _validate_score_boundaries(self) -> None:
        """Validate that score thresholds are integers 0-100 with safe > suspicious."""
        safe_min = self._data.get("scoring.thresholds.safe_min")
        suspicious_min = self._data.get("scoring.thresholds.suspicious_min")

        for name, value in [
            ("scoring.thresholds.safe_min", safe_min),
            ("scoring.thresholds.suspicious_min", suspicious_min),
        ]:
            if value is None:
                continue
            if not isinstance(value, int):
                raise ConfigError(
                    f"Invalid configuration for '{name}': "
                    f"expected integer, got {type(value).__name__} ({value!r})"
                )
            if not 0 <= value <= 100:
                raise ConfigError(
                    f"Invalid configuration for '{name}': "
                    f"value {value} is not in range 0–100"
                )

        if safe_min is not None and suspicious_min is not None:
            if safe_min <= suspicious_min:
                raise ConfigError(
                    f"Invalid configuration: 'scoring.thresholds.safe_min' ({safe_min}) "
                    f"must be greater than 'scoring.thresholds.suspicious_min' ({suspicious_min})"
                )

    def _validate_cron_expressions(self) -> None:
        """Validate cron expression format."""
        for key in ["scheduling.scrape_cron", "scheduling.publish_cron"]:
            value = self._data.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise ConfigError(
                    f"Invalid configuration for '{key}': "
                    f"expected string cron expression, got {type(value).__name__}"
                )
            if not croniter.is_valid(value):
                raise ConfigError(
                    f"Invalid configuration for '{key}': "
                    f"malformed cron expression '{value}'"
                )

    def _validate_numeric_fields(self) -> None:
        """Validate numeric configuration fields."""
        int_fields = [
            "scraping.olx.max_pages",
            "scraping.olx.page_delay_seconds",
            "scraping.olx.timeout_seconds",
            "scraping.olx.captcha_pause_seconds",
            "scraping.facebook.max_listings",
            "scraping.facebook.timeout_seconds",
            "scraping.facebook.captcha_pause_seconds",
            "publishing.twitter.max_posts_per_run",
            "publishing.twitter.post_interval_seconds",
            "publishing.threads.max_posts_per_run",
            "publishing.threads.post_interval_seconds",
            "database.retry_max",
        ]

        for key in int_fields:
            value = self._data.get(key)
            if value is None:
                continue
            if not isinstance(value, (int, float)):
                raise ConfigError(
                    f"Invalid configuration for '{key}': "
                    f"expected numeric value, got {type(value).__name__} ({value!r})"
                )
            if value < 0:
                raise ConfigError(
                    f"Invalid configuration for '{key}': "
                    f"value {value} must be non-negative"
                )

        # Validate weight fields are floats between 0 and 1
        weight_keys = [
            "scoring.weights.price_deviation",
            "scoring.weights.image_count",
            "scoring.weights.description_length",
            "scoring.weights.seller_age",
            "scoring.weights.seller_listing_history",
            "scoring.weights.contact_info",
            "scoring.weights.mileage_consistency",
            "scoring.weights.location_specificity",
        ]

        for key in weight_keys:
            value = self._data.get(key)
            if value is None:
                continue
            if not isinstance(value, (int, float)):
                raise ConfigError(
                    f"Invalid configuration for '{key}': "
                    f"expected numeric value, got {type(value).__name__} ({value!r})"
                )
            if not 0.0 <= float(value) <= 1.0:
                raise ConfigError(
                    f"Invalid configuration for '{key}': "
                    f"weight {value} must be between 0.0 and 1.0"
                )
    def _validate_boolean_fields(self) -> None:
        """Validate boolean configuration fields."""
        bool_fields = [
            "browser.headless",
            "scheduling.enable_scrape",
            "scheduling.enable_publish",
        ]

        for key in bool_fields:
            value = self._data.get(key)
            if value is None:
                continue
            if not isinstance(value, bool):
                raise ConfigError(
                    f"Invalid configuration for '{key}': "
                    f"expected boolean value, got {type(value).__name__} ({value!r})"
                )
    def _validate_browser_profile(self) -> None:
        """Validate the browser profile directory path is a valid path string."""
        profile_dir = self._data.get("browser.profile_dir")
        if profile_dir is not None:
            if not isinstance(profile_dir, str) or not profile_dir.strip():
                raise ConfigError(
                    f"Invalid configuration for 'browser.profile_dir': "
                    f"must be a non-empty string path"
                )

    def reload(self) -> None:
        """Reload configuration from sources (for next-run application)."""
        self._data = {}
        self._load()
