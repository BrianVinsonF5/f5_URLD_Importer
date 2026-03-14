"""
loader.py — Load and normalize URL lists from a local file or remote HTTP/HTTPS endpoint.

Supports two JSON input schemas:
  - Simple:   { "urls": ["https://example.com", "http://bad.com"] }
  - Extended: { "urls": [{ "url": "https://example.com", "type": "exact" }] }

Both are normalized to a list of dicts: [{"url": "...", "type": "..."}, ...]
"""

import json
import logging
from pathlib import Path
from typing import List, Dict, Any, Union
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

# Normalized URL entry type
UrlEntry = Dict[str, str]


def _is_http_source(source: str) -> bool:
    """Return True if *source* looks like an HTTP or HTTPS URL."""
    scheme = urlparse(source).scheme
    return scheme in ("http", "https")


def _normalize_entries(raw_urls: List[Any], default_type: str = "exact") -> List[UrlEntry]:
    """
    Normalize a raw URL list to a uniform list of ``{"url": ..., "type": ...}`` dicts.

    Args:
        raw_urls:     The value of the ``"urls"`` key from the source JSON.
        default_type: URL match type to assign when the source entry omits it.

    Returns:
        A list of normalized URL entry dicts.

    Raises:
        ValueError: If an entry cannot be interpreted as a URL.
    """
    normalized: List[UrlEntry] = []
    for idx, entry in enumerate(raw_urls):
        if isinstance(entry, str):
            if not entry.strip():
                logger.warning("Skipping blank URL at index %d", idx)
                continue
            normalized.append({"url": entry.strip(), "type": default_type})
        elif isinstance(entry, dict):
            url = entry.get("url", "").strip()
            if not url:
                logger.warning("Skipping entry at index %d with missing/empty 'url' field", idx)
                continue
            entry_type = entry.get("type", default_type)
            normalized.append({"url": url, "type": entry_type})
        else:
            raise ValueError(
                f"Unexpected URL entry type at index {idx}: {type(entry).__name__!r} — "
                f"expected str or dict."
            )
    return normalized


def load_from_file(path: Union[str, Path], default_type: str = "exact") -> List[UrlEntry]:
    """
    Load and normalize a URL list from a local JSON file.

    Args:
        path:         Path to the JSON file.
        default_type: Default URL match type for simple-format entries.

    Returns:
        Normalized list of URL entry dicts.

    Raises:
        FileNotFoundError: If *path* does not exist.
        ValueError:        If the JSON structure is invalid.
    """
    path = Path(path)
    logger.info("Loading URL list from local file: %s", path)
    if not path.exists():
        raise FileNotFoundError(f"Source file not found: {path}")

    with path.open("r", encoding="utf-8") as fh:
        data: Any = json.load(fh)

    return _parse_json_payload(data, default_type=default_type)


def load_from_http(
    url: str,
    default_type: str = "exact",
    timeout: int = 30,
    verify_ssl: bool = True,
    headers: Dict[str, str] | None = None,
) -> List[UrlEntry]:
    """
    Load and normalize a URL list from a remote HTTP/HTTPS endpoint.

    Args:
        url:          The HTTP/HTTPS URL to fetch JSON from.
        default_type: Default URL match type for simple-format entries.
        timeout:      Request timeout in seconds.
        verify_ssl:   Whether to verify the remote server's TLS certificate.
        headers:      Optional extra HTTP request headers (e.g. Authorization).

    Returns:
        Normalized list of URL entry dicts.

    Raises:
        requests.HTTPError:    On non-2xx HTTP responses.
        requests.ConnectionError: On network-level failures.
        ValueError:            If the response body is not valid JSON or has
                               an unexpected structure.
    """
    logger.info("Loading URL list from remote endpoint: %s", url)
    if not verify_ssl:
        logger.warning(
            "SSL certificate verification is DISABLED for source URL %s. "
            "Enable verification in production environments.",
            url,
        )

    try:
        response = requests.get(
            url,
            timeout=timeout,
            verify=verify_ssl,
            headers=headers or {},
        )
        response.raise_for_status()
    except requests.exceptions.SSLError as exc:
        raise requests.exceptions.SSLError(
            f"SSL error fetching source URL {url}: {exc}"
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        raise requests.exceptions.ConnectionError(
            f"Connection error fetching source URL {url}: {exc}"
        ) from exc
    except requests.exceptions.HTTPError as exc:
        raise requests.exceptions.HTTPError(
            f"HTTP {response.status_code} fetching source URL {url}: {exc}"
        ) from exc

    try:
        data: Any = response.json()
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Response from {url} is not valid JSON: {exc}"
        ) from exc

    return _parse_json_payload(data, default_type=default_type)


def _parse_json_payload(data: Any, default_type: str = "exact") -> List[UrlEntry]:
    """
    Validate the top-level JSON structure and delegate to the normalizer.

    Args:
        data:         Parsed JSON object (expected to be a dict with a ``"urls"`` key).
        default_type: Default URL match type.

    Returns:
        Normalized list of URL entry dicts.

    Raises:
        ValueError: If ``data`` is not a dict or lacks a ``"urls"`` key.
    """
    if not isinstance(data, dict):
        raise ValueError(
            f"Expected a JSON object at the top level, got {type(data).__name__!r}."
        )
    if "urls" not in data:
        raise ValueError(
            "JSON payload is missing the required 'urls' key."
        )
    raw_urls = data["urls"]
    if not isinstance(raw_urls, list):
        raise ValueError(
            f"Expected 'urls' to be a list, got {type(raw_urls).__name__!r}."
        )

    entries = _normalize_entries(raw_urls, default_type=default_type)
    logger.info("Loaded %d URL entries from source.", len(entries))
    return entries


def load(
    source: str,
    default_type: str = "exact",
    timeout: int = 30,
    verify_ssl: bool = True,
    headers: Dict[str, str] | None = None,
) -> List[UrlEntry]:
    """
    Auto-detect whether *source* is a local file path or HTTP URL and load accordingly.

    Args:
        source:       Local file path or HTTP/HTTPS URL.
        default_type: Default URL match type for simple-format entries.
        timeout:      HTTP request timeout in seconds (ignored for file sources).
        verify_ssl:   SSL verification flag for HTTP sources.
        headers:      Extra HTTP headers for remote sources.

    Returns:
        Normalized list of URL entry dicts.
    """
    if _is_http_source(source):
        return load_from_http(
            source,
            default_type=default_type,
            timeout=timeout,
            verify_ssl=verify_ssl,
            headers=headers,
        )
    return load_from_file(source, default_type=default_type)
