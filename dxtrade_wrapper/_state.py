"""Attributes shared by the client's layers.

The client is built as a stack of layers (session, accounts, orders, push), one
module each, all operating on the same object. The attributes they share are
declared here so each layer type-checks on its own; `DXTradeClient.__init__`
assigns them.
"""

import itertools
import logging
import queue
import threading
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import requests

if TYPE_CHECKING:
    from ._push import _PushChannel

JSON = Dict[str, Any]

_LOG = logging.getLogger("dxtrade_wrapper")


class _ClientState:
    _base_url: str
    _username: str
    _password: str
    _domain_or_vendor: str
    _timeout: float
    _auto_relogin: bool
    _api_prefix: str
    _login_path: str
    _websocket_path: str
    _websocket_url: Optional[str]
    _logger: logging.Logger

    _session: requests.Session
    _auth_token: Optional[str]
    _is_authenticated: bool
    _session_timeout: Optional[float]
    _auth_lock: "threading.RLock"

    _configured_account: Optional[str]
    _account_id: Optional[str]
    _account_currency: Dict[str, str]
    _accounts_cache: Optional[List[JSON]]

    _keepalive_interval: Optional[float]
    _keepalive_thread: Optional[threading.Thread]
    _keepalive_stop: threading.Event

    _channels: Dict[str, "_PushChannel"]
    _subscriptions: Dict[str, Tuple[str, str, JSON]]
    _subscribed_instruments: set
    _account_updates_subscribed: bool
    _request_ids: "itertools.count[int]"
    _lock: threading.Lock

    price_update_queue: "queue.Queue[JSON]"
    order_update_queue: "queue.Queue[JSON]"
    account_update_queue: "queue.Queue[JSON]"
