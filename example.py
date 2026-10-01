"""Read-only tour of a real DXtrade account: balance, positions, orders, live quotes.

    pip install -e ".[examples]"
    cp .env.example .env        # fill in your broker URL and credentials
    python example.py

It places no orders. Pass --demo-order to place (and immediately cancel) one far
from the market; use a DEMO account for that. This client follows the public
DXtrade spec but has not been verified against a live broker.

No broker account? Run examples/offline_demo.py instead.
"""

import logging
import os
import queue
import sys
import time

from dotenv import load_dotenv

from dxtrade_wrapper import DXTradeClient, DXTradeWrapperError

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("example")

SYMBOLS = os.getenv("DXTRADE_SYMBOLS", "EUR/USD,GBP/USD").split(",")


def main() -> int:
    base_url = os.getenv("DXTRADE_BASE_URL", "")
    if not base_url.startswith("https://") or not os.getenv("DXTRADE_USERNAME"):
        log.error("Set DXTRADE_BASE_URL (https://...), DXTRADE_USERNAME and DXTRADE_PASSWORD "
                  "in .env (see .env.example).")
        return 2

    client = DXTradeClient(
        base_url=base_url,
        username=os.environ["DXTRADE_USERNAME"],
        password=os.getenv("DXTRADE_PASSWORD", ""),
        domain_or_vendor=os.getenv("DXTRADE_DOMAIN_VENDOR", "default"),
        api_prefix=os.getenv("DXTRADE_API_PREFIX", "/dxsca-web"),
        # Still honoured so .env files written for 0.1 keep working.
        login_path=os.getenv("DXTRADE_LOGIN_PATH", "/login"),
        websocket_path=os.getenv("DXTRADE_WEBSOCKET_PATH", "/websocket"),
        account=os.getenv("DXTRADE_ACCOUNT") or None,
        websocket_url=os.getenv("DXTRADE_WEBSOCKET_URL") or None,
    )
    try:
        with client:
            log.info("Accounts: %s", client.get_accounts())
            log.info("Balance: %s", client.get_balance())
            for position in client.get_positions():
                log.info("Position: %s", position)
            for order in client.get_orders():
                log.info("Working order: %s", order)

            if "--demo-order" in sys.argv:
                demo_order(client)

            if os.getenv("DXTRADE_WEBSOCKET_URL") or os.getenv("DXTRADE_WEBSOCKET_PATH"):
                stream_quotes(client, seconds=15)
            else:
                log.info("Set DXTRADE_WEBSOCKET_URL (ask your broker) to stream quotes.")
    except DXTradeWrapperError as exc:
        log.error("%s: %s", type(exc).__name__, exc)
        return 1
    return 0


def demo_order(client: DXTradeClient) -> None:
    """A tiny LIMIT buy at DXTRADE_DEMO_PRICE (default 0.5, far below market), then cancelled."""
    symbol = SYMBOLS[0]
    price = float(os.getenv("DXTRADE_DEMO_PRICE", "0.5"))
    code = client._generate_order_code()
    log.info("Placing demo LIMIT BUY %s @ %s (orderCode %s)", symbol, price, code)
    log.info("Response: %s", client.place_order(symbol, "BUY", 1000, "LIMIT", price=price,
                                                orderCode=code))
    log.info("Cancel: %s", client.cancel_order(code))


def stream_quotes(client: DXTradeClient, seconds: int) -> None:
    client.connect_websocket()
    client.subscribe_market_data(SYMBOLS)
    client.subscribe_account_updates()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for name, q in (("quote", client.price_update_queue),
                        ("order", client.order_update_queue),
                        ("account", client.account_update_queue)):
            try:
                log.info("%s: %s", name, q.get(timeout=0.2))
            except queue.Empty:
                pass


if __name__ == "__main__":
    sys.exit(main())
