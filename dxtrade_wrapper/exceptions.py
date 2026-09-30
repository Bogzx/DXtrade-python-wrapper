"""Exception hierarchy for dxtrade_wrapper.

Every error the client raises derives from :class:`DXTradeWrapperError`.
Errors that come back from the server derive from :class:`DXTradeAPIError` and
carry the HTTP status plus the DXtrade application error (``errorCode`` and
``description`` from the REST API's error body), so callers can branch on
``exc.error_code`` instead of parsing messages.

Messages never contain credentials or session tokens.
"""

from typing import Any, Optional


class DXTradeWrapperError(Exception):
    """Base exception for all DXTradeDashboardWrapper errors."""


class DXTradeAPIError(DXTradeWrapperError):
    """An error returned by (or about a response from) the DXtrade REST API.

    Attributes:
        status_code: HTTP status, or None when no response was involved.
        error_code: DXtrade ``errorCode`` from the error body, if any.
        description: DXtrade ``description`` from the error body, if any.
        body: The decoded error body (dict) or raw text, if any.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        error_code: Optional[str] = None,
        description: Optional[str] = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.description = description
        self.body = body


class AuthenticationError(DXTradeAPIError):
    """Not logged in, bad credentials (401), or API access not enabled (403)."""


class NotFoundError(DXTradeAPIError):
    """404. DXtrade also uses this for 'not permitted' (see the REST spec)."""


class ConflictError(DXTradeAPIError):
    """409: the request was understood but rejected by business rules."""


class PreconditionFailedError(DXTradeAPIError):
    """412: the If-Match ETag no longer matches; re-read and retry."""


class RateLimitError(DXTradeAPIError):
    """429: too many requests. ``retry_after`` is seconds, if the server said."""

    def __init__(self, message: str, *, retry_after: Optional[float] = None, **kwargs: Any):
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class ServerError(DXTradeAPIError):
    """5xx from the server."""


class OrderPlacementError(DXTradeAPIError):
    """Placing an order failed.

    Attributes:
        order_code: The client ``orderCode`` that was sent. When ``ambiguous`` is
            True (timeout / connection drop after sending) the order may or may
            not exist on the server: look it up by this code, or resend with the
            same ``orderCode`` - DXtrade rejects a duplicate code with 409 /
            error 100 rather than opening a second position.
        ambiguous: True when the outcome is unknown.
    """

    def __init__(
        self,
        message: str,
        *,
        order_code: Optional[str] = None,
        ambiguous: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.order_code = order_code
        self.ambiguous = ambiguous


class ConnectionError(DXTradeWrapperError):  # noqa: A001 - kept for backward compatibility
    """Transport failure: DNS, TCP, TLS or timeout. No server response."""


#: Alias that does not shadow the builtin ``ConnectionError``.
DXTradeConnectionError = ConnectionError


class WebSocketError(DXTradeWrapperError):
    """Push API (WebSocket) errors."""
