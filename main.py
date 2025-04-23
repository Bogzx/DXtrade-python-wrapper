# main.py

import logging
import os
import queue
import time
import threading
from dotenv import load_dotenv # Optional: for loading .env files (pip install python-dotenv)

# Import your wrapper class (assuming it's in a file named dxtrade_wrapper.py)
from dxtrade_wrapper import (
    DXTradeDashboardWrapper,
    AuthenticationError,
    DXTradeAPIError,
    OrderPlacementError,
    WebSocketError
)

# --- Configuration ---
# Load environment variables from .env file if it exists
load_dotenv()

# !! IMPORTANT: Replace placeholders or set environment variables !!
# Use environment variables for security
# Example: export DXTRADE_USERNAME="your_username"
DXTRADE_BASE_URL = os.getenv("DXTRADE_BASE_URL", "https://dxtrade.ftmo.com") # Replace with your broker's URL
DXTRADE_USERNAME = os.getenv("DXTRADE_USERNAME", "YOUR_USERNAME")
DXTRADE_PASSWORD = os.getenv("DXTRADE_PASSWORD", "YOUR_PASSWORD")
# For FTMO, this should likely be "ftmo"
DXTRADE_DOMAIN_VENDOR = os.getenv("DXTRADE_DOMAIN_VENDOR", "ftmo")
# Potential login paths: "/dxsca-web/login" or "/api/auth/"
DXTRADE_LOGIN_PATH = os.getenv("DXTRADE_LOGIN_PATH", "/dxsca-web/login")
# WebSocket path needs verification for your broker
DXTRADE_WEBSOCKET_PATH = os.getenv("DXTRADE_WEBSOCKET_PATH", "/websocket/events") # Adjust as needed!

# Example symbols to subscribe to
SYMBOLS_TO_SUBSCRIBE = ["EURUSD", "GBPUSD", "USDJPY"] # Adjust as needed

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO, # Set to DEBUG for more detailed wrapper logs
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("main_test")


# --- Main Test Function ---
def main():
    logger.info("Starting DXTrade Wrapper Test...")

    # --- Input Validation ---
    if "YOUR_USERNAME" == DXTRADE_USERNAME or "YOUR_PASSWORD" == DXTRADE_PASSWORD:
        logger.error("Please replace placeholder credentials or set environment variables.")
        return
    if "https://" not in DXTRADE_BASE_URL:
         logger.error(f"Invalid DXTRADE_BASE_URL: {DXTRADE_BASE_URL}")
         return

    # --- Instantiate Wrapper ---
    # Pass necessary configuration parameters
    wrapper = DXTradeDashboardWrapper(
        base_url=DXTRADE_BASE_URL,
        username=DXTRADE_USERNAME,
        password=DXTRADE_PASSWORD,
        domain_or_vendor=DXTRADE_DOMAIN_VENDOR,
        login_path=DXTRADE_LOGIN_PATH,
        websocket_path=DXTRADE_WEBSOCKET_PATH,
        logger=logging.getLogger("DXTradeWrapper") # Pass specific logger
    )
    logger.info("DXTradeDashboardWrapper instantiated.")

    try:
        # --- Login ---
        logger.info("Attempting login...")
        wrapper.login()
        if wrapper.is_authenticated:
            logger.info(f"Login successful! Account ID: {wrapper._account_id}") # Accessing internal for demo
        else:
            logger.error("Login reported success but wrapper state is not authenticated.")
            return # Exit if login fails internally

        # --- Test REST API Calls (Authenticated) ---
        try:
            logger.info("Fetching account balance...")
            balance = wrapper.get_balance()
            logger.info(f"Balance: {balance}")

            logger.info("Fetching open positions...")
            positions = wrapper.get_positions()
            if positions:
                logger.info(f"Found {len(positions)} open positions:")
                for pos in positions:
                    logger.info(f"  - {pos}")
            else:
                logger.info("No open positions found.")

            logger.info("Fetching pending orders...")
            orders = wrapper.get_orders()
            if orders:
                logger.info(f"Found {len(orders)} pending orders:")
                for order in orders:
                    logger.info(f"  - {order}")
            else:
                logger.info("No pending orders found.")

        except DXTradeAPIError as api_err:
            logger.error(f"API Error during data fetch: {api_err}")
        except Exception as e:
            logger.error(f"Unexpected error during data fetch: {e}", exc_info=True)


        # --- Test WebSocket ---
        try:
            logger.info("Connecting to WebSocket...")
            wrapper.connect_websocket()
            # Give it a moment to potentially connect
            time.sleep(2)

            if wrapper._ws_connected: # Accessing internal for demo check
                logger.info("WebSocket connection initiated (check logs for confirmation).")

                logger.info(f"Subscribing to market data for: {SYMBOLS_TO_SUBSCRIBE}")
                wrapper.subscribe_market_data(SYMBOLS_TO_SUBSCRIBE)

                logger.info("Subscribing to account updates...")
                wrapper.subscribe_account_updates()

                # --- Process WebSocket Messages Loop ---
                logger.info("Starting WebSocket message processing loop (Press Ctrl+C to stop)...")
                running = True
                while running:
                    try:
                        # Check Price Queue (Non-blocking)
                        try:
                            price_update = wrapper.price_update_queue.get_nowait()
                            logger.info(f"WebSocket << Price Update: {price_update}")
                        except queue.Empty:
                            pass # No message

                        # Check Order Queue (Non-blocking)
                        try:
                            order_update = wrapper.order_update_queue.get_nowait()
                            logger.info(f"WebSocket << Order Update: {order_update}")
                        except queue.Empty:
                            pass # No message

                        # Check Account Queue (Non-blocking)
                        try:
                            account_update = wrapper.account_update_queue.get_nowait()
                            logger.info(f"WebSocket << Account Update: {account_update}")
                        except queue.Empty:
                            pass # No message

                        # --- !! Optional: Place a Test Order (USE DEMO ACCOUNT ONLY) !! ---
                        # Uncomment carefully for testing order placement
                        # try:
                        #     logger.info("Attempting to place a small test market order...")
                        #     order_result = wrapper.place_order(
                        #         instrument="EURUSD", # Or another valid symbol
                        #         side="BUY",
                        #         quantity=0.01, # Minimum allowed quantity
                        #         order_type="MARKET"
                        #     )
                        #     logger.info(f"Test order placement result: {order_result}")
                        #     # Prevent placing multiple orders in the loop
                        #     # You'd typically place orders based on external triggers
                        # except OrderPlacementError as ord_err:
                        #     logger.error(f"Test order placement failed: {ord_err}")
                        # except Exception as ord_exc:
                        #      logger.error(f"Unexpected error during test order: {ord_exc}", exc_info=True)
                        # # --- End Optional Test Order ---

                        # Sleep briefly to avoid busy-waiting
                        time.sleep(0.1)

                    except KeyboardInterrupt:
                        logger.info("KeyboardInterrupt received, stopping WebSocket processing.")
                        running = False
                    except Exception as loop_err:
                        logger.error(f"Error in WebSocket processing loop: {loop_err}", exc_info=True)
                        # Avoid continuous error loops
                        time.sleep(1)

            else:
                logger.error("WebSocket failed to connect.")

        except WebSocketError as ws_err:
            logger.error(f"WebSocket Error: {ws_err}")
        except Exception as e:
            logger.error(f"Unexpected error during WebSocket setup: {e}", exc_info=True)


    except AuthenticationError as auth_err:
        logger.error(f"Authentication Failed: {auth_err}")
    
    except Exception as e:
        logger.error(f"An unexpected error occurred in main: {e}", exc_info=True)

    finally:
        # --- Cleanup ---
        logger.info("Cleaning up...")
        if wrapper: # Check if wrapper was instantiated
             if wrapper._ws_connected:
                 logger.info("Disconnecting WebSocket...")
                 wrapper.disconnect_websocket()
             if wrapper.is_authenticated:
                 logger.info("Logging out...")
                 wrapper.logout()
        logger.info("Test finished.")


# --- Run the main function ---
if __name__ == "__main__":
    main()