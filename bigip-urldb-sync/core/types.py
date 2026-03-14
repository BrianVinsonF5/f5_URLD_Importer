"""
types.py — Shared type aliases for the bigip-urldb-sync core package.

Centralising these here prevents the same alias from being re-defined in
multiple modules, which would silently diverge if either definition changed.
"""

from typing import Any, Dict

# A normalized URL entry as produced by core.loader:
#   {"url": "https://example.com", "type": "exact"}
UrlEntry = Dict[str, str]

# An iControl REST payload ready to POST/PATCH to BIG-IP.
IControlPayload = Dict[str, Any]
