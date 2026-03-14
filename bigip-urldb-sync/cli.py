"""
cli.py — Click-based command-line interface for bigip-urldb-sync.

Commands:
  sync    Load a JSON URL list and push it to a BIG-IP URLDB category.
  status  Print the last sync result from the status file.

Usage examples::

    # Basic sync from local file
    python cli.py sync --source urls.json --host 192.0.2.1 --password secret

    # Sync from HTTP endpoint with dry-run
    python cli.py sync --source https://feeds.example.com/urls.json \\
        --host 192.0.2.1 --password secret --dry-run

    # Show last sync result
    python cli.py status
"""

import json
import logging
import sys
from pathlib import Path
from typing import NoReturn, Optional

import click
import requests

from config import Settings
from core.bigip import APIError, AuthenticationError, BIGIPClient
from core.loader import load
from core.transformer import transform
from status import SyncStatus, read_status, write_status_safe

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(version="1.0.0", prog_name="bigip-urldb-sync")
def cli() -> None:
    """bigip-urldb-sync — Push custom URL lists to F5 BIG-IP URLDB categories."""


# ---------------------------------------------------------------------------
# sync command
# ---------------------------------------------------------------------------


@cli.command("sync")
@click.option(
    "--source",
    envvar="JSON_SOURCE",
    required=True,
    help="Source JSON file path or HTTP/HTTPS URL containing the URL list.",
)
@click.option(
    "--host",
    envvar="BIGIP_HOST",
    required=True,
    help="BIG-IP management IP address or hostname.",
)
@click.option(
    "--user",
    envvar="BIGIP_USER",
    default="admin",
    show_default=True,
    help="BIG-IP username.",
)
@click.option(
    "--password",
    envvar="BIGIP_PASSWORD",
    default=None,
    help="BIG-IP password.  Can be set via BIGIP_PASSWORD env var.",
)
@click.option(
    "--category",
    envvar="URLDB_CATEGORY",
    default="custom_block_list",
    show_default=True,
    help="URLDB category name to create or update.",
)
@click.option(
    "--url-type",
    envvar="URLDB_URL_TYPE",
    default="exact",
    show_default=True,
    type=click.Choice(["exact", "glob", "any"], case_sensitive=False),
    help="URL match type applied to entries that do not specify their own type.",
)
@click.option(
    "--partition",
    envvar="BIGIP_PARTITION",
    default="Common",
    show_default=True,
    help="BIG-IP partition.",
)
@click.option(
    "--chunk-size",
    envvar="URLDB_CHUNK_SIZE",
    default=9000,
    show_default=True,
    type=int,
    help="Maximum URLs per URLDB category object.  Larger lists are split into numbered sub-categories.",
)
@click.option(
    "--status-file",
    envvar="STATUS_FILE_PATH",
    default="/var/log/urldb-sync/status.json",
    show_default=True,
    type=click.Path(),
    help="Path to write the sync status JSON file.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Parse and transform only — do NOT push to BIG-IP.",
)
@click.option(
    "--verify-ssl",
    is_flag=True,
    default=False,
    help="Enable TLS certificate verification for BIG-IP management API calls.",
)
@click.option(
    "--log-level",
    envvar="LOG_LEVEL",
    default="INFO",
    show_default=True,
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=False),
    help="Logging verbosity.",
)
def sync_command(
    source: str,
    host: str,
    user: str,
    password: Optional[str],
    category: str,
    url_type: str,
    partition: str,
    chunk_size: int,
    status_file: str,
    dry_run: bool,
    verify_ssl: bool,
    log_level: str,
) -> None:
    """
    Load a JSON URL list and push it to a BIG-IP URLDB category.

    The source can be a local file path or an HTTP/HTTPS URL.  The JSON
    must contain a ``"urls"`` key with either plain strings or dicts of
    ``{"url": "...", "type": "..."}`` form.
    """
    _configure_logging(log_level)

    if not password:
        # Prompt interactively if no password was supplied
        password = click.prompt("BIG-IP password", hide_input=True)

    status_path = Path(status_file)

    # ------------------------------------------------------------------
    # Step 1: Load URL list
    # ------------------------------------------------------------------
    logger.info("Loading URL list from source: %s", source)
    try:
        entries = load(source, default_type=url_type, verify_ssl=verify_ssl)
    except FileNotFoundError as exc:
        _fail(str(exc), source=source, host=host, status_path=status_path)
    except (ValueError, requests.RequestException) as exc:
        _fail(str(exc), source=source, host=host, status_path=status_path)

    click.echo(f"Loaded {len(entries)} URL entries from source.")

    # ------------------------------------------------------------------
    # Step 2: Transform to iControl payloads
    # ------------------------------------------------------------------
    logger.info("Transforming URL entries into iControl REST payloads.")
    pairs = transform(
        category_name=category,
        entries=entries,
        partition=partition,
        chunk_size=chunk_size,
    )
    category_names = [name for name, _ in pairs]
    click.echo(
        f"Transformed into {len(pairs)} category object(s): {', '.join(category_names)}"
    )

    if dry_run:
        click.echo("[DRY RUN] Skipping BIG-IP push.  Payload preview:")
        for name, payload in pairs:
            url_count = len(payload.get("urls", []))
            click.echo(f"  {name}: {url_count} URLs")
        sys.exit(0)

    # ------------------------------------------------------------------
    # Step 3: Push to BIG-IP
    # ------------------------------------------------------------------
    logger.info("Connecting to BIG-IP at '%s'.", host)
    client = BIGIPClient(
        host=host,
        username=user,
        password=password,
        partition=partition,
        verify_ssl=verify_ssl,
    )

    try:
        client.upsert_all(pairs)
    except AuthenticationError as exc:
        _fail(f"Authentication failed: {exc}", source=source, host=host, status_path=status_path)
    except requests.exceptions.ConnectionError as exc:
        _fail(f"Connection error: {exc}", source=source, host=host, status_path=status_path)
    except APIError as exc:
        _fail(f"BIG-IP API error: {exc}", source=source, host=host, status_path=status_path)

    # ------------------------------------------------------------------
    # Step 4: Record success status
    # ------------------------------------------------------------------
    sync_status = SyncStatus.success(
        urls_pushed=len(entries),
        categories_updated=category_names,
        source=source,
        bigip_host=host,
    )
    write_status_safe(sync_status, status_path)

    click.echo(
        click.style(
            f"Sync complete: {len(entries)} URLs pushed to {len(pairs)} category object(s) on {host}.",
            fg="green",
        )
    )


# ---------------------------------------------------------------------------
# status command
# ---------------------------------------------------------------------------


@cli.command("status")
@click.option(
    "--status-file",
    envvar="STATUS_FILE_PATH",
    default="/var/log/urldb-sync/status.json",
    show_default=True,
    type=click.Path(),
    help="Path to the sync status JSON file.",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output raw JSON instead of formatted text.",
)
def status_command(status_file: str, output_json: bool) -> None:
    """Print the last sync result from the status file."""
    _configure_logging("WARNING")  # Quiet for status display
    path = Path(status_file)

    try:
        data = read_status(path)
    except (ValueError, OSError) as exc:
        click.echo(click.style(f"Error reading status file: {exc}", fg="red"), err=True)
        sys.exit(1)

    if data is None:
        click.echo(f"No status file found at {path}. No sync has run yet.")
        sys.exit(0)

    if output_json:
        click.echo(json.dumps(data, indent=2))
        return

    # Formatted output
    status_colour = "green" if data.get("status") == "success" else "red"
    click.echo(f"Last run:    {data.get('last_run', 'unknown')}")
    click.echo(
        f"Status:      {click.style(data.get('status', 'unknown').upper(), fg=status_colour)}"
    )
    click.echo(f"BIG-IP host: {data.get('bigip_host', 'unknown')}")
    click.echo(f"Source:      {data.get('source', 'unknown')}")
    click.echo(f"URLs pushed: {data.get('urls_pushed', 0)}")
    cats = data.get("categories_updated", [])
    click.echo(f"Categories:  {', '.join(cats) if cats else 'none'}")
    if data.get("error_message"):
        click.echo(
            f"Error:       {click.style(data['error_message'], fg='red')}"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _configure_logging(level: str) -> None:
    """Configure root logging at the given level, delegating to :class:`Settings`."""
    Settings(log_level=level).configure_logging()


def _fail(
    message: str,
    source: str,
    host: str,
    status_path: Path,
    exit_code: int = 1,
) -> NoReturn:
    """Log an error, write an error status, and exit."""
    logger.error(message)
    click.echo(click.style(f"ERROR: {message}", fg="red"), err=True)
    err_status = SyncStatus.error(
        error_message=message,
        source=source,
        bigip_host=host,
    )
    write_status_safe(err_status, status_path)
    sys.exit(exit_code)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cli()
