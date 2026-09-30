"""Python client for the DXtrade REST and Push APIs.

Spec-conformant (checked against Devexperts' public OpenAPI document), not yet
verified against a live broker. See the README before trading real money.
"""

import logging

from .client import DXTradeDashboardWrapper
from .exceptions import (
    AuthenticationError,
    ConflictError,
    ConnectionError,
    DXTradeAPIError,
    DXTradeConnectionError,
    DXTradeWrapperError,
    NotFoundError,
    OrderPlacementError,
    PreconditionFailedError,
    RateLimitError,
    ServerError,
    WebSocketError,
)
from .models import Balance, Order, Position

__version__ = "0.2.0"

#: Shorter alias for the client class.
DXTradeClient = DXTradeDashboardWrapper

# Libraries must not configure logging; applications opt in with logging.basicConfig().
logging.getLogger("dxtrade_wrapper").addHandler(logging.NullHandler())

__all__ = [
    "AuthenticationError",
    "Balance",
    "ConflictError",
    "ConnectionError",
    "DXTradeAPIError",
    "DXTradeClient",
    "DXTradeConnectionError",
    "DXTradeDashboardWrapper",
    "DXTradeWrapperError",
    "NotFoundError",
    "Order",
    "OrderPlacementError",
    "Position",
    "PreconditionFailedError",
    "RateLimitError",
    "ServerError",
    "WebSocketError",
    "__version__",
]
