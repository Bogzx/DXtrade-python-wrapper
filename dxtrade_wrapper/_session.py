"""HTTP plumbing, login, session keepalive and re-login (REST spec, "Token Authentication")."""

import re
import threading
import time
from typing import Any, Mapping, Optional
from urllib.parse import quote

import requests

from .exceptions import (
    AuthenticationError,
    ConflictError,
    DXTradeAPIError,
    DXTradeConnectionError,
    NotFoundError,
    PreconditionFailedError,
    RateLimitError,
    ServerError,
)
from ._state import _ClientState


def parse_interval(value: Any) -> Optional[float]:
    """Parses a session ``timeout`` ("00:30:00", "PT30M" or seconds) to seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    match = re.fullmatch(r"(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)", text)
    if match:
        hours, minutes, seconds = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", text, re.IGNORECASE)
    if match and any(match.groups()):
        hours, minutes, seconds = (float(g) if g else 0.0 for g in match.groups())
        return hours * 3600 + minutes * 60 + seconds
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    return None



class _SessionLayer(_ClientState):
    #: Authorization scheme for token-authenticated REST calls (REST spec, "Token Authentication").
    AUTH_SCHEME = "DXAPI"

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        """Absolute REST URL for a path relative to the API prefix.

        Every REST call goes through here, so the prefix cannot be forgotten on
        one endpoint (the original bug: login had it, eight other calls did not).
        """
        return f"{self._base_url}{self._api_prefix}/{path.lstrip('/')}"

    @staticmethod
    def _seg(value: str) -> str:
        """URL-encodes one path segment. Account codes contain ':' and client
        order ids may contain '/', '?', '#' - the spec requires encoding both."""
        return quote(str(value), safe="")

    def _raise_for_response(self, response: requests.Response, context: str) -> None:
        if response.status_code < 400:
            return
        body: Any
        try:
            body = response.json()
        except ValueError:
            body = response.text[:500] or None
        error_code = description = None
        if isinstance(body, Mapping):
            error_code = body.get("errorCode")
            description = body.get("description")
        status = response.status_code
        detail = description or (body if isinstance(body, str) else None) or response.reason
        message = f"{context} failed: HTTP {status}"
        if error_code is not None:
            message += f" (error {error_code})"
        if detail:
            message += f": {detail}"
        kwargs = dict(status_code=status, error_code=error_code, description=description, body=body)
        if status == 401:
            raise AuthenticationError(message, **kwargs)
        if status == 404:
            raise NotFoundError(message, **kwargs)
        if status == 409:
            raise ConflictError(message, **kwargs)
        if status == 412:
            raise PreconditionFailedError(message, **kwargs)
        if status == 429:
            retry = self._retry_after(response) if "Retry-After" in response.headers else None
            raise RateLimitError(message, retry_after=retry, **kwargs)
        if status >= 500:
            raise ServerError(message, **kwargs)
        raise DXTradeAPIError(message, **kwargs)

    def _request(
        self,
        method: str,
        path: str,
        context: str,
        *,
        json_body: Any = None,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        retry_auth: bool = True,
    ) -> requests.Response:
        """Sends one REST request and maps failures to typed exceptions.

        On 401 with an established session it logs in again once and retries:
        the spec says an expired token gets 401 and the client "is expected to
        repeat the authentication procedure". Retrying an order POST is safe
        because a 401 means it was not processed, and the retry carries the same
        orderCode.
        """
        url = self._url(path)
        token_used = self._auth_token
        response = self._send(method, url, context, json_body, params, headers)
        if response.status_code == 429 and method == "GET":
            # Reads are idempotent: wait as asked (briefly) and try once more.
            delay = self._retry_after(response)
            if delay is not None and delay <= self.MAX_RATE_LIMIT_WAIT:
                self._logger.info("%s rate-limited; retrying in %.1fs", context, delay)
                time.sleep(delay)
                response = self._send(method, url, context, json_body, params, headers)

        if (
            response.status_code == 401
            and retry_auth
            and self._auto_relogin
            and self._is_authenticated
        ):
            self._logger.info("%s got 401; session expired, logging in again", context)
            self._relogin(stale_token=token_used)
            return self._request(
                method, path, context,
                json_body=json_body, params=params, headers=headers, retry_auth=False,
            )

        self._raise_for_response(response, context)
        return response

    #: Longest Retry-After (seconds) a GET waits out before one automatic retry.
    MAX_RATE_LIMIT_WAIT = 5.0

    def _send(self, method: str, url: str, context: str, json_body: Any,
              params: Optional[Mapping[str, Any]],
              headers: Optional[Mapping[str, str]]) -> requests.Response:
        try:
            return self._session.request(
                method, url, json=json_body, params=params, headers=headers, timeout=self._timeout
            )
        except requests.exceptions.RequestException as exc:
            # str(exc) carries the URL but never the body or auth header.
            raise DXTradeConnectionError(f"{context}: network error: {exc}") from exc

    @staticmethod
    def _retry_after(response: requests.Response) -> Optional[float]:
        """Retry-After in seconds; 1s when the server sent none."""
        raw = response.headers.get("Retry-After")
        if raw is None:
            return 1.0
        try:
            return max(float(raw), 0.0)
        except ValueError:
            return None

    @staticmethod
    def _json(response: requests.Response, context: str) -> Any:
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise DXTradeAPIError(
                f"{context}: response is not JSON", status_code=response.status_code,
                body=response.text[:500],
            ) from None

    # ------------------------------------------------------------------
    # Authentication and session
    # ------------------------------------------------------------------

    def login(self) -> None:
        """POST /login with username, domain and password; stores the session token.

        Raises:
            AuthenticationError: Wrong credentials (401) or API access refused (403).
            DXTradeConnectionError: The server could not be reached.
        """
        with self._auth_lock:
            payload = {
                "username": self._username,
                "domain": self._domain_or_vendor,
                "password": self._password,
            }
            self._logger.debug("Logging in to %s", self._url(self._login_path))
            try:
                response = self._session.post(
                    self._url(self._login_path),
                    json=payload,
                    headers={"Authorization": None},  # never send a stale token to /login
                    timeout=self._timeout,
                )
            except requests.exceptions.RequestException as exc:
                self._is_authenticated = False
                raise DXTradeConnectionError(f"Login: network error: {exc}") from exc

            if response.status_code == 403:
                self._is_authenticated = False
                raise AuthenticationError(
                    "Login refused with HTTP 403: this broker has not enabled DXtrade REST "
                    "API access for your account (FTMO, for example, disabled it in April 2024).",
                    status_code=403,
                )
            try:
                self._raise_for_response(response, "Login")
            except DXTradeAPIError as exc:
                self._is_authenticated = False
                if isinstance(exc, AuthenticationError):
                    raise
                raise AuthenticationError(str(exc), status_code=exc.status_code,
                                          error_code=exc.error_code,
                                          description=exc.description) from exc

            data = self._json(response, "Login") or {}
            token = data.get("sessionToken") if isinstance(data, Mapping) else None
            if not token:
                self._is_authenticated = False
                raise AuthenticationError("Login response contained no sessionToken")

            self._auth_token = token
            self._session_timeout = parse_interval(data.get("timeout"))
            self._configure_auth_headers()
            self._is_authenticated = True
            self._start_keepalive()
            self._logger.info("Authenticated as %s", self._username)

    def _relogin(self, stale_token: Optional[str] = None) -> None:
        """Logs in again, unless another thread already replaced ``stale_token``.

        Two threads that both get 401 on the same expired token must not both log
        in: the second login could invalidate the session the first just made.
        """
        with self._auth_lock:
            if (stale_token is not None and self._is_authenticated
                    and self._auth_token != stale_token):
                return
            self.login()

    def logout(self) -> None:
        """Stops the keepalive and POSTs /logout (best effort)."""
        self._stop_keepalive()
        if self._is_authenticated:
            try:
                self._request("POST", "logout", "Logout", retry_auth=False)
            except Exception as exc:  # noqa: BLE001 - logout must always clear local state
                self._logger.warning("Logout request failed (ignored): %s", exc)
        # Close the old pool: a bot that logs out and in again for days would
        # otherwise leak one connection pool per session.
        self._session.close()
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._auth_token = None
        self._is_authenticated = False
        self._account_id = self._configured_account
        self._accounts_cache = None
        self._logger.info("Logged out")

    @property
    def is_authenticated(self) -> bool:
        return self._is_authenticated

    def ping(self) -> bool:
        """POST /ping to extend the session. Returns False instead of raising.

        If the server returns a new sessionToken, it replaces the old one.
        """
        if not self._is_authenticated:
            return False
        try:
            response = self._request("POST", "ping", "Ping", retry_auth=False)
        except Exception as exc:  # noqa: BLE001 - never kill the keepalive thread
            self._logger.warning("Keepalive ping failed: %s", exc)
            return False
        try:
            data = self._json(response, "Ping")
        except DXTradeAPIError:
            data = None
        if isinstance(data, Mapping) and data.get("sessionToken"):
            with self._auth_lock:
                self._auth_token = data["sessionToken"]
                self._configure_auth_headers()
            self._session_timeout = parse_interval(data.get("timeout")) or self._session_timeout
        return True

    def _effective_keepalive(self) -> Optional[float]:
        interval = self._keepalive_interval
        if not interval or interval <= 0:
            return None
        if self._session_timeout:
            interval = min(interval, max(self._session_timeout / 2, 1.0))
        return interval

    def _start_keepalive(self) -> None:
        if not self._effective_keepalive():
            return
        if self._keepalive_thread and self._keepalive_thread.is_alive():
            return
        self._keepalive_stop.clear()
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop, name="DXTradeKeepalive", daemon=True
        )
        self._keepalive_thread.start()

    def _stop_keepalive(self) -> None:
        self._keepalive_stop.set()
        thread = self._keepalive_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)
        self._keepalive_thread = None

    def _keepalive_loop(self) -> None:
        while not self._keepalive_stop.wait(self._effective_keepalive() or 60.0):
            if not self._is_authenticated:
                break
            token = self._auth_token
            if not self.ping() and self._auto_relogin:
                try:
                    self._relogin(stale_token=token)
                except Exception as exc:  # noqa: BLE001 - keep the thread alive
                    self._logger.warning("Re-login after failed ping failed: %s", exc)

    def _configure_auth_headers(self) -> None:
        if self._auth_token:
            self._session.headers["Authorization"] = f"{self.AUTH_SCHEME} {self._auth_token}"

    def _check_authenticated(self) -> None:
        if not self._is_authenticated:
            raise AuthenticationError("Not authenticated. Call login() first.")
