"""
CTrader Trading SDK for the cTrader Open API v2.0.

This module provides a user-friendly SDK class that wraps the official OpenApiPy
library, simplifying common trading tasks and abstracting the complexities of
the underlying API.
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

# Twisted imports
from twisted.internet import defer, reactor
from twisted.python import failure

# OpenApiPy imports
from ctrader_open_api import Auth, Client, EndPoints, Protobuf, TcpProtocol
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import (
    ProtoErrorRes, ProtoHeartbeatEvent, ProtoMessage
)
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthReq, ProtoOAAccountAuthRes,
    ProtoOAAmendOrderReq, ProtoOAAmendPositionSLTPReq,
    ProtoOAApplicationAuthReq, ProtoOAApplicationAuthRes,
    ProtoOACancelOrderReq, ProtoOAClosePositionReq,
    ProtoOAExecutionEvent, ProtoOAGetAccountListByAccessTokenReq, ProtoOAGetAccountListByAccessTokenRes,
    ProtoOAGetTrendbarsReq, ProtoOAGetTrendbarsRes,
    ProtoOAMarginChangedEvent, ProtoOANewOrderReq, ProtoOAOrderErrorEvent,
    ProtoOARefreshTokenReq, ProtoOARefreshTokenRes,
    ProtoOAReconcileReq, ProtoOAReconcileRes,
    ProtoOASpotEvent, ProtoOASubscribeSpotsReq, ProtoOASubscribeSpotsRes,
    ProtoOASymbolByIdReq, ProtoOASymbolByIdRes,
    ProtoOASymbolsListReq, ProtoOASymbolsListRes,
    ProtoOATraderReq, ProtoOATraderRes,
    ProtoOATraderUpdatedEvent, ProtoOAUnsubscribeSpotsReq, ProtoOAUnsubscribeSpotsRes
)

# Enums and models
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAOrder, ProtoOAPosition, ProtoOAExecutionType, 
    ProtoOAOrderType, ProtoOATradeSide
)

# Import custom exceptions
from .exceptions import (
    AuthenticationError, ConnectionError, InsufficientFundsError,
    InvalidSymbolError, OrderError, PositionError, RateLimitError,
    RequestError, ServerError, SubscriptionError, TokenError,
    ValidationError
)

# Configure logging
logger = logging.getLogger(__name__)


class CTraderTradingSDK:
    """
    A user-friendly SDK for interacting with the cTrader Open API v2.0.
    
    This class wraps the OpenApiPy client and provides simplified methods for
    common trading operations, abstracting the complexities of Protobuf messages,
    asynchronous operations, and authentication flows.
    
    Attributes:
        on_tick_data: Callback for price updates.
        on_order_update: Callback for order status changes.
        on_position_update: Callback for position updates.
        on_error: Callback for error events.
        on_disconnect: Callback for disconnection events.
        on_margin_change: Callback for margin level changes.
        reconnect_on_disconnect: Whether to automatically reconnect on disconnect.
    """
    
    # API payload type constants - add more as needed
    PROTO_MESSAGE = 0
    ERROR_RES = 50
    HEARTBEAT_EVENT = 51
    
    # Error codes mapping
    ERROR_CODE_MAP = {
        "REQUEST_FREQUENCY_EXCEEDED": RateLimitError,
        "MARKET_CLOSED": OrderError,
        "SYMBOL_NOT_FOUND": InvalidSymbolError,
        "UNAUTHORIZED": AuthenticationError,
        "ACCOUNT_NOT_AUTHORIZED": AuthenticationError,
        "TOKEN_EXPIRED": TokenError,
        "INSUFFICIENT_FUNDS": InsufficientFundsError,
        "INTERNAL_SERVER_ERROR": ServerError,
    }
    
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str):
        """
        Initialize the CTrader Trading SDK.
        
        Args:
            client_id: The application's client ID from cTrader.
            client_secret: The application's client secret from cTrader.
            redirect_uri: The registered redirect URI for OAuth flow.
        """
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._auth = Auth(client_id, client_secret, redirect_uri)
        self._client = None
        self._access_token = None
        self._refresh_token = None
        self._token_expiry_time = None
        self._ctid_trader_account_id = None
        self._host_type = "demo"  # Default to demo

        # Internal state flags
        self._is_connecting = False
        self._is_connected = False
        self._is_app_authorized = False
        self._is_account_authorized = False
        self._reconnection_attempts = 0
        self._max_reconnection_attempts = 5
        self._reconnection_delay = 1  # Initial delay in seconds (will use exponential backoff)

        # Internal data caches
        self._spot_subscriptions: Set[int] = set()  # Set of subscribed symbol IDs
        self._positions: Dict[int, Any] = {}  # Cache: {position_id: ProtoOAPosition object}
        self._orders: Dict[int, Any] = {}  # Cache: {order_id: ProtoOAOrder object}
        self._symbols: Dict[int, Any] = {}  # Cache: {symbol_id: ProtoOALightSymbol or ProtoOASymbol}
        self._assets: Dict[int, Any] = {}  # Cache: {asset_id: ProtoOAAsset}
        self._trader_info = None  # Cache for trader info
        self._price_cache: Dict[int, Dict[str, float]] = {}  # Cache: {symbol_id: {"bid": x, "ask": y, "timestamp": z}}

        # For message correlation
        self._pending_requests: Dict[str, asyncio.Future] = {}

        # User-definable callbacks
        self.on_tick_data: Optional[Callable[[int, float, float, int], None]] = None
        self.on_order_update: Optional[Callable[[Any], None]] = None
        self.on_position_update: Optional[Callable[[Any], None]] = None
        self.on_error: Optional[Callable[[str, str], None]] = None
        self.on_disconnect: Optional[Callable[[str], None]] = None
        self.on_margin_change: Optional[Callable[[Any], None]] = None
        
        # Configuration options
        self.reconnect_on_disconnect = True
        self.heartbeat_interval = 10  # seconds
        self._heartbeat_timer = None

    # --- Connection & Lifecycle ---
    
    async def connect(self, host_type: str = "demo") -> bool:
        """
        Connect to the cTrader Open API server.
        
        Args:
            host_type: Either "demo" or "live" to specify environment.
            
        Returns:
            True if connection was successful, False otherwise.
            
        Raises:
            ConnectionError: If connection fails or is already in progress.
            ValueError: If host_type is invalid.
        """
        if self._is_connecting:
            raise ConnectionError("Connection already in progress")
            
        if self._is_connected:
            logger.info("Already connected to cTrader API")
            return True
            
        if host_type not in ["demo", "live"]:
            raise ValueError("host_type must be either 'demo' or 'live'")
            
        self._host_type = host_type
        self._is_connecting = True
        
        try:
            # Determine the host based on type
            if host_type == "demo":
                host = EndPoints.PROTOBUF_DEMO_HOST
            else:
                host = EndPoints.PROTOBUF_LIVE_HOST
                
            port = EndPoints.PROTOBUF_PORT
            protocol = TcpProtocol
            
            # Create client if it doesn't exist
            if self._client is None:
                self._client = Client(host, port, protocol)
                
                # Set up client callbacks
                self._client.setConnectedCallback(self._internal_on_connect)
                self._client.setDisconnectedCallback(self._internal_on_disconnect)
                self._client.setMessageReceivedCallback(self._internal_on_message)
                self._client.setOnErrorCallback(self._internal_on_error)
            
            # Create a future to wait for connection
            connection_future = asyncio.Future()
            self._pending_requests["connection"] = connection_future
            
            # Start the client service (initiates connection)
            logger.info(f"Connecting to cTrader {host_type} API at {host}:{port}")
            self._client.startService()
            
            # Wait for connection (handled by _internal_on_connect callback)
            await connection_future
            
            logger.info("Successfully connected to cTrader API")
            return True
            
        except Exception as e:
            logger.error(f"Connection error: {str(e)}")
            self._is_connecting = False
            raise ConnectionError(f"Failed to connect: {str(e)}") from e
            
        finally:
            self._is_connecting = False

    async def disconnect(self) -> bool:
        """
        Disconnect from the cTrader Open API server.
        
        Returns:
            True if disconnection was successful, False otherwise.
        """
        if not self._is_connected or self._client is None:
            logger.info("Not connected to cTrader API")
            return True
            
        try:
            # Stop heartbeat timer if active
            self._stop_heartbeat()
            
            # Stop the client service
            logger.info("Disconnecting from cTrader API")
            self._client.stopService()
            
            # Reset state flags
            self._is_connected = False
            self._is_app_authorized = False
            self._is_account_authorized = False
            
            # Clear subscriptions
            self._spot_subscriptions.clear()
            
            logger.info("Successfully disconnected from cTrader API")
            return True
            
        except Exception as e:
            logger.error(f"Disconnection error: {str(e)}")
            return False

    def _internal_on_connect(self, client):
        """
        Internal callback for successful connection.
        
        Args:
            client: The OpenApiPy client instance.
        """
        logger.info("Connection established to cTrader API")
        self._is_connected = True
        self._reconnection_attempts = 0
        
        # Resolve the connection future if exists
        if "connection" in self._pending_requests:
            future = self._pending_requests.pop("connection")
            if not future.done():
                future.set_result(True)
        
        # Start sending heartbeats
        self._start_heartbeat()
        
        # If we have credentials, automatically authenticate
        if self._access_token:
            asyncio.create_task(self._authorize_application())

    def _internal_on_disconnect(self, client, reason):
        """
        Internal callback for disconnection.
        
        Args:
            client: The OpenApiPy client instance.
            reason: The reason for disconnection.
        """
        logger.info(f"Disconnected from cTrader API: {reason}")
        self._is_connected = False
        self._is_app_authorized = False
        self._is_account_authorized = False
        
        # Stop heartbeat timer
        self._stop_heartbeat()
        
        # Call user's disconnect callback if set
        if self.on_disconnect:
            try:
                self.on_disconnect(str(reason))
            except Exception as e:
                logger.error(f"Error in user disconnect callback: {str(e)}")
        
        # Handle automatic reconnection if enabled
        if self.reconnect_on_disconnect and self._reconnection_attempts < self._max_reconnection_attempts:
            self._reconnection_attempts += 1
            delay = self._reconnection_delay * (2 ** (self._reconnection_attempts - 1))  # Exponential backoff
            logger.info(f"Attempting reconnection in {delay} seconds (attempt {self._reconnection_attempts})")
            
            # Schedule reconnection
            reactor.callLater(delay, self._attempt_reconnection)

    def _attempt_reconnection(self):
        """Schedule reconnection attempt using asyncio."""
        asyncio.create_task(self._reconnect())

    async def _reconnect(self):
        """Attempt to reconnect to the API server."""
        try:
            await self.connect(self._host_type)
            
            # If we were previously authenticated, try to re-authenticate
            if self._access_token:
                await self._authorize_application()
                
                # If we had an active account, re-authorize it
                if self._ctid_trader_account_id:
                    await self._authorize_account()
                    
                    # Resubscribe to previously subscribed symbols
                    await self._resubscribe_to_spots()
                    
        except Exception as e:
            logger.error(f"Reconnection failed: {str(e)}")
            
            # Schedule another reconnection attempt if needed
            if self._reconnection_attempts < self._max_reconnection_attempts:
                self._reconnection_attempts += 1
                delay = self._reconnection_delay * (2 ** (self._reconnection_attempts - 1))
                logger.info(f"Retrying reconnection in {delay} seconds (attempt {self._reconnection_attempts})")
                reactor.callLater(delay, self._attempt_reconnection)
            else:
                logger.error("Maximum reconnection attempts reached")

    async def _resubscribe_to_spots(self):
        """Resubscribe to previously subscribed spot prices after reconnection."""
        if not self._spot_subscriptions:
            return
            
        logger.info(f"Resubscribing to {len(self._spot_subscriptions)} symbol price feeds")
        
        # Copy the set to avoid modification during iteration
        symbols_to_resubscribe = self._spot_subscriptions.copy()
        self._spot_subscriptions.clear()
        
        for symbol_id in symbols_to_resubscribe:
            try:
                await self.subscribe_to_prices(symbol_id)
            except Exception as e:
                logger.error(f"Failed to resubscribe to symbol {symbol_id}: {str(e)}")

    def _start_heartbeat(self):
        """Start sending periodic heartbeat messages."""
        if self._heartbeat_timer is not None:
            self._stop_heartbeat()
            
        self._send_heartbeat()

    def _stop_heartbeat(self):
        """Stop the heartbeat timer."""
        if self._heartbeat_timer is not None and self._heartbeat_timer.active():
            self._heartbeat_timer.cancel()
            self._heartbeat_timer = None

    def _send_heartbeat(self):
        """Send a heartbeat message and schedule the next one."""
        if self._is_connected and self._client:
            try:
                # Create and send heartbeat message
                heartbeat = ProtoHeartbeatEvent()
                self._client.send(heartbeat)
                
                # Schedule next heartbeat
                self._heartbeat_timer = reactor.callLater(
                    self.heartbeat_interval, self._send_heartbeat
                )
            except Exception as e:
                logger.error(f"Error sending heartbeat: {str(e)}")

    def _internal_on_message(self, client, message):
        """
        Internal callback for received messages.
        
        Args:
            client: The OpenApiPy client instance.
            message: The received ProtoMessage.
        """
        try:
            # Extract payload type to determine message type
            payload_type = message.payloadType
            
            # Extract client message ID if present
            client_msg_id = message.clientMsgId if message.HasField("clientMsgId") else None
            
            # Extract and deserialize the payload based on type
            deserialized_message = Protobuf.extract(message)
            
            # Dispatch message to appropriate handler
            self._dispatch_incoming_message(payload_type, deserialized_message, client_msg_id)
            
        except Exception as e:
            logger.error(f"Error processing message: {str(e)}")
            # Call error callback with generic error
            if self.on_error:
                try:
                    self.on_error("MESSAGE_PROCESSING_ERROR", str(e))
                except Exception as callback_err:
                    logger.error(f"Error in user error callback: {str(callback_err)}")

    def _internal_on_error(self, failure):
        """
        Internal callback for client errors.
        
        Args:
            failure: The Twisted Failure object containing error details.
        """
        error_msg = str(failure.value)
        logger.error(f"Client error: {error_msg}")
        failure.printTraceback()
        
        # Call user's error callback if set
        if self.on_error:
            try:
                self.on_error("CLIENT_ERROR", error_msg)
            except Exception as e:
                logger.error(f"Error in user error callback: {str(e)}")

    # --- Authentication ---
    
    def get_auth_url(self, scope: str = "trading") -> str:
        """
        Get the authorization URL for the OAuth flow.
        
        Args:
            scope: The requested access scope, either "trading" (full access) or "accounts" (read-only).
            
        Returns:
            The authorization URL to redirect the user to.
        """
        if scope not in ["trading", "accounts"]:
            raise ValueError("scope must be either 'trading' or 'accounts'")
            
        return self._auth.getAuthUri(scope=scope)

    async def authorize_with_code(self, auth_code: str) -> bool:
        """
        Complete the OAuth flow by exchanging the authorization code for tokens.
        
        Args:
            auth_code: The authorization code received from the redirect.
            
        Returns:
            True if authorization was successful.
            
        Raises:
            AuthenticationError: If token exchange fails.
        """
        try:
            # Exchange auth code for tokens
            token_response = self._auth.getToken(auth_code)
            
            # Check for errors
            if "errorCode" in token_response:
                error_msg = token_response.get("description", "Unknown error")
                logger.error(f"Token exchange failed: {error_msg}")
                raise AuthenticationError(f"Token exchange failed: {error_msg}")
                
            # Store tokens
            self._access_token = token_response.get("accessToken")
            self._refresh_token = token_response.get("refreshToken")
            
            # Calculate expiry time (typically 30 days)
            expires_in = token_response.get("expiresIn", 2592000)  # Default 30 days
            self._token_expiry_time = datetime.now() + timedelta(seconds=expires_in)
            
            logger.info("Successfully obtained access token")
            
            # If already connected, authorize application
            if self._is_connected:
                await self._authorize_application()
                
            return True
            
        except Exception as e:
            if not isinstance(e, AuthenticationError):
                logger.error(f"Authorization error: {str(e)}")
                raise AuthenticationError(f"Authorization failed: {str(e)}") from e
            raise

    async def set_active_account(self, account_id: int) -> bool:
        """
        Set the active trading account and authorize it.
        
        Args:
            account_id: The cTrader account ID (ctidTraderAccountId).
            
        Returns:
            True if account authorization was successful.
            
        Raises:
            AuthenticationError: If account authorization fails.
            ConnectionError: If not connected to the API server.
        """
        if not self._is_connected:
            raise ConnectionError("Not connected to cTrader API")
            
        if not self._is_app_authorized:
            raise AuthenticationError("Application not authorized. Call authorize_with_code first")
            
        # Store the account ID
        self._ctid_trader_account_id = account_id
        
        # Authorize the account
        return await self._authorize_account()

    async def get_available_accounts(self) -> List[Dict[str, Any]]:
        """
        Get a list of available trading accounts for the authenticated user.
        
        Returns:
            A list of account information dictionaries.
            
        Raises:
            AuthenticationError: If application is not authorized.
            ConnectionError: If not connected to the API server.
        """
        if not self._is_connected:
            raise ConnectionError("Not connected to cTrader API")
            
        if not self._is_app_authorized:
            raise AuthenticationError("Application not authorized. Call authorize_with_code first")
            
        # Check if token refresh is needed
        await self._refresh_token_if_needed()
        
        # Prepare the request
        request = ProtoOAGetAccountListByAccessTokenReq()
        request.accessToken = self._access_token
        
        # Send the request
        response = await self._send_request(request)
        
        # Process the response
        accounts = []
        for account in response.ctidTraderAccount:
            accounts.append({
                "ctid_trader_account_id": account.ctidTraderAccountId,
                "trader_login": account.traderLogin,
                "account_type": account.accountType,
                "is_live": account.isLive,
                "broker_name": account.brokerName,
                "broker_timezone": account.brokerTimezone,
            })
            
        return accounts

    async def _authorize_application(self) -> bool:
        """
        Authorize the application with client credentials.
        
        Returns:
            True if authorization was successful.
            
        Raises:
            AuthenticationError: If authorization fails.
            ConnectionError: If not connected to the API server.
        """
        if not self._is_connected:
            raise ConnectionError("Not connected to cTrader API")
            
        logger.info("Authorizing application...")
        
        # Reset authorization state
        self._is_app_authorized = False
        
        try:
            # Prepare auth request
            request = ProtoOAApplicationAuthReq()
            request.clientId = self._client_id
            request.clientSecret = self._client_secret
            
            # Send the request
            response = await self._send_request(request)
            
            # Response handling is done in _dispatch_incoming_message
            # via ProtoOAApplicationAuthRes handler
            
            self._is_app_authorized = True
            logger.info("Application authorization successful")
            return True
            
        except Exception as e:
            logger.error(f"Application authorization failed: {str(e)}")
            raise AuthenticationError(f"Application authorization failed: {str(e)}") from e

    async def _authorize_account(self) -> bool:
        """
        Authorize access to a specific trading account.
        
        Returns:
            True if authorization was successful.
            
        Raises:
            AuthenticationError: If authorization fails.
        """
        if not self._ctid_trader_account_id:
            raise AuthenticationError("No account ID set. Call set_active_account first")
            
        logger.info(f"Authorizing account {self._ctid_trader_account_id}...")
        
        # Reset authorization state
        self._is_account_authorized = False
        
        try:
            # Check if token refresh is needed
            await self._refresh_token_if_needed()
            
            # Prepare auth request
            request = ProtoOAAccountAuthReq()
            request.ctidTraderAccountId = self._ctid_trader_account_id
            request.accessToken = self._access_token
            
            # Send the request
            response = await self._send_request(request)
            
            # Response handling is done in _dispatch_incoming_message
            # via ProtoOAAccountAuthRes handler
            
            self._is_account_authorized = True
            logger.info(f"Account {self._ctid_trader_account_id} authorization successful")
            return True
            
        except Exception as e:
            logger.error(f"Account authorization failed: {str(e)}")
            raise AuthenticationError(f"Account authorization failed: {str(e)}") from e

    async def _refresh_token_if_needed(self) -> bool:
        """
        Check if the access token needs refreshing and refresh if needed.
        
        Returns:
            True if token was refreshed or is still valid, False otherwise.
        """
        if not self._access_token or not self._refresh_token:
            logger.error("No tokens available for refresh")
            return False
            
        # Check if token is about to expire (within 1 hour)
        if self._token_expiry_time and self._token_expiry_time > datetime.now() + timedelta(hours=1):
            # Token is still valid
            return True
            
        logger.info("Access token expiring soon, refreshing...")
        
        try:
            # Option 1: Use OpenApiPy Auth helper (HTTP based)
            token_response = self._auth.refreshToken(self._refresh_token)
            
            # Check for errors
            if "errorCode" in token_response:
                error_msg = token_response.get("description", "Unknown error")
                logger.error(f"Token refresh failed: {error_msg}")
                raise TokenError(f"Token refresh failed: {error_msg}")
                
            # Update tokens
            self._access_token = token_response.get("accessToken")
            self._refresh_token = token_response.get("refreshToken")
            
            # Calculate new expiry time
            expires_in = token_response.get("expiresIn", 2592000)  # Default 30 days
            self._token_expiry_time = datetime.now() + timedelta(seconds=expires_in)
            
            logger.info("Successfully refreshed access token")
            return True
            
        except Exception as e:
            logger.error(f"Token refresh failed: {str(e)}")
            return False

    # --- Core API Communication ---
    
    async def _send_request(self, request_obj, client_msg_id: str = None) -> Any:
        """
        Send a request to the API and await response.
        
        Args:
            request_obj: The Protobuf request object.
            client_msg_id: Optional client message ID for correlation.
            
        Returns:
            The deserialized response message.
            
        Raises:
            ConnectionError: If not connected to the API server.
            AuthenticationError: If not properly authenticated.
            Various other exceptions based on error responses.
        """
        # Check connection and authorization state
        if not self._is_connected:
            raise ConnectionError("Not connected to cTrader API")
            
        # For requests that require app authorization
        app_auth_required = not isinstance(request_obj, (
            ProtoHeartbeatEvent,
            ProtoOAApplicationAuthReq
        ))
        
        if app_auth_required and not self._is_app_authorized:
            raise AuthenticationError("Application not authorized")
            
        # For requests that require account authorization
        account_auth_required = hasattr(request_obj, "ctidTraderAccountId") and not isinstance(request_obj, (
            ProtoOAAccountAuthReq,
            ProtoOAGetAccountListByAccessTokenReq,
            ProtoOARefreshTokenReq
        ))
        
        if account_auth_required:
            if not self._ctid_trader_account_id:
                raise AuthenticationError("No account ID set. Call set_active_account first")
                
            if not self._is_account_authorized:
                raise AuthenticationError("Account not authorized")
                
            # Populate ctidTraderAccountId if the message has this field
            request_obj.ctidTraderAccountId = self._ctid_trader_account_id
            
        # Generate a client message ID if not provided
        if client_msg_id is None:
            client_msg_id = str(uuid.uuid4())
            
        # Create a future to receive the response
        response_future = asyncio.Future()
        self._pending_requests[client_msg_id] = response_future
        
        try:
            # Send the request
            logger.debug(f"Sending {type(request_obj).__name__} with ID {client_msg_id}")
            deferred = self._client.send(request_obj, clientMsgId=client_msg_id)
            
            # Attach error handler to the deferred
            deferred.addErrback(self._handle_deferred_error, client_msg_id)
            
            # Wait for response with timeout
            return await asyncio.wait_for(response_future, timeout=30.0)
            
        except asyncio.TimeoutError:
            if client_msg_id in self._pending_requests:
                del self._pending_requests[client_msg_id]
            raise RequestError("Request timed out")
            
        except Exception as e:
            if client_msg_id in self._pending_requests:
                del self._pending_requests[client_msg_id]
            logger.error(f"Request error: {str(e)}")
            raise

    def _handle_deferred_error(self, failure, client_msg_id):
        """
        Handle errors from Twisted Deferreds.
        
        Args:
            failure: The Twisted Failure object.
            client_msg_id: The client message ID for the request.
        """
        logger.error(f"Request failed: {str(failure.value)}")
        
        # Resolve the future with an exception if it exists
        if client_msg_id in self._pending_requests:
            future = self._pending_requests.pop(client_msg_id)
            if not future.done():
                future.set_exception(RequestError(str(failure.value)))

    def _dispatch_incoming_message(self, payload_type, message_obj, client_msg_id=None):
        """
        Dispatch incoming messages to appropriate handlers.
        
        Args:
            payload_type: The numeric payload type identifier.
            message_obj: The deserialized message object.
            client_msg_id: The client message ID if available.
        """
        logger.debug(f"Received message type: {type(message_obj).__name__}")
        
        # Handle common auth responses and update state
        if isinstance(message_obj, ProtoOAApplicationAuthRes):
            logger.info("Application authentication confirmed")
            self._is_app_authorized = True
            
        elif isinstance(message_obj, ProtoOAAccountAuthRes):
            logger.info(f"Account {self._ctid_trader_account_id} authentication confirmed")
            self._is_account_authorized = True
            
        # Handle error responses
        elif isinstance(message_obj, ProtoErrorRes):
            self._handle_error_response(message_obj, client_msg_id)
            return
            
        # Handle heartbeat events (typically ignore)
        elif isinstance(message_obj, ProtoHeartbeatEvent):
            # Just log at debug level
            logger.debug("Heartbeat received")
            
        # Handle specific event types
        elif isinstance(message_obj, ProtoOASpotEvent):
            self._handle_spot_event(message_obj)
            
        elif isinstance(message_obj, ProtoOAExecutionEvent):
            self._handle_execution_event(message_obj)
            
        elif isinstance(message_obj, ProtoOAMarginChangedEvent):
            self._handle_margin_changed_event(message_obj)
            
        elif isinstance(message_obj, ProtoOATraderUpdatedEvent):
            self._handle_trader_updated_event(message_obj)
            
        elif isinstance(message_obj, ProtoOAOrderErrorEvent):
            self._handle_order_error_event(message_obj, client_msg_id)
            
        # Complete pending request if this message is a response to one
        if client_msg_id and client_msg_id in self._pending_requests:
            future = self._pending_requests.pop(client_msg_id)
            if not future.done():
                future.set_result(message_obj)

    def _handle_error_response(self, error_res, client_msg_id=None):
        """
        Handle API error responses.
        
        Args:
            error_res: The error response message.
            client_msg_id: The client message ID if available.
        """
        # Extract error details
        if hasattr(error_res, "errorCode"):
            error_code = error_res.errorCode
        else:
            error_code = "UNKNOWN_ERROR"
            
        if hasattr(error_res, "description"):
            description = error_res.description
        else:
            description = "Unknown error"
            
        logger.error(f"API error: {error_code} - {description}")
        
        # Determine appropriate exception type
        exception_class = self.ERROR_CODE_MAP.get(error_code, RequestError)
        exception = exception_class(f"{error_code}: {description}")
        
        # Resolve pending request with exception if applicable
        if client_msg_id and client_msg_id in self._pending_requests:
            future = self._pending_requests.pop(client_msg_id)
            if not future.done():
                future.set_exception(exception)
                
        # Call user's error callback if set
        if self.on_error:
            try:
                self.on_error(error_code, description)
            except Exception as e:
                logger.error(f"Error in user error callback: {str(e)}")

    def _handle_order_error_event(self, event, client_msg_id=None):
        """
        Handle order error events.
        
        Args:
            event: The ProtoOAOrderErrorEvent object.
            client_msg_id: The client message ID if available.
        """
        # Extract error details
        error_code = event.errorCode
        description = getattr(event, "description", "No description")
        
        logger.error(f"Order error: {error_code} - {description}")
        
        # Create appropriate exception
        exception_class = self.ERROR_CODE_MAP.get(error_code, OrderError)
        exception = exception_class(f"{error_code}: {description}")
        
        # Resolve pending request with exception if applicable
        if client_msg_id and client_msg_id in self._pending_requests:
            future = self._pending_requests.pop(client_msg_id)
            if not future.done():
                future.set_exception(exception)
                
        # Call user's error callback if set
        if self.on_error:
            try:
                self.on_error(error_code, description)
            except Exception as e:
                logger.error(f"Error in user error callback: {str(e)}")

    # --- Event Handlers ---
    
    def _handle_spot_event(self, event):
        """
        Handle spot price update events.
        
        Args:
            event: The ProtoOASpotEvent object.
        """
        symbol_id = event.symbolId
        
        # Check if we're subscribed to this symbol
        if symbol_id not in self._spot_subscriptions:
            logger.debug(f"Received spot event for unsubscribed symbol {symbol_id}")
            return
            
        # Extract price information
        # Note: Prices in cTrader are typically integers that need to be divided by a factor
        digits = 5  # Default, should be retrieved from symbol specs
        if symbol_id in self._symbols and hasattr(self._symbols[symbol_id], "digits"):
            digits = self._symbols[symbol_id].digits
            
        bid = event.bid / (10 ** digits)
        ask = event.ask / (10 ** digits)
        timestamp = event.timestamp
        
        # Update price cache
        self._price_cache[symbol_id] = {
            "bid": bid,
            "ask": ask,
            "timestamp": timestamp
        }
        
        # Call user's tick data callback if set
        if self.on_tick_data:
            try:
                self.on_tick_data(symbol_id, bid, ask, timestamp)
            except Exception as e:
                logger.error(f"Error in user tick data callback: {str(e)}")

    def _handle_execution_event(self, event):
        """
        Handle execution events (order fills, cancellations, position changes).
        
        Args:
            event: The ProtoOAExecutionEvent object.
        """
        logger.debug(f"Execution event: type={event.executionType}")
        
        # Update internal state based on execution type
        execution_type = event.executionType
        
        # Handle order events
        if hasattr(event, "order") and event.HasField("order"):
            order = event.order
            order_id = order.orderId
            
            if execution_type in [ProtoOAExecutionType.ORDER_ACCEPTED, ProtoOAExecutionType.ORDER_REPLACED]:
                # New or updated order
                self._orders[order_id] = order
                
            elif execution_type in [ProtoOAExecutionType.ORDER_CANCELLED, ProtoOAExecutionType.ORDER_EXPIRED,
                                   ProtoOAExecutionType.ORDER_FILLED, ProtoOAExecutionType.ORDER_REJECTED]:
                # Order removed from pending
                if order_id in self._orders:
                    del self._orders[order_id]
                    
            # Call user's order update callback if set
            if self.on_order_update:
                try:
                    self.on_order_update(event)
                except Exception as e:
                    logger.error(f"Error in user order update callback: {str(e)}")
                    
        # Handle position events
        if hasattr(event, "position") and event.HasField("position"):
            position = event.position
            position_id = position.positionId
            
            if execution_type in [ProtoOAExecutionType.ORDER_FILLED, ProtoOAExecutionType.POSITION_MODIFIED]:
                # New or updated position
                self._positions[position_id] = position
                
            elif execution_type == ProtoOAExecutionType.POSITION_CLOSED:
                # Position closed
                if position_id in self._positions:
                    del self._positions[position_id]
                    
            # Call user's position update callback if set
            if self.on_position_update:
                try:
                    self.on_position_update(event)
                except Exception as e:
                    logger.error(f"Error in user position update callback: {str(e)}")

    def _handle_margin_changed_event(self, event):
        """
        Handle margin changed events.
        
        Args:
            event: The ProtoOAMarginChangedEvent object.
        """
        logger.debug(f"Margin changed: account={event.ctidTraderAccountId}")
        
        # Call user's margin change callback if set
        if self.on_margin_change:
            try:
                self.on_margin_change(event)
            except Exception as e:
                logger.error(f"Error in user margin change callback: {str(e)}")

    def _handle_trader_updated_event(self, event):
        """
        Handle trader updated events.
        
        Args:
            event: The ProtoOATraderUpdatedEvent object.
        """
        logger.debug(f"Trader updated: account={event.ctidTraderAccountId}")
        
        # Update trader info cache if we have it
        if self._trader_info and hasattr(event, "trader") and event.HasField("trader"):
            self._trader_info = event.trader

    # --- Trading Methods ---
    
    async def get_account_info(self) -> Dict[str, Any]:
        """
        Get detailed information about the active trading account.
        
        Returns:
            A dictionary containing account information.
            
        Raises:
            AuthenticationError: If not properly authenticated.
            ConnectionError: If not connected to the API server.
        """
        if not self._is_connected:
            raise ConnectionError("Not connected to cTrader API")
            
        if not self._is_account_authorized:
            raise AuthenticationError("Account not authorized")
            
        # Prepare the request
        request = ProtoOATraderReq()
        request.ctidTraderAccountId = self._ctid_trader_account_id
        
        # Send the request
        response = await self._send_request(request)
        
        # Store trader info in cache
        self._trader_info = response.trader
        
        # Convert to a dictionary
        trader_dict = {
            "account_id": self._ctid_trader_account_id,
            "balance": response.trader.balance,
            "equity": response.trader.equity,
            "margin_used": response.trader.marginUsed,
            "free_margin": response.trader.freeMargin,
            "margin_level": response.trader.marginLevel,
            "unrealized_gross_profit": response.trader.unrealizedGrossPl,
            "unrealized_net_profit": response.trader.unrealizedNetPl,
        }
        
        # Add more fields as needed
        
        return trader_dict

    async def get_positions(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Get a list of open positions.
        
        Args:
            force_refresh: Whether to force a refresh from the server (otherwise use cache if available).
            
        Returns:
            A list of position information dictionaries.
            
        Raises:
            AuthenticationError: If not properly authenticated.
            ConnectionError: If not connected to the API server.
        """
        if not self._is_connected:
            raise ConnectionError("Not connected to cTrader API")
            
        if not self._is_account_authorized:
            raise AuthenticationError("Account not authorized")
            
        # If cache is empty or force refresh requested, fetch from server
        if not self._positions or force_refresh:
            # Prepare reconcile request (gets positions, orders, and account state)
            request = ProtoOAReconcileReq()
            request.ctidTraderAccountId = self._ctid_trader_account_id
            
            # Send the request
            response = await self._send_request(request)
            
            # Update position cache
            self._positions = {}
            for position in response.position:
                self._positions[position.positionId] = position
                
            # Update order cache
            self._orders = {}
            for order in response.order:
                self._orders[order.orderId] = order
                
        # Convert positions to dictionaries
        positions = []
        for position in self._positions.values():
            # Get symbol details for additional info
            symbol_id = position.symbolId
            symbol = await self._ensure_symbol_loaded(symbol_id)
            
            # Calculate proper price values
            digits = getattr(symbol, "digits", 5)  # Default to 5 if not available
            price_factor = 10 ** digits
            
            # Convert position to dictionary
            position_dict = {
                "position_id": position.positionId,
                "symbol_id": position.symbolId,
                "symbol_name": getattr(symbol, "symbolName", str(symbol_id)),
                "volume": position.volume / 100,  # Volume is typically in 0.01 lots
                "trade_side": "BUY" if position.tradeSide == ProtoOATradeSide.BUY else "SELL",
                "price": position.price / price_factor,
                "swap": position.swap,
                "commission": position.commission,
                "open_timestamp": position.openTimestamp,
            }
            
            # Add SL/TP if present
            if position.HasField("stopLoss"):
                position_dict["stop_loss"] = position.stopLoss / price_factor
                
            if position.HasField("takeProfit"):
                position_dict["take_profit"] = position.takeProfit / price_factor
                
            positions.append(position_dict)
            
        return positions

    async def get_orders(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Get a list of pending orders.
        
        Args:
            force_refresh: Whether to force a refresh from the server (otherwise use cache if available).
            
        Returns:
            A list of order information dictionaries.
            
        Raises:
            AuthenticationError: If not properly authenticated.
            ConnectionError: If not connected to the API server.
        """
        if not self._is_connected:
            raise ConnectionError("Not connected to cTrader API")
            
        if not self._is_account_authorized:
            raise AuthenticationError("Account not authorized")
            
        # If cache is empty or force refresh requested, fetch from server
        if not self._orders or force_refresh:
            # Prepare reconcile request (gets positions, orders, and account state)
            request = ProtoOAReconcileReq()
            request.ctidTraderAccountId = self._ctid_trader_account_id
            
            # Send the request
            response = await self._send_request(request)
            
            # Update order cache
            self._orders = {}
            for order in response.order:
                self._orders[order.orderId] = order
                
            # Update position cache
            self._positions = {}
            for position in response.position:
                self._positions[position.positionId] = position
                
        # Convert orders to dictionaries
        orders = []
        for order in self._orders.values():
            # Get symbol details for additional info
            symbol_id = order.symbolId
            symbol = await self._ensure_symbol_loaded(symbol_id)
            
            # Calculate proper price values
            digits = getattr(symbol, "digits", 5)  # Default to 5 if not available
            price_factor = 10 ** digits
            
            # Convert order to dictionary
            order_dict = {
                "order_id": order.orderId,
                "symbol_id": order.symbolId,
                "symbol_name": getattr(symbol, "symbolName", str(symbol_id)),
                "volume": order.volume / 100,  # Volume is typically in 0.01 lots
                "trade_side": "BUY" if order.tradeSide == ProtoOATradeSide.BUY else "SELL",
                "order_type": self._get_order_type_name(order.orderType),
                "order_status": order.orderStatus,
                "expiration_timestamp": getattr(order, "expirationTimestamp", None),
                "creation_timestamp": order.tradeData.openTimestamp if hasattr(order, "tradeData") else None,
            }
            
            # Add price fields based on order type
            if order.orderType == ProtoOAOrderType.LIMIT:
                order_dict["limit_price"] = order.limitPrice / price_factor
                
            elif order.orderType == ProtoOAOrderType.STOP:
                order_dict["stop_price"] = order.stopPrice / price_factor
                
            elif order.orderType == ProtoOAOrderType.STOP_LIMIT:
                order_dict["stop_price"] = order.stopPrice / price_factor
                order_dict["limit_price"] = order.limitPrice / price_factor
                
            # Add SL/TP if present
            if order.HasField("stopLoss"):
                order_dict["stop_loss"] = order.stopLoss / price_factor
                
            if order.HasField("takeProfit"):
                order_dict["take_profit"] = order.takeProfit / price_factor
                
            orders.append(order_dict)
            
        return orders

    async def get_symbols(self, include_archived: bool = False, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Get a list of available trading symbols.
        
        Args:
            include_archived: Whether to include archived (non-tradable) symbols.
            force_refresh: Whether to force a refresh from the server (otherwise use cache if available).
            
        Returns:
            A list of symbol information dictionaries.
            
        Raises:
            AuthenticationError: If not properly authenticated.
            ConnectionError: If not connected to the API server.
        """
        if not self._is_connected:
            raise ConnectionError("Not connected to cTrader API")
            
        if not self._is_account_authorized:
            raise AuthenticationError("Account not authorized")
            
        # If cache is empty or force refresh requested, fetch from server
        if not self._symbols or force_refresh:
            # Prepare symbol list request
            request = ProtoOASymbolsListReq()
            request.ctidTraderAccountId = self._ctid_trader_account_id
            request.includeArchived = include_archived
            
            # Send the request
            response = await self._send_request(request)
            
            # Update symbol cache
            self._symbols = {}
            for symbol in response.symbol:
                self._symbols[symbol.symbolId] = symbol
                
        # Convert symbols to dictionaries
        symbols = []
        for symbol in self._symbols.values():
            # Filter archived symbols if not requested
            if not include_archived and getattr(symbol, "isArchived", False):
                continue
                
            # Convert symbol to dictionary
            symbol_dict = {
                "symbol_id": symbol.symbolId,
                "symbol_name": symbol.symbolName,
                "asset_class": getattr(symbol, "assetClass", "Unknown"),
                "digits": symbol.digits,
                "pip_position": getattr(symbol, "pipPosition", 0),
                "tick_size": getattr(symbol, "tickSize", 1) / (10 ** symbol.digits),
                "min_volume": getattr(symbol, "minVolume", 0) / 100,
                "max_volume": getattr(symbol, "maxVolume", 0) / 100,
                "step_volume": getattr(symbol, "stepVolume", 0) / 100,
                "margin_hedged": getattr(symbol, "marginHedged", 0),
                "is_tradable": not getattr(symbol, "isArchived", False),
            }
            
            symbols.append(symbol_dict)
            
        return symbols

    async def get_symbol_details(self, symbol_id: int) -> Dict[str, Any]:
        """
        Get detailed information about a specific symbol.
        
        Args:
            symbol_id: The symbol ID to get details for.
            
        Returns:
            A dictionary containing symbol details.
            
        Raises:
            AuthenticationError: If not properly authenticated.
            ConnectionError: If not connected to the API server.
            InvalidSymbolError: If the symbol ID is invalid.
        """
        symbol = await self._ensure_symbol_loaded(symbol_id)
        
        # Calculate proper price values
        digits = symbol.digits
        price_factor = 10 ** digits
        
        # Convert symbol to dictionary with extended information
        symbol_dict = {
            "symbol_id": symbol.symbolId,
            "symbol_name": symbol.symbolName,
            "asset_class": getattr(symbol, "assetClass", "Unknown"),
            "digits": symbol.digits,
            "pip_position": getattr(symbol, "pipPosition", 0),
            "tick_size": getattr(symbol, "tickSize", 1) / price_factor,
            "min_volume": getattr(symbol, "minVolume", 0) / 100,
            "max_volume": getattr(symbol, "maxVolume", 0) / 100,
            "step_volume": getattr(symbol, "stepVolume", 0) / 100,
            "margin_hedged": getattr(symbol, "marginHedged", 0),
            "is_tradable": not getattr(symbol, "isArchived", False),
        }
        
        # Add current price if available
        if symbol_id in self._price_cache:
            symbol_dict["bid"] = self._price_cache[symbol_id]["bid"]
            symbol_dict["ask"] = self._price_cache[symbol_id]["ask"]
            symbol_dict["spread"] = symbol_dict["ask"] - symbol_dict["bid"]
            
        return symbol_dict

    async def subscribe_to_prices(self, symbol_id: int) -> bool:
        """
        Subscribe to real-time price updates for a symbol.
        
        Args:
            symbol_id: The symbol ID to subscribe to.
            
        Returns:
            True if subscription was successful.
            
        Raises:
            AuthenticationError: If not properly authenticated.
            ConnectionError: If not connected to the API server.
            SubscriptionError: If subscription fails.
        """
        if not self._is_connected:
            raise ConnectionError("Not connected to cTrader API")
            
        if not self._is_account_authorized:
            raise AuthenticationError("Account not authorized")
            
        # Check if already subscribed
        if symbol_id in self._spot_subscriptions:
            logger.info(f"Already subscribed to symbol {symbol_id}")
            return True
            
        try:
            # Ensure symbol details are loaded
            await self._ensure_symbol_loaded(symbol_id)
            
            # Prepare subscription request
            request = ProtoOASubscribeSpotsReq()
            request.ctidTraderAccountId = self._ctid_trader_account_id
            request.symbolId = symbol_id
            
            # Send the request
            response = await self._send_request(request)
            
            # Update subscription cache
            self._spot_subscriptions.add(symbol_id)
            
            logger.info(f"Successfully subscribed to symbol {symbol_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to subscribe to symbol {symbol_id}: {str(e)}")
            raise SubscriptionError(f"Failed to subscribe to symbol {symbol_id}: {str(e)}") from e

    async def unsubscribe_from_prices(self, symbol_id: int) -> bool:
        """
        Unsubscribe from real-time price updates for a symbol.
        
        Args:
            symbol_id: The symbol ID to unsubscribe from.
            
        Returns:
            True if unsubscription was successful.
            
        Raises:
            AuthenticationError: If not properly authenticated.
            ConnectionError: If not connected to the API server.
            SubscriptionError: If unsubscription fails.
        """
        if not self._is_connected:
            raise ConnectionError("Not connected to cTrader API")
            
        if not self._is_account_authorized:
            raise AuthenticationError("Account not authorized")
            
        # Check if subscribed
        if symbol_id not in self._spot_subscriptions:
            logger.info(f"Not subscribed to symbol {symbol_id}")
            return True
            
        try:
            # Prepare unsubscription request
            request = ProtoOAUnsubscribeSpotsReq()
            request.ctidTraderAccountId = self._ctid_trader_account_id
            request.symbolId = symbol_id
            
            # Send the request
            response = await self._send_request(request)
            
            # Update subscription cache
            if symbol_id in self._spot_subscriptions:
                self._spot_subscriptions.remove(symbol_id)
                
            # Remove from price cache
            if symbol_id in self._price_cache:
                del self._price_cache[symbol_id]
                
            logger.info(f"Successfully unsubscribed from symbol {symbol_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to unsubscribe from symbol {symbol_id}: {str(e)}")
            raise SubscriptionError(f"Failed to unsubscribe from symbol {symbol_id}: {str(e)}") from e

    async def get_historical_prices(
        self, symbol_id: int, period: str, from_timestamp: int, to_timestamp: int = None
    ) -> List[Dict[str, Any]]:
        """
        Get historical price data (OHLC) for a symbol.
        
        Args:
            symbol_id: The symbol ID to get data for.
            period: The time period/timeframe (e.g., "M1", "H1", "D1").
            from_timestamp: The start timestamp in milliseconds.
            to_timestamp: The end timestamp in milliseconds (defaults to current time).
            
        Returns:
            A list of historical price bars.
            
        Raises:
            AuthenticationError: If not properly authenticated.
            ConnectionError: If not connected to the API server.
            InvalidSymbolError: If the symbol ID is invalid.
            RequestError: If the request fails.
        """
        if not self._is_connected:
            raise ConnectionError("Not connected to cTrader API")
            
        if not self._is_account_authorized:
            raise AuthenticationError("Account not authorized")
            
        # Ensure symbol details are loaded
        symbol = await self._ensure_symbol_loaded(symbol_id)
        
        # Map period string to ProtoOATrendbarPeriod enum
        period_map = {
            "M1": 1,   # 1 minute
            "M2": 2,   # 2 minutes
            "M3": 3,   # 3 minutes
            "M4": 4,   # 4 minutes
            "M5": 5,   # 5 minutes
            "M10": 6,  # 10 minutes
            "M15": 7,  # 15 minutes
            "M30": 8,  # 30 minutes
            "H1": 9,   # 1 hour
            "H4": 10,  # 4 hours
            "H12": 11, # 12 hours
            "D1": 12,  # 1 day
            "W1": 13,  # 1 week
            "MN1": 14, # 1 month
        }
        
        if period not in period_map:
            raise ValidationError(f"Invalid period: {period}. Must be one of {list(period_map.keys())}")
            
        # Set default to_timestamp if not provided
        if to_timestamp is None:
            to_timestamp = int(time.time() * 1000)
            
        try:
            # Prepare historical data request
            request = ProtoOAGetTrendbarsReq()
            request.ctidTraderAccountId = self._ctid_trader_account_id
            request.symbolId = symbol_id
            request.period = period_map[period]
            request.fromTimestamp = from_timestamp
            request.toTimestamp = to_timestamp
            
            # Send the request
            response = await self._send_request(request)
            
            # Calculate price factor based on symbol digits
            digits = symbol.digits
            price_factor = 10 ** digits
            
            # Convert trendbars to dictionaries
            trendbars = []
            for bar in response.trendbar:
                trendbar_dict = {
                    "timestamp": bar.timestamp,
                    "open": bar.open / price_factor,
                    "high": bar.high / price_factor,
                    "low": bar.low / price_factor,
                    "close": bar.close / price_factor,
                    "volume": bar.volume,
                }
                trendbars.append(trendbar_dict)
                
            return trendbars
            
        except Exception as e:
            logger.error(f"Failed to get historical prices: {str(e)}")
            raise RequestError(f"Failed to get historical prices: {str(e)}") from e

    async def place_market_order(
        self, symbol_id: int, trade_side: str, volume: float, 
        stop_loss: float = None, take_profit: float = None, 
        label: str = None, comment: str = None
    ) -> Dict[str, Any]:
        """
        Place a market order.
        
        Args:
            symbol_id: The symbol ID to trade.
            trade_side: The trade direction, either "BUY" or "SELL".
            volume: The order volume in lots (e.g., 0.01, 0.1, 1.0).
            stop_loss: Optional stop loss price.
            take_profit: Optional take profit price.
            label: Optional order label.
            comment: Optional order comment.
            
        Returns:
            A dictionary containing the executed order/position details.
            
        Raises:
            AuthenticationError: If not properly authenticated.
            ConnectionError: If not connected to the API server.
            ValidationError: If input parameters are invalid.
            OrderError: If order placement fails.
        """
        if not self._is_connected:
            raise ConnectionError("Not connected to cTrader API")
            
        if not self._is_account_authorized:
            raise AuthenticationError("Account not authorized")
            
        # Validate trade side
        if trade_side not in ["BUY", "SELL"]:
            raise ValidationError("trade_side must be either 'BUY' or 'SELL'")
            
        # Validate volume
        if volume <= 0:
            raise ValidationError("volume must be positive")
            
        # Get symbol details
        symbol = await self._ensure_symbol_loaded(symbol_id)
        
        # Calculate proper volume and price values
        digits = symbol.digits
        price_factor = 10 ** digits
        volume_lots = int(volume * 100)  # Convert to 0.01 lot units
        
        # Convert stop loss and take profit to integer format if provided
        sl_price = int(stop_loss * price_factor) if stop_loss is not None else None
        tp_price = int(take_profit * price_factor) if take_profit is not None else None
        
        try:
            # Prepare market order request
            request = ProtoOANewOrderReq()
            request.ctidTraderAccountId = self._ctid_trader_account_id
            request.symbolId = symbol_id
            request.orderType = ProtoOAOrderType.MARKET
            request.tradeSide = ProtoOATradeSide.BUY if trade_side == "BUY" else ProtoOATradeSide.SELL
            request.volume = volume_lots
            
            # Add optional parameters if provided
            if sl_price is not None:
                request.stopLoss = sl_price
                
            if tp_price is not None:
                request.takeProfit = tp_price
                
            if label:
                request.label = label
                
            if comment:
                request.comment = comment
                
            # Send the request
            client_msg_id = str(uuid.uuid4())
            execution_event = await self._send_request(request, client_msg_id)
            
            # Process the execution event to get position details
            if not isinstance(execution_event, ProtoOAExecutionEvent):
                raise OrderError("Unexpected response type")
                
            # The position will have been updated in our cache by the execution handler
            if hasattr(execution_event, "position") and execution_event.HasField("position"):
                position = execution_event.position
                position_id = position.positionId
                
                # Convert position to dictionary (similar to get_positions)
                position_dict = {
                    "position_id": position.positionId,
                    "symbol_id": position.symbolId,
                    "symbol_name": symbol.symbolName,
                    "volume": position.volume / 100,
                    "trade_side": "BUY" if position.tradeSide == ProtoOATradeSide.BUY else "SELL",
                    "price": position.price / price_factor,
                    "swap": position.swap,
                    "commission": position.commission,
                    "open_timestamp": position.openTimestamp,
                }
                
                # Add SL/TP if present
                if position.HasField("stopLoss"):
                    position_dict["stop_loss"] = position.stopLoss / price_factor
                    
                if position.HasField("takeProfit"):
                    position_dict["take_profit"] = position.takeProfit / price_factor
                    
                logger.info(f"Successfully placed market order, position ID: {position_id}")
                return position_dict
                
            # Handle case where position wasn't created
            raise OrderError("Order execution did not create a position")
            
        except Exception as e:
            if isinstance(e, OrderError):
                raise
            logger.error(f"Failed to place market order: {str(e)}")
            raise OrderError(f"Failed to place market order: {str(e)}") from e

    async def place_limit_order(
        self, symbol_id: int, trade_side: str, volume: float, price: float,
        stop_loss: float = None, take_profit: float = None,
        expiry_timestamp: int = None, label: str = None, comment: str = None
    ) -> Dict[str, Any]:
        """
        Place a limit order.
        
        Args:
            symbol_id: The symbol ID to trade.
            trade_side: The trade direction, either "BUY" or "SELL".
            volume: The order volume in lots (e.g., 0.01, 0.1, 1.0).
            price: The limit price.
            stop_loss: Optional stop loss price.
            take_profit: Optional take profit price.
            expiry_timestamp: Optional expiry timestamp in milliseconds.
            label: Optional order label.
            comment: Optional order comment.
            
        Returns:
            A dictionary containing the order details.
            
        Raises:
            AuthenticationError: If not properly authenticated.
            ConnectionError: If not connected to the API server.
            ValidationError: If input parameters are invalid.
            OrderError: If order placement fails.
        """
        if not self._is_connected:
            raise ConnectionError("Not connected to cTrader API")
            
        if not self._is_account_authorized:
            raise AuthenticationError("Account not authorized")
            
        # Validate trade side
        if trade_side not in ["BUY", "SELL"]:
            raise ValidationError("trade_side must be either 'BUY' or 'SELL'")
            
        # Validate volume
        if volume <= 0:
            raise ValidationError("volume must be positive")
            
        # Validate price
        if price <= 0:
            raise ValidationError("price must be positive")
            
        # Get symbol details
        symbol = await self._ensure_symbol_loaded(symbol_id)
        
        # Calculate proper volume and price values
        digits = symbol.digits
        price_factor = 10 ** digits
        volume_lots = int(volume * 100)  # Convert to 0.01 lot units
        limit_price = int(price * price_factor)
        
        # Convert stop loss and take profit to integer format if provided
        sl_price = int(stop_loss * price_factor) if stop_loss is not None else None
        tp_price = int(take_profit * price_factor) if take_profit is not None else None
        
        try:
            # Prepare limit order request
            request = ProtoOANewOrderReq()
            request.ctidTraderAccountId = self._ctid_trader_account_id
            request.symbolId = symbol_id
            request.orderType = ProtoOAOrderType.LIMIT
            request.tradeSide = ProtoOATradeSide.BUY if trade_side == "BUY" else ProtoOATradeSide.SELL
            request.volume = volume_lots
            request.limitPrice = limit_price
            
            # Add optional parameters if provided
            if sl_price is not None:
                request.stopLoss = sl_price
                
            if tp_price is not None:
                request.takeProfit = tp_price
                
            if expiry_timestamp is not None:
                request.expirationTimestamp = expiry_timestamp
                
            if label:
                request.label = label
                
            if comment:
                request.comment = comment
                
            # Send the request
            client_msg_id = str(uuid.uuid4())
            execution_event = await self._send_request(request, client_msg_id)
            
            # Process the execution event to get order details
            if not isinstance(execution_event, ProtoOAExecutionEvent):
                raise OrderError("Unexpected response type")
                
            # The order will have been updated in our cache by the execution handler
            if hasattr(execution_event, "order") and execution_event.HasField("order"):
                order = execution_event.order
                order_id = order.orderId
                
                # Convert order to dictionary (similar to get_orders)
                order_dict = {
                    "order_id": order.orderId,
                    "symbol_id": order.symbolId,
                    "symbol_name": symbol.symbolName,
                    "volume": order.volume / 100,
                    "trade_side": "BUY" if order.tradeSide == ProtoOATradeSide.BUY else "SELL",
                    "order_type": "LIMIT",
                    "limit_price": order.limitPrice / price_factor,
                    "order_status": order.orderStatus,
                    "creation_timestamp": order.tradeData.openTimestamp if hasattr(order, "tradeData") else None,
                }
                
                # Add expiry if present
                if order.HasField("expirationTimestamp"):
                    order_dict["expiration_timestamp"] = order.expirationTimestamp
                    
                # Add SL/TP if present
                if order.HasField("stopLoss"):
                    order_dict["stop_loss"] = order.stopLoss / price_factor
                    
                if order.HasField("takeProfit"):
                    order_dict["take_profit"] = order.takeProfit / price_factor
                    
                logger.info(f"Successfully placed limit order, order ID: {order_id}")
                return order_dict
                
            # Handle case where order wasn't created
            raise OrderError("Order execution did not create a limit order")
            
        except Exception as e:
            if isinstance(e, OrderError):
                raise
            logger.error(f"Failed to place limit order: {str(e)}")
            raise OrderError(f"Failed to place limit order: {str(e)}") from e

    async def place_stop_order(
        self, symbol_id: int, trade_side: str, volume: float, price: float,
        stop_loss: float = None, take_profit: float = None,
        expiry_timestamp: int = None, label: str = None, comment: str = None
    ) -> Dict[str, Any]:
        """
        Place a stop order.
        
        Args:
            symbol_id: The symbol ID to trade.
            trade_side: The trade direction, either "BUY" or "SELL".
            volume: The order volume in lots (e.g., 0.01, 0.1, 1.0).
            price: The stop price.
            stop_loss: Optional stop loss price.
            take_profit: Optional take profit price.
            expiry_timestamp: Optional expiry timestamp in milliseconds.
            label: Optional order label.
            comment: Optional order comment.
            
        Returns:
            A dictionary containing the order details.
            
        Raises:
            AuthenticationError: If not properly authenticated.
            ConnectionError: If not connected to the API server.
            ValidationError: If input parameters are invalid.
            OrderError: If order placement fails.
        """
        if not self._is_connected:
            raise ConnectionError("Not connected to cTrader API")
            
        if not self._is_account_authorized:
            raise AuthenticationError("Account not authorized")
            
        # Validate trade side
        if trade_side not in ["BUY", "SELL"]:
            raise ValidationError("trade_side must be either 'BUY' or 'SELL'")
            
        # Validate volume
        if volume <= 0:
            raise ValidationError("volume must be positive")
            
        # Validate price
        if price <= 0:
            raise ValidationError("price must be positive")
            
        # Get symbol details
        symbol = await self._ensure_symbol_loaded(symbol_id)
        
        # Calculate proper volume and price values
        digits = symbol.digits
        price_factor = 10 ** digits
        volume_lots = int(volume * 100)  # Convert to 0.01 lot units
        stop_price = int(price * price_factor)
        
        # Convert stop loss and take profit to integer format if provided
        sl_price = int(stop_loss * price_factor) if stop_loss is not None else None
        tp_price = int(take_profit * price_factor) if take_profit is not None else None
        
        try:
            # Prepare stop order request
            request = ProtoOANewOrderReq()
            request.ctidTraderAccountId = self._ctid_trader_account_id
            request.symbolId = symbol_id
            request.orderType = ProtoOAOrderType.STOP
            request.tradeSide = ProtoOATradeSide.BUY if trade_side == "BUY" else ProtoOATradeSide.SELL
            request.volume = volume_lots
            request.stopPrice = stop_price
            
            # Add optional parameters if provided
            if sl_price is not None:
                request.stopLoss = sl_price
                
            if tp_price is not None:
                request.takeProfit = tp_price
                
            if expiry_timestamp is not None:
                request.expirationTimestamp = expiry_timestamp
                
            if label:
                request.label = label
                
            if comment:
                request.comment = comment
                
            # Send the request
            client_msg_id = str(uuid.uuid4())
            execution_event = await self._send_request(request, client_msg_id)
            
            # Process the execution event to get order details
            if not isinstance(execution_event, ProtoOAExecutionEvent):
                raise OrderError("Unexpected response type")
                
            # The order will have been updated in our cache by the execution handler
            if hasattr(execution_event, "order") and execution_event.HasField("order"):
                order = execution_event.order
                order_id = order.orderId
                
                # Convert order to dictionary (similar to get_orders)
                order_dict = {
                    "order_id": order.orderId,
                    "symbol_id": order.symbolId,
                    "symbol_name": symbol.symbolName,
                    "volume": order.volume / 100,
                    "trade_side": "BUY" if order.tradeSide == ProtoOATradeSide.BUY else "SELL",
                    "order_type": "STOP",
                    "stop_price": order.stopPrice / price_factor,
                    "order_status": order.orderStatus,
                    "creation_timestamp": order.tradeData.openTimestamp if hasattr(order, "tradeData") else None,
                }
                
                # Add expiry if present
                if order.HasField("expirationTimestamp"):
                    order_dict["expiration_timestamp"] = order.expirationTimestamp
                    
                # Add SL/TP if present
                if order.HasField("stopLoss"):
                    order_dict["stop_loss"] = order.stopLoss / price_factor
                    
                if order.HasField("takeProfit"):
                    order_dict["take_profit"] = order.takeProfit / price_factor
                    
                logger.info(f"Successfully placed stop order, order ID: {order_id}")
                return order_dict
                
            # Handle case where order wasn't created
            raise OrderError("Order execution did not create a stop order")
            
        except Exception as e:
            if isinstance(e, OrderError):
                raise
            logger.error(f"Failed to place stop order: {str(e)}")
            raise OrderError(f"Failed to place stop order: {str(e)}") from e

    async def cancel_order(self, order_id: int) -> bool:
        """
        Cancel a pending order.
        
        Args:
            order_id: The order ID to cancel.
            
        Returns:
            True if cancellation was successful.
            
        Raises:
            AuthenticationError: If not properly authenticated.
            ConnectionError: If not connected to the API server.
            OrderError: If cancellation fails or the order doesn't exist.
        """
        if not self._is_connected:
            raise ConnectionError("Not connected to cTrader API")
            
        if not self._is_account_authorized:
            raise AuthenticationError("Account not authorized")
            
        # Check if order exists
        if order_id not in self._orders:
            await self.get_orders(force_refresh=True)
            if order_id not in self._orders:
                raise OrderError(f"Order {order_id} not found")
                
        try:
            # Prepare cancel order request
            request = ProtoOACancelOrderReq()
            request.ctidTraderAccountId = self._ctid_trader_account_id
            request.orderId = order_id
            
            # Send the request
            client_msg_id = str(uuid.uuid4())
            execution_event = await self._send_request(request, client_msg_id)
            
            # Check if cancellation was successful
            if not isinstance(execution_event, ProtoOAExecutionEvent):
                raise OrderError("Unexpected response type")
                
            # If we got here, the cancellation was successful
            # The order should have been removed from self._orders by the execution handler
            
            logger.info(f"Successfully cancelled order {order_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {str(e)}")
            raise OrderError(f"Failed to cancel order: {str(e)}") from e

    async def amend_order(
        self, order_id: int, volume: float = None, price: float = None,
        stop_loss: float = None, take_profit: float = None,
        expiry_timestamp: int = None
    ) -> Dict[str, Any]:
        """
        Modify a pending order.
        
        Args:
            order_id: The order ID to modify.
            volume: Optional new volume (in lots).
            price: Optional new price (limit price or stop price).
            stop_loss: Optional new stop loss price.
            take_profit: Optional new take profit price.
            expiry_timestamp: Optional new expiry timestamp in milliseconds.
            
        Returns:
            A dictionary containing the updated order details.
            
        Raises:
            AuthenticationError: If not properly authenticated.
            ConnectionError: If not connected to the API server.
            OrderError: If modification fails or the order doesn't exist.
        """
        if not self._is_connected:
            raise ConnectionError("Not connected to cTrader API")
            
        if not self._is_account_authorized:
            raise AuthenticationError("Account not authorized")
            
        # Check if order exists
        if order_id not in self._orders:
            await self.get_orders(force_refresh=True)
            if order_id not in self._orders:
                raise OrderError(f"Order {order_id} not found")
                
        # Get current order
        order = self._orders[order_id]
        
        # Get symbol details
        symbol = await self._ensure_symbol_loaded(order.symbolId)
        
        # Calculate proper volume and price values
        digits = symbol.digits
        price_factor = 10 ** digits
        
        try:
            # Prepare amend order request
            request = ProtoOAAmendOrderReq()
            request.ctidTraderAccountId = self._ctid_trader_account_id
            request.orderId = order_id
            
            # Add fields to modify if provided
            if volume is not None:
                volume_lots = int(volume * 100)
                request.volume = volume_lots
                
            if price is not None:
                price_int = int(price * price_factor)
                if order.orderType == ProtoOAOrderType.LIMIT:
                    request.limitPrice = price_int
                elif order.orderType == ProtoOAOrderType.STOP:
                    request.stopPrice = price_int
                elif order.orderType == ProtoOAOrderType.STOP_LIMIT:
                    # For STOP_LIMIT orders, we assume the price refers to the limit price
                    # If you need to modify the stop price, you'll need to extend this logic
                    request.limitPrice = price_int
                    
            if stop_loss is not None:
                request.stopLoss = int(stop_loss * price_factor)
                
            if take_profit is not None:
                request.takeProfit = int(take_profit * price_factor)
                
            if expiry_timestamp is not None:
                request.expirationTimestamp = expiry_timestamp
                
            # Send the request
            client_msg_id = str(uuid.uuid4())
            execution_event = await self._send_request(request, client_msg_id)
            
            # Process the execution event to get updated order details
            if not isinstance(execution_event, ProtoOAExecutionEvent):
                raise OrderError("Unexpected response type")
                
            # The order will have been updated in our cache by the execution handler
            if hasattr(execution_event, "order") and execution_event.HasField("order"):
                updated_order = execution_event.order
                
                # Convert order to dictionary (similar to get_orders)
                order_dict = {
                    "order_id": updated_order.orderId,
                    "symbol_id": updated_order.symbolId,
                    "symbol_name": symbol.symbolName,
                    "volume": updated_order.volume / 100,
                    "trade_side": "BUY" if updated_order.tradeSide == ProtoOATradeSide.BUY else "SELL",
                    "order_type": self._get_order_type_name(updated_order.orderType),
                    "order_status": updated_order.orderStatus,
                }
                
                # Add price fields based on order type
                if updated_order.orderType == ProtoOAOrderType.LIMIT:
                    order_dict["limit_price"] = updated_order.limitPrice / price_factor
                    
                elif updated_order.orderType == ProtoOAOrderType.STOP:
                    order_dict["stop_price"] = updated_order.stopPrice / price_factor
                    
                elif updated_order.orderType == ProtoOAOrderType.STOP_LIMIT:
                    order_dict["stop_price"] = updated_order.stopPrice / price_factor
                    order_dict["limit_price"] = updated_order.limitPrice / price_factor
                    
                # Add expiry if present
                if updated_order.HasField("expirationTimestamp"):
                    order_dict["expiration_timestamp"] = updated_order.expirationTimestamp
                    
                # Add SL/TP if present
                if updated_order.HasField("stopLoss"):
                    order_dict["stop_loss"] = updated_order.stopLoss / price_factor
                    
                if updated_order.HasField("takeProfit"):
                    order_dict["take_profit"] = updated_order.takeProfit / price_factor
                    
                logger.info(f"Successfully amended order {order_id}")
                return order_dict
                
            # Handle case where order wasn't updated
            raise OrderError("Order execution did not update the order")
            
        except Exception as e:
            if isinstance(e, OrderError):
                raise
            logger.error(f"Failed to amend order {order_id}: {str(e)}")
            raise OrderError(f"Failed to amend order: {str(e)}") from e

    async def close_position(self, position_id: int, volume: float = None) -> bool:
        """
        Close a position (fully or partially).
        
        Args:
            position_id: The position ID to close.
            volume: Optional volume to close (in lots). If None, close the entire position.
            
        Returns:
            True if closure was successful.
            
        Raises:
            AuthenticationError: If not properly authenticated.
            ConnectionError: If not connected to the API server.
            PositionError: If closure fails or the position doesn't exist.
        """
        if not self._is_connected:
            raise ConnectionError("Not connected to cTrader API")
            
        if not self._is_account_authorized:
            raise AuthenticationError("Account not authorized")
            
        # Check if position exists
        if position_id not in self._positions:
            await self.get_positions(force_refresh=True)
            if position_id not in self._positions:
                raise PositionError(f"Position {position_id} not found")
                
        # Get current position
        position = self._positions[position_id]
        
        # If volume not specified, close entire position
        if volume is None:
            volume_lots = position.volume
        else:
            # Convert to lots format and validate
            volume_lots = int(volume * 100)
            if volume_lots <= 0 or volume_lots > position.volume:
                raise ValidationError(f"Invalid volume: {volume}. Must be between 0 and {position.volume / 100}")
                
        try:
            # Prepare close position request
            request = ProtoOAClosePositionReq()
            request.ctidTraderAccountId = self._ctid_trader_account_id
            request.positionId = position_id
            request.volume = volume_lots
            
            # Send the request
            client_msg_id = str(uuid.uuid4())
            execution_event = await self._send_request(request, client_msg_id)
            
            # Check if closure was successful
            if not isinstance(execution_event, ProtoOAExecutionEvent):
                raise PositionError("Unexpected response type")
                
            # If we got here, the closure was successful
            # If full closure, the position should have been removed from self._positions by the execution handler
            # If partial closure, the position volume will have been updated
            
            logger.info(f"Successfully closed position {position_id} ({volume_lots / 100} lots)")
            return True
            
        except Exception as e:
            logger.error(f"Failed to close position {position_id}: {str(e)}")
            raise PositionError(f"Failed to close position: {str(e)}") from e

    async def amend_position_sltp(
        self, position_id: int, stop_loss: float = None, take_profit: float = None
    ) -> Dict[str, Any]:
        """
        Modify a position's stop loss and/or take profit.
        
        Args:
            position_id: The position ID to modify.
            stop_loss: Optional new stop loss price.
            take_profit: Optional new take profit price.
            
        Returns:
            A dictionary containing the updated position details.
            
        Raises:
            AuthenticationError: If not properly authenticated.
            ConnectionError: If not connected to the API server.
            PositionError: If modification fails or the position doesn't exist.
        """
        if not self._is_connected:
            raise ConnectionError("Not connected to cTrader API")
            
        if not self._is_account_authorized:
            raise AuthenticationError("Account not authorized")
            
        # Check if position exists
        if position_id not in self._positions:
            await self.get_positions(force_refresh=True)
            if position_id not in self._positions:
                raise PositionError(f"Position {position_id} not found")
                
        # Get current position
        position = self._positions[position_id]
        
        # Get symbol details
        symbol = await self._ensure_symbol_loaded(position.symbolId)
        
        # Calculate proper price values
        digits = symbol.digits
        price_factor = 10 ** digits
        
        try:
            # Prepare amend position request
            request = ProtoOAAmendPositionSLTPReq()
            request.ctidTraderAccountId = self._ctid_trader_account_id
            request.positionId = position_id
            
            # Add fields to modify if provided
            if stop_loss is not None:
                request.stopLoss = int(stop_loss * price_factor)
                
            if take_profit is not None:
                request.takeProfit = int(take_profit * price_factor)
                
            # Send the request
            client_msg_id = str(uuid.uuid4())
            execution_event = await self._send_request(request, client_msg_id)
            
            # Process the execution event to get updated position details
            if not isinstance(execution_event, ProtoOAExecutionEvent):
                raise PositionError("Unexpected response type")
                
            # The position will have been updated in our cache by the execution handler
            if hasattr(execution_event, "position") and execution_event.HasField("position"):
                updated_position = execution_event.position
                
                # Convert position to dictionary (similar to get_positions)
                position_dict = {
                    "position_id": updated_position.positionId,
                    "symbol_id": updated_position.symbolId,
                    "symbol_name": symbol.symbolName,
                    "volume": updated_position.volume / 100,
                    "trade_side": "BUY" if updated_position.tradeSide == ProtoOATradeSide.BUY else "SELL",
                    "price": updated_position.price / price_factor,
                    "swap": updated_position.swap,
                    "commission": updated_position.commission,
                    "open_timestamp": updated_position.openTimestamp,
                }
                
                # Add SL/TP if present
                if updated_position.HasField("stopLoss"):
                    position_dict["stop_loss"] = updated_position.stopLoss / price_factor
                    
                if updated_position.HasField("takeProfit"):
                    position_dict["take_profit"] = updated_position.takeProfit / price_factor
                    
                logger.info(f"Successfully amended position {position_id} SL/TP")
                return position_dict
                
            # Handle case where position wasn't updated
            raise PositionError("Order execution did not update the position")
            
        except Exception as e:
            if isinstance(e, PositionError):
                raise
            logger.error(f"Failed to amend position {position_id} SL/TP: {str(e)}")
            raise PositionError(f"Failed to amend position SL/TP: {str(e)}") from e

    # --- Helper Methods ---
    
    async def _ensure_symbol_loaded(self, symbol_id: int) -> Any:
        """
        Ensure symbol details are loaded, fetching them if necessary.
        
        Args:
            symbol_id: The symbol ID to load.
            
        Returns:
            The symbol details object.
            
        Raises:
            InvalidSymbolError: If the symbol ID is invalid.
        """
        # Check if symbol is already loaded
        if symbol_id in self._symbols:
            return self._symbols[symbol_id]
            
        try:
            # Try to load from symbols list first (lighter)
            if not self._symbols:
                await self.get_symbols()
                if symbol_id in self._symbols:
                    return self._symbols[symbol_id]
                    
            # Need to fetch specific symbol details
            request = ProtoOASymbolByIdReq()
            request.ctidTraderAccountId = self._ctid_trader_account_id
            request.symbolId = symbol_id
            
            # Send the request
            response = await self._send_request(request)
            
            # Store symbol details
            if hasattr(response, "symbol") and response.HasField("symbol"):
                self._symbols[symbol_id] = response.symbol
                return response.symbol
                
            raise InvalidSymbolError(f"Symbol {symbol_id} not found")
            
        except Exception as e:
            if isinstance(e, InvalidSymbolError):
                raise
            logger.error(f"Failed to load symbol {symbol_id}: {str(e)}")
            raise InvalidSymbolError(f"Symbol {symbol_id} not found: {str(e)}") from e

    def _get_order_type_name(self, order_type: int) -> str:
        """Convert order type enum to readable string."""
        type_map = {
            ProtoOAOrderType.MARKET: "MARKET",
            ProtoOAOrderType.LIMIT: "LIMIT",
            ProtoOAOrderType.STOP: "STOP",
            ProtoOAOrderType.STOP_LIMIT: "STOP_LIMIT",
            ProtoOAOrderType.MARKET_RANGE: "MARKET_RANGE",
        }
        return type_map.get(order_type, f"UNKNOWN ({order_type})")