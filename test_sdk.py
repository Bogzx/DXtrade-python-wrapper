"""
Unit tests for the CTrader Trading SDK.

This module contains unit tests for the SDK, focusing on mocking the
underlying API interactions to test the SDK's logic without making
actual network requests.
"""

import asyncio
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAExecutionType, ProtoOAOrderType, ProtoOATradeSide
)

from ctrader_trading_sdk import CTraderTradingSDK
from ctrader_trading_sdk.exceptions import (
    AuthenticationError, ConnectionError, InvalidSymbolError, OrderError,
    PositionError, ValidationError
)


class TestCTraderTradingSDK(unittest.TestCase):
    """Tests for the CTraderTradingSDK class."""
    
    def setUp(self):
        """Set up the test environment."""
        self.client_id = "test_client_id"
        self.client_secret = "test_client_secret"
        self.redirect_uri = "http://localhost:5000/callback"
        
        # Create SDK instance with mocked dependencies
        with patch('ctrader_trading_sdk.sdk.Auth'), \
             patch('ctrader_trading_sdk.sdk.Client'), \
             patch('ctrader_trading_sdk.sdk.reactor'):
            self.sdk = CTraderTradingSDK(self.client_id, self.client_secret, self.redirect_uri)
            
            # Mock client properties
            self.sdk._client = MagicMock()
            self.sdk._client.send = AsyncMock()
            self.sdk._client.startService = MagicMock()
            self.sdk._client.stopService = MagicMock()
            
            # Mock Auth methods
            self.sdk._auth = MagicMock()
            self.sdk._auth.getAuthUri = MagicMock(return_value="https://id.ctrader.com/auth/example")
            self.sdk._auth.getToken = MagicMock(return_value={
                "accessToken": "test_access_token",
                "refreshToken": "test_refresh_token",
                "expiresIn": 2592000
            })
            
            # Set up internal state for testing
            self.sdk._is_connected = True
            self.sdk._is_app_authorized = True
            self.sdk._is_account_authorized = True
            self.sdk._ctid_trader_account_id = 12345
            self.sdk._access_token = "test_access_token"
            self.sdk._refresh_token = "test_refresh_token"
            self.sdk._token_expiry_time = datetime.now() + timedelta(days=30)
            
            # Mock internal methods
            self.sdk._send_request = AsyncMock()
            self.sdk._deferred_to_future = MagicMock()
    
    async def asyncSetUp(self):
        """Async setup for tests that need it."""
        # Additional setup for async tests if needed
        pass
    
    def test_get_auth_url(self):
        """Test generating the authentication URL."""
        # Test with default scope
        auth_url = self.sdk.get_auth_url()
        self.sdk._auth.getAuthUri.assert_called_once_with(scope="trading")
        self.assertEqual(auth_url, "https://id.ctrader.com/auth/example")
        
        # Test with custom scope
        self.sdk._auth.getAuthUri.reset_mock()
        auth_url = self.sdk.get_auth_url(scope="accounts")
        self.sdk._auth.getAuthUri.assert_called_once_with(scope="accounts")
        
        # Test with invalid scope
        with self.assertRaises(ValueError):
            self.sdk.get_auth_url(scope="invalid")
    
    async def test_connect(self):
        """Test connecting to the API."""
        # Reset state for this test
        self.sdk._is_connected = False
        
        # Mock the connection future
        future = asyncio.Future()
        future.set_result(True)
        self.sdk._pending_requests = {"connection": future}
        
        # Test connection
        result = await self.sdk.connect(host_type="demo")
        self.sdk._client.startService.assert_called_once()
        self.assertTrue(result)
        
        # Test connecting when already connected
        self.sdk._is_connected = True
        result = await self.sdk.connect(host_type="demo")
        self.assertTrue(result)
        
        # Test connecting with invalid host type
        with self.assertRaises(ValueError):
            await self.sdk.connect(host_type="invalid")
    
    async def test_disconnect(self):
        """Test disconnecting from the API."""
        # Test successful disconnection
        result = await self.sdk.disconnect()
        self.sdk._client.stopService.assert_called_once()
        self.assertTrue(result)
        self.assertFalse(self.sdk._is_connected)
        self.assertFalse(self.sdk._is_app_authorized)
        self.assertFalse(self.sdk._is_account_authorized)
        
        # Test disconnecting when not connected
        self.sdk._client.stopService.reset_mock()
        self.sdk._is_connected = False
        result = await self.sdk.disconnect()
        self.sdk._client.stopService.assert_not_called()
        self.assertTrue(result)
    
    async def test_authorize_with_code(self):
        """Test authorizing with an authorization code."""
        # Mock _authorize_application
        self.sdk._authorize_application = AsyncMock()
        
        # Test successful authorization
        result = await self.sdk.authorize_with_code("test_auth_code")
        self.sdk._auth.getToken.assert_called_once_with("test_auth_code")
        self.sdk._authorize_application.assert_called_once()
        self.assertTrue(result)
        self.assertEqual(self.sdk._access_token, "test_access_token")
        self.assertEqual(self.sdk._refresh_token, "test_refresh_token")
        
        # Test error response
        self.sdk._auth.getToken.reset_mock()
        self.sdk._authorize_application.reset_mock()
        self.sdk._auth.getToken.return_value = {
            "errorCode": "invalid_grant",
            "description": "Invalid authorization code"
        }
        
        with self.assertRaises(AuthenticationError):
            await self.sdk.authorize_with_code("invalid_code")
    
    async def test_set_active_account(self):
        """Test setting the active trading account."""
        # Mock _authorize_account
        self.sdk._authorize_account = AsyncMock(return_value=True)
        
        # Test successful account selection
        result = await self.sdk.set_active_account(67890)
        self.assertEqual(self.sdk._ctid_trader_account_id, 67890)
        self.sdk._authorize_account.assert_called_once()
        self.assertTrue(result)
        
        # Test when not connected
        self.sdk._is_connected = False
        with self.assertRaises(ConnectionError):
            await self.sdk.set_active_account(67890)
            
        # Test when app not authorized
        self.sdk._is_connected = True
        self.sdk._is_app_authorized = False
        with self.assertRaises(AuthenticationError):
            await self.sdk.set_active_account(67890)
    
    async def test_place_market_order(self):
        """Test placing a market order."""
        # Mock _ensure_symbol_loaded
        symbol_mock = MagicMock()
        symbol_mock.symbolId = 1
        symbol_mock.symbolName = "EURUSD"
        symbol_mock.digits = 5
        self.sdk._ensure_symbol_loaded = AsyncMock(return_value=symbol_mock)
        
        # Mock execution event response
        execution_event = MagicMock()
        execution_event.executionType = ProtoOAExecutionType.ORDER_FILLED
        
        position = MagicMock()
        position.positionId = 123456
        position.symbolId = 1
        position.tradeSide = ProtoOATradeSide.BUY
        position.volume = 100  # 1.0 lot
        position.price = 12345  # 1.2345
        position.swap = 0
        position.commission = 0
        position.openTimestamp = 1609459200000  # 2021-01-01
        position.HasField = MagicMock(return_value=True)
        position.stopLoss = 12200  # 1.2200
        position.takeProfit = 12500  # 1.2500
        
        execution_event.position = position
        execution_event.HasField = MagicMock(side_effect=lambda field: field == "position")
        
        self.sdk._send_request.return_value = execution_event
        
        # Test successful market order
        result = await self.sdk.place_market_order(
            symbol_id=1,
            trade_side="BUY",
            volume=1.0,
            stop_loss=1.22,
            take_profit=1.25,
            label="Test Order",
            comment="Test Comment"
        )
        
        # Verify the result
        self.assertEqual(result["position_id"], 123456)
        self.assertEqual(result["symbol_id"], 1)
        self.assertEqual(result["symbol_name"], "EURUSD")
        self.assertEqual(result["volume"], 1.0)
        self.assertEqual(result["trade_side"], "BUY")
        self.assertEqual(result["price"], 1.2345)
        self.assertEqual(result["stop_loss"], 1.22)
        self.assertEqual(result["take_profit"], 1.25)
        
        # Test invalid trade side
        with self.assertRaises(ValidationError):
            await self.sdk.place_market_order(
                symbol_id=1,
                trade_side="INVALID",
                volume=1.0
            )
            
        # Test invalid volume
        with self.assertRaises(ValidationError):
            await self.sdk.place_market_order(
                symbol_id=1,
                trade_side="BUY",
                volume=0
            )
    
    async def test_get_positions(self):
        """Test getting positions."""
        # Mock reconcile response
        reconcile_res = MagicMock()
        
        position1 = MagicMock()
        position1.positionId = 123456
        position1.symbolId = 1
        position1.tradeSide = ProtoOATradeSide.BUY
        position1.volume = 100  # 1.0 lot
        position1.price = 12345  # 1.2345
        position1.swap = 0
        position1.commission = 0
        position1.openTimestamp = 1609459200000  # 2021-01-01
        position1.HasField = MagicMock(return_value=False)
        
        position2 = MagicMock()
        position2.positionId = 789012
        position2.symbolId = 2
        position2.tradeSide = ProtoOATradeSide.SELL
        position2.volume = 50  # 0.5 lot
        position2.price = 15000  # 1.5000
        position2.swap = 0
        position2.commission = 0
        position2.openTimestamp = 1609545600000  # 2021-01-02
        position2.HasField = MagicMock(return_value=False)
        
        reconcile_res.position = [position1, position2]
        reconcile_res.order = []
        
        self.sdk._send_request.return_value = reconcile_res
        
        # Mock _ensure_symbol_loaded
        symbol1 = MagicMock()
        symbol1.symbolId = 1
        symbol1.symbolName = "EURUSD"
        symbol1.digits = 5
        
        symbol2 = MagicMock()
        symbol2.symbolId = 2
        symbol2.symbolName = "GBPUSD"
        symbol2.digits = 5
        
        self.sdk._ensure_symbol_loaded = AsyncMock(side_effect=lambda symbol_id: 
                                              symbol1 if symbol_id == 1 else symbol2)
        
        # Test getting positions with force refresh
        positions = await self.sdk.get_positions(force_refresh=True)
        
        # Verify the result
        self.assertEqual(len(positions), 2)
        self.assertEqual(positions[0]["position_id"], 123456)
        self.assertEqual(positions[0]["symbol_name"], "EURUSD")
        self.assertEqual(positions[0]["volume"], 1.0)
        self.assertEqual(positions[0]["trade_side"], "BUY")
        self.assertEqual(positions[0]["price"], 1.2345)
        
        self.assertEqual(positions[1]["position_id"], 789012)
        self.assertEqual(positions[1]["symbol_name"], "GBPUSD")
        self.assertEqual(positions[1]["volume"], 0.5)
        self.assertEqual(positions[1]["trade_side"], "SELL")
        self.assertEqual(positions[1]["price"], 1.5000)
    
    async def test_close_position(self):
        """Test closing a position."""
        # Set up position in cache
        position = MagicMock()
        position.positionId = 123456
        position.volume = 100  # 1.0 lot
        self.sdk._positions = {123456: position}
        
        # Mock execution event response
        execution_event = MagicMock()
        execution_event.executionType = ProtoOAExecutionType.POSITION_CLOSED
        self.sdk._send_request.return_value = execution_event
        
        # Test full position closure
        result = await self.sdk.close_position(123456)
        self.assertTrue(result)
        
        # Test partial position closure
        result = await self.sdk.close_position(123456, volume=0.5)
        self.assertTrue(result)
        
        # Test closing non-existent position
        self.sdk._positions = {}
        self.sdk.get_positions = AsyncMock(return_value=[])  # Empty positions list
        
        with self.assertRaises(PositionError):
            await self.sdk.close_position(999999)
            
        # Test invalid volume
        self.sdk._positions = {123456: position}
        with self.assertRaises(ValidationError):
            await self.sdk.close_position(123456, volume=2.0)  # Volume too large
    
    async def test_subscribe_to_prices(self):
        """Test subscribing to price updates."""
        # Mock _ensure_symbol_loaded
        symbol_mock = MagicMock()
        symbol_mock.symbolId = 1
        symbol_mock.symbolName = "EURUSD"
        symbol_mock.digits = 5
        self.sdk._ensure_symbol_loaded = AsyncMock(return_value=symbol_mock)
        
        # Mock response
        response = MagicMock()  # Simple response mock
        self.sdk._send_request.return_value = response
        
        # Test successful subscription
        result = await self.sdk.subscribe_to_prices(1)
        self.assertTrue(result)
        self.assertIn(1, self.sdk._spot_subscriptions)
        
        # Test subscribing when already subscribed
        self.sdk._send_request.reset_mock()
        result = await self.sdk.subscribe_to_prices(1)
        self.assertTrue(result)
        self.sdk._send_request.assert_not_called()
    
    def test_handle_spot_event(self):
        """Test handling spot price events."""
        # Set up test data
        symbol_id = 1
        self.sdk._spot_subscriptions.add(symbol_id)
        
        # Mock symbol details
        symbol_mock = MagicMock()
        symbol_mock.digits = 5
        self.sdk._symbols = {symbol_id: symbol_mock}
        
        # Create spot event
        event = MagicMock()
        event.symbolId = symbol_id
        event.bid = 12345  # 1.2345
        event.ask = 12347  # 1.2347
        event.timestamp = 1609459200000  # 2021-01-01
        
        # Mock tick data callback
        tick_data_called = False
        tick_data_args = None
        
        async def on_tick_data(symbol_id, bid, ask, timestamp):
            nonlocal tick_data_called, tick_data_args
            tick_data_called = True
            tick_data_args = (symbol_id, bid, ask, timestamp)
        
        self.sdk.on_tick_data = on_tick_data
        
        # Process the event
        loop = asyncio.get_event_loop()
        loop.run_until_complete(asyncio.gather(
            self.sdk._handle_spot_event(event)
        ))
        
        # Verify price cache update
        self.assertIn(symbol_id, self.sdk._price_cache)
        self.assertEqual(self.sdk._price_cache[symbol_id]["bid"], 1.2345)
        self.assertEqual(self.sdk._price_cache[symbol_id]["ask"], 1.2347)
        
        # Verify callback was called
        self.assertTrue(tick_data_called)
        self.assertEqual(tick_data_args[0], symbol_id)
        self.assertEqual(tick_data_args[1], 1.2345)
        self.assertEqual(tick_data_args[2], 1.2347)
        self.assertEqual(tick_data_args[3], 1609459200000)


if __name__ == "__main__":
    unittest.main()
