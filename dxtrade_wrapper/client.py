"""
DXTradeDashboardWrapper: a Python client for the DXtrade REST and Push APIs.

It covers authentication and session upkeep, account data, order management and
real-time updates for a trading dashboard or bot.

=============================================================================
SPEC-CONFORMANT, NOT LIVE-VERIFIED
=============================================================================

Every request and response shape here follows Devexperts' public DXtrade
documentation:

* REST API: https://demo.dx.trade/developers/#/DXtrade-REST-API
  (OpenAPI: https://demo.dx.trade/dxsca-web/swagger/openapi.json)
* Push API: https://demo.dx.trade/developers/#/DXtrade-Push-API

The test suite checks the requests this client sends against that OpenAPI
document. Nothing has been run against a live broker yet, and brokers can switch
REST access off entirely (FTMO did in April 2024). Try it on a demo account
before you trust it with money.
"""

import itertools
import json
import logging
import queue
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote, urlencode

import requests
import websocket

from .exceptions import (
    AuthenticationError,
    ConflictError,
    ConnectionError,
    DXTradeAPIError,
    NotFoundError,
    OrderPlacementError,
    PreconditionFailedError,
    RateLimitError,
    ServerError,
    WebSocketError,
)
from .models import (
    Balance,
    Order,
    Position,
    account_code,
    order_leg,
    parse_balance,
    parse_order,
    parse_position,
    unwrap_list,
)

JSON = Dict[str, Any]

_ORDER_TYPES = ("MARKET", "LIMIT", "STOP")
_SIDES = ("BUY", "SELL")
_TIFS = ("GTC", "DAY", "GTD")

_LOG = logging.getLogger("dxtrade_wrapper")


def _opposite(side: str) -> str:
    return "SELL" if side.upper() == "BUY" else "BUY"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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


class DXTradeDashboardWrapper:
    """
    A client for the DXtrade REST API (token authentication) and Push API.

    Typical use::

        with DXTradeDashboardWrapper(base_url, username, password, "default") as dx:
            print(dx.get_balance())
            dx.place_order("EUR/USD", "BUY", 1000, "MARKET", stop_loss=1.05, take_profit=1.2)

    Spec-conformant but not verified against a live broker: see the module docstring.
    """

    #: Authorization scheme for token-authenticated REST calls (REST spec, "Token Authentication").
    AUTH_SCHEME = "DXAPI"

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        domain_or_vendor: str = "default",
        api_prefix: str = "/dxsca-web",
        login_path: str = "/login",
        websocket_path: str = "/websocket",
        account: Optional[str] = None,
        keepalive_interval: Optional[float] = 60.0,
        logger: Optional[logging.Logger] = None,
        timeout: float = 10.0,
        auto_relogin: bool = True,
        websocket_url: Optional[str] = None,
    ):
        """
        Args:
            base_url: The broker's DXtrade host, e.g. "https://dxtrade.broker.com".
            username: Login, or a full "login@domain" username.
            password: Password. Never logged.
            domain_or_vendor: The user's DXtrade domain (usually "default").
            api_prefix: Path prefix of every REST endpoint. "/dxsca-web" per the spec.
            login_path: Login path relative to `api_prefix`. A value that already
                includes the prefix (e.g. "/dxsca-web/login") is normalised.
            websocket_path: Path of the Push API websocket on the host (not under
                `api_prefix`). The Push spec leaves this URL to each deployment; ask
                your broker. Market data uses the same URL plus "/md".
            websocket_url: Full Push API URL; overrides `websocket_path`.
            account: Account code ("clearing:account"). If omitted it is looked up
                via GET /users/{username} on first use; if the user has several
                accounts you must choose one here.
            keepalive_interval: Seconds between POST /ping calls after login (capped
                at half the session timeout the server reports). None disables it.
            logger: Logger to use instead of the "dxtrade_wrapper" logger.
            timeout: Per-request HTTP timeout in seconds.
            auto_relogin: Log in again once and retry when a request gets 401
                because the session expired.
        """
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._domain_or_vendor = domain_or_vendor
        self._timeout = timeout
        self._auto_relogin = auto_relogin

        prefix = (api_prefix or "").strip("/")
        self._api_prefix = f"/{prefix}" if prefix else ""
        login_path = (login_path or "login").strip("/")
        if prefix and (login_path == prefix or login_path.startswith(f"{prefix}/")):
            login_path = login_path[len(prefix):].strip("/")
        self._login_path = login_path or "login"

        self._websocket_path = websocket_path.lstrip("/")
        self._websocket_url = websocket_url

        # A library must not configure logging; the package adds a NullHandler.
        self._logger = logger or _LOG

        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._auth_token: Optional[str] = None
        self._is_authenticated = False
        self._session_timeout: Optional[float] = None
        self._auth_lock = threading.RLock()

        self._configured_account = account
        self._account_id = account
        self._account_currency: Dict[str, str] = {}
        self._accounts_cache: Optional[List[JSON]] = None

        self._keepalive_interval = keepalive_interval
        self._keepalive_thread: Optional[threading.Thread] = None
        self._keepalive_stop = threading.Event()

        # Push API state
        self._channels: Dict[str, "_PushChannel"] = {}
        self._subscriptions: Dict[str, Tuple[str, str, JSON]] = {}
        self._subscribed_instruments: set = set()
        self._account_updates_subscribed = False
        self._request_ids = itertools.count(1)
        self._lock = threading.Lock()

        self.price_update_queue: "queue.Queue[JSON]" = queue.Queue()
        self.order_update_queue: "queue.Queue[JSON]" = queue.Queue()
        self.account_update_queue: "queue.Queue[JSON]" = queue.Queue()

    def __repr__(self) -> str:
        # Never include the password or the session token.
        return (
            f"{type(self).__name__}(base_url={self._base_url!r}, username={self._username!r}, "
            f"domain={self._domain_or_vendor!r}, account={self._account_id!r}, "
            f"authenticated={self._is_authenticated})"
        )

    def __enter__(self) -> "DXTradeDashboardWrapper":
        self.login()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.logout()

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
            raise ConnectionError(f"{context}: network error: {exc}") from exc

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
            ConnectionError: The server could not be reached.
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
                raise ConnectionError(f"Login: network error: {exc}") from exc

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
        """Closes websockets, stops the keepalive and POSTs /logout (best effort)."""
        self.disconnect_websocket()
        self._stop_keepalive()
        if self._is_authenticated:
            try:
                self._request("POST", "logout", "Logout", retry_auth=False)
            except Exception as exc:  # noqa: BLE001 - logout must always clear local state
                self._logger.warning("Logout request failed (ignored): %s", exc)
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

    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------

    def _full_username(self) -> str:
        if "@" in self._username:
            return self._username
        return f"{self._username}@{self._domain_or_vendor}"

    def get_accounts(self) -> List[JSON]:
        """GET /users/{login@domain}: the user's accounts.

        Returns a list of dicts with ``account`` (code), ``base_currency``,
        ``status`` and ``position_based``.
        """
        self._check_authenticated()
        response = self._request(
            "GET", f"users/{self._seg(self._full_username())}", "Get user accounts"
        )
        users = unwrap_list(self._json(response, "Get user accounts"), "userDetails")
        accounts = []
        for user in users:
            for details in user.get("accounts") or []:
                code = account_code(details.get("account"))
                accounts.append({
                    "account": code,
                    "base_currency": details.get("baseCurrency"),
                    "status": details.get("accountStatus"),
                    "position_based": details.get("positionBased", details.get("isPositionBased")),
                })
                if code and details.get("baseCurrency"):
                    self._account_currency[code] = details["baseCurrency"]
        self._accounts_cache = accounts
        return accounts

    def _get_account_id(self) -> str:
        """The account code for account-scoped calls, discovering it if needed.

        Never invents one: with zero or several accounts it raises and says how
        to choose.
        """
        if self._account_id:
            return self._account_id
        known = self._accounts_cache if self._accounts_cache is not None else self.get_accounts()
        accounts = [a["account"] for a in known if a["account"]]
        if len(accounts) == 1:
            self._account_id = accounts[0]
            self._logger.info("Using account %s", self._account_id)
            return self._account_id
        if not accounts:
            raise DXTradeAPIError(
                "No account code available: the user has no accounts. Pass account=... "
                "(DXTRADE_ACCOUNT) with the code shown in your broker's web terminal."
            )
        raise DXTradeAPIError(
            f"User has {len(accounts)} accounts ({', '.join(accounts)}); pass account=... "
            "(DXTRADE_ACCOUNT) to choose one."
        )

    def _account_path(self, suffix: str) -> str:
        return f"accounts/{self._seg(self._get_account_id())}/{suffix}"

    # ------------------------------------------------------------------
    # Account data
    # ------------------------------------------------------------------

    def get_account_metrics(self, include_positions: bool = False) -> JSON:
        """GET /accounts/{account}/metrics: the raw AccountMetrics object."""
        self._check_authenticated()
        params = {"include-positions": "true"} if include_positions else None
        response = self._request("GET", self._account_path("metrics"), "Get account metrics",
                                 params=params)
        metrics = unwrap_list(self._json(response, "Get account metrics"), "metrics")
        account = self._get_account_id()
        for entry in metrics:
            if account_code(entry.get("account")) == account:
                return entry
        if len(metrics) == 1:
            return metrics[0]
        raise DXTradeAPIError(f"No metrics returned for account {account}")

    def get_balance(self) -> Balance:
        """Equity, balance, margin and P/L from GET /accounts/{account}/metrics."""
        metrics = self.get_account_metrics()
        return parse_balance(metrics, currency=self._account_currency.get(self._get_account_id()))

    def get_portfolio(self) -> JSON:
        """GET /accounts/{account}/portfolio: raw balances, positions and orders."""
        self._check_authenticated()
        response = self._request("GET", self._account_path("portfolio"), "Get portfolio")
        portfolios = unwrap_list(self._json(response, "Get portfolio"), "portfolios")
        if not portfolios:
            raise DXTradeAPIError("Portfolio response was empty")
        return portfolios[0]

    def get_positions(self, include_pnl: bool = False) -> List[Position]:
        """Open positions from GET /accounts/{account}/positions.

        Args:
            include_pnl: Also fetch per-position metrics and fill ``Position.pnl``
                with the floating P/L (``fpl``, account currency). One extra request.
        """
        self._check_authenticated()
        response = self._request("GET", self._account_path("positions"), "Get positions")
        positions = [parse_position(p)
                     for p in unwrap_list(self._json(response, "Get positions"), "positions")]
        if include_pnl and positions:
            metrics = self.get_account_metrics(include_positions=True)
            fpl = {str(m.get("positionCode")): m.get("fpl")
                   for m in metrics.get("positions") or [] if isinstance(m, Mapping)}
            for position in positions:
                value = fpl.get(position.position_id)
                if value is not None:
                    position.pnl = float(value)
        return positions

    def _open_orders_raw(self) -> Tuple[List[JSON], Optional[str]]:
        """Open orders plus the ETag the server sent with them (for If-Match)."""
        self._check_authenticated()
        response = self._request("GET", self._account_path("orders"), "Get orders")
        orders = unwrap_list(self._json(response, "Get orders"), "orders")
        return orders, response.headers.get("ETag")

    def get_orders(self) -> List[Order]:
        """Working orders from GET /accounts/{account}/orders."""
        orders, _ = self._open_orders_raw()
        return [parse_order(o) for o in orders]

    def get_order_history(self, limit: int = 100, **filters: Any) -> List[JSON]:
        """GET /accounts/{account}/orders/history.

        Args:
            limit: Maximum orders to return (the server may cap it).
            **filters: Any documented query filter, with underscores for dashes:
                ``in_status="COMPLETED"``, ``period="today"``, ``for_instrument="EUR/USD"``,
                ``transaction_to=...`` (for paging via nextPageTransactionTime).

        Returns:
            The raw ``Order`` dicts, most recent first.
        """
        self._check_authenticated()
        params = {"limit": limit}
        params.update({k.replace("_", "-"): v for k, v in filters.items()})
        response = self._request("GET", self._account_path("orders/history"),
                                 "Get order history", params=params)
        return unwrap_list(self._json(response, "Get order history"), "orders")

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def _generate_order_code(self) -> str:
        """Client order id: unique per account, <= 64 chars of the allowed set."""
        return f"dxpw-{uuid.uuid4().hex[:24]}"

    def _post_order(self, body: JSON, order_code: str, context: str) -> Any:
        """POSTs an order or order group, raising OrderPlacementError on failure."""
        path = self._account_path("orders")
        self._logger.debug("%s: %s", context, body)
        try:
            response = self._request("POST", path, context, json_body=body)
        except ConnectionError as exc:
            cause = exc.__cause__
            sent = not isinstance(cause, requests.exceptions.ConnectTimeout)
            raise OrderPlacementError(
                f"{context}: {exc}. " + (
                    f"The order may have been placed: check for orderCode {order_code!r} "
                    "before retrying, or resend with the same orderCode."
                    if sent else "The request was not sent."
                ),
                order_code=order_code, ambiguous=sent,
            ) from exc
        except DXTradeAPIError as exc:
            if isinstance(exc, AuthenticationError) and not self._is_authenticated:
                raise
            # A 5xx (often a gateway timeout) does not say whether the order reached
            # the matching engine, so the outcome is unknown, like a read timeout.
            unknown = isinstance(exc, ServerError)
            message = str(exc)
            if unknown:
                message += (f". The order may have been placed: check for orderCode "
                            f"{order_code!r} before retrying, or resend with the same orderCode.")
            raise OrderPlacementError(
                message, order_code=order_code, ambiguous=unknown, status_code=exc.status_code,
                error_code=exc.error_code, description=exc.description, body=exc.body,
            ) from exc
        return self._json(response, context)

    def place_order(
        self,
        instrument: str,
        side: str,
        quantity: float,
        order_type: str,
        price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        position_effect: str = "OPEN",
        tif: str = "GTC",
        **kwargs: Any,
    ) -> Any:
        """Places an order; with stop_loss / take_profit, atomically.

        Args:
            instrument: Symbol, e.g. "EUR/USD".
            side: "BUY" or "SELL".
            quantity: Units (not lots).
            order_type: "MARKET", "LIMIT" or "STOP".
            price: Limit price (LIMIT) or stop price (STOP).
            stop_loss, take_profit: Protection prices. They are sent in the same
                request as an IF-THEN order group, so there is no window where the
                entry is live and its protections have not been sent yet. If the
                request is rejected, none of it is placed. The protections only
                become active when the entry fills; the spec does not say how a
                protection rejected at that point (e.g. a stop already through the
                market) is reported, so check get_orders() after placing. If the
                server acknowledges fewer orders than were sent, this raises an
                ambiguous OrderPlacementError. The spec allows order groups only on
                position-based accounts; elsewhere the server rejects the request.
            position_effect: "OPEN" or "CLOSE" (CLOSE needs positionCode=...).
            tif: "GTC", "DAY" or "GTD" (GTD needs expireDate=...; MARKET only GTC).
            **kwargs: Extra SingleOrderRequest fields (positionCode, expireDate,
                metadata, ...). ``orderCode=`` supplies your own idempotency key.

        Returns:
            The server response: ``{"orderId", "updateOrderId"}`` for one order, or
            ``{"orderResponses": [...]}`` for an order group.

        Raises:
            ValueError: Invalid arguments (nothing is sent).
            OrderPlacementError: The server rejected it, or the outcome is unknown
                (see ``OrderPlacementError.ambiguous``).
        """
        self._check_authenticated()
        side, order_type = side.upper(), order_type.upper()
        position_effect, tif = position_effect.upper(), tif.upper()
        if side not in _SIDES:
            raise ValueError("Side must be 'BUY' or 'SELL'")
        if order_type not in _ORDER_TYPES:
            raise ValueError("Order type must be 'MARKET', 'LIMIT', or 'STOP'")
        if tif not in _TIFS:
            raise ValueError("tif must be 'GTC', 'DAY' or 'GTD'")
        if order_type == "MARKET" and tif != "GTC":
            raise ValueError("MARKET orders only support tif='GTC'")
        if tif == "GTD" and not kwargs.get("expireDate"):
            raise ValueError("tif='GTD' requires expireDate=...")
        if order_type in ("LIMIT", "STOP") and price is None:
            raise ValueError(f"Price is required for {order_type} orders")
        if position_effect not in ("OPEN", "CLOSE"):
            raise ValueError("position_effect must be 'OPEN' or 'CLOSE'")
        if position_effect == "CLOSE" and not kwargs.get("positionCode"):
            raise ValueError("position_effect='CLOSE' requires positionCode=...")
        protected = stop_loss is not None or take_profit is not None
        if protected and position_effect != "OPEN":
            raise ValueError(
                "stop_loss/take_profit can only be attached to an OPEN order; "
                "use modify_position_sl_tp() for an existing position"
            )
        if quantity is None or quantity <= 0:
            raise ValueError("quantity must be positive")

        account = self._get_account_id()
        order_code = kwargs.pop("orderCode", None) or self._generate_order_code()
        primary: JSON = {
            "account": account,
            "orderCode": order_code,
            "type": order_type,
            "instrument": instrument,
            "quantity": quantity,
            "positionEffect": position_effect,
            "side": side,
            "tif": tif,
        }
        if order_type == "LIMIT":
            primary["limitPrice"] = price
        elif order_type == "STOP":
            primary["stopPrice"] = price
        primary.update(kwargs)

        if not protected:
            return self._post_order(primary, order_code, "Place order")

        # IF-THEN group (REST spec, "Order Group Request"): THEN orders close the
        # position the IF order opens, have the opposite side, no quantity and GTC.
        group_orders = [primary]
        for kind, value in (("STOP", stop_loss), ("LIMIT", take_profit)):
            if value is None:
                continue
            leg: JSON = {
                "account": account,
                "orderCode": self._generate_order_code(),
                "type": kind,
                "instrument": instrument,
                "quantity": 0,
                "positionEffect": "CLOSE",
                "side": _opposite(side),
                "tif": "GTC",
                ("stopPrice" if kind == "STOP" else "limitPrice"): value,
            }
            group_orders.append(leg)
        body = {"orders": group_orders, "contingencyType": "IF-THEN"}
        result = self._post_order(body, order_code, "Place order with protection")
        responses = result.get("orderResponses") if isinstance(result, Mapping) else result
        if isinstance(responses, list) and len(responses) < len(group_orders):
            # The spec answers a group with one OrderResponse per order. Fewer means
            # a protection may be missing while the entry stands: say so loudly.
            raise OrderPlacementError(
                f"Place order with protection: sent {len(group_orders)} orders but the "
                f"server acknowledged {len(responses)}. The entry {order_code!r} may be "
                "working or filled WITHOUT its stop loss / take profit: check get_orders() "
                "and get_positions() now.",
                order_code=order_code, ambiguous=True, body=result,
            )
        return result

    def _find_open_order(self, orders: List[JSON], order_id: str) -> JSON:
        for order in orders:
            ids = {str(order.get("orderCode")), str(order.get("clientOrderId")),
                   str(order.get("orderId"))}
            if str(order_id) in ids:
                return order
        raise NotFoundError(f"No working order {order_id!r} on account {self._get_account_id()}")

    def _replace_request(
        self,
        order: Mapping[str, Any],
        price: Optional[float] = None,
        quantity: Optional[float] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> JSON:
        """A complete SingleOrderRequest for PUT (Modify Order).

        The spec requires the whole order to be resubmitted and the ``type`` to be
        omitted from replace requests.
        """
        leg = order_leg(order)
        body: JSON = {
            "account": account_code(order.get("account")) or self._get_account_id(),
            "orderCode": order.get("clientOrderId") or order.get("orderCode"),
            "instrument": order.get("instrument") or leg.get("instrument"),
            "positionEffect": leg.get("positionEffect"),
            "side": order.get("side") or leg.get("side"),
            "tif": order.get("tif"),
        }
        # The Open Orders listing gives OPEN legs a positionCode too (the order's
        # own id), but the spec says it "must be omitted" unless the effect is CLOSE.
        if leg.get("positionEffect") == "CLOSE" and leg.get("positionCode"):
            body["positionCode"] = leg["positionCode"]
        if order.get("expireDate"):
            body["expireDate"] = order["expireDate"]
        attached = leg.get("positionEffect") == "CLOSE" and leg.get("positionCode")
        if quantity is not None:
            body["quantity"] = quantity
        elif not attached:
            body["quantity"] = leg.get("quantity")
        order_type = str(order.get("type", "")).upper()
        current_price = leg.get("price")
        if order_type == "LIMIT":
            body["limitPrice"] = price if price is not None else current_price
        elif order_type == "STOP":
            body["stopPrice"] = price if price is not None else current_price
        elif price is not None:
            raise ValueError("A MARKET order has no price to modify")
        if extra:
            body.update(extra)
        return {k: v for k, v in body.items() if v is not None}

    def _conditional(
        self,
        method: str,
        path: str,
        context: str,
        etag: Optional[str],
        build: Optional[Callable[[List[JSON]], JSON]] = None,
        prefetched: Optional[Tuple[List[JSON], Optional[str]]] = None,
    ) -> Any:
        """PUT/DELETE with If-Match; on 412 re-reads the orders and retries once.

        ``build(orders)`` makes the request body from the current open orders. It
        runs again on the retry, so a 412 never resends a body built from the
        stale state the server just refused. ``etag`` is a caller-supplied value
        (never retried: the caller asked for that exact version).
        """
        auto = etag is None
        for attempt in (1, 2):
            tag, body = etag, None
            if auto or build is not None:
                orders, fetched = prefetched or self._open_orders_raw()
                prefetched = None
                tag = etag or fetched
                if build is not None:
                    body = build(orders)
            if not tag:
                raise DXTradeAPIError(
                    f"{context}: the server sent no ETag with the order list, and the spec "
                    "requires If-Match for this request. Pass etag=... explicitly."
                )
            try:
                response = self._request(method, path, context, json_body=body,
                                         headers={"If-Match": tag})
                return self._json(response, context)
            except PreconditionFailedError:
                if not auto or attempt == 2:
                    raise
                self._logger.info("%s: orders changed, re-reading and retrying", context)
        return None  # pragma: no cover

    def _find_linked(self, orders: List[JSON], link: Mapping[str, Any]) -> Optional[JSON]:
        wanted = {str(link.get("linkedOrder")), str(link.get("linkedClientOrderId"))} - {"None"}
        for order in orders:
            if {str(order.get("orderCode")), str(order.get("clientOrderId"))} & wanted:
                return order
        return None

    def _working_group(self, orders: List[JSON], order: Mapping[str, Any]) -> Optional[List[JSON]]:
        """The working IF-THEN group ``order`` belongs to, parent first, or None.

        Per the spec's Modify Order example 1, a single-order PUT for the parent of
        an IF-THEN group turns the group into a single order: its stop loss and take
        profit disappear. So group members are always modified as a whole group.
        """
        links = order.get("links") or []
        if any(link.get("linkType") == "OCO" for link in links):
            raise DXTradeAPIError(
                "Order is part of an OCO group; modifying OCO groups is not supported "
                "(a single-order PUT would break the group). Nothing was sent."
            )
        parent: Optional[Mapping[str, Any]] = order
        if not any(link.get("linkType") == "CHILD" for link in links):
            parent_link = next((lk for lk in links if lk.get("linkType") == "PARENT"), None)
            # A filled parent is no longer a working order: the children are then
            # plain protections on the position and are modified on their own.
            parent = self._find_linked(orders, parent_link) if parent_link else None
        if parent is None:
            return None
        group = [dict(parent)]
        for link in parent.get("links") or []:
            if link.get("linkType") != "CHILD":
                continue
            child = self._find_linked(orders, link)
            if child is None:
                raise DXTradeAPIError(
                    f"Order group of {parent.get('clientOrderId') or parent.get('orderCode')!r} "
                    f"lists child {link.get('linkedClientOrderId') or link.get('linkedOrder')!r}, "
                    "which is not a working order; refusing to send a group PUT that would "
                    "drop it. Nothing was sent."
                )
            group.append(child)
        return group

    def _modify_body(
        self,
        orders: List[JSON],
        order: Mapping[str, Any],
        price: Optional[float] = None,
        quantity: Optional[float] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> JSON:
        """The PUT body changing ``order``: a single order, or its whole IF-THEN group
        (REST spec, Modify Order example 2b) with only ``order`` changed."""
        group = self._working_group(orders, order)
        if group is None:
            return self._replace_request(order, price, quantity, extra)
        target = str(order.get("orderCode"))
        members = []
        for index, member in enumerate(group):
            if str(member.get("orderCode")) == target:
                body = self._replace_request(member, price, quantity, extra)
            else:
                body = self._replace_request(member)
            if index > 0:
                # THEN orders as in the spec's group examples: zero quantity, no positionCode.
                body.pop("positionCode", None)
                body["quantity"] = 0
            members.append(body)
        return {"orders": members, "contingencyType": "IF-THEN"}

    def modify_order(
        self,
        order_id: str,
        new_price: Optional[float] = None,
        new_quantity: Optional[float] = None,
        etag: Optional[str] = None,
        **kwargs: Any,
    ) -> Any:
        """Modifies a working order: PUT /accounts/{account}/orders with If-Match.

        The current order is read first, because the spec requires the complete
        order in a modify request; only the given fields change.

        Args:
            order_id: Server orderCode, client orderCode, or numeric orderId.
            new_price: New limit/stop price.
            new_quantity: New quantity in units.
            etag: If-Match value; read from GET orders when omitted.
            **kwargs: Other SingleOrderRequest fields to change (e.g. tif, expireDate).
        """
        if new_price is None and new_quantity is None and not kwargs:
            raise ValueError("At least one parameter to modify must be provided")
        prefetched = self._open_orders_raw()
        self._find_open_order(prefetched[0], order_id)  # fail fast on an unknown id

        def build(orders: List[JSON]) -> JSON:
            order = self._find_open_order(orders, order_id)
            return self._modify_body(orders, order, new_price, new_quantity, kwargs)

        return self._conditional("PUT", self._account_path("orders"), "Modify order",
                                 etag, build, prefetched=prefetched)

    def cancel_order(self, order_id: str, etag: Optional[str] = None) -> Any:
        """Cancels a working order: DELETE /accounts/{account}/orders/{code} with If-Match."""
        self._check_authenticated()
        path = self._account_path(f"orders/{self._seg(order_id)}")
        result = self._conditional("DELETE", path, "Cancel order", etag)
        return result if result is not None else {"success": True, "order_id": order_id}

    def _find_position(self, position_id: str) -> Position:
        for position in self.get_positions():
            if position.position_id == str(position_id):
                return position
        raise NotFoundError(f"No open position {position_id!r}")

    @staticmethod
    def _find_protection(orders: List[JSON], kind: str, position_code: str) -> Optional[JSON]:
        return next(
            (o for o in orders
             if str(o.get("type", "")).upper() == kind
             and order_leg(o).get("positionCode") == position_code
             and order_leg(o).get("positionEffect") == "CLOSE"),
            None,
        )

    def close_position(self, position_id: str, quantity: Optional[float] = None) -> Any:
        """Closes a position fully or partially with a MARKET order linked to it.

        DXtrade has no DELETE-position endpoint: closing is a MARKET order with
        positionEffect=CLOSE and the position's code, on the opposite side.
        """
        self._check_authenticated()
        position = self._find_position(position_id)
        qty = position.quantity if quantity is None else quantity
        if qty <= 0 or qty > position.quantity:
            raise ValueError(f"quantity must be in (0, {position.quantity}]")
        return self.place_order(
            instrument=position.instrument,
            side=_opposite(position.side),
            quantity=qty,
            order_type="MARKET",
            position_effect="CLOSE",
            positionCode=position.position_id,
        )

    def close_all(
        self,
        instrument: Optional[str] = None,
        close_positions: bool = True,
        cancel_orders: bool = True,
        comment: Optional[str] = None,
    ) -> Any:
        """Bulk close: POST /accounts/{account}/close.

        Flattens the account (or one instrument) in a single request, e.g. before
        a news event or a prop-firm cut-off.
        """
        self._check_authenticated()
        if not (close_positions or cancel_orders):
            raise ValueError("Set close_positions and/or cancel_orders")
        body: JSON = {"closePositions": close_positions, "cancelOrders": cancel_orders}
        if instrument:
            body["instrument"] = instrument
        if comment:
            body["comment"] = comment
        response = self._request("POST", self._account_path("close"), "Bulk close",
                                 json_body=body)
        return self._json(response, "Bulk close")

    def modify_position_sl_tp(
        self,
        position_id: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> JSON:
        """Sets or moves a position's stop loss and/or take profit.

        Per the spec, protections are CLOSE orders linked by positionCode: an
        existing STOP (SL) or LIMIT (TP) is modified via PUT, a missing one is
        placed via POST.

        Returns:
            ``{"stop_loss": response, "take_profit": response}`` for what changed.
        """
        self._check_authenticated()
        if stop_loss is None and take_profit is None:
            raise ValueError("At least one of stop_loss or take_profit must be provided")
        position = self._find_position(position_id)
        results: JSON = {}
        for key, kind, value in (("stop_loss", "STOP", stop_loss),
                                 ("take_profit", "LIMIT", take_profit)):
            if value is None:
                continue
            prefetched = self._open_orders_raw()
            existing = self._find_protection(prefetched[0], kind, position.position_id)
            if existing is not None:
                code = str(existing.get("orderCode"))

                def build(orders: List[JSON], code: str = code, value: float = value) -> JSON:
                    current = self._find_open_order(orders, code)
                    return self._modify_body(orders, current, price=value)

                results[key] = self._conditional(
                    "PUT", self._account_path("orders"), f"Modify {key}", None, build,
                    prefetched=prefetched,
                )
            else:
                order_code = self._generate_order_code()
                body = {
                    "account": self._get_account_id(),
                    "orderCode": order_code,
                    "type": kind,
                    "instrument": position.instrument,
                    "positionEffect": "CLOSE",
                    "positionCode": position.position_id,
                    "side": _opposite(position.side),
                    "tif": "GTC",
                    ("stopPrice" if kind == "STOP" else "limitPrice"): value,
                }
                results[key] = self._post_order(body, order_code, f"Place {key}")
        return results

    # ------------------------------------------------------------------
    # Push API (WebSocket)
    # ------------------------------------------------------------------

    def _build_websocket_url(self, market_data: bool = False) -> str:
        """Push API URL. The session token is NOT in the URL: per the Push spec it
        travels in each message's ``session`` field (and URLs end up in logs)."""
        if self._websocket_url:
            base = self._websocket_url.split("?")[0].rstrip("/")
        else:
            scheme = "wss://" if self._base_url.startswith("https://") else "ws://"
            host = self._base_url.split("://", 1)[1]
            base = f"{scheme}{host}/{self._websocket_path}".rstrip("/")
        if market_data:
            base += "/md"
        return f"{base}?{urlencode({'format': 'JSON'})}"

    def _build_websocket_headers(self) -> List[str]:
        return [f"User-Agent: dxtrade-wrapper/{_version()}"]

    def _envelope(self, msg_type: str, payload: Optional[JSON] = None,
                  request_id: Optional[str] = None, **extra: Any) -> JSON:
        message: JSON = {
            "type": msg_type,
            "requestId": request_id or f"dxpw-{next(self._request_ids)}",
            "timestamp": _utc_now(),
            "session": self._auth_token,
        }
        if payload is not None:
            message["payload"] = payload
        message.update(extra)
        return message

    def connect_websocket(self) -> None:
        """Opens the Push API business-events and market-data websockets.

        Each channel reconnects with exponential backoff and replays its
        subscriptions after reconnecting.
        """
        self._check_authenticated()
        with self._lock:
            for name, md in (("events", False), ("md", True)):
                channel = self._channels.get(name)
                if channel is None or not channel.running:
                    channel = _PushChannel(self, name, self._build_websocket_url(market_data=md))
                    self._channels[name] = channel
                    channel.start()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not self._ws_connected:
            time.sleep(0.05)
        if not self._ws_connected:
            self._logger.warning("Push API connection not established yet; still retrying")

    @property
    def _ws_connected(self) -> bool:
        channel = self._channels.get("events")
        return bool(channel and channel.connected)

    def disconnect_websocket(self) -> None:
        with self._lock:
            channels, self._channels = list(self._channels.values()), {}
        for channel in channels:
            channel.stop()
        self._subscriptions.clear()
        self._subscribed_instruments = set()
        self._account_updates_subscribed = False

    def _subscribe(self, key: str, channel: str, msg_type: str, payload: JSON) -> None:
        self._subscriptions[key] = (channel, msg_type, payload)
        chan = self._channels.get(channel)
        if chan is None:
            raise WebSocketError("WebSocket not connected. Call connect_websocket() first.")
        if chan.connected:
            chan.send_subscription(key)

    def _unsubscribe(self, key: str, close_type: str) -> None:
        entry = self._subscriptions.pop(key, None)
        if entry is None:
            return
        chan = self._channels.get(entry[0])
        request_id = chan.request_ids.pop(key, None) if chan else None
        if chan and chan.connected and request_id:
            chan.send(self._envelope(close_type, refRequestId=request_id))

    def subscribe_market_data(self, instruments: List[str]) -> None:
        """Quotes for the given symbols (MarketDataSubscriptionRequest on /md).

        Each quote arrives on ``price_update_queue`` as
        ``{"type": "price_update", "instrument", "bid", "ask", "spread", "time"}``.
        """
        if not self._channels:
            raise WebSocketError("WebSocket not connected. Call connect_websocket() first.")
        for symbol in instruments:
            if symbol in self._subscribed_instruments:
                continue
            payload: JSON = {"symbols": [symbol],
                             "eventTypes": [{"type": "Quote", "format": "COMPACT"}]}
            if self._account_id:
                payload["account"] = self._account_id
            self._subscribe(f"md:{symbol}", "md", "MarketDataSubscriptionRequest", payload)
            self._subscribed_instruments.add(symbol)

    def unsubscribe_market_data(self, instruments: List[str]) -> None:
        if not self._channels:
            raise WebSocketError("WebSocket not connected. Call connect_websocket() first.")
        for symbol in instruments:
            if symbol in self._subscribed_instruments:
                self._unsubscribe(f"md:{symbol}", "MarketDataCloseSubscriptionRequest")
                self._subscribed_instruments.discard(symbol)

    def subscribe_account_updates(self) -> None:
        """Portfolio and metrics updates for the account.

        ``AccountPortfolios`` messages put each order on ``order_update_queue`` and
        the portfolio on ``account_update_queue``; ``AccountMetrics`` messages put
        equity/margin updates on ``account_update_queue``.
        """
        if not self._channels:
            raise WebSocketError("WebSocket not connected. Call connect_websocket() first.")
        if self._account_updates_subscribed:
            return
        accounts = [self._get_account_id()]
        self._subscribe("portfolio", "events", "AccountPortfoliosSubscriptionRequest",
                        {"requestType": "LIST", "accounts": accounts})
        self._subscribe("metrics", "events", "AccountMetricsSubscriptionRequest",
                        {"requestType": "LIST", "accounts": accounts, "includePositions": "true"})
        self._account_updates_subscribed = True

    def _handle_push_message(self, channel: "_PushChannel", raw: str) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            self._logger.error("Push API sent non-JSON data (%d bytes)", len(raw))
            return
        msg_type = message.get("type")
        payload = message.get("payload") or {}
        if msg_type == "PingRequest":
            # The server closes the session if it gets no Ping back.
            channel.send({"type": "Ping", "timestamp": _utc_now(), "session": self._auth_token})
        elif msg_type == "MarketData":
            for event in payload.get("events") or []:
                if event.get("type", "Quote") != "Quote":
                    continue
                update: JSON = {"type": "price_update", "instrument": event.get("symbol"),
                                "time": event.get("time")}
                for field in ("bid", "ask"):
                    if event.get(field) is not None:
                        update[field] = float(event[field])
                if "bid" in update and "ask" in update:
                    update["spread"] = update["ask"] - update["bid"]
                self.price_update_queue.put(update)
        elif msg_type == "AccountPortfolios":
            for portfolio in payload.get("portfolios") or []:
                self.account_update_queue.put({"type": "portfolio", **portfolio})
                for order in portfolio.get("orders") or []:
                    try:
                        parsed = parse_order(order).__dict__
                    except DXTradeAPIError:
                        parsed = {}
                    self.order_update_queue.put({"type": "order_update", **parsed, "raw": order})
        elif msg_type == "AccountMetrics":
            for metrics in payload.get("metrics") or []:
                try:
                    balance = parse_balance(metrics).__dict__
                except DXTradeAPIError:
                    balance = {}
                self.account_update_queue.put({"type": "account_update", **balance,
                                               "raw": metrics})
        elif msg_type == "Reject":
            self._logger.error(
                "Push API rejected request %s: error %s %s", message.get("inReplyTo"),
                payload.get("errorCode"), payload.get("description"),
            )
            if str(payload.get("errorCode")) == "1" and self._auto_relogin:
                # Session expired: log in again and resubscribe on the new token.
                try:
                    self._relogin()
                    channel.resubscribe()
                except Exception as exc:  # noqa: BLE001
                    self._logger.error("Re-login after Push reject failed: %s", exc)
        else:
            self._logger.debug("Unhandled Push API message type %s", msg_type)


class _PushChannel:
    """One Push API websocket with reconnect/backoff and subscription replay."""

    def __init__(self, client: DXTradeDashboardWrapper, name: str, url: str):
        self.client = client
        self.name = name
        self.url = url
        self.connected = False
        self.running = False
        self.request_ids: Dict[str, str] = {}
        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        self.running = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"DXTradePush-{self.name}",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        self._stop.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001, S110 - closing a dead socket
                pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self.connected = False

    def _run(self) -> None:
        failures = 0
        log = self.client._logger
        while not self._stop.is_set():
            opened = threading.Event()

            def on_open(ws: websocket.WebSocketApp, opened: threading.Event = opened) -> None:
                opened.set()
                self._on_open()

            self._ws = websocket.WebSocketApp(
                self.url,
                header=self.client._build_websocket_headers(),
                on_open=on_open,
                on_message=lambda ws, msg: self.client._handle_push_message(self, msg),
                on_error=lambda ws, err: log.warning("Push %s error: %s", self.name, err),
                on_close=lambda ws, code, msg: self._on_close(code, msg),
            )
            log.info("Connecting Push API %s channel: %s", self.name, self.url)
            try:
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as exc:  # noqa: BLE001 - keep reconnecting
                log.error("Push %s channel crashed: %s", self.name, exc)
            self.connected = False
            if self._stop.is_set():
                break
            failures = 0 if opened.is_set() else failures + 1
            delay = min(2 ** failures, 30)
            log.warning("Push %s channel closed; reconnecting in %ss", self.name, delay)
            self._stop.wait(delay)

    def _on_open(self) -> None:
        self.connected = True
        self.client._logger.info("Push API %s channel connected", self.name)
        self.resubscribe()

    def _on_close(self, code: Any, msg: Any) -> None:
        self.connected = False
        self.client._logger.info("Push %s channel closed (%s %s)", self.name, code, msg)

    def resubscribe(self) -> None:
        for key, (channel, _, _) in list(self.client._subscriptions.items()):
            if channel == self.name:
                self.send_subscription(key)

    def send_subscription(self, key: str) -> None:
        channel, msg_type, payload = self.client._subscriptions[key]
        message = self.client._envelope(msg_type, payload)
        self.request_ids[key] = message["requestId"]
        self.send(message)

    def send(self, message: JSON) -> None:
        if self._ws is None:
            raise WebSocketError(f"Push {self.name} channel is not open")
        try:
            self._ws.send(json.dumps(message))
        except Exception as exc:  # noqa: BLE001
            raise WebSocketError(f"Failed to send {message.get('type')}: {exc}") from exc


def _version() -> str:
    from . import __version__

    return __version__
