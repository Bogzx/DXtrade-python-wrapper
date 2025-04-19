"""
Custom exceptions for the cTrader Trading SDK.

This module contains custom exception classes that provide more specific error information
when interacting with the cTrader Open API.
"""


class CTraderSDKException(Exception):
    """Base exception class for all SDK-related exceptions."""
    pass


class ConnectionError(CTraderSDKException):
    """Raised when there is an issue establishing or maintaining a connection."""
    pass


class AuthenticationError(CTraderSDKException):
    """Raised when authentication fails (app or account level)."""
    pass


class TokenError(AuthenticationError):
    """Raised for issues with OAuth tokens (expired, invalid, etc.)."""
    pass


class RequestError(CTraderSDKException):
    """Base class for request-related errors."""
    pass


class RateLimitError(RequestError):
    """Raised when the API rate limit is exceeded."""
    pass


class ValidationError(RequestError):
    """Raised when input validation fails."""
    pass


class OrderError(CTraderSDKException):
    """Raised when there's an issue with order operations."""
    pass


class PositionError(CTraderSDKException):
    """Raised when there's an issue with position operations."""
    pass


class SubscriptionError(CTraderSDKException):
    """Raised when there's an issue with market data subscriptions."""
    pass


class InsufficientFundsError(OrderError):
    """Raised when an account has insufficient funds for an operation."""
    pass


class InvalidSymbolError(RequestError):
    """Raised when an invalid symbol ID is provided."""
    pass


class ServerError(CTraderSDKException):
    """Raised for server-side errors reported by the API."""
    pass
