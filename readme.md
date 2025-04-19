# CTrader Trading SDK

A user-friendly Python SDK for the cTrader Open API v2.0, simplifying common trading tasks and abstracting the complexities of Protobuf messages, asynchronous operations, and authentication flows.

## Features

- Simple connection and authentication handling
- Comprehensive trading operations (market, limit, stop orders)
- Real-time price data subscriptions
- Position and order management
- Historical data retrieval
- Account information access
- Robust error handling
- Automatic reconnection and token refresh
- Detailed documentation and examples

## Installation

```bash
pip install ctrader-trading-sdk
```

## Quick Start

```python
import asyncio
import webbrowser
from urllib.parse import parse_qs, urlparse

from ctrader_trading_sdk import CTraderTradingSDK


async def on_tick_data(symbol_id, bid, ask, timestamp):
    """Callback for price updates."""
    print(f"Tick: Symbol {symbol_id} - Bid: {bid}, Ask: {ask}")


async def quick_start():
    # Initialize the SDK with your credentials
    client_id = "your_client_id"
    client_secret = "your_client_secret"
    redirect_uri = "http://localhost:5000/callback"
    
    sdk = CTraderTradingSDK(client_id, client_secret, redirect_uri)
    sdk.on_tick_data = on_tick_data
    
    try:
        # Connect to the API (demo environment)
        await sdk.connect(host_type="demo")
        
        # Get authorization URL and open browser for user to login
        auth_url = sdk.get_auth_url(scope="trading")
        webbrowser.open(auth_url)
        
        # In a real application, you would have a proper redirect handler
        # Here we'll simulate by asking the user to paste the redirect URL
        redirect_url = input("After authorizing, paste the full redirect URL here: ")
        
        # Extract the authorization code from the URL
        parsed_url = urlparse(redirect_url)
        query_params = parse_qs(parsed_url.query)
        auth_code = query_params['code'][0]
        
        # Exchange authorization code for access token
        await sdk.authorize_with_code(auth_code)
        
        # Get available accounts
        accounts = await sdk.get_available_accounts()
        print(f"Available accounts: {accounts}")
        
        # Set active account
        account_id = accounts[0]['ctid_trader_account_id']
        await sdk.set_active_account(account_id)
        
        # Get account information
        account_info = await sdk.get_account_info()
        print(f"Account info: {account_info}")
        
        # Get available symbols
        symbols = await sdk.get_symbols()
        symbol_id = symbols[0]['symbol_id']
        
        # Subscribe to price updates
        await sdk.subscribe_to_prices(symbol_id)
        
        # Wait to observe some data
        print("Waiting for price updates...")
        await asyncio.sleep(10)
        
        # Unsubscribe from price updates
        await sdk.unsubscribe_from_prices(symbol_id)
        
    finally:
        # Disconnect from the API
        await sdk.disconnect()
        print("Disconnected from API")


if __name__ == "__main__":
    asyncio.run(quick_start())
```

## Authentication Flow

The SDK uses OAuth 2.0 for authentication:

1. First, register your application on the cTrader Open API portal to obtain Client ID and Client Secret
2. Initialize the SDK with your credentials
3. Generate an authorization URL using `get_auth_url()`
4. Redirect the user to this URL to authorize your application
5. Capture the authorization code from the redirect URL
6. Exchange the code for access and refresh tokens using `authorize_with_code()`

After authentication, the SDK handles token refresh automatically.

## Trading Examples

### Placing a Market Order

```python
# Place a market order
position = await sdk.place_market_order(
    symbol_id=12345,
    trade_side="BUY",
    volume=0.1,  # 0.1 lots
    stop_loss=1.10,  # Optional stop loss price
    take_profit=1.15,  # Optional take profit price
    label="My Market Order",  # Optional label
    comment="SDK Example"  # Optional comment
)
print(f"Position opened: {position}")
```

### Placing a Limit Order

```python
# Place a limit order
order = await sdk.place_limit_order(
    symbol_id=12345,
    trade_side="BUY",
    volume=0.1,
    price=1.12,  # Limit price
    stop_loss=1.10,
    take_profit=1.15
)
print(f"Limit order placed: {order}")
```

### Managing Positions

```python
# Get all open positions
positions = await sdk.get_positions()

if positions:
    position_id = positions[0]['position_id']
    
    # Modify stop loss and take profit
    await sdk.amend_position_sltp(
        position_id=position_id,
        stop_loss=1.09,
        take_profit=1.16
    )
    
    # Partially close position
    await sdk.close_position(position_id, volume=0.05)
    
    # Fully close position
    await sdk.close_position(position_id)
```

### Managing Orders

```python
# Get all pending orders
orders = await sdk.get_orders()

if orders:
    order_id = orders[0]['order_id']
    
    # Modify order
    await sdk.amend_order(
        order_id=order_id,
        volume=0.2,
        price=1.13
    )
    
    # Cancel order
    await sdk.cancel_order(order_id)
```

## Subscribing to Market Data

```python
# Set up callback for price updates
sdk.on_tick_data = on_tick_data

# Subscribe to price feed
await sdk.subscribe_to_prices(symbol_id)

# Get historical price data
historical_data = await sdk.get_historical_prices(
    symbol_id=12345,
    period="H1",  # 1-hour timeframe
    from_timestamp=1609459200000,  # Unix timestamp in milliseconds
    to_timestamp=1609545600000
)
```

## Error Handling

The SDK provides specific exception types for different error scenarios:

```python
from ctrader_trading_sdk import OrderError, InsufficientFundsError

try:
    await sdk.place_market_order(symbol_id=12345, trade_side="BUY", volume=100)
except InsufficientFundsError as e:
    print(f"Not enough funds: {e}")
except OrderError as e:
    print(f"Order error: {e}")
```

## Callback Events

Register callbacks to handle real-time events:

```python
sdk.on_tick_data = on_tick_data  # Price updates
sdk.on_order_update = on_order_update  # Order status changes
sdk.on_position_update = on_position_update  # Position changes
sdk.on_error = on_error  # Error events
sdk.on_disconnect = on_disconnect  # Disconnection events
sdk.on_margin_change = on_margin_change  # Margin level changes
```

## Advanced Usage

See the [example_usage.py](examples/example_usage.py) file for more comprehensive examples.

## License

This project is licensed under the MIT License - see the LICENSE file for details.
