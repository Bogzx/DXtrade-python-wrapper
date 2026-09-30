"""Account discovery and read endpoints, parsed per the spec's field names."""

import pytest
import responses

from conftest import ACC, ACCOUNT, API, fixture, make_wrapper
from dxtrade_wrapper import DXTradeAPIError


@pytest.fixture
def anon_client(api):
    """Logged in, but no account configured: it must be discovered."""
    api.add(responses.POST, f"{API}/login", json=fixture("login_success"))
    wrapper = make_wrapper()
    wrapper.login()
    return wrapper


def test_single_account_is_discovered_from_users_endpoint(anon_client, api):
    api.add(responses.GET, f"{API}/users/user%40default", json=fixture("users"))
    api.add(responses.GET, f"{ACC}/metrics", json=fixture("metrics"))

    balance = anon_client.get_balance()

    assert anon_client._account_id == ACCOUNT
    assert balance.currency == "USD"  # from the account's baseCurrency


def test_full_username_is_used_as_is(api):
    api.add(responses.POST, f"{API}/login", json=fixture("login_success"))
    api.add(responses.GET, f"{API}/users/trader%40prop", json=fixture("users"))
    wrapper = make_wrapper(username="trader@prop")
    wrapper.login()
    assert wrapper.get_accounts()[0]["account"] == ACCOUNT


def test_several_accounts_require_an_explicit_choice(anon_client, api):
    users = fixture("users")
    second = dict(users["userDetails"][0]["accounts"][0], account="default:ACC99999")
    users["userDetails"][0]["accounts"].append(second)
    api.add(responses.GET, f"{API}/users/user%40default", json=users)

    with pytest.raises(DXTradeAPIError) as excinfo:
        anon_client.get_positions()
    message = str(excinfo.value)
    assert "default:ACC12345" in message and "default:ACC99999" in message
    assert "account=" in message


def test_no_accounts_raises_instead_of_inventing_one(anon_client, api):
    users = fixture("users")
    users["userDetails"][0]["accounts"] = []
    api.add(responses.GET, f"{API}/users/user%40default", json=users)
    with pytest.raises(DXTradeAPIError) as excinfo:
        anon_client.get_positions()
    assert "primary" not in str(excinfo.value)


def test_object_shaped_account_codes_are_normalised(anon_client, api):
    """The OpenAPI schema models AccountCode as {clearing, account}."""
    users = fixture("users")
    users["userDetails"][0]["accounts"][0]["account"] = {"clearing": "default",
                                                         "account": "ACC12345"}
    api.add(responses.GET, f"{API}/users/user%40default", json=users)
    assert anon_client.get_accounts()[0]["account"] == ACCOUNT


def test_balance_comes_from_account_metrics(client, api):
    api.add(responses.GET, f"{ACC}/metrics", json=fixture("metrics"))
    balance = client.get_balance()

    assert balance.equity == 100250.0
    assert balance.balance == 100000.0
    assert balance.margin == 1500.0
    assert balance.free_margin == 98750.0
    assert balance.available_funds == 98750.0
    assert balance.open_pl == 250.0
    assert balance.margin_level == pytest.approx(100250.0 / 1500.0 * 100)
    assert balance.account == ACCOUNT


def test_zero_margin_gives_no_margin_level(client, api):
    metrics = fixture("metrics")
    metrics["metrics"][0]["margin"] = 0
    api.add(responses.GET, f"{ACC}/metrics", json=metrics)
    assert client.get_balance().margin_level is None


def test_malformed_metrics_raise_instead_of_returning_zeros(client, api):
    """The old parser returned Balance(0, 0, 0, 0) on any parse problem."""
    api.add(responses.GET, f"{ACC}/metrics", json={"metrics": [{"account": ACCOUNT}]})
    with pytest.raises(DXTradeAPIError, match="equity"):
        client.get_balance()


def test_numbers_sent_as_strings_are_accepted(client, api):
    metrics = fixture("metrics")
    metrics["metrics"][0].update(equity="100250.5", margin="1500", balance="1e5",
                                 marginFree="98750")
    api.add(responses.GET, f"{ACC}/metrics", json=metrics)
    assert client.get_balance().equity == 100250.5


def test_positions_are_parsed_from_the_documented_fields(client, api):
    api.add(responses.GET, f"{ACC}/positions", json=fixture("positions"))
    [position] = client.get_positions()

    assert position.position_id == "63649"
    assert position.instrument == "EUR/USD"
    assert position.quantity == 100000
    assert position.side == "BUY"
    assert position.entry_price == 1.0850
    assert position.take_profit == 1.0950
    assert position.stop_loss is None
    assert position.open_time == "2026-09-30T08:00:00.000Z"


def test_position_missing_required_field_raises(client, api):
    api.add(responses.GET, f"{ACC}/positions", json={"positions": [{"symbol": "EUR/USD"}]})
    with pytest.raises(DXTradeAPIError, match="positionCode"):
        client.get_positions()


def test_orders_are_parsed_from_order_and_leg(client, api):
    api.add(responses.GET, f"{ACC}/orders", json=fixture("orders"))
    tp, entry = client.get_orders()

    assert tp.order_id == "3a4-ABC"
    assert tp.client_order_id == "dxpw-tp-1"
    assert tp.server_order_id == 63655
    assert tp.order_type == "LIMIT"
    assert tp.price == 1.0950
    assert tp.position_effect == "CLOSE"
    assert tp.position_code == "63649"
    assert tp.final_status is False
    assert entry.quantity == 5000
    assert entry.side == "BUY"
    assert entry.status == "WORKING"


def test_non_envelope_payload_is_rejected(client, api):
    api.add(responses.GET, f"{ACC}/orders", json={"orders": "nope"})
    with pytest.raises(DXTradeAPIError):
        client.get_orders()


def test_order_history_uses_documented_path_and_filters(client, api):
    api.add(responses.GET, f"{ACC}/orders/history", json=fixture("history"))
    history = client.get_order_history(limit=50, in_status="COMPLETED", period="today")

    assert history[0]["status"] == "COMPLETED"
    url = api.calls[-1].request.url
    assert "/orders/history?" in url
    assert "limit=50" in url and "in-status=COMPLETED" in url and "period=today" in url


def test_portfolio_is_unwrapped(client, api):
    api.add(responses.GET, f"{ACC}/portfolio",
            json={"portfolios": [{"account": ACCOUNT, "version": 1, "balances": [],
                                  "positions": [], "orders": []}]})
    assert client.get_portfolio()["account"] == ACCOUNT


def test_positions_can_include_floating_pnl(client, api):
    api.add(responses.GET, f"{ACC}/positions", json=fixture("positions"))
    metrics = fixture("metrics")
    metrics["metrics"][0]["positions"] = [{"positionCode": "63649", "symbol": "EUR/USD",
                                           "fpl": 125.5, "quantity": 100000}]
    api.add(responses.GET, f"{ACC}/metrics", json=metrics)

    [position] = client.get_positions(include_pnl=True)

    assert position.pnl == 125.5
    assert "include-positions=true" in api.calls[-1].request.url


def test_positions_without_pnl_make_one_request(client, api):
    api.add(responses.GET, f"{ACC}/positions", json=fixture("positions"))
    [position] = client.get_positions()
    assert position.pnl is None
    assert len(api.calls) == 2
