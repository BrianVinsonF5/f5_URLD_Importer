"""
status.py — Read and write last-run sync status to a JSON file.

The status file records the outcome of each sync attempt and is consumed by
both the CLI ``status`` command and the FastAPI ``GET /status`` endpoint.

Status file schema::

    {
        "last_run": "2024-01-15T10:30:00Z",
        "status": "success",
        "urls_pushed": 1234,
        "categories_updated": ["custom_block_list"],
        "error_message": null,
        "source": "/etc/urldb-sync/urls.json",
        "bigip_host": "192.0.2.1"
    }
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Default status file location
DEFAULT_STATUS_PATH = Path("/var/log/urldb-sync/status.json")


class SyncStatus:
    """
    Represents the outcome of a single sync run.

    Args:
        status:              ``"success"`` or ``"error"``.
        urls_pushed:         Number of URLs pushed to BIG-IP.
        categories_updated:  List of category names that were upserted.
        source:              The JSON source path or URL used.
        bigip_host:          The BIG-IP host targeted.
        error_message:       Error description on failure; ``None`` on success.
        last_run:            Timestamp of the run (defaults to UTC now).
    """

    def __init__(
        self,
        status: str,
        urls_pushed: int,
        categories_updated: List[str],
        source: str,
        bigip_host: str,
        error_message: Optional[str] = None,
        last_run: Optional[datetime] = None,
    ) -> None:
        self.status = status
        self.urls_pushed = urls_pushed
        self.categories_updated = categories_updated
        self.source = source
        self.bigip_host = bigip_host
        self.error_message = error_message
        self.last_run = last_run or datetime.now(timezone.utc)

    def to_dict(self) -> dict:
        """Serialize this status record to a plain dict."""
        return {
            "last_run": self.last_run.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": self.status,
            "urls_pushed": self.urls_pushed,
            "categories_updated": self.categories_updated,
            "error_message": self.error_message,
            "source": self.source,
            "bigip_host": self.bigip_host,
        }

    @classmethod
    def success(
        cls,
        urls_pushed: int,
        categories_updated: List[str],
        source: str,
        bigip_host: str,
    ) -> "SyncStatus":
        """
        Create a successful :class:`SyncStatus` record.

        Args:
            urls_pushed:        Total URLs pushed.
            categories_updated: Names of categories upserted.
            source:             JSON source used.
            bigip_host:         Target BIG-IP host.

        Returns:
            A :class:`SyncStatus` with ``status="success"``.
        """
        return cls(
            status="success",
            urls_pushed=urls_pushed,
            categories_updated=categories_updated,
            source=source,
            bigip_host=bigip_host,
            error_message=None,
        )

    @classmethod
    def error(
        cls,
        error_message: str,
        source: str,
        bigip_host: str,
        urls_pushed: int = 0,
        categories_updated: Optional[List[str]] = None,
    ) -> "SyncStatus":
        """
        Create a failed :class:`SyncStatus` record.

        Args:
            error_message:      Human-readable description of the failure.
            source:             JSON source used (may be unknown on early failure).
            bigip_host:         Target BIG-IP host.
            urls_pushed:        Number of URLs pushed before the failure (default 0).
            categories_updated: Categories updated before the failure (default []).

        Returns:
            A :class:`SyncStatus` with ``status="error"``.
        """
        return cls(
            status="error",
            urls_pushed=urls_pushed,
            categories_updated=categories_updated or [],
            source=source,
            bigip_host=bigip_host,
            error_message=error_message,
        )


def write_status(sync_status: SyncStatus, path: Path = DEFAULT_STATUS_PATH) -> None:
    """
    Write a :class:`SyncStatus` record to a JSON file at *path*.

    The parent directory is created if it does not exist.

    Args:
        sync_status: The status record to persist.
        path:        Destination file path (default: ``/var/log/urldb-sync/status.json``).

    Raises:
        OSError: If the file cannot be written (e.g. permission denied).
    """
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(sync_status.to_dict(), fh, indent=2)
            fh.write("\n")
        logger.debug("Status written to %s", path)
    except OSError as exc:
        logger.error("Failed to write status file '%s': %s", path, exc)
        raise


def read_status(path: Path = DEFAULT_STATUS_PATH) -> Optional[dict]:
    """
    Read and parse the status JSON file at *path*.

    Args:
        path: Path to the status file.

    Returns:
        The parsed status dict, or ``None`` if the file does not exist.

    Raises:
        ValueError: If the file exists but contains invalid JSON.
        OSError:    If the file exists but cannot be read.
    """
    path = Path(path)
    if not path.exists():
        logger.debug("Status file '%s' does not exist yet.", path)
        return None

    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        logger.debug("Status read from %s", path)
        return data
    except json.JSONDecodeError as exc:
        raise ValueError(f"Status file '{path}' contains invalid JSON: {exc}") from exc
    except OSError as exc:
        logger.error("Cannot read status file '%s': %s", path, exc)
        raise
