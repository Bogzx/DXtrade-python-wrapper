"""
DXTradeClient: a Python client for the DXtrade REST and Push APIs.

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
import logging
import queue
import threading
from typing import Any, Dict, List, Optional, Tuple

import requests

from ._state import JSON, _LOG
from ._push import _PushChannel, _PushLayer
from ._session import parse_interval

# Still importable from here, as in the single-module client.
__all__ = ["DXTradeClient", "DXTradeDashboardWrapper", "parse_interval", "_PushChannel"]


class DXTradeClient(_PushLayer):
    """
    A client for the DXtrade REST API (token authentication) and Push API.

    Typical use::

        with DXTradeClient(base_url, username, password, "default") as dx:
            print(dx.get_balance())
            dx.place_order("EUR/USD", "BUY", 1000, "MARKET", stop_loss=1.05, take_profit=1.2)

    Spec-conformant but not verified against a live broker: see the module docstring.
    """

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

    def __enter__(self) -> "DXTradeClient":
        self.login()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.logout()


#: The 0.1 name of :class:`DXTradeClient`, kept as an alias.
DXTradeDashboardWrapper = DXTradeClient
