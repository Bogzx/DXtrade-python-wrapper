"""Runs the client end to end against a simulated DXtrade server. No account needed.

    pip install -e ".[test]"
    python examples/offline_demo.py

The simulated responses follow the public DXtrade REST spec. Every request the
client sends is printed, so you can see exactly what would go over the wire.
"""

import json
import re

import responses

from dxtrade_wrapper import DXTradeClient, OrderPlacementError

BASE = "https://dxtrade.example-broker.com"
API = f"{BASE}/dxsca-web"
ACC = f"{API}/accounts/default%3ADEMO1"

POSITION = {"account": "default:DEMO1", "version": 5, "positionCode": "1001",
            "symbol": "EUR/USD", "quantity": 10000, "quantityNotional": 10000, "side": "BUY",
            "openTime": "2026-09-30T08:00:00Z", "openPrice": 1.0850,
            "lastUpdateTime": "2026-09-30T08:00:00Z"}
ORDER = {"account": "default:DEMO1", "orderId": 2002, "orderCode": "srv-2002", "version": 5,
         "clientOrderId": "my-limit-1", "actionCode": "a1", "legCount": 1, "type": "LIMIT",
         "instrument": "EUR/USD", "status": "WORKING", "finalStatus": False,
         "legs": [{"instrument": "EUR/USD", "positionEffect": "OPEN", "side": "BUY",
                   "price": 1.0500, "quantity": 5000}],
         "side": "BUY", "tif": "GTC", "issueTime": "2026-09-30T08:05:00Z",
         "transactionTime": "2026-09-30T08:05:00Z"}


def simulate(api: responses.RequestsMock) -> None:
    api.add(responses.POST, f"{API}/login", json={"sessionToken": "demo-token",
                                                  "timeout": "00:30:00"})
    api.add(responses.GET, f"{API}/users/demo%40default", json={"userDetails": [{
        "login": "demo", "domain": "default", "username": "demo@default", "version": 1,
        "fullName": "Demo", "accounts": [{"account": "default:DEMO1", "baseCurrency": "USD",
                                          "accountStatus": "FULL_TRADING",
                                          "positionBased": True}]}]})
    api.add(responses.GET, f"{ACC}/metrics", json={"metrics": [{
        "account": "default:DEMO1", "equity": 10050.0, "balance": 10000.0, "margin": 350.0,
        "marginFree": 9700.0, "availableFunds": 9700.0, "openPL": 50.0, "totalPL": 50.0}]})
    api.add(responses.GET, f"{ACC}/positions", json={"positions": [POSITION]})
    api.add(responses.GET, f"{ACC}/orders", json={"orders": [ORDER]}, headers={"ETag": '"v5"'})
    api.add(responses.POST, f"{ACC}/orders", json={"orderResponses": [
        {"orderId": 3003, "updateOrderId": 3003}, {"orderId": 3004, "updateOrderId": 3004},
        {"orderId": 3005, "updateOrderId": 3005}]})
    api.add(responses.PUT, f"{ACC}/orders", json={"orderId": 2002, "updateOrderId": 2010})
    api.add(responses.DELETE, re.compile(rf"{re.escape(ACC)}/orders/.+"),
            json={"orderId": 2002, "updateOrderId": 2011})
    api.add(responses.POST, f"{API}/logout", body="")


def main() -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as api:
        simulate(api)
        with DXTradeClient(BASE, "demo", "demo-password", "default",
                                     keepalive_interval=None) as dx:
            print("accounts :", dx.get_accounts())
            print("balance  :", dx.get_balance())
            print("positions:", dx.get_positions())
            print("orders   :", dx.get_orders())
            print("entry+SL+TP:", dx.place_order("EUR/USD", "BUY", 10000, "MARKET",
                                                 stop_loss=1.0800, take_profit=1.0950))
            print("modify   :", dx.modify_order("my-limit-1", new_price=1.0520))
            print("cancel   :", dx.cancel_order("my-limit-1"))
            try:
                dx.place_order("EUR/USD", "BUY", 1000, "LIMIT")  # no price
            except ValueError as exc:
                print("rejected locally:", exc)
            except OrderPlacementError as exc:  # pragma: no cover
                print("rejected by server:", exc)

        print("\nRequests sent:")
        for call in api.calls:
            req = call.request
            headers = {k: v for k, v in req.headers.items() if k in ("Authorization", "If-Match")}
            payload = json.loads(req.body) if req.body else None
            if payload and "password" in payload:
                payload["password"] = "***"
            print(f"  {req.method:6} {req.url.replace(BASE, '')} {headers or ''}")
            if payload:
                print("         ", json.dumps(payload))


if __name__ == "__main__":
    main()
