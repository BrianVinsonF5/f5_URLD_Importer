"""
transformer.py — Transform a normalized URL entry list into iControl REST payloads.

Handles chunking when the URL list exceeds a configurable size limit, producing
numbered sub-category payloads (e.g. ``custom_block_list_01``, ``_02``, …).
"""

import logging
import math
from typing import Any, Dict, List, Optional, Tuple

from core.types import IControlPayload, UrlEntry

logger = logging.getLogger(__name__)

# Maps user-facing URL type names to the iControl REST API values.
#
# BIG-IP quirks:
#   "exact"     → omit the "type" key entirely; exact is the default and the
#                 API returns HTTP 400 if you send "type":"exact" explicitly.
#   "glob"      → the REST API uses the hyphenated form "glob-match".
#   "glob-match"→ already in API form; pass through unchanged.
#   "any"       → not a standard BIG-IP URL-DB type; omit and let BIG-IP use
#                 its default, or the caller can supply "glob-match".
#
# A mapped value of None means: do not include "type" in the URL entry object.
_BIGIP_URL_TYPE_MAP: Dict[str, Optional[str]] = {
    "exact": None,
    "glob": "glob-match",
    "glob-match": "glob-match",
    "any": None,
}


def _build_urls_field(entries: List[UrlEntry]) -> List[Dict[str, Any]]:
    """
    Convert normalized URL entries to the ``urls`` array format expected by iControl REST.

    The iControl ``url-db/url-category`` endpoint accepts objects with a
    ``name`` key and an optional ``type`` key.  Sending ``"type":"exact"``
    causes a 400 error on BIG-IP, so exact-match entries are emitted without
    a ``type`` field (exact is the server-side default).  Glob entries use the
    hyphenated ``"glob-match"`` spelling required by the REST API.

    Args:
        entries: Normalized URL entry dicts from the loader.

    Returns:
        List of iControl-formatted URL dicts.
    """
    result: List[Dict[str, Any]] = []
    for e in entries:
        raw_type = e.get("type", "exact")
        api_type = _BIGIP_URL_TYPE_MAP.get(raw_type, raw_type)
        entry: Dict[str, Any] = {"name": e["url"]}
        if api_type:
            entry["type"] = api_type
        result.append(entry)
    return result


def build_payload(
    category_name: str,
    entries: List[UrlEntry],
    partition: str = "Common",
    description: str = "Managed by bigip-urldb-sync",
) -> IControlPayload:
    """
    Build a single iControl REST payload for the given category name and URL entries.

    Args:
        category_name: The URLDB category name (e.g. ``custom_block_list``).
        entries:       Normalized URL entries to include in this payload.
        partition:     BIG-IP partition (default: ``Common``).
        description:   Human-readable description stored on the category object.

    Returns:
        A dict suitable for serialisation to JSON and submission via POST or PATCH.
    """
    return {
        "name": category_name,
        "partition": partition,
        "description": description,
        "urls": _build_urls_field(entries),
    }


def chunk_entries(entries: List[UrlEntry], chunk_size: int) -> List[List[UrlEntry]]:
    """
    Split *entries* into a list of sub-lists, each at most *chunk_size* long.

    Args:
        entries:    Full URL entry list.
        chunk_size: Maximum number of entries per chunk.

    Returns:
        A list of sub-lists.  Returns ``[entries]`` (a single chunk) when
        ``len(entries) <= chunk_size``.
    """
    if chunk_size < 1:
        raise ValueError(f"chunk_size must be >= 1, got {chunk_size}.")
    return [entries[i : i + chunk_size] for i in range(0, max(len(entries), 1), chunk_size)]


def transform(
    category_name: str,
    entries: List[UrlEntry],
    partition: str = "Common",
    chunk_size: int = 9000,
    description: str = "Managed by bigip-urldb-sync",
) -> List[Tuple[str, IControlPayload]]:
    """
    Transform a URL entry list into one or more ``(category_name, payload)`` pairs.

    When ``len(entries) <= chunk_size``, a single pair is returned using
    *category_name* as-is.  When the list exceeds *chunk_size*, multiple pairs
    are returned with names suffixed ``_01``, ``_02``, etc.

    Args:
        category_name: Base URLDB category name.
        entries:       Full normalized URL entry list.
        partition:     BIG-IP partition.
        chunk_size:    Maximum URLs per category object (default: 9000).
        description:   Description stored on each BIG-IP category object.

    Returns:
        Ordered list of ``(name, payload)`` tuples.

    Example::

        pairs = transform("custom_block_list", entries, chunk_size=9000)
        # → [("custom_block_list", {...})]          # if ≤ 9000 entries
        # → [("custom_block_list_01", {...}),
        #    ("custom_block_list_02", {...})]        # if > 9000 entries
    """
    if not entries:
        logger.warning(
            "URL entry list is empty; a single empty payload will be produced for '%s'.",
            category_name,
        )

    chunks = chunk_entries(entries, chunk_size)
    total_chunks = len(chunks)

    if total_chunks == 1:
        logger.debug(
            "Single chunk (%d entries) — using category name '%s' as-is.",
            len(chunks[0]),
            category_name,
        )
        payload = build_payload(
            category_name=category_name,
            entries=chunks[0],
            partition=partition,
            description=description,
        )
        return [(category_name, payload)]

    # Multiple chunks — suffix with zero-padded numbers
    pad_width = max(2, math.floor(math.log10(total_chunks)) + 1)
    logger.info(
        "%d URL entries exceed chunk_size=%d — splitting into %d sub-categories.",
        len(entries),
        chunk_size,
        total_chunks,
    )

    result: List[Tuple[str, IControlPayload]] = []
    for idx, chunk in enumerate(chunks, start=1):
        suffix = str(idx).zfill(pad_width)
        name = f"{category_name}_{suffix}"
        logger.debug(
            "  Sub-category '%s': %d entries (chunk %d/%d).",
            name,
            len(chunk),
            idx,
            total_chunks,
        )
        payload = build_payload(
            category_name=name,
            entries=chunk,
            partition=partition,
            description=f"{description} [part {idx}/{total_chunks}]",
        )
        result.append((name, payload))

    return result
