"""
Fixture-based tests for DXTradeDashboardWrapper.

WHAT THESE TESTS DO AND DO NOT PROVE
------------------------------------
These tests run entirely against `responses`-mocked HTTP. They prove that the
wrapper *builds the requests it intends to build*: the right URL, the right
authorization scheme, the right payload fields.

They prove NOTHING about whether those requests are what a DXtrade server
actually accepts. Every fixture in `tests/fixtures/` is hand-written from the
same community examples and third-party guide the wrapper itself was inferred
from - none is a recording of a real broker response, because no live session
has ever succeeded. If the inferred protocol is wrong, these tests will pass
happily while the wrapper remains broken against a real server.

Their real purpose is regression-locking the three blocking bugs found by code
review in 2026, so they cannot silently return:

  * `test_login_uses_dxsca_web_prefix` and `test_login_sets_dxapi_auth_header`
    together would have caught two of the three blockers on the first run.
  * `test_every_rest_call_carries_the_api_prefix` covers the eight endpoints
    that were missing the prefix entirely.
  * `test_order_payload_includes_account_and_order_code` covers the third.

Run with:  python -m pytest tests/ -v
"""

import json
import pathlib
import sys

import pytest
import responses

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dxtrade_wrapper import (  # noqa: E402
    DXTradeAPIError,
    DXTradeDashboardWrapper,
)

BASE_URL = "https://dxtrade.example-broker.com"
PREFIX = "/dxsca-web"
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


def fixture(name):
    """Load a recorded-shape JSON fixture by filename stem."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def make_wrapper(**kwargs):
    """Build a wrapper with keepalive disabled so tests spawn no threads."""
    params = dict(
        base_url=BASE_URL,
        username="user",
        password="pass",
        domain_or_vendor="default",
        keepalive_interval=None,
    )
    params.update(kwargs)
    return DXTradeDashboardWrapper(**params)


def register_login(body=None, status=200):
    responses.add(
        responses.POST,
        f"{BASE_URL}{PREFIX}/login",
        json=body if body is not None else fixture("login_success"),
        status=status,
    )


@pytest.fixture
def logged_in():
    """
    A wrapper that has completed a mocked login.

    This fixture owns the responses mock itself rather than relying on a
    @responses.activate decorator on the test: pytest sets fixtures up before the
    decorator's context is entered, so a decorated test would let the fixture's
    own login escape to the real network. Tests using this fixture must NOT also
    be decorated with @responses.activate.
    """
    responses.start()
    try:
        register_login()
        wrapper = make_wrapper()
        wrapper.login()
        yield wrapper
    finally:
        responses.stop()
        responses.reset()


# --------------------------------------------------------------------------
# Blocker 1: the /dxsca-web prefix
# --------------------------------------------------------------------------

@responses.activate
def test_login_uses_dxsca_web_prefix():
    """Login must POST to {base}/dxsca-web/login."""
    register_login()
    make_wrapper().login()

    assert len(responses.calls) == 1
    assert responses.calls[0].request.url == f"{BASE_URL}{PREFIX}/login"


def test_every_rest_call_carries_the_api_prefix(logged_in):
    """
    The regression test for the headline bug: login had the prefix and the eight
    endpoints after it did not, so everything past authentication 404'd at any
    broker. Each call below must land under /dxsca-web.
    """
    account = "default:ACC12345"
    # NOTE: the list endpoints are given bare JSON arrays because that is what
    # this wrapper's parsers iterate directly - _parse_positions() and friends
    # do `for x in data`, so an envelope like {"positions": [...]} would iterate
    # the dict's keys and crash on `str.get`. Whether a real DXtrade server
    # returns a bare array or an envelope is UNVERIFIED; if it returns an
    # envelope, those parsers are a fourth bug that these tests do not catch,
    # because the fixtures were written to match the wrapper's assumption.
    endpoints = [
        (responses.GET, f"/accounts/{account}/portfolio", fixture("portfolio")),
        (responses.GET, f"/accounts/{account}/positions", []),
        (responses.GET, f"/accounts/{account}/orders", []),
        (responses.GET, f"/accounts/{account}/history", []),
        (responses.GET, "/ping", {}),
    ]
    for method, path, body in endpoints:
        responses.add(method, f"{BASE_URL}{PREFIX}{path}", json=body, status=200)

    logged_in.get_balance()
    logged_in.get_positions()
    logged_in.get_orders()
    logged_in.get_order_history()
    logged_in.ping()

    # calls[0] is the login itself.
    for call in responses.calls[1:]:
        assert call.request.url.startswith(f"{BASE_URL}{PREFIX}/"), (
            f"{call.request.url} is missing the {PREFIX} prefix"
        )


@responses.activate
def test_url_helper_is_the_single_place_the_prefix_is_applied():
    """
    A custom prefix must flow to every endpoint, which is only true because all
    URLs route through _url(). Eight independent f-strings could not do this.
    """
    wrapper = make_wrapper(api_prefix="/custom-api")
    assert wrapper._url("login") == f"{BASE_URL}/custom-api/login"
    assert wrapper._url("/accounts/A/orders") == f"{BASE_URL}/custom-api/accounts/A/orders"


@responses.activate
def test_empty_prefix_serves_api_at_root():
    """A deployment with no prefix must not produce a doubled slash."""
    wrapper = make_wrapper(api_prefix="")
    assert wrapper._url("login") == f"{BASE_URL}/login"


def test_login_path_with_embedded_prefix_is_not_doubled():
    """
    Existing .env files say DXTRADE_LOGIN_PATH=/dxsca-web/login. That must not
    become /dxsca-web/dxsca-web/login now that the prefix is applied separately.
    """
    wrapper = make_wrapper(login_path="/dxsca-web/login")
    assert wrapper._url(wrapper._login_path) == f"{BASE_URL}{PREFIX}/login"


# --------------------------------------------------------------------------
# Blocker 2: the DXAPI authorization scheme
# --------------------------------------------------------------------------

@responses.activate
def test_login_sets_dxapi_auth_header():
    """
    DXtrade SCA expects `Authorization: DXAPI <token>`. The wrapper sent
    `Bearer`, so every authenticated request would have 401'd.
    """
    register_login()
    wrapper = make_wrapper()
    wrapper.login()

    auth = wrapper._session.headers["Authorization"]
    assert auth.startswith("DXAPI "), f"expected DXAPI scheme, got {auth!r}"
    assert "Bearer" not in auth


def test_authenticated_requests_send_dxapi_header(logged_in):
    """The scheme must actually reach the wire, not just the session object."""
    responses.add(
        responses.GET,
        f"{BASE_URL}{PREFIX}/accounts/default:ACC12345/portfolio",
        json=fixture("portfolio"),
        status=200,
    )
    logged_in.get_balance()

    sent = responses.calls[-1].request.headers["Authorization"]
    assert sent.startswith("DXAPI ")


# --------------------------------------------------------------------------
# Blocker 3: order payload account + orderCode
# --------------------------------------------------------------------------

def test_order_payload_includes_account_and_order_code(logged_in):
    """Both fields are required by SCA and both were previously absent."""
    responses.add(
        responses.POST,
        f"{BASE_URL}{PREFIX}/accounts/default:ACC12345/orders",
        json=fixture("order_accepted"),
        status=200,
    )
    logged_in.place_order(
        instrument="EUR/USD", side="BUY", quantity=1.0, order_type="MARKET"
    )

    payload = json.loads(responses.calls[-1].request.body)
    assert payload["account"] == "default:ACC12345"
    assert payload["orderCode"], "orderCode must be a non-empty idempotency key"
    assert payload["instrument"] == "EUR/USD"


def test_caller_supplied_order_code_is_respected(logged_in):
    """A caller retrying a timed-out order must be able to reuse its own key."""
    responses.add(
        responses.POST,
        f"{BASE_URL}{PREFIX}/accounts/default:ACC12345/orders",
        json=fixture("order_accepted"),
        status=200,
    )
    logged_in.place_order(
        instrument="EUR/USD",
        side="BUY",
        quantity=1.0,
        order_type="MARKET",
        orderCode="my-key-1",
    )

    payload = json.loads(responses.calls[-1].request.body)
    assert payload["orderCode"] == "my-key-1"


def test_generated_order_codes_are_unique():
    """Idempotency keys that collided would suppress legitimate orders."""
    wrapper = make_wrapper()
    codes = {wrapper._generate_order_code() for _ in range(500)}
    assert len(codes) == 500


# --------------------------------------------------------------------------
# Account code: raise instead of fabricating "primary"
# --------------------------------------------------------------------------

@responses.activate
def test_missing_account_raises_instead_of_guessing_primary():
    """
    The old fallback returned the invented code "primary", which exists at no
    broker, so every downstream call 404'd with a misleading error.
    """
    register_login(body=fixture("login_no_account"))
    wrapper = make_wrapper()
    wrapper.login()

    with pytest.raises(DXTradeAPIError) as excinfo:
        wrapper.get_balance()
    assert "account" in str(excinfo.value).lower()
    assert "primary" not in str(excinfo.value)


@responses.activate
def test_constructor_account_used_when_login_omits_one():
    """The documented escape hatch for the case above must work."""
    register_login(body=fixture("login_no_account"))
    responses.add(
        responses.GET,
        f"{BASE_URL}{PREFIX}/accounts/MYACC/portfolio",
        json=fixture("portfolio"),
        status=200,
    )
    wrapper = make_wrapper(account="MYACC")
    wrapper.login()
    balance = wrapper.get_balance()

    assert balance is not None
    assert responses.calls[-1].request.url.endswith("/accounts/MYACC/portfolio")


# --------------------------------------------------------------------------
# Keepalive
# --------------------------------------------------------------------------

def test_ping_hits_prefixed_endpoint(logged_in):
    responses.add(responses.GET, f"{BASE_URL}{PREFIX}/ping", json={}, status=200)
    assert logged_in.ping() is True
    assert responses.calls[-1].request.url == f"{BASE_URL}{PREFIX}/ping"


def test_ping_reports_failure_without_raising(logged_in):
    """A failed keepalive must never take down the background thread."""
    responses.add(responses.GET, f"{BASE_URL}{PREFIX}/ping", json={}, status=401)
    assert logged_in.ping() is False


def test_ping_is_noop_when_not_authenticated():
    assert make_wrapper().ping() is False


@responses.activate
def test_keepalive_thread_starts_and_stops():
    """Login starts the thread; logout must stop it rather than leak it."""
    register_login()
    responses.add(responses.POST, f"{BASE_URL}{PREFIX}/logout", json={}, status=200)
    responses.add(responses.GET, f"{BASE_URL}{PREFIX}/ping", json={}, status=200)

    wrapper = make_wrapper(keepalive_interval=30.0)
    wrapper.login()
    assert wrapper._keepalive_thread is not None
    assert wrapper._keepalive_thread.is_alive()
    assert wrapper._keepalive_thread.daemon

    wrapper.logout()
    assert wrapper._keepalive_thread is None


# --------------------------------------------------------------------------
# Logout
# --------------------------------------------------------------------------

def test_logout_calls_server_and_clears_state(logged_in):
    """The HTTP call used to be commented out, leaving sessions alive server-side."""
    responses.add(responses.POST, f"{BASE_URL}{PREFIX}/logout", json={}, status=200)
    logged_in.logout()

    assert responses.calls[-1].request.url == f"{BASE_URL}{PREFIX}/logout"
    assert logged_in.is_authenticated is False


def test_logout_survives_server_rejection(logged_in):
    """Local state must be cleared even if the server errors or 404s."""
    responses.add(responses.POST, f"{BASE_URL}{PREFIX}/logout", json={}, status=500)
    logged_in.logout()
    assert logged_in.is_authenticated is False


# --------------------------------------------------------------------------
# WebSocket URL and headers
# --------------------------------------------------------------------------

def test_websocket_url_is_well_formed_with_account_and_no_token():
    """
    Regression: the old builder appended "&account=..." unconditionally but only
    added "?token=..." when a token existed, emitting a query string whose first
    parameter was introduced by "&".
    """
    wrapper = make_wrapper(account="ACC1")
    url = wrapper._build_websocket_url()

    assert "?" in url
    assert "&account=" not in url.split("?")[0]
    assert url.index("?") < url.index("account=")
    assert url.startswith("wss://")


def test_websocket_url_encodes_reserved_characters():
    """Account codes contain ':' at real brokers; raw interpolation corrupts them."""
    wrapper = make_wrapper(account="default:ACC 12345")
    url = wrapper._build_websocket_url()
    assert " " not in url
    assert "account=default%3AACC+12345" in url


def test_websocket_url_has_no_query_when_nothing_to_send():
    assert "?" not in make_wrapper()._build_websocket_url()


def test_websocket_auth_header_is_actually_emitted(logged_in):
    """
    Regression: the header was guarded by `"token=" not in _build_websocket_url()`,
    which is false exactly when a token exists - so it was never sent even once.
    """
    headers = logged_in._build_websocket_headers()
    auth = [h for h in headers if h.startswith("Authorization:")]

    assert len(auth) == 1, f"expected one Authorization header, got {auth}"
    assert auth[0].startswith("Authorization: DXAPI ")
