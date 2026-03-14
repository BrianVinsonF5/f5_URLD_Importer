"""
api.py — FastAPI web service for bigip-urldb-sync.

Endpoints:
  POST /push-urldb          Trigger an immediate sync.
  GET  /status              Return the last sync status.
  POST /schedule/update     Update the polling interval (writes sync_interval.conf).
  GET  /health              Liveness check.

Start with::

    uvicorn api:app --host 0.0.0.0 --port 8080

Example requests::

    # Trigger a sync
    curl -X POST http://localhost:8080/push-urldb \\
         -H 'Content-Type: application/json' \\
         -d '{"source":"https://feeds.example.com/urls.json","host":"192.0.2.1","password":"secret"}'

    # Get last sync status
    curl http://localhost:8080/status

    # Update sync interval
    curl -X POST http://localhost:8080/schedule/update \\
         -H 'Content-Type: application/json' \\
         -d '{"interval_minutes": 30}'

    # Health check
    curl http://localhost:8080/health
"""

import logging
from pathlib import Path
from typing import List, Literal, Optional

import requests
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from config import get_settings
from core.bigip import APIError, AuthenticationError, BIGIPClient
from core.loader import load
from core.transformer import transform
from status import SyncStatus, read_status, write_status_safe

logger = logging.getLogger(__name__)

# Path to the interval config file read by the systemd timer helper script
INTERVAL_CONF_PATH = Path("/etc/urldb-sync/sync_interval.conf")

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class PushRequest(BaseModel):
    """Request body for POST /push-urldb."""

    source: str = Field(
        ...,
        description="Local file path or HTTP/HTTPS URL for the JSON URL list.",
        examples=["https://feeds.example.com/urls.json", "/etc/urldb-sync/urls.json"],
    )
    host: str = Field(
        ...,
        description="BIG-IP management IP or hostname.",
        examples=["192.0.2.1"],
    )
    user: str = Field(
        default="admin",
        description="BIG-IP username.",
    )
    password: str = Field(
        ...,
        description="BIG-IP password.",
    )
    category: str = Field(
        default="custom_block_list",
        description="URLDB category name to create or update.",
    )
    url_type: Literal["exact", "glob", "any"] = Field(
        default="exact",
        description="Default URL match type for entries without an explicit type.",
    )
    partition: str = Field(
        default="Common",
        description="BIG-IP partition.",
    )
    chunk_size: int = Field(
        default=9000,
        ge=1,
        description="Maximum URLs per URLDB category object.",
    )
    verify_ssl: bool = Field(
        default=False,
        description="Enable TLS certificate verification for BIG-IP.",
    )
    dry_run: bool = Field(
        default=False,
        description="Parse and transform only — do NOT push to BIG-IP.",
    )

    @field_validator("chunk_size", mode="before")
    @classmethod
    def validate_chunk_size(cls, v: int) -> int:
        """Ensure chunk_size is a positive integer."""
        if int(v) < 1:
            raise ValueError(f"chunk_size must be >= 1, got {v}.")
        return int(v)


class PushResponse(BaseModel):
    """Response body for POST /push-urldb."""

    status: str = Field(description="'success' or 'error'.")
    urls_pushed: int = Field(description="Total URLs pushed to BIG-IP.")
    categories_updated: List[str] = Field(description="Category names upserted.")
    dry_run: bool = Field(description="Whether this was a dry-run (no actual push).")
    message: str = Field(description="Human-readable summary.")
    error_message: Optional[str] = Field(
        default=None,
        description="Error detail on failure; null on success.",
    )


class ScheduleUpdateRequest(BaseModel):
    """Request body for POST /schedule/update."""

    interval_minutes: int = Field(
        ...,
        ge=1,
        le=10080,  # max 1 week
        description="Desired sync interval in minutes (1 – 10080).",
        examples=[60],
    )


class ScheduleUpdateResponse(BaseModel):
    """Response body for POST /schedule/update."""

    interval_minutes: int = Field(description="The newly configured interval in minutes.")
    conf_path: str = Field(description="Path to the written interval config file.")
    message: str = Field(description="Human-readable confirmation.")


class StatusResponse(BaseModel):
    """Response body for GET /status."""

    last_run: Optional[str] = None
    status: Optional[str] = None
    urls_pushed: Optional[int] = None
    categories_updated: Optional[List[str]] = None
    error_message: Optional[str] = None
    source: Optional[str] = None
    bigip_host: Optional[str] = None


class HealthResponse(BaseModel):
    """Response body for GET /health."""

    status: str = "ok"
    service: str = "bigip-urldb-sync"


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

_settings = get_settings()
_settings.configure_logging()

app = FastAPI(
    title="bigip-urldb-sync",
    description=(
        "Sync custom URL lists to F5 BIG-IP URLDB categories via the iControl REST API."
    ),
    version="1.0.0",
    contact={
        "name": "bigip-urldb-sync",
        "url": "https://github.com/your-org/bigip-urldb-sync",
    },
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse, tags=["operations"])
def health() -> HealthResponse:
    """Liveness check — returns HTTP 200 when the service is running."""
    return HealthResponse()


@app.get("/status", response_model=StatusResponse, tags=["operations"])
def get_status() -> StatusResponse:
    """
    Return the last sync status from the status JSON file.

    Returns HTTP 404 if no sync has run yet.
    """
    status_path = _settings.status_file_path
    try:
        data = read_status(status_path)
    except (ValueError, OSError) as exc:
        logger.error("Error reading status file: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to read status file: {exc}",
        ) from exc

    if data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No status file found — no sync has run yet.",
        )

    return StatusResponse(**data)


@app.post(
    "/push-urldb",
    response_model=PushResponse,
    status_code=status.HTTP_200_OK,
    tags=["sync"],
)
def push_urldb(request: PushRequest) -> PushResponse:
    """
    Trigger an immediate URLDB sync.

    Loads the URL list from *source*, transforms it, and pushes to BIG-IP
    via iControl REST.  Pass ``dry_run=true`` to validate without pushing.
    """
    status_path = _settings.status_file_path

    # Step 1: Load
    logger.info("API: loading URL list from %s", request.source)
    try:
        entries = load(
            request.source,
            default_type=request.url_type,
            verify_ssl=request.verify_ssl,
        )
    except (FileNotFoundError, ValueError, requests.RequestException) as exc:
        _record_error(str(exc), request, status_path)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    # Step 2: Transform
    pairs = transform(
        category_name=request.category,
        entries=entries,
        partition=request.partition,
        chunk_size=request.chunk_size,
    )
    category_names = [name for name, _ in pairs]

    if request.dry_run:
        logger.info("API: dry-run requested — skipping BIG-IP push.")
        return PushResponse(
            status="success",
            urls_pushed=len(entries),
            categories_updated=category_names,
            dry_run=True,
            message=(
                f"Dry run complete. {len(entries)} URLs validated across "
                f"{len(pairs)} category object(s). No data pushed."
            ),
        )

    # Step 3: Push
    client = BIGIPClient(
        host=request.host,
        username=request.user,
        password=request.password,
        partition=request.partition,
        verify_ssl=request.verify_ssl,
    )

    try:
        client.upsert_all(pairs)
    except AuthenticationError as exc:
        msg = f"Authentication failed: {exc}"
        _record_error(msg, request, status_path)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=msg,
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        msg = f"Connection error: {exc}"
        _record_error(msg, request, status_path)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=msg,
        ) from exc
    except APIError as exc:
        msg = f"BIG-IP API error: {exc}"
        _record_error(msg, request, status_path)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=msg,
        ) from exc

    # Step 4: Record success
    sync_status = SyncStatus.success(
        urls_pushed=len(entries),
        categories_updated=category_names,
        source=request.source,
        bigip_host=request.host,
    )
    write_status_safe(sync_status, status_path)

    return PushResponse(
        status="success",
        urls_pushed=len(entries),
        categories_updated=category_names,
        dry_run=False,
        message=(
            f"Sync complete. {len(entries)} URLs pushed to {len(pairs)} "
            f"category object(s) on {request.host}."
        ),
    )


@app.post(
    "/schedule/update",
    response_model=ScheduleUpdateResponse,
    tags=["scheduler"],
)
def schedule_update(request: ScheduleUpdateRequest) -> ScheduleUpdateResponse:
    """
    Update the sync polling interval.

    Writes the new interval (in minutes) to ``sync_interval.conf``, which the
    systemd timer helper script reads on the next run.  Does NOT restart or
    modify any running process — the systemd timer picks up the change on its
    next execution cycle.
    """
    conf_path = INTERVAL_CONF_PATH
    try:
        conf_path.parent.mkdir(parents=True, exist_ok=True)
        conf_path.write_text(f"{request.interval_minutes}\n", encoding="utf-8")
        logger.info(
            "Sync interval updated to %d minutes (written to %s).",
            request.interval_minutes,
            conf_path,
        )
    except OSError as exc:
        logger.error("Failed to write interval config: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to write interval config to '{conf_path}': {exc}",
        ) from exc

    return ScheduleUpdateResponse(
        interval_minutes=request.interval_minutes,
        conf_path=str(conf_path),
        message=(
            f"Interval set to {request.interval_minutes} minute(s). "
            "The systemd timer will use this value on its next cycle."
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record_error(message: str, request: PushRequest, status_path: Path) -> None:
    """Write an error status record to the status file."""
    err_status = SyncStatus.error(
        error_message=message,
        source=request.source,
        bigip_host=request.host,
    )
    write_status_safe(err_status, status_path)
