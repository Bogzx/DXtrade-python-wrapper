"""
DXTradeDashboardWrapper: A user-friendly Python wrapper for DXTRADE API trading dashboard functionalities.

This wrapper abstracts the complexities of direct API interaction for common dashboard-related tasks,
providing a simple interface for authentication, account data retrieval, order management,
and real-time data handling via WebSockets.
"""

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import requests
import websocket


# Custom exceptions
class DXTradeWrapperError(Exception):
    """Base exception for all DXTradeDashboardWrapper errors."""
    pass


class DXTradeAPIError(DXTradeWrapperError):
    """Exception raised for errors returned by the DXTRADE API."""
    pass


class AuthenticationError(DXTradeWrapperError):
    """Exception raised for authentication failures."""
    pass


class ConnectionError(DXTradeWrapperError):
    """Exception raised for connection failures."""
    pass


class OrderPlacementError(DXTradeWrapperError):
    """Exception raised for order placement failures."""
    pass


class WebSocketError(DXTradeWrapperError):
    """Exception raised for WebSocket related errors."""
    pass


# Data classes for structured responses
@dataclass
class Balance:
    """Represents account balance information."""
    equity: float
    balance: float
    margin: float
    free_margin: float
    margin_level: Optional[float] = None
    currency: Optional[str] = None


@dataclass
class Position:
    """Represents an open position."""
    position_id: str
    instrument: str
    quantity: float
    side: str  # 'BUY' or 'SELL'
    entry_price: float
    current_price: Optional[float] = None
    pnl: Optional[float] = None
    open_time: Optional[str] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


@dataclass
class Order:
    """Represents a pending order."""
    order_id: str
    instrument: str
    quantity: float
    side: str  # 'BUY' or 'SELL'
    order_type: str  # 'MARKET', 'LIMIT', 'STOP'
    status: str  # 'PENDING', 'FILLED', 'CANCELLED', etc.
    price: Optional[float] = None  # Limit price or stop price
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    created_time: Optional[str] = None
    position_effect: Optional[str] = None  # 'OPEN' or 'CLOSE'
    tif: Optional[str] = None  # Time In Force, e.g., 'GTC'
    position_code: Optional[str] = None  # For linked orders


class DXTradeDashboardWrapper:
    """
    A high-level, user-friendly wrapper for the DXTRADE trading platform API,
    specifically targeting functionalities essential for trading dashboards.
    
    This wrapper abstracts the complexities of direct API interaction and provides
    a simple interface for authentication, account data retrieval, order management,
    and real-time data handling via WebSockets.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        domain_or_vendor: str = "default",
        login_path: str = "/dxsca-web/login",
        websocket_path: str = "/websocket",
        logger: Optional[logging.Logger] = None
    ):
        """
        Initialize the DXTradeDashboardWrapper.

        Args:
            base_url: The base URL for the broker's DXTRADE REST API (e.g., "https://dxtrade.broker.com").
            username: User's trading account username.
            password: User's trading account password.
            domain_or_vendor: The domain or vendor identifier required for login (defaults to "default").
            login_path: The specific path for the login endpoint relative to base_url.
            websocket_path: The specific path for the WebSocket connection.
            logger: Optional standard Python logger instance for internal logging.
        """
        # Configuration
        self._base_url = base_url.rstrip('/')
        self._username = username
        self._password = password
        self._domain_or_vendor = domain_or_vendor
        self._login_path = login_path.lstrip('/')
        self._websocket_path = websocket_path.lstrip('/')
        
        # Use provided logger or create a default one
        self._logger = logger or logging.getLogger("DXTradeWrapper")
        if not self._logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
            self._logger.addHandler(handler)
            self._logger.setLevel(logging.INFO)
            
        # Authentication state
        self._session = requests.Session()
        self._auth_token = None
        self._is_authenticated = False
        self._account_id = None  # Will be set during login if available
        
        # WebSocket state
        self._ws_client = None
        self._ws_thread = None
        self._ws_connected = False
        self._ws_reconnect = True  # Flag to control reconnection attempts
        
        # Thread safety
        self._lock = threading.Lock()
        
        # Queues for asynchronous WebSocket data
        self.price_update_queue = queue.Queue()
        self.order_update_queue = queue.Queue()
        self.account_update_queue = queue.Queue()
        
        # Tracking subscribed instruments
        self._subscribed_instruments = set()
        self._account_updates_subscribed = False

    # Authentication methods
    def login(self) -> None:
        """
        Authenticate with the DXTRADE API.
        
        Raises:
            AuthenticationError: If authentication fails due to invalid credentials,
                                network issues, or unexpected response format.
        """
        login_url = f"{self._base_url}/{self._login_path}"
        
        # Prepare request payload
        payload = {
            "username": self._username,
            "password": self._password,
            # Some brokers use 'domain', others might use 'vendor'
            self._get_auth_param_name(): self._domain_or_vendor
        }
        
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
        try:
            self._logger.debug(f"Attempting login to {login_url}")
            response = self._session.post(
                login_url,
                headers=headers,
                json=payload,
                timeout=10
            )
            response.raise_for_status()
            
            # Parse response data
            response_data = response.json()
            
            # Extract auth token - adjust key based on actual response structure
            self._auth_token = self._extract_auth_token(response_data)
            
            if self._auth_token:
                self._is_authenticated = True
                self._configure_auth_headers()
                
                # Try to extract account ID if available
                self._account_id = self._extract_account_id(response_data)
                
                self._logger.info("Authentication successful")
                return
            else:
                self._is_authenticated = False
                self._logger.error("No authentication token found in login response")
                raise AuthenticationError("Login successful but no token received")
                
        except requests.exceptions.RequestException as e:
            self._is_authenticated = False
            self._logger.error(f"Login network error: {e}")
            raise AuthenticationError(f"Network error during login: {e}")
        except json.JSONDecodeError as e:
            self._is_authenticated = False
            self._logger.error(f"Failed to decode login response: {e}")
            raise AuthenticationError(f"Failed to decode login response: {e}")
        except Exception as e:
            self._is_authenticated = False
            self._logger.error(f"Unexpected error during login: {e}")
            raise AuthenticationError(f"Unexpected error during login: {e}")

    def logout(self) -> None:
        """
        Log out from the DXTRADE API and invalidate the current session.
        """
        # Disconnect WebSocket if connected
        if self._ws_connected:
            self.disconnect_websocket()
        
        # Check if we have a logout endpoint (if found in documentation)
        # logout_url = f"{self._base_url}/logout"  # Adjust based on actual API
        # try:
        #     self._session.post(logout_url, timeout=10)
        # except Exception as e:
        #     self._logger.warning(f"Error during logout request: {e}")
        
        # Reset session and authentication state
        self._session = requests.Session()
        self._auth_token = None
        self._is_authenticated = False
        self._account_id = None
        self._logger.info("Logged out successfully")

    @property
    def is_authenticated(self) -> bool:
        """
        Check if the wrapper is currently authenticated.
        
        Returns:
            bool: True if authenticated, False otherwise.
        """
        return self._is_authenticated

    # Account data methods
    def get_balance(self) -> Balance:
        """
        Retrieve the current account balance.
        
        Returns:
            Balance: A Balance object containing account balance details.
            
        Raises:
            AuthenticationError: If not authenticated.
            DXTradeAPIError: If the API request fails.
        """
        self._check_authenticated()
        
        account_id = self._get_account_id()
        balance_url = f"{self._base_url}/accounts/{account_id}/portfolio"
        
        try:
            response = self._session.get(balance_url, timeout=10)
            response.raise_for_status()
            
            balance_data = response.json()
            return self._parse_balance_data(balance_data)
            
        except requests.exceptions.RequestException as e:
            self._logger.error(f"Get balance network error: {e}")
            raise DXTradeAPIError(f"Network error getting balance: {e}")
        except json.JSONDecodeError as e:
            self._logger.error(f"Failed to decode balance response: {e}")
            raise DXTradeAPIError(f"Failed to decode balance response: {e}")
        except Exception as e:
            self._logger.error(f"Unexpected error getting balance: {e}")
            raise DXTradeAPIError(f"Unexpected error getting balance: {e}")

    def get_positions(self) -> List[Position]:
        """
        Retrieve a list of currently open positions.
        
        Returns:
            List[Position]: A list of Position objects representing open positions.
            
        Raises:
            AuthenticationError: If not authenticated.
            DXTradeAPIError: If the API request fails.
        """
        self._check_authenticated()
        
        account_id = self._get_account_id()
        positions_url = f"{self._base_url}/accounts/{account_id}/positions"
        
        try:
            response = self._session.get(positions_url, timeout=10)
            response.raise_for_status()
            
            positions_data = response.json()
            return self._parse_positions_data(positions_data)
            
        except requests.exceptions.RequestException as e:
            self._logger.error(f"Get positions network error: {e}")
            raise DXTradeAPIError(f"Network error getting positions: {e}")
        except json.JSONDecodeError as e:
            self._logger.error(f"Failed to decode positions response: {e}")
            raise DXTradeAPIError(f"Failed to decode positions response: {e}")
        except Exception as e:
            self._logger.error(f"Unexpected error getting positions: {e}")
            raise DXTradeAPIError(f"Unexpected error getting positions: {e}")

    def get_orders(self) -> List[Order]:
        """
        Retrieve a list of active pending orders.
        
        Returns:
            List[Order]: A list of Order objects representing pending orders.
            
        Raises:
            AuthenticationError: If not authenticated.
            DXTradeAPIError: If the API request fails.
        """
        self._check_authenticated()
        
        account_id = self._get_account_id()
        orders_url = f"{self._base_url}/accounts/{account_id}/orders"
        
        try:
            response = self._session.get(orders_url, timeout=10)
            response.raise_for_status()
            
            orders_data = response.json()
            return self._parse_orders_data(orders_data)
            
        except requests.exceptions.RequestException as e:
            self._logger.error(f"Get orders network error: {e}")
            raise DXTradeAPIError(f"Network error getting orders: {e}")
        except json.JSONDecodeError as e:
            self._logger.error(f"Failed to decode orders response: {e}")
            raise DXTradeAPIError(f"Failed to decode orders response: {e}")
        except Exception as e:
            self._logger.error(f"Unexpected error getting orders: {e}")
            raise DXTradeAPIError(f"Unexpected error getting orders: {e}")

    def get_order_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Retrieve historical orders/trades.
        
        Args:
            limit: Maximum number of records to return.
            
        Returns:
            List[Dict[str, Any]]: A list of dictionaries representing historical orders/trades.
            
        Raises:
            AuthenticationError: If not authenticated.
            DXTradeAPIError: If the API request fails.
        """
        self._check_authenticated()
        
        account_id = self._get_account_id()
        history_url = f"{self._base_url}/accounts/{account_id}/history"
        params = {"limit": limit}
        
        try:
            response = self._session.get(history_url, params=params, timeout=10)
            response.raise_for_status()
            
            history_data = response.json()
            # Return raw history data as structure might vary
            return history_data
            
        except requests.exceptions.RequestException as e:
            self._logger.error(f"Get order history network error: {e}")
            raise DXTradeAPIError(f"Network error getting order history: {e}")
        except json.JSONDecodeError as e:
            self._logger.error(f"Failed to decode order history response: {e}")
            raise DXTradeAPIError(f"Failed to decode order history response: {e}")
        except Exception as e:
            self._logger.error(f"Unexpected error getting order history: {e}")
            raise DXTradeAPIError(f"Unexpected error getting order history: {e}")

    # Order management methods
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
        **kwargs
    ) -> Dict[str, Any]:
        """
        Place a new trading order.
        
        Args:
            instrument: The instrument to trade (symbol).
            side: 'BUY' or 'SELL'.
            quantity: Order quantity.
            order_type: 'MARKET', 'LIMIT', or 'STOP'.
            price: Required for 'LIMIT' or 'STOP' orders.
            stop_loss: Optional stop loss price.
            take_profit: Optional take profit price.
            position_effect: 'OPEN' (new position) or 'CLOSE' (close existing).
            tif: Time In Force, typically 'GTC' (Good Till Cancel).
            **kwargs: Additional parameters for the order.
            
        Returns:
            Dict[str, Any]: Details of the placed order, including order_id.
            
        Raises:
            AuthenticationError: If not authenticated.
            OrderPlacementError: If order placement fails.
            ValueError: If required parameters are missing or invalid.
        """
        self._check_authenticated()
        
        # Validate inputs
        side = side.upper()
        order_type = order_type.upper()
        position_effect = position_effect.upper()
        
        if side not in ['BUY', 'SELL']:
            raise ValueError("Side must be 'BUY' or 'SELL'")
        
        if order_type not in ['MARKET', 'LIMIT', 'STOP']:
            raise ValueError("Order type must be 'MARKET', 'LIMIT', or 'STOP'")
        
        if order_type in ['LIMIT', 'STOP'] and price is None:
            raise ValueError(f"Price is required for {order_type} orders")
        
        account_id = self._get_account_id()
        orders_url = f"{self._base_url}/accounts/{account_id}/orders"
        
        # Construct order payload
        payload = {
            "instrument": instrument,
            "side": side,
            "quantity": quantity,
            "type": order_type,
            "positionEffect": position_effect,
            "tif": tif
        }
        
        # Add price for LIMIT or STOP orders
        if order_type == 'LIMIT':
            payload["limitPrice"] = price
        elif order_type == 'STOP':
            payload["stopPrice"] = price
        
        # Add any additional parameters
        payload.update(kwargs)
        
        try:
            self._logger.debug(f"Placing order: {payload}")
            response = self._session.post(orders_url, json=payload, timeout=10)
            response.raise_for_status()
            
            order_response = response.json()
            
            # If SL/TP are provided, handle them appropriately
            if (stop_loss or take_profit) and position_effect == "OPEN":
                position_code = self._extract_position_code(order_response)
                if position_code:
                    self._place_linked_sl_tp_orders(
                        position_code=position_code,
                        quantity=quantity,
                        original_side=side,
                        stop_loss_price=stop_loss,
                        take_profit_price=take_profit
                    )
            
            return order_response
            
        except requests.exceptions.RequestException as e:
            self._logger.error(f"Order placement network error: {e}")
            raise OrderPlacementError(f"Network error placing order: {e}")
        except json.JSONDecodeError as e:
            self._logger.error(f"Failed to decode order placement response: {e}")
            raise OrderPlacementError(f"Failed to decode order placement response: {e}")
        except Exception as e:
            self._logger.error(f"Unexpected error placing order: {e}")
            raise OrderPlacementError(f"Unexpected error placing order: {e}")

    def modify_order(
        self,
        order_id: str,
        new_price: Optional[float] = None,
        new_quantity: Optional[float] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Modify an existing pending order.
        
        Args:
            order_id: The ID of the order to modify.
            new_price: New price for the order (for LIMIT or STOP orders).
            new_quantity: New quantity for the order.
            **kwargs: Additional parameters to modify.
            
        Returns:
            Dict[str, Any]: Details of the modified order.
            
        Raises:
            AuthenticationError: If not authenticated.
            DXTradeAPIError: If the API request fails.
        """
        self._check_authenticated()
        
        account_id = self._get_account_id()
        modify_url = f"{self._base_url}/accounts/{account_id}/orders/{order_id}"
        
        # Construct payload with only the fields to modify
        payload = {}
        if new_price is not None:
            payload["price"] = new_price
        if new_quantity is not None:
            payload["qty"] = new_quantity
        
        # Add any additional parameters
        payload.update(kwargs)
        
        if not payload:
            raise ValueError("At least one parameter to modify must be provided")
        
        try:
            self._logger.debug(f"Modifying order {order_id}: {payload}")
            response = self._session.patch(modify_url, json=payload, timeout=10)
            response.raise_for_status()
            
            return response.json()
            
        except requests.exceptions.RequestException as e:
            self._logger.error(f"Order modification network error: {e}")
            raise DXTradeAPIError(f"Network error modifying order: {e}")
        except json.JSONDecodeError as e:
            self._logger.error(f"Failed to decode order modification response: {e}")
            raise DXTradeAPIError(f"Failed to decode order modification response: {e}")
        except Exception as e:
            self._logger.error(f"Unexpected error modifying order: {e}")
            raise DXTradeAPIError(f"Unexpected error modifying order: {e}")

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """
        Cancel an existing pending order.
        
        Args:
            order_id: The ID of the order to cancel.
            
        Returns:
            Dict[str, Any]: Confirmation of cancellation.
            
        Raises:
            AuthenticationError: If not authenticated.
            DXTradeAPIError: If the API request fails.
        """
        self._check_authenticated()
        
        account_id = self._get_account_id()
        cancel_url = f"{self._base_url}/accounts/{account_id}/orders/{order_id}"
        
        try:
            self._logger.debug(f"Canceling order {order_id}")
            response = self._session.delete(cancel_url, timeout=10)
            response.raise_for_status()
            
            # Return confirmation (might be empty for DELETE requests)
            try:
                return response.json()
            except json.JSONDecodeError:
                return {"success": True, "order_id": order_id}
            
        except requests.exceptions.RequestException as e:
            self._logger.error(f"Order cancellation network error: {e}")
            raise DXTradeAPIError(f"Network error canceling order: {e}")
        except Exception as e:
            self._logger.error(f"Unexpected error canceling order: {e}")
            raise DXTradeAPIError(f"Unexpected error canceling order: {e}")

    def close_position(
        self,
        position_id: str,
        quantity: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Close an open position, either fully or partially.
        
        Args:
            position_id: The ID of the position to close.
            quantity: Quantity to close. If None, the entire position is closed.
            
        Returns:
            Dict[str, Any]: Confirmation of position closure.
            
        Raises:
            AuthenticationError: If not authenticated.
            DXTradeAPIError: If the API request fails.
        """
        self._check_authenticated()
        
        account_id = self._get_account_id()
        close_url = f"{self._base_url}/positions/{position_id}"
        
        payload = {}
        if quantity is not None:
            payload["qty"] = quantity
        
        try:
            self._logger.debug(f"Closing position {position_id}")
            if payload:
                # Partial close with PATCH
                response = self._session.patch(close_url, json=payload, timeout=10)
            else:
                # Full close with DELETE
                response = self._session.delete(close_url, timeout=10)
                
            response.raise_for_status()
            
            # Return confirmation (might be empty for DELETE requests)
            try:
                return response.json()
            except json.JSONDecodeError:
                return {"success": True, "position_id": position_id}
            
        except requests.exceptions.RequestException as e:
            self._logger.error(f"Position close network error: {e}")
            raise DXTradeAPIError(f"Network error closing position: {e}")
        except Exception as e:
            self._logger.error(f"Unexpected error closing position: {e}")
            raise DXTradeAPIError(f"Unexpected error closing position: {e}")

    def modify_position_sl_tp(
        self,
        position_id: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Modify the Stop Loss and/or Take Profit levels of an open position.
        
        Args:
            position_id: The ID of the position to modify.
            stop_loss: New Stop Loss price. None to leave unchanged.
            take_profit: New Take Profit price. None to leave unchanged.
            
        Returns:
            Dict[str, Any]: Details of the modified position.
            
        Raises:
            AuthenticationError: If not authenticated.
            DXTradeAPIError: If the API request fails.
            ValueError: If neither stop_loss nor take_profit is provided.
        """
        self._check_authenticated()
        
        if stop_loss is None and take_profit is None:
            raise ValueError("At least one of stop_loss or take_profit must be provided")
        
        modify_url = f"{self._base_url}/positions/{position_id}"
        
        # Construct payload with only the fields to modify
        payload = {}
        if stop_loss is not None:
            payload["stopLoss"] = stop_loss
        if take_profit is not None:
            payload["takeProfit"] = take_profit
        
        try:
            self._logger.debug(f"Modifying position {position_id} SL/TP: {payload}")
            response = self._session.patch(modify_url, json=payload, timeout=10)
            response.raise_for_status()
            
            return response.json()
            
        except requests.exceptions.RequestException as e:
            self._logger.error(f"Position SL/TP modification network error: {e}")
            raise DXTradeAPIError(f"Network error modifying position SL/TP: {e}")
        except json.JSONDecodeError as e:
            self._logger.error(f"Failed to decode position SL/TP modification response: {e}")
            raise DXTradeAPIError(f"Failed to decode position SL/TP modification response: {e}")
        except Exception as e:
            self._logger.error(f"Unexpected error modifying position SL/TP: {e}")
            raise DXTradeAPIError(f"Unexpected error modifying position SL/TP: {e}")

    # WebSocket methods
    def connect_websocket(self) -> None:
        """
        Establish a WebSocket connection for receiving real-time updates.
        
        Raises:
            AuthenticationError: If not authenticated.
            WebSocketError: If WebSocket connection fails.
        """
        self._check_authenticated()
        
        if self._ws_connected or self._ws_thread is not None:
            self._logger.info("WebSocket already connected")
            return
        
        # Prepare WebSocket thread
        self._ws_reconnect = True
        self._ws_thread = threading.Thread(
            target=self._websocket_listener_thread,
            daemon=True
        )
        self._ws_thread.start()
        
        # Wait briefly to allow connection to establish
        time.sleep(0.5)
        
        if not self._ws_connected:
            self._logger.warning("WebSocket connection not established immediately")

    def disconnect_websocket(self) -> None:
        """
        Disconnect the WebSocket connection.
        """
        self._ws_reconnect = False
        
        if self._ws_client:
            try:
                self._ws_client.close()
            except Exception as e:
                self._logger.warning(f"Error closing WebSocket: {e}")
            
        self._ws_client = None
        self._ws_connected = False
        
        # Reset subscriptions
        self._subscribed_instruments = set()
        self._account_updates_subscribed = False
        
        if self._ws_thread:
            # Don't wait indefinitely
            self._ws_thread.join(timeout=1.0)
            self._ws_thread = None

    def subscribe_market_data(self, instruments: List[str]) -> None:
        """
        Subscribe to real-time market data for specified instruments.
        
        Args:
            instruments: List of instrument symbols to subscribe to.
            
        Raises:
            WebSocketError: If WebSocket is not connected.
        """
        if not self._ws_connected:
            raise WebSocketError("WebSocket not connected. Call connect_websocket() first.")
        
        # Filter out already subscribed instruments
        new_instruments = [i for i in instruments if i not in self._subscribed_instruments]
        if not new_instruments:
            return
        
        # Construct subscription message
        subscription_message = {
            "type": "subscribe",
            "instruments": new_instruments
        }
        
        try:
            self._ws_client.send(json.dumps(subscription_message))
            self._subscribed_instruments.update(new_instruments)
            self._logger.info(f"Subscribed to market data for: {new_instruments}")
        except Exception as e:
            self._logger.error(f"Error subscribing to market data: {e}")
            raise WebSocketError(f"Failed to subscribe to market data: {e}")

    def unsubscribe_market_data(self, instruments: List[str]) -> None:
        """
        Unsubscribe from real-time market data for specified instruments.
        
        Args:
            instruments: List of instrument symbols to unsubscribe from.
            
        Raises:
            WebSocketError: If WebSocket is not connected.
        """
        if not self._ws_connected:
            raise WebSocketError("WebSocket not connected. Call connect_websocket() first.")
        
        # Filter only currently subscribed instruments
        to_unsubscribe = [i for i in instruments if i in self._subscribed_instruments]
        if not to_unsubscribe:
            return
        
        # Construct unsubscribe message
        unsubscribe_message = {
            "type": "unsubscribe",
            "instruments": to_unsubscribe
        }
        
        try:
            self._ws_client.send(json.dumps(unsubscribe_message))
            self._subscribed_instruments.difference_update(to_unsubscribe)
            self._logger.info(f"Unsubscribed from market data for: {to_unsubscribe}")
        except Exception as e:
            self._logger.error(f"Error unsubscribing from market data: {e}")
            raise WebSocketError(f"Failed to unsubscribe from market data: {e}")

    def subscribe_account_updates(self) -> None:
        """
        Subscribe to real-time account updates (orders, positions, balance).
        
        Raises:
            WebSocketError: If WebSocket is not connected.
        """
        if not self._ws_connected:
            raise WebSocketError("WebSocket not connected. Call connect_websocket() first.")
        
        if self._account_updates_subscribed:
            return
        
        # Construct subscription message
        subscription_message = {
            "type": "subscribe",
            "channel": "account"
        }
        
        try:
            self._ws_client.send(json.dumps(subscription_message))
            self._account_updates_subscribed = True
            self._logger.info("Subscribed to account updates")
        except Exception as e:
            self._logger.error(f"Error subscribing to account updates: {e}")
            raise WebSocketError(f"Failed to subscribe to account updates: {e}")

    # Internal helper methods
    def _check_authenticated(self) -> None:
        """
        Check if the wrapper is authenticated and raise an exception if not.
        
        Raises:
            AuthenticationError: If not authenticated.
        """
        if not self._is_authenticated:
            raise AuthenticationError("Not authenticated. Call login() first.")

    def _get_account_id(self) -> str:
        """
        Get the account ID, with fallback to a default if not available.
        
        Returns:
            str: The account ID.
        """
        return self._account_id or "primary"  # Fallback to a default

    def _get_auth_param_name(self) -> str:
        """
        Get the appropriate authentication parameter name (domain or vendor).
        
        Returns:
            str: The parameter name.
        """
        # Default to 'domain' but could be overridden based on broker
        return "domain"  # or "vendor" for some brokers

    def _extract_auth_token(self, response_data: Dict[str, Any]) -> Optional[str]:
        """
        Extract the authentication token from the login response.
        
        Args:
            response_data: The parsed JSON response from login.
            
        Returns:
            Optional[str]: The authentication token, or None if not found.
        """
        # Try common token field names
        for key in ["token", "access_token", "authToken", "sessionToken"]:
            if key in response_data:
                return response_data[key]
        
        # Handle nested structures (adjust based on actual response format)
        if "data" in response_data and isinstance(response_data["data"], dict):
            for key in ["token", "access_token", "authToken", "sessionToken"]:
                if key in response_data["data"]:
                    return response_data["data"][key]
        
        return None

    def _extract_account_id(self, response_data: Dict[str, Any]) -> Optional[str]:
        """
        Extract the account ID from the login response.
        
        Args:
            response_data: The parsed JSON response from login.
            
        Returns:
            Optional[str]: The account ID, or None if not found.
        """
        # Try common account ID field names
        for key in ["accountId", "account_id", "accountCode"]:
            if key in response_data:
                return response_data[key]
        
        # Handle nested structures (adjust based on actual response format)
        if "data" in response_data and isinstance(response_data["data"], dict):
            for key in ["accountId", "account_id", "accountCode"]:
                if key in response_data["data"]:
                    return response_data["data"][key]
        
        return None

    def _configure_auth_headers(self) -> None:
        """
        Configure authentication headers for subsequent requests.
        """
        if self._auth_token:
            self._session.headers.update({
                "Authorization": f"Bearer {self._auth_token}"
            })

    def _extract_position_code(self, order_response: Dict[str, Any]) -> Optional[str]:
        """
        Extract the position code from an order response for linked SL/TP orders.
        
        Args:
            order_response: The parsed JSON response from a place_order call.
            
        Returns:
            Optional[str]: The position code, or None if not found.
        """
        # Try common position code field names
        for key in ["positionCode", "position_code", "positionId", "position_id"]:
            if key in order_response:
                return order_response[key]
        
        return None

    def _place_linked_sl_tp_orders(
        self,
        position_code: str,
        quantity: float,
        original_side: str,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None
    ) -> None:
        """
        Place linked Stop Loss and/or Take Profit orders for a position.
        
        Args:
            position_code: The position code to link the orders to.
            quantity: Order quantity.
            original_side: The side of the original order ('BUY' or 'SELL').
            stop_loss_price: Stop Loss price.
            take_profit_price: Take Profit price.
        """
        account_id = self._get_account_id()
        orders_url = f"{self._base_url}/accounts/{account_id}/orders"
        
        # Determine opposite side for closing orders
        close_side = "SELL" if original_side == "BUY" else "BUY"
        
        try:
            # Place Stop Loss order if price provided
            if stop_loss_price is not None:
                sl_payload = {
                    "type": "STOP",
                    "positionEffect": "CLOSE",
                    "positionCode": position_code,
                    "quantity": quantity,
                    "side": close_side,
                    "stopPrice": stop_loss_price,
                    "tif": "GTC"
                }
                
                self._logger.debug(f"Placing Stop Loss order: {sl_payload}")
                sl_response = self._session.post(orders_url, json=sl_payload, timeout=10)
                sl_response.raise_for_status()
                self._logger.info("Stop Loss order placed successfully")
            
            # Place Take Profit order if price provided
            if take_profit_price is not None:
                tp_payload = {
                    "type": "LIMIT",
                    "positionEffect": "CLOSE",
                    "positionCode": position_code,
                    "quantity": quantity,
                    "side": close_side,
                    "limitPrice": take_profit_price,
                    "tif": "GTC"
                }
                
                self._logger.debug(f"Placing Take Profit order: {tp_payload}")
                tp_response = self._session.post(orders_url, json=tp_payload, timeout=10)
                tp_response.raise_for_status()
                self._logger.info("Take Profit order placed successfully")
                
        except Exception as e:
            self._logger.error(f"Error placing linked SL/TP orders: {e}")
            # We don't raise here to avoid affecting the main order
            # But we log the error for debugging

    def _parse_balance_data(self, balance_data: Dict[str, Any]) -> Balance:
        """
        Parse the balance data from the API response.
        
        Args:
            balance_data: The parsed JSON response from get_balance.
            
        Returns:
            Balance: A Balance object containing account balance details.
        """
        # This needs to be adjusted based on actual response format
        try:
            # Example parsing - adjust based on actual API response
            equity = float(balance_data.get("equity", 0))
            balance = float(balance_data.get("balance", 0))
            margin = float(balance_data.get("margin", 0))
            free_margin = float(balance_data.get("freeMargin", 0))
            margin_level = float(balance_data.get("marginLevel", 0)) if "marginLevel" in balance_data else None
            currency = balance_data.get("currency")
            
            return Balance(
                equity=equity,
                balance=balance,
                margin=margin,
                free_margin=free_margin,
                margin_level=margin_level,
                currency=currency
            )
        except (KeyError, ValueError) as e:
            self._logger.error(f"Error parsing balance data: {e}")
            # Return default values if parsing fails
            return Balance(
                equity=0.0,
                balance=0.0,
                margin=0.0,
                free_margin=0.0
            )

    def _parse_positions_data(self, positions_data: List[Dict[str, Any]]) -> List[Position]:
        """
        Parse the positions data from the API response.
        
        Args:
            positions_data: The parsed JSON response from get_positions.
            
        Returns:
            List[Position]: A list of Position objects.
        """
        # This needs to be adjusted based on actual response format
        positions = []
        
        try:
            for position_data in positions_data:
                position = Position(
                    position_id=str(position_data.get("id", "")),
                    instrument=position_data.get("instrument", ""),
                    quantity=float(position_data.get("quantity", 0)),
                    side=position_data.get("side", ""),
                    entry_price=float(position_data.get("entryPrice", 0)),
                    current_price=float(position_data.get("currentPrice", 0)) if "currentPrice" in position_data else None,
                    pnl=float(position_data.get("pnl", 0)) if "pnl" in position_data else None,
                    open_time=position_data.get("openTime"),
                    stop_loss=float(position_data.get("stopLoss", 0)) if "stopLoss" in position_data else None,
                    take_profit=float(position_data.get("takeProfit", 0)) if "takeProfit" in position_data else None
                )
                positions.append(position)
                
        except (KeyError, ValueError) as e:
            self._logger.error(f"Error parsing positions data: {e}")
            
        return positions

    def _parse_orders_data(self, orders_data: List[Dict[str, Any]]) -> List[Order]:
        """
        Parse the orders data from the API response.
        
        Args:
            orders_data: The parsed JSON response from get_orders.
            
        Returns:
            List[Order]: A list of Order objects.
        """
        # This needs to be adjusted based on actual response format
        orders = []
        
        try:
            for order_data in orders_data:
                order = Order(
                    order_id=str(order_data.get("id", "")),
                    instrument=order_data.get("instrument", ""),
                    quantity=float(order_data.get("quantity", 0)),
                    side=order_data.get("side", ""),
                    order_type=order_data.get("type", ""),
                    status=order_data.get("status", ""),
                    price=float(order_data.get("price", 0)) if "price" in order_data else None,
                    stop_loss=float(order_data.get("stopLoss", 0)) if "stopLoss" in order_data else None,
                    take_profit=float(order_data.get("takeProfit", 0)) if "takeProfit" in order_data else None,
                    created_time=order_data.get("createdTime"),
                    position_effect=order_data.get("positionEffect"),
                    tif=order_data.get("tif"),
                    position_code=order_data.get("positionCode")
                )
                orders.append(order)
                
        except (KeyError, ValueError) as e:
            self._logger.error(f"Error parsing orders data: {e}")
            
        return orders

    def _build_websocket_url(self) -> str:
        """
        Build the WebSocket connection URL.
        
        Returns:
            str: The full WebSocket URL.
        """
        # Convert HTTP(S) to WS(S)
        ws_protocol = "wss://" if self._base_url.startswith("https://") else "ws://"
        base_without_protocol = self._base_url.split("://")[1]
        
        # Construct WebSocket URL with auth token if available
        ws_url = f"{ws_protocol}{base_without_protocol}/{self._websocket_path}"
        
        # Add authentication token as query parameter if available
        if self._auth_token:
            ws_url += f"?token={self._auth_token}"
            
        # Add any additional required parameters (adjust based on actual requirements)
        ws_url += f"&account={self._account_id}" if self._account_id else ""
        
        return ws_url

    def _build_websocket_headers(self) -> List[str]:
        """
        Build the WebSocket connection headers.
        
        Returns:
            List[str]: A list of header strings in the format "Header: Value".
        """
        headers = []
        
        # Add standard headers (adjust based on actual requirements)
        headers.append("User-Agent: DXTradeDashboardWrapper/1.0")
        
        # Add content type
        headers.append("Content-Type: application/json")
        
        # Add any framework-specific headers if needed (adjust based on research findings)
        headers.append("X-Atmosphere-tracking-id: 0")
        headers.append("X-Atmosphere-Framework: 1.0")
        headers.append("X-atmo-protocol: true")
        
        # Add authorization header if token is available and not already in URL
        if self._auth_token and "token=" not in self._build_websocket_url():
            headers.append(f"Authorization: Bearer {self._auth_token}")
            
        return headers

    def _websocket_listener_thread(self) -> None:
        """
        Target function for the WebSocket listener thread.
        """
        ws_url = self._build_websocket_url()
        headers = self._build_websocket_headers()
        
        self._logger.info(f"Connecting to WebSocket: {ws_url}")
        
        # Create WebSocket client
        self._ws_client = websocket.WebSocketApp(
            ws_url,
            header=headers,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close
        )
        
        # Run WebSocket connection with ping/pong for keep-alive
        retry_count = 0
        while self._ws_reconnect:
            try:
                # Run the WebSocket client
                self._ws_client.run_forever(ping_interval=30, ping_timeout=10)
                
                # If we get here, the connection was closed
                if not self._ws_reconnect:
                    break
                
                # Calculate backoff time (exponential with max)
                backoff_time = min(2 ** retry_count, 30)
                retry_count += 1
                
                self._logger.warning(f"WebSocket disconnected. Reconnecting in {backoff_time} seconds...")
                time.sleep(backoff_time)
                
            except Exception as e:
                self._logger.error(f"WebSocket thread error: {e}")
                time.sleep(5)  # Brief delay before retry

    def _on_open(self, ws) -> None:
        """
        Callback when WebSocket connection is established.
        
        Args:
            ws: The WebSocket client instance.
        """
        self._ws_connected = True
        self._logger.info("WebSocket connection established")
        
        # Re-subscribe to previously subscribed data if any
        if self._subscribed_instruments:
            self.subscribe_market_data(list(self._subscribed_instruments))
            
        if self._account_updates_subscribed:
            self.subscribe_account_updates()

    def _on_message(self, ws, message_str: str) -> None:
        """
        Callback when a message is received on the WebSocket.
        
        Args:
            ws: The WebSocket client instance.
            message_str: The message string received.
        """
        try:
            message = json.loads(message_str)
            message_type = self._determine_message_type(message)
            
            if message_type == "price_update":
                parsed_data = self._parse_price_update(message)
                self.price_update_queue.put(parsed_data)
                
            elif message_type == "order_update":
                parsed_data = self._parse_order_update(message)
                self.order_update_queue.put(parsed_data)
                
            elif message_type == "account_update":
                parsed_data = self._parse_account_update(message)
                self.account_update_queue.put(parsed_data)
                
            elif message_type == "position_update":
                parsed_data = self._parse_position_update(message)
                # Add to account updates as it's account-related
                self.account_update_queue.put(parsed_data)
                
            elif message_type == "error":
                self._logger.error(f"WebSocket error message: {message}")
                
            else:
                self._logger.debug(f"Received unhandled message type: {message_type}")
                
        except json.JSONDecodeError:
            self._logger.error(f"Failed to decode WebSocket message: {message_str}")
        except Exception as e:
            self._logger.error(f"Error processing WebSocket message: {e}")

    def _on_error(self, ws, error) -> None:
        """
        Callback when a WebSocket error occurs.
        
        Args:
            ws: The WebSocket client instance.
            error: The error that occurred.
        """
        self._logger.error(f"WebSocket error: {error}")
        self._ws_connected = False

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        """
        Callback when WebSocket connection is closed.
        
        Args:
            ws: The WebSocket client instance.
            close_status_code: The status code for the closure.
            close_msg: The closure message.
        """
        self._logger.info(f"WebSocket closed. Status code: {close_status_code}, Message: {close_msg}")
        self._ws_connected = False

    def _determine_message_type(self, message: Dict[str, Any]) -> str:
        """
        Determine the type of a WebSocket message.
        
        Args:
            message: The parsed message.
            
        Returns:
            str: The message type ("price_update", "order_update", "account_update", "error", "unknown").
        """
        # First check for explicit type field
        if "type" in message:
            msg_type = message["type"].lower()
            
            if "price" in msg_type or "quote" in msg_type:
                return "price_update"
            elif "order" in msg_type:
                return "order_update"
            elif "account" in msg_type or "balance" in msg_type:
                return "account_update"
            elif "position" in msg_type:
                return "position_update"
            elif "error" in msg_type:
                return "error"
            else:
                return msg_type
        
        # If no type field, infer from content
        if "symbol" in message and ("bid" in message or "ask" in message):
            return "price_update"
        elif "orderId" in message or "order_id" in message:
            return "order_update"
        elif "balance" in message or "equity" in message:
            return "account_update"
        elif "positionId" in message or "position_id" in message:
            return "position_update"
        elif "error" in message or "errorMessage" in message:
            return "error"
            
        return "unknown"

    def _parse_price_update(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parse a price update message.
        
        Args:
            message: The parsed message.
            
        Returns:
            Dict[str, Any]: Structured price update data.
        """
        # Create a standardized format, adjusting field names as needed
        update = {
            "type": "price_update",
            "timestamp": message.get("timestamp", time.time() * 1000)
        }
        
        # Extract instrument/symbol
        for key in ["symbol", "instrument", "instrumentId"]:
            if key in message:
                update["instrument"] = message[key]
                break
        
        # Extract price data
        for key in ["bid", "bidPrice", "bid_price"]:
            if key in message:
                update["bid"] = float(message[key])
                break
                
        for key in ["ask", "askPrice", "ask_price"]:
            if key in message:
                update["ask"] = float(message[key])
                break
                
        for key in ["last", "lastPrice", "last_price"]:
            if key in message:
                update["last"] = float(message[key])
                break
        
        # Calculate spread if bid and ask are available
        if "bid" in update and "ask" in update:
            update["spread"] = update["ask"] - update["bid"]
        
        return update

    def _parse_order_update(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parse an order update message.
        
        Args:
            message: The parsed message.
            
        Returns:
            Dict[str, Any]: Structured order update data.
        """
        # Create a standardized format, adjusting field names as needed
        update = {
            "type": "order_update",
            "timestamp": message.get("timestamp", time.time() * 1000)
        }
        
        # Extract order identifier
        for key in ["orderId", "order_id", "id"]:
            if key in message:
                update["order_id"] = str(message[key])
                break
        
        # Extract other order details
        field_mappings = {
            "instrument": ["instrument", "symbol", "instrumentId"],
            "status": ["status", "orderStatus", "order_status"],
            "side": ["side", "direction"],
            "order_type": ["type", "orderType", "order_type"],
            "quantity": ["quantity", "qty", "size"],
            "filled_quantity": ["filledQuantity", "filled_qty", "executed"],
            "price": ["price", "limitPrice", "limit_price", "stopPrice", "stop_price"],
            "position_id": ["positionId", "position_id", "positionCode"],
            "created_time": ["createdTime", "created_time", "timestamp"]
        }
        
        for target_field, source_fields in field_mappings.items():
            for source_field in source_fields:
                if source_field in message:
                    update[target_field] = message[source_field]
                    break
        
        # Convert numeric fields
        for field in ["quantity", "filled_quantity", "price"]:
            if field in update and update[field] is not None:
                try:
                    update[field] = float(update[field])
                except (ValueError, TypeError):
                    pass
        
        return update

    def _parse_account_update(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parse an account update message.
        
        Args:
            message: The parsed message.
            
        Returns:
            Dict[str, Any]: Structured account update data.
        """
        # Create a standardized format, adjusting field names as needed
        update = {
            "type": "account_update",
            "timestamp": message.get("timestamp", time.time() * 1000)
        }
        
        # Extract account data
        field_mappings = {
            "balance": ["balance", "accountBalance", "account_balance"],
            "equity": ["equity", "accountEquity", "account_equity"],
            "margin": ["margin", "usedMargin", "used_margin"],
            "free_margin": ["freeMargin", "free_margin", "availableMargin"],
            "margin_level": ["marginLevel", "margin_level"],
            "currency": ["currency", "accountCurrency"]
        }
        
        for target_field, source_fields in field_mappings.items():
            for source_field in source_fields:
                if source_field in message:
                    update[target_field] = message[source_field]
                    break
        
        # Convert numeric fields
        for field in ["balance", "equity", "margin", "free_margin", "margin_level"]:
            if field in update and update[field] is not None:
                try:
                    update[field] = float(update[field])
                except (ValueError, TypeError):
                    pass
        
        return update

    def _parse_position_update(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parse a position update message.
        
        Args:
            message: The parsed message.
            
        Returns:
            Dict[str, Any]: Structured position update data.
        """
        # Create a standardized format, adjusting field names as needed
        update = {
            "type": "position_update",
            "timestamp": message.get("timestamp", time.time() * 1000)
        }
        
        # Extract position identifier
        for key in ["positionId", "position_id", "id"]:
            if key in message:
                update["position_id"] = str(message[key])
                break
        
        # Extract other position details
        field_mappings = {
            "instrument": ["instrument", "symbol", "instrumentId"],
            "side": ["side", "direction"],
            "quantity": ["quantity", "qty", "size"],
            "entry_price": ["entryPrice", "entry_price", "openPrice"],
            "current_price": ["currentPrice", "current_price", "marketPrice"],
            "pnl": ["pnl", "profit", "profit_loss", "unrealizedPnL"],
            "stop_loss": ["stopLoss", "stop_loss", "sl"],
            "take_profit": ["takeProfit", "take_profit", "tp"],
            "open_time": ["openTime", "open_time", "opened"]
        }
        
        for target_field, source_fields in field_mappings.items():
            for source_field in source_fields:
                if source_field in message:
                    update[target_field] = message[source_field]
                    break
        
        # Convert numeric fields
        for field in ["quantity", "entry_price", "current_price", "pnl", "stop_loss", "take_profit"]:
            if field in update and update[field] is not None:
                try:
                    update[field] = float(update[field])
                except (ValueError, TypeError):
                    pass
        
        return update