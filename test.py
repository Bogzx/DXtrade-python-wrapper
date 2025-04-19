import asyncio
from ctrader_trading_sdk import CTraderTradingSDK

async def main():
    # Create SDK instance with your credentials
    sdk = CTraderTradingSDK(
        client_id="your_client_id",
        client_secret="your_client_secret",
        redirect_uri="your_redirect_uri"
    )
    
    # Connect to the API and authenticate
    await sdk.connect(host_type="demo")
    await sdk.authorize_with_code("auth_code_from_redirect")
    
    # Set active account
    accounts = await sdk.get_available_accounts()
    await sdk.set_active_account(accounts[0]['ctid_trader_account_id'])
    
    # Get account information
    account_info = await sdk.get_account_info()
    print(f"Balance: {account_info['balance']}")
    
    # Place a market order
    
    
    # Clean up
    await sdk.disconnect()

if __name__ == "__main__":
    asyncio.run(main())