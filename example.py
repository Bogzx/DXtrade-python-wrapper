"""
Example usage of the CTrader Trading SDK.

This example demonstrates how to connect to the cTrader API, authenticate,
and perform common trading operations using the SDK.
"""

import asyncio
import logging
import os
import webbrowser
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

from ctrader_trading_sdk import CTraderTradingSDK
from ctrader_trading_sdk.models import TradeSide

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)

# Replace these with your actual credentials
CLIENT_ID = "your_client_id"
CLIENT_SECRET = "your_client_secret"
REDIRECT_URI = "http://localhost:5000/callback"

# Replace this with your cTrader account ID or retrieve it programmatically
ACCOUNT_ID = 0  # This will be populated after authentication


async def on_tick_data(symbol_id, bid, ask, timestamp):
    """Callback for tick data updates."""
    print(f"Tick: Symbol {symbol_id} - Bid: {bid}, Ask: {ask}, Time: {datetime.fromtimestamp(timestamp / 1000)}")


async def on_order_update(execution_event):
    """Callback for order updates."""
    print(f"Order updated: {execution_event.executionType}")
    if hasattr(execution_event, "order") and execution_event.HasField("order"):
        print(f"  Order ID: {execution_event.order.orderId}")


async def on_position_update(execution_event):
    """Callback for position updates."""
    print(f"Position updated: {execution_event.executionType}")
    if hasattr(execution_event, "position") and execution_event.HasField("position"):
        print(f"  Position ID: {execution_event.position.positionId}")


async def on_error(error_code, description):
    """Callback for errors."""
    print(f"Error: {error_code} - {description}")


async def on_disconnect(reason):
    """Callback for disconnections."""
    print(f"Disconnected: {reason}")


async def get_auth_code(sdk):
    """Open browser for authentication and get the authorization code."""
    # Get authorization URL
    auth_url = sdk.get_auth_url(scope="trading")
    
    # Open browser for authentication
    print(f"Opening browser for authentication: {auth_url}")
    webbrowser.open(auth_url)
    
    # In a real application, you would have a proper redirect handler
    # Here we'll simulate by asking the user to paste the redirect URL
    redirect_url = input("After authorizing, paste the full redirect URL here: ")
    
    # Extract the authorization code from the URL
    parsed_url = urlparse(redirect_url)
    query_params = parse_qs(parsed_url.query)
    
    if 'code' in query_params:
        return query_params['code'][0]
    else:
        raise ValueError("No authorization code found in the redirect URL")


async def demo_api_usage():
    """Demonstrate the SDK's capabilities."""
    # Create SDK instance
    sdk = CTraderTradingSDK(CLIENT_ID, CLIENT_SECRET, REDIRECT_URI)
    
    # Set up callbacks
    sdk.on_tick_data = on_tick_data
    sdk.on_order_update = on_order_update
    sdk.on_position_update = on_position_update
    sdk.on_error = on_error
    sdk.on_disconnect = on_disconnect
    
    try:
        # Connect to the API (demo environment)
        await sdk.connect(host_type="demo")
        
        # Authenticate and get authorization code
        auth_code = await get_auth_code(sdk)
        
        # Exchange authorization code for access token
        await sdk.authorize_with_code(auth_code)
        
        # Get list of available accounts
        accounts = await sdk.get_available_accounts()
        print(f"Available accounts: {accounts}")
        
        if not accounts:
            print("No accounts available")
            return
        
        # Set active account (first one for this example)
        global ACCOUNT_ID
        ACCOUNT_ID = accounts[0]['ctid_trader_account_id']
        await sdk.set_active_account(ACCOUNT_ID)
        
        # Get account information
        account_info = await sdk.get_account_info()
        print(f"Account info: {account_info}")
        
        # Get available symbols
        symbols = await sdk.get_symbols()
        print(f"Found {len(symbols)} symbols")
        
        if symbols:
            # Get first symbol for example
            symbol = symbols[0]
            symbol_id = symbol['symbol_id']
            symbol_name = symbol['symbol_name']
            print(f"Using symbol: {symbol_name} (ID: {symbol_id})")
            
            # Get detailed symbol information
            symbol_details = await sdk.get_symbol_details(symbol_id)
            print(f"Symbol details: {symbol_details}")
            
            # Subscribe to price updates
            await sdk.subscribe_to_prices(symbol_id)
            print(f"Subscribed to {symbol_name} price updates")
            
            # Get historical data
            now = datetime.now()
            one_day_ago = now - timedelta(days=1)
            from_timestamp = int(one_day_ago.timestamp() * 1000)
            to_timestamp = int(now.timestamp() * 1000)
            
            historical_data = await sdk.get_historical_prices(
                symbol_id, "H1", from_timestamp, to_timestamp
            )
            print(f"Got {len(historical_data)} historical bars")
            
            # Get current positions
            positions = await sdk.get_positions()
            print(f"Current positions: {positions}")
            
            # Get pending orders
            orders = await sdk.get_orders()
            print(f"Pending orders: {orders}")
            
            # Place a market order example (commented out to avoid actual trading)
            # Uncomment to test real trading functionality
            """
            market_order = await sdk.place_market_order(
                symbol_id=symbol_id,
                trade_side="BUY",
                volume=0.01,  # Minimum volume, adjust based on symbol
                stop_loss=symbol_details['bid'] * 0.99,  # 1% below current price
                take_profit=symbol_details['bid'] * 1.01,  # 1% above current price
                label="SDK Test Order",
                comment="Testing the CTrader SDK"
            )
            print(f"Placed market order: {market_order}")
            
            # Wait a moment to see the order execution
            await asyncio.sleep(2)
            
            # Get the new position
            position_id = market_order['position_id']
            
            # Modify position SL/TP
            updated_position = await sdk.amend_position_sltp(
                position_id=position_id,
                stop_loss=symbol_details['bid'] * 0.98,  # 2% below current price
                take_profit=symbol_details['bid'] * 1.02,  # 2% above current price
            )
            print(f"Updated position: {updated_position}")
            
            # Wait a moment
            await asyncio.sleep(2)
            
            # Close the position
            await sdk.close_position(position_id)
            print(f"Closed position: {position_id}")
            """
            
            # Example of a limit order (commented out to avoid actual trading)
            """
            limit_order = await sdk.place_limit_order(
                symbol_id=symbol_id,
                trade_side="BUY",
                volume=0.01,
                price=symbol_details['bid'] * 0.99,  # 1% below current price
                stop_loss=symbol_details['bid'] * 0.98,  # 2% below current price
                take_profit=symbol_details['bid'] * 1.02,  # 2% above current price
                label="SDK Test Limit Order"
            )
            print(f"Placed limit order: {limit_order}")
            
            # Wait a moment
            await asyncio.sleep(2)
            
            # Cancel the limit order
            await sdk.cancel_order(limit_order['order_id'])
            print(f"Cancelled limit order: {limit_order['order_id']}")
            """
            
            # Unsubscribe from price updates
            await sdk.unsubscribe_from_prices(symbol_id)
            print(f"Unsubscribed from {symbol_name} price updates")
        
        # Wait to see some data
        print("Waiting for 10 seconds to observe data...")
        await asyncio.sleep(10)
        
    except Exception as e:
        print(f"Error: {str(e)}")
    finally:
        # Disconnect from the API
        await sdk.disconnect()
        print("Disconnected from API")


if __name__ == "__main__":
    # Run the demo
    asyncio.run(demo_api_usage())
