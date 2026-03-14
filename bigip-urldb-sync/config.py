"""
config.py — Application settings via pydantic-settings.

All settings can be supplied via environment variables or a ``.env`` file.
Environment variable names match the field names (case-insensitive by default
in pydantic-settings).

Example ``.env``::

    BIGIP_HOST=192.0.2.1
    BIGIP_USER=admin
    BIGIP_PASSWORD=supersecret
    BIGIP_PARTITION=Common
    BIGIP_VERIFY_SSL=false
    URLDB_CATEGORY=custom_block_list
    URLDB_URL_TYPE=exact
    URLDB_CHUNK_SIZE=9000
    JSON_SOURCE=/etc/urldb-sync/urls.json
    STATUS_FILE_PATH=/var/log/urldb-sync/status.json
    LOG_LEVEL=INFO
"""

import logging
from pathlib import Path
from typing import Literal, Optional

from pydantic import Field, field_validator
# Allowed Python log level names (used as a Literal type for log_level)
_LOG_LEVELS = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Central configuration for bigip-urldb-sync.

    Reads values from environment variables and/or a ``.env`` file in the
    current working directory.  CLI flags and FastAPI request bodies take
    precedence over these defaults when supplied explicitly.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # BIG-IP connection
    # ------------------------------------------------------------------
    bigip_host: Optional[str] = Field(
        default=None,
        description="BIG-IP management IP address or hostname.",
    )
    bigip_user: str = Field(
        default="admin",
        description="BIG-IP username.",
    )
    bigip_password: Optional[str] = Field(
        default=None,
        description="BIG-IP password.  Set via BIGIP_PASSWORD env var.",
    )
    bigip_partition: str = Field(
        default="Common",
        description="BIG-IP partition (default: Common).",
    )
    bigip_verify_ssl: bool = Field(
        default=False,
        description=(
            "Whether to verify the BIG-IP management TLS certificate.  "
            "Defaults to False for self-signed certs; enable in production."
        ),
    )

    # ------------------------------------------------------------------
    # URLDB category settings
    # ------------------------------------------------------------------
    urldb_category: str = Field(
        default="custom_block_list",
        description="Name of the URLDB category to create/update.",
    )
    urldb_url_type: Literal["exact", "glob", "any"] = Field(
        default="exact",
        description="Default URL match type: exact, glob, or any.",
    )
    urldb_chunk_size: int = Field(
        default=9000,
        ge=1,
        description="Maximum number of URLs per URLDB category object.",
    )

    # ------------------------------------------------------------------
    # Source and status
    # ------------------------------------------------------------------
    json_source: Optional[str] = Field(
        default=None,
        description="Local file path or HTTP/HTTPS URL to the JSON URL list.",
    )
    status_file_path: Path = Field(
        default=Path("/var/log/urldb-sync/status.json"),
        description="Path where the last-run status JSON is written.",
    )

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    log_level: _LOG_LEVELS = Field(
        default="INFO",
        description="Python logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).",
    )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("log_level", mode="before")
    @classmethod
    def uppercase_log_level(cls, value: str) -> str:
        """Uppercase the log level so env vars like ``info`` are accepted."""
        return value.upper()

    def configure_logging(self) -> None:
        """
        Apply the configured log level to the root logger.

        Call this once at application startup (CLI or API).
        """
        level = getattr(logging, self.log_level, logging.INFO)
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("requests").setLevel(logging.WARNING)


def get_settings() -> Settings:
    """
    Instantiate and return the application :class:`Settings` object.

    The returned object reads from environment variables and ``.env`` on
    first call.  Use this factory in FastAPI dependency injection or CLI
    setup to ensure consistent configuration loading.

    Returns:
        A populated :class:`Settings` instance.
    """
    return Settings()
