"""
bigip.py — iControl REST client for F5 BIG-IP URLDB category management.

Responsibilities:
  - Token-based authentication (POST /mgmt/shared/authn/login)
  - Automatic token caching and refresh (TTL: 1200 seconds)
  - Upsert logic: PUT (full replacement) if exists, POST if new
  - Graceful error handling for auth, network, and API failures
"""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
import urllib3

from core.types import IControlPayload

logger = logging.getLogger(__name__)

# iControl REST token TTL as documented by F5 (seconds)
_TOKEN_TTL_SECONDS = 1200
# Refresh slightly before expiry to avoid race conditions
_TOKEN_REFRESH_BUFFER = 60


class AuthenticationError(Exception):
    """Raised when BIG-IP authentication fails."""


class APIError(Exception):
    """Raised when a BIG-IP iControl REST call returns an unexpected error."""


class BIGIPClient:
    """
    Thin iControl REST client for managing custom URLDB categories on F5 BIG-IP.

    Authentication tokens are cached and transparently refreshed when they
    approach expiry (TTL – 60 s).

    Args:
        host:       BIG-IP management IP address or hostname.
        username:   BIG-IP username (default: ``admin``).
        password:   BIG-IP password.
        partition:  Target partition (default: ``Common``).
        verify_ssl: Whether to validate the management-plane TLS certificate.
                    Defaults to ``False`` because BIG-IP ships with self-signed
                    certs; set to ``True`` (or a CA bundle path) in environments
                    where a trusted cert is installed.
        timeout:    HTTP request timeout in seconds (default: 30).

    Example::

        client = BIGIPClient(host="192.0.2.1", username="admin", password="secret")
        client.upsert_category("custom_block_list", payload)
    """

    _URLDB_BASE = "/mgmt/tm/sys/url-db/url-category"
    _AUTH_ENDPOINT = "/mgmt/shared/authn/login"

    def __init__(
        self,
        host: str,
        username: str = "admin",
        password: str = "",
        partition: str = "Common",
        verify_ssl: bool = False,
        timeout: int = 30,
    ) -> None:
        self.host = host.rstrip("/")
        self.username = username
        self._password = password
        self.partition = partition
        self.verify_ssl = verify_ssl
        self.timeout = timeout

        self._token: Optional[str] = None
        self._token_acquired_at: float = 0.0

        self._session = requests.Session()
        if not verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            logger.warning(
                "SSL certificate verification is DISABLED for BIG-IP host '%s'. "
                "This is acceptable for management interfaces with self-signed certs "
                "but should be reviewed in production or federal environments.",
                self.host,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"https://{self.host}{path}"

    def _token_is_valid(self) -> bool:
        """Return True if the cached token is still usable (not near expiry)."""
        if not self._token:
            return False
        age = time.monotonic() - self._token_acquired_at
        return age < (_TOKEN_TTL_SECONDS - _TOKEN_REFRESH_BUFFER)

    def _authenticate(self) -> None:
        """
        Obtain a new authentication token from BIG-IP and cache it.

        Raises:
            AuthenticationError: On HTTP 401/403 or missing token in response.
            requests.ConnectionError: On network-level failures.
        """
        logger.info("Authenticating to BIG-IP '%s' as user '%s'.", self.host, self.username)
        url = self._url(self._AUTH_ENDPOINT)
        payload = {
            "username": self.username,
            "password": self._password,
            "loginProviderName": "tmos",
        }
        try:
            resp = self._session.post(
                url,
                json=payload,
                verify=self.verify_ssl,
                timeout=self.timeout,
            )
        except requests.exceptions.ConnectionError as exc:
            raise requests.exceptions.ConnectionError(
                f"Cannot connect to BIG-IP at '{self.host}': {exc}"
            ) from exc

        if resp.status_code in (401, 403):
            raise AuthenticationError(
                f"Authentication failed for user '{self.username}' on '{self.host}' "
                f"(HTTP {resp.status_code}). Check credentials."
            )

        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            raise AuthenticationError(
                f"Unexpected HTTP {resp.status_code} during authentication on '{self.host}': "
                f"{resp.text[:200]}"
            ) from exc

        body = resp.json()
        token: Optional[str] = (body.get("token") or {}).get("token")
        if not token:
            raise AuthenticationError(
                f"BIG-IP authentication response did not contain a token. "
                f"Response body: {str(body)[:200]}"
            )

        self._token = token
        self._token_acquired_at = time.monotonic()
        logger.info("Successfully obtained BIG-IP auth token (TTL: %ds).", _TOKEN_TTL_SECONDS)

    def _ensure_authenticated(self) -> None:
        """Authenticate (or re-authenticate) if the cached token is stale/absent."""
        if not self._token_is_valid():
            logger.debug("Token absent or near expiry — refreshing.")
            self._authenticate()

    def _headers(self) -> Dict[str, str]:
        """Return request headers including the cached auth token."""
        self._ensure_authenticated()
        return {
            "X-F5-Auth-Token": self._token,  # type: ignore[return-value]
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Category management
    # ------------------------------------------------------------------

    def _category_url(self, name: str) -> str:
        """Build the full iControl URL for a named URLDB category."""
        # BIG-IP iControl REST expects ~Partition~name notation for named objects
        partition_prefix = f"~{self.partition}~"
        return self._url(f"{self._URLDB_BASE}/{partition_prefix}{name}")

    def _patch_category(self, name: str, payload: IControlPayload) -> requests.Response:
        """
        Attempt to PATCH an existing URLDB category.

        Returns:
            The raw :class:`requests.Response`.
        """
        url = self._category_url(name)
        logger.debug("PATCH %s", url)
        return self._session.patch(
            url,
            json=payload,
            headers=self._headers(),
            verify=self.verify_ssl,
            timeout=self.timeout,
        )

    def _post_category(self, payload: IControlPayload) -> requests.Response:
        """
        POST a new URLDB category.

        Returns:
            The raw :class:`requests.Response`.
        """
        url = self._url(self._URLDB_BASE)
        logger.debug("POST %s", url)
        return self._session.post(
            url,
            json=payload,
            headers=self._headers(),
            verify=self.verify_ssl,
            timeout=self.timeout,
        )

    def _put_category(self, name: str, payload: IControlPayload) -> requests.Response:
        """
        PUT (full replacement) an existing URLDB category.

        Unlike PATCH which merges fields, PUT replaces the entire resource,
        ensuring that URLs removed from the source are also removed from
        the BIG-IP category.

        Returns:
            The raw :class:`requests.Response`.
        """
        url = self._category_url(name)
        logger.debug("PUT %s", url)
        return self._session.put(
            url,
            json=payload,
            headers=self._headers(),
            verify=self.verify_ssl,
            timeout=self.timeout,
        )

    def upsert_category(self, name: str, payload: IControlPayload) -> Dict[str, Any]:
        """
        Upsert a URLDB category: PUT (full replacement) if it exists, POST if it does not.

        The method first checks if the category exists.  If it does, it uses
        PUT to fully replace the category (including all URLs), ensuring that
        URLs removed from the source file are also removed from the BIG-IP.
        If it doesn't exist, it uses POST to create the category with all fields.

        Args:
            name:    Category name (without partition prefix).
            payload: iControl REST payload as produced by :mod:`core.transformer`.

        Returns:
            The parsed JSON response body from BIG-IP.

        Raises:
            AuthenticationError:   On auth failures.
            APIError:              On unexpected HTTP error responses.
            requests.ConnectionError: On network-level failures.
        """
        logger.info("Upserting URLDB category '%s' (partition: %s).", name, self.partition)

        # Check if category already exists
        try:
            exists = self.category_exists(name)
        except requests.exceptions.ConnectionError as exc:
            raise requests.exceptions.ConnectionError(
                f"Network error checking category '{name}' on '{self.host}': {exc}"
            ) from exc

        if exists:
            logger.info("Category '%s' exists — replacing via PUT.", name)
            # For PUT (full replacement), remove displayName as it cannot be changed after creation
            put_payload = {k: v for k, v in payload.items() if k != "displayName"}
            try:
                resp = self._put_category(name, put_payload)
            except requests.exceptions.ConnectionError as exc:
                raise requests.exceptions.ConnectionError(
                    f"Network error reaching BIG-IP '{self.host}': {exc}"
                ) from exc

            if resp.status_code == 401:
                logger.warning("Received 401 — token may have expired; re-authenticating once.")
                self._token = None
                try:
                    resp = self._put_category(name, put_payload)
                except requests.exceptions.ConnectionError as exc:
                    raise requests.exceptions.ConnectionError(
                        f"Network error reaching BIG-IP '{self.host}' on retry: {exc}"
                    ) from exc
        else:
            logger.info("Category '%s' not found — creating via POST.", name)
            try:
                resp = self._post_category(payload)
            except requests.exceptions.ConnectionError as exc:
                raise requests.exceptions.ConnectionError(
                    f"Network error reaching BIG-IP '{self.host}' during POST: {exc}"
                ) from exc

            if resp.status_code == 401:
                logger.warning("Received 401 — token may have expired; re-authenticating once.")
                self._token = None
                try:
                    resp = self._post_category(payload)
                except requests.exceptions.ConnectionError as exc:
                    raise requests.exceptions.ConnectionError(
                        f"Network error reaching BIG-IP '{self.host}' on retry: {exc}"
                    ) from exc

        self._raise_for_api_error(resp, context=f"upsert category '{name}'")
        logger.info(
            "Category '%s' upserted successfully (HTTP %d).", name, resp.status_code
        )
        return resp.json()

    def upsert_all(
        self, pairs: List[Tuple[str, IControlPayload]]
    ) -> List[Dict[str, Any]]:
        """
        Upsert multiple URLDB categories in order.

        Args:
            pairs: Ordered list of ``(category_name, payload)`` tuples as
                   produced by :func:`core.transformer.transform`.

        Returns:
            List of parsed JSON response bodies, one per category.

        Raises:
            AuthenticationError:   On auth failures.
            APIError:              On unexpected HTTP error responses.
            requests.ConnectionError: On network-level failures.
        """
        return [self.upsert_category(name, payload) for name, payload in pairs]

    def category_exists(self, name: str) -> bool:
        """
        Check whether a URLDB category with *name* exists on the BIG-IP.

        Args:
            name: Category name (without partition prefix).

        Returns:
            ``True`` if the category exists, ``False`` if it returns 404.

        Raises:
            APIError: On unexpected HTTP error responses.
        """
        try:
            resp = self._session.get(
                self._category_url(name),
                headers=self._headers(),
                verify=self.verify_ssl,
                timeout=self.timeout,
            )
        except requests.exceptions.ConnectionError as exc:
            raise requests.exceptions.ConnectionError(
                f"Network error checking category '{name}' on '{self.host}': {exc}"
            ) from exc

        if resp.status_code == 404:
            return False
        self._raise_for_api_error(resp, context=f"check existence of category '{name}'")
        return True

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    @staticmethod
    def _raise_for_api_error(resp: requests.Response, context: str = "") -> None:
        """
        Raise an :class:`APIError` if *resp* indicates a failure.

        Args:
            resp:    The HTTP response to inspect.
            context: Human-readable description of the operation (for error messages).

        Raises:
            AuthenticationError: On 401/403 responses.
            APIError:            On other non-2xx responses.
        """
        if resp.ok:
            return

        try:
            body = resp.json()
            message = body.get("message", resp.text[:200])
        except ValueError:
            message = resp.text[:200]

        prefix = f"[{context}] " if context else ""

        if resp.status_code in (401, 403):
            raise AuthenticationError(
                f"{prefix}Authorization error (HTTP {resp.status_code}): {message}"
            )

        raise APIError(
            f"{prefix}BIG-IP API error (HTTP {resp.status_code}): {message}"
        )
