"""Regression locks for the blocking protocol bugs found in 2026.

Each of these would have broken the client at every broker: the missing
/dxsca-web prefix, the Bearer scheme instead of DXAPI, and orders missing
account/orderCode. They now also pin the spec-conformant URL encoding.
"""

import responses

from conftest import ACC, ACCOUNT, API, BASE_URL, PREFIX, body, fixture, make_wrapper


def test_login_posts_documented_body_to_prefixed_url(api):
    api.add(responses.POST, f"{API}/login", json=fixture("login_success"))
    make_wrapper().login()

    call = api.calls[0]
    assert call.request.url == f"{API}/login"
    assert body(call) == {"username": "user", "domain": "default", "password": "s3cret-Pa55word"}
    assert "Authorization" not in call.request.headers


def test_every_rest_call_carries_the_api_prefix(client, api):
    api.add(responses.GET, f"{ACC}/metrics", json=fixture("metrics"))
    api.add(responses.GET, f"{ACC}/positions", json=fixture("positions"))
    api.add(responses.GET, f"{ACC}/orders", json=fixture("orders"))
    api.add(responses.GET, f"{ACC}/orders/history", json=fixture("history"))
    api.add(responses.POST, f"{API}/ping", json={})

    client.get_balance()
    client.get_positions()
    client.get_orders()
    client.get_order_history()
    client.ping()

    for call in api.calls[1:]:
        assert call.request.url.startswith(f"{BASE_URL}{PREFIX}/"), call.request.url


def test_url_helper_is_the_single_place_the_prefix_is_applied():
    wrapper = make_wrapper(api_prefix="/custom-api")
    assert wrapper._url("login") == f"{BASE_URL}/custom-api/login"
    assert wrapper._url("/accounts/A/orders") == f"{BASE_URL}/custom-api/accounts/A/orders"


def test_empty_prefix_serves_api_at_root():
    assert make_wrapper(api_prefix="")._url("login") == f"{BASE_URL}/login"


def test_login_path_with_embedded_prefix_is_not_doubled():
    wrapper = make_wrapper(login_path="/dxsca-web/login")
    assert wrapper._url(wrapper._login_path) == f"{API}/login"


def test_authenticated_requests_send_dxapi_header(client, api):
    api.add(responses.GET, f"{ACC}/metrics", json=fixture("metrics"))
    client.get_balance()

    sent = api.calls[-1].request.headers["Authorization"]
    assert sent == f"DXAPI {fixture('login_success')['sessionToken']}"


def test_account_code_is_url_encoded_in_paths(client, api):
    """The spec says account codes (clearing:account) must be URL-encoded."""
    api.add(responses.GET, f"{ACC}/positions", json=fixture("positions"))
    client.get_positions()
    assert "/accounts/default%3AACC12345/positions" in api.calls[-1].request.url


def test_order_payload_includes_account_and_order_code(client, api):
    api.add(responses.POST, f"{ACC}/orders", json=fixture("order_response"))
    client.place_order(instrument="EUR/USD", side="BUY", quantity=1000, order_type="MARKET")

    payload = body(api.calls[-1])
    assert payload["account"] == ACCOUNT
    assert payload["orderCode"]
    assert payload == {
        "account": ACCOUNT,
        "orderCode": payload["orderCode"],
        "type": "MARKET",
        "instrument": "EUR/USD",
        "quantity": 1000,
        "positionEffect": "OPEN",
        "side": "BUY",
        "tif": "GTC",
    }


def test_caller_supplied_order_code_is_respected(client, api):
    api.add(responses.POST, f"{ACC}/orders", json=fixture("order_response"))
    client.place_order("EUR/USD", "BUY", 1000, "MARKET", orderCode="my-key-1")
    assert body(api.calls[-1])["orderCode"] == "my-key-1"


def test_generated_order_codes_are_unique_and_valid_client_ids():
    wrapper = make_wrapper()
    codes = {wrapper._generate_order_code() for _ in range(500)}
    assert len(codes) == 500
    assert all(len(code) <= 64 and code.replace("-", "").isalnum() for code in codes)
