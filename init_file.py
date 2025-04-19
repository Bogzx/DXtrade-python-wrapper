"""
CTrader Trading SDK - A user-friendly Python SDK for the cTrader Open API v2.0.

This SDK simplifies interaction with the cTrader Open API by abstracting the
complexities of Protobuf messages, asynchronous operations, and authentication flows.
"""

__version__ = "0.1.0"

# Import main classes for direct import from package
from .sdk import CTraderTradingSDK
from .exceptions import (
    CTraderSDKException, ConnectionError, AuthenticationError,
    TokenError, RequestError, RateLimitError, ValidationError,
    OrderError, PositionError, SubscriptionError,
    InsufficientFundsError, InvalidSymbolError, ServerError
)

__all__ = [
    'CTraderTradingSDK',
    'CTraderSDKException',
    'ConnectionError',
    'AuthenticationError',
    'TokenError',
    'RequestError',
    'RateLimitError',
    'ValidationError',
    'OrderError',
    'PositionError',
    'SubscriptionError',
    'InsufficientFundsError',
    'InvalidSymbolError',
    'ServerError',
]
