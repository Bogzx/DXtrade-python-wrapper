"""Order management per the REST spec: placement, protections, modify, cancel, close."""

import pytest
import requests
import responses
from responses import matchers

from conftest import ACC, ACCOUNT, API, body, fixture
from dxtrade_wrapper import (
    ConflictError,
    DXTradeAPIError,
    NotFoundError,
    OrderPlacementError,
    PreconditionFailedError,
)


# --- validation: nothing is sent for bad input --------------------------------

@pytest.mark.parametrize(
    "kwargs, match",
    [
        (dict(side="HOLD"), "Side"),
        (dict(order_type="TRAILING"), "Order type"),
        (dict(order_type="LIMIT"), "Price is required"),
        (dict(tif="DAY"), "MARKET orders only support"),
        (dict(order_type="LIMIT", price=1.1, tif="GTD"), "expireDate"),
        (dict(position_effect="CLOSE"), "positionCode"),
        (dict(position_effect="CLOSE", positionCode="1", stop_loss=1.0), "OPEN order"),
        (dict(quantity=0), "positive"),
    ],
)
def test_invalid_orders_are_rejected_locally(client, api, kwargs, match):
    args = dict(instrument="EUR/USD", side="BUY", quantity=1000, order_type="MARKET")
    args.update(kwargs)
    with pytest.raises(ValueError, match=match):
        client.place_order(**args)
    assert len(api.calls) == 1  # only the login


def test_limit_and_stop_prices_use_the_documented_fields(client, api):
    api.add(responses.POST, f"{ACC}/orders", json=fixture("order_response"))
    client.place_order("EUR/USD", "SELL", 1000, "LIMIT", price=1.2)
    client.place_order("EUR/USD", "SELL", 1000, "STOP", price=1.0)
    limit, stop = body(api.calls[-2]), body(api.calls[-1])
    assert limit["limitPrice"] == 1.2 and "stopPrice" not in limit
    assert stop["stopPrice"] == 1.0 and "limitPrice" not in stop


def test_stop_loss_and_take_profit_are_one_atomic_if_then_group(client, api):
    """The old code posted SL/TP as separate orders only if the order response
    carried a positionCode. The spec's OrderResponse has only orderId and
    updateOrderId, so protections were silently never placed."""
    api.add(responses.POST, f"{ACC}/orders", json=fixture("group_response"))
    result = client.place_order("EUR/USD", "BUY", 100000, "MARKET",
                                stop_loss=1.05, take_profit=1.15)

    assert len(api.calls) == 2  # login + ONE order request
    group = body(api.calls[-1])
    assert group["contingencyType"] == "IF-THEN"
    entry, sl, tp = group["orders"]
    assert entry["positionEffect"] == "OPEN" and entry["quantity"] == 100000
    for leg, kind, field, price in ((sl, "STOP", "stopPrice", 1.05),
                                    (tp, "LIMIT", "limitPrice", 1.15)):
        assert leg["type"] == kind and leg[field] == price
        assert leg["positionEffect"] == "CLOSE"
        assert leg["side"] == "SELL"
        assert leg["quantity"] == 0
        assert leg["tif"] == "GTC"
        assert leg["instrument"] == "EUR/USD" and leg["account"] == ACCOUNT
    assert len({o["orderCode"] for o in group["orders"]}) == 3
    assert len(result["orderResponses"]) == 3


def test_stop_loss_only_group_has_two_orders(client, api):
    api.add(responses.POST, f"{ACC}/orders", json=fixture("group_response"))
    client.place_order("EUR/USD", "SELL", 1000, "MARKET", stop_loss=1.2)
    entry, sl = body(api.calls[-1])["orders"]
    assert sl["type"] == "STOP" and sl["side"] == "BUY"


def test_rejected_protected_order_raises_and_places_nothing_else(client, api):
    api.add(responses.POST, f"{ACC}/orders", status=400,
            json={"errorCode": "33", "description": "Incorrect request (groups need a "
                                                     "position-based account)"})
    with pytest.raises(OrderPlacementError) as excinfo:
        client.place_order("EUR/USD", "BUY", 1000, "MARKET", stop_loss=1.05)
    assert excinfo.value.error_code == "33"
    assert excinfo.value.ambiguous is False
    assert len(api.calls) == 2


def test_business_rejection_carries_status_and_code(client, api):
    api.add(responses.POST, f"{ACC}/orders", status=409,
            json={"errorCode": "100", "description": "Order with this id already exists"})
    with pytest.raises(OrderPlacementError) as excinfo:
        client.place_order("EUR/USD", "BUY", 1000, "MARKET", orderCode="dup-1")
    exc = excinfo.value
    assert exc.status_code == 409 and exc.error_code == "100"
    assert exc.order_code == "dup-1"


def test_read_timeout_on_placement_is_flagged_ambiguous(client, api):
    api.add(responses.POST, f"{ACC}/orders", body=requests.exceptions.ReadTimeout("slow"))
    with pytest.raises(OrderPlacementError) as excinfo:
        client.place_order("EUR/USD", "BUY", 1000, "MARKET", orderCode="k-1")
    assert excinfo.value.ambiguous is True
    assert "k-1" in str(excinfo.value)


def test_connect_timeout_on_placement_is_not_ambiguous(client, api):
    api.add(responses.POST, f"{ACC}/orders", body=requests.exceptions.ConnectTimeout("down"))
    with pytest.raises(OrderPlacementError) as excinfo:
        client.place_order("EUR/USD", "BUY", 1000, "MARKET")
    assert excinfo.value.ambiguous is False


def test_order_retried_after_session_expiry_keeps_its_order_code(client, api):
    api.add(responses.POST, f"{ACC}/orders", status=401, json={"errorCode": "1"})
    api.add(responses.POST, f"{API}/login", json={"sessionToken": "NEW"})
    api.add(responses.POST, f"{ACC}/orders", json=fixture("order_response"))
    client.place_order("EUR/USD", "BUY", 1000, "MARKET")
    first, second = body(api.calls[1]), body(api.calls[3])
    assert first["orderCode"] == second["orderCode"]


# --- modify / cancel: PUT and DELETE need If-Match ------------------------------

def orders_with_etag(api, etag='"v42"'):
    api.add(responses.GET, f"{ACC}/orders", json=fixture("orders"), headers={"ETag": etag})


def test_modify_order_puts_the_complete_order_with_if_match(client, api):
    orders_with_etag(api)
    api.add(responses.PUT, f"{ACC}/orders", json=fixture("order_response"),
            match=[matchers.header_matcher({"If-Match": '"v42"'})])

    client.modify_order("3a4-DEF", new_price=1.305)

    put = body(api.calls[-1])
    assert api.calls[-1].request.method == "PUT"
    assert put == {
        "account": ACCOUNT,
        "orderCode": "dxpw-entry-2",
        "instrument": "GBP/USD",
        "positionEffect": "OPEN",
        "side": "BUY",
        "tif": "GTC",
        "quantity": 5000,
        "stopPrice": 1.305,
    }
    assert "type" not in put  # the spec says omit type in replace requests


@pytest.mark.parametrize("identifier", ["3a4-DEF", "dxpw-entry-2", "63700"])
def test_modify_accepts_any_order_identifier(client, api, identifier):
    orders_with_etag(api)
    api.add(responses.PUT, f"{ACC}/orders", json=fixture("order_response"))
    client.modify_order(identifier, new_quantity=6000)
    assert body(api.calls[-1])["quantity"] == 6000


def test_modify_unknown_order_raises_not_found(client, api):
    orders_with_etag(api)
    with pytest.raises(NotFoundError):
        client.modify_order("nope", new_price=1.0)


def test_modify_with_nothing_to_change_is_rejected(client):
    with pytest.raises(ValueError):
        client.modify_order("3a4-DEF")


def test_stale_etag_is_refreshed_and_retried_once(client, api):
    orders_with_etag(api, '"v42"')
    api.add(responses.PUT, f"{ACC}/orders", status=412,
            match=[matchers.header_matcher({"If-Match": '"v42"'})])
    orders_with_etag(api, '"v43"')
    api.add(responses.PUT, f"{ACC}/orders", json=fixture("order_response"),
            match=[matchers.header_matcher({"If-Match": '"v43"'})])
    client.modify_order("3a4-DEF", new_price=1.3)
    assert api.calls[-1].response.status_code == 200


def test_explicit_stale_etag_is_not_retried(client, api):
    orders_with_etag(api)
    api.add(responses.PUT, f"{ACC}/orders", status=412)
    with pytest.raises(PreconditionFailedError):
        client.modify_order("3a4-DEF", new_price=1.3, etag='"old"')


def test_missing_etag_is_a_clear_error(client, api):
    api.add(responses.GET, f"{ACC}/orders", json=fixture("orders"))
    with pytest.raises(DXTradeAPIError, match="ETag"):
        client.modify_order("3a4-DEF", new_price=1.3)


def test_cancel_order_deletes_encoded_code_with_if_match(client, api):
    orders_with_etag(api)
    api.add(responses.DELETE, f"{ACC}/orders/dxpw%2Fweird%23id",
            json=fixture("order_response"),
            match=[matchers.header_matcher({"If-Match": '"v42"'})])
    client.cancel_order("dxpw/weird#id")
    assert api.calls[-1].request.method == "DELETE"


def test_cancel_with_caller_etag_skips_the_lookup(client, api):
    api.add(responses.DELETE, f"{ACC}/orders/3a4-DEF", body="")
    assert client.cancel_order("3a4-DEF", etag='"v9"') == {"success": True,
                                                          "order_id": "3a4-DEF"}
    assert len(api.calls) == 2


def test_cancel_of_final_order_surfaces_conflict(client, api):
    orders_with_etag(api)
    api.add(responses.DELETE, f"{ACC}/orders/3a4-DEF", status=409,
            json={"errorCode": "1005", "description": "Reference order is closed"})
    with pytest.raises(ConflictError, match="Reference order is closed"):
        client.cancel_order("3a4-DEF")


# --- positions -------------------------------------------------------------------

def test_close_position_sends_linked_market_close(client, api):
    api.add(responses.GET, f"{ACC}/positions", json=fixture("positions"))
    api.add(responses.POST, f"{ACC}/orders", json=fixture("order_response"))
    client.close_position("63649")
    order = body(api.calls[-1])
    assert order["type"] == "MARKET"
    assert order["positionEffect"] == "CLOSE"
    assert order["positionCode"] == "63649"
    assert order["side"] == "SELL"
    assert order["quantity"] == 100000
    assert order["instrument"] == "EUR/USD"


def test_partial_close_and_bounds(client, api):
    api.add(responses.GET, f"{ACC}/positions", json=fixture("positions"))
    api.add(responses.POST, f"{ACC}/orders", json=fixture("order_response"))
    client.close_position("63649", quantity=40000)
    assert body(api.calls[-1])["quantity"] == 40000
    with pytest.raises(ValueError):
        client.close_position("63649", quantity=200000)


def test_close_unknown_position_raises(client, api):
    api.add(responses.GET, f"{ACC}/positions", json=fixture("positions"))
    with pytest.raises(NotFoundError):
        client.close_position("nope")


def test_close_all_uses_bulk_close(client, api):
    api.add(responses.POST, f"{ACC}/close", body="")
    client.close_all(instrument="EUR/USD", comment="flat before NFP")
    assert body(api.calls[-1]) == {"closePositions": True, "cancelOrders": True,
                                   "instrument": "EUR/USD", "comment": "flat before NFP"}
    with pytest.raises(ValueError):
        client.close_all(close_positions=False, cancel_orders=False)


def test_modify_sl_tp_moves_existing_tp_and_adds_missing_sl(client, api):
    api.add(responses.GET, f"{ACC}/positions", json=fixture("positions"))
    orders_with_etag(api)
    api.add(responses.POST, f"{ACC}/orders", json=fixture("order_response"))
    api.add(responses.PUT, f"{ACC}/orders", json=fixture("order_response"))

    result = client.modify_position_sl_tp("63649", stop_loss=1.08, take_profit=1.10)

    assert set(result) == {"stop_loss", "take_profit"}
    posted = [body(c) for c in api.calls
              if c.request.method == "POST" and "orders" in c.request.url]
    put = [body(c) for c in api.calls if c.request.method == "PUT"]
    assert posted[0]["type"] == "STOP" and posted[0]["stopPrice"] == 1.08
    assert posted[0]["positionCode"] == "63649" and posted[0]["side"] == "SELL"
    assert "quantity" not in posted[0]
    assert put[0]["orderCode"] == "dxpw-tp-1" and put[0]["limitPrice"] == 1.10
    assert "quantity" not in put[0]  # position-attached protection keeps no quantity


def test_modify_sl_tp_requires_a_value(client):
    with pytest.raises(ValueError):
        client.modify_position_sl_tp("63649")


# --- review 2026-09-30: IF-THEN groups, stale 412 bodies, acknowledged legs -------

def group_orders_with_etag(api, data=None, etag='"g1"'):
    api.add(responses.GET, f"{ACC}/orders", json=data or fixture("orders_group"),
            headers={"ETag": etag})


def test_modify_open_order_omits_its_own_position_code(client, api):
    """The Open Orders listing gives an OPEN leg positionCode == its orderId; the
    spec says positionCode must be omitted unless positionEffect is CLOSE."""
    orders_with_etag(api)
    api.add(responses.PUT, f"{ACC}/orders", json=fixture("order_response"))
    client.modify_order("3a4-DEF", new_price=1.305)
    assert "positionCode" not in body(api.calls[-1])


def test_modifying_a_group_parent_resends_the_whole_group(client, api):
    """Spec, Modify Order example 1: a single-order PUT for the parent of an IF-THEN
    group turns it into a single order - the stop loss and take profit vanish."""
    group_orders_with_etag(api)
    api.add(responses.PUT, f"{ACC}/orders", json=fixture("group_response"),
            match=[matchers.header_matcher({"If-Match": '"g1"'})])

    client.modify_order("grp-entry", new_price=1.04)

    put = body(api.calls[-1])
    assert put["contingencyType"] == "IF-THEN"
    entry, sl, tp = put["orders"]
    assert entry == {"account": ACCOUNT, "orderCode": "grp-entry", "instrument": "EUR/USD",
                     "positionEffect": "OPEN", "side": "BUY", "tif": "GTC",
                     "quantity": 200000, "limitPrice": 1.04}
    assert sl == {"account": ACCOUNT, "orderCode": "grp-sl", "instrument": "EUR/USD",
                  "positionEffect": "CLOSE", "side": "SELL", "tif": "GTC",
                  "quantity": 0, "stopPrice": 1.00}
    assert tp["orderCode"] == "grp-tp" and tp["limitPrice"] == 1.10 and tp["quantity"] == 0
    assert all("type" not in o for o in put["orders"])


def test_modifying_a_pending_groups_stop_loss_keeps_the_group(client, api):
    group_orders_with_etag(api)
    api.add(responses.PUT, f"{ACC}/orders", json=fixture("group_response"))
    client.modify_order("grp-sl", new_price=1.01)
    entry, sl, tp = body(api.calls[-1])["orders"]
    assert entry["orderCode"] == "grp-entry" and entry["limitPrice"] == 1.05
    assert sl["stopPrice"] == 1.01 and tp["limitPrice"] == 1.10


def test_child_of_a_filled_parent_is_modified_on_its_own(client, api):
    data = fixture("orders_group")
    data["orders"] = data["orders"][1:]  # entry filled: no longer a working order
    group_orders_with_etag(api, data)
    api.add(responses.PUT, f"{ACC}/orders", json=fixture("order_response"))
    client.modify_order("grp-sl", new_price=1.01)
    put = body(api.calls[-1])
    assert put["orderCode"] == "grp-sl" and put["stopPrice"] == 1.01
    assert put["positionCode"] == "70001" and "quantity" not in put


def test_group_with_a_missing_child_is_not_modified(client, api):
    data = fixture("orders_group")
    del data["orders"][2]
    group_orders_with_etag(api, data)
    with pytest.raises(DXTradeAPIError, match="not a working order"):
        client.modify_order("grp-entry", new_price=1.04)
    assert not [c for c in api.calls if c.request.method == "PUT"]


def test_oco_member_is_not_modified_with_a_single_put(client, api):
    data = fixture("orders")
    data["orders"][1]["links"] = [{"linkType": "OCO", "linkedOrder": "3a4-ABC"}]
    orders_with_etag(api)
    api.replace(responses.GET, f"{ACC}/orders", json=data, headers={"ETag": '"v42"'})
    with pytest.raises(DXTradeAPIError, match="OCO"):
        client.modify_order("3a4-DEF", new_price=1.3)
    assert not [c for c in api.calls if c.request.method == "PUT"]


def test_412_retry_rebuilds_the_body_from_the_fresh_order(client, api):
    """Retrying the body built from the stale read would silently undo whatever
    change caused the 412 - exactly what If-Match exists to prevent."""
    orders_with_etag(api, '"v42"')
    api.add(responses.PUT, f"{ACC}/orders", status=412)
    changed = fixture("orders")
    changed["orders"][1]["legs"][0]["quantity"] = 7000
    api.add(responses.GET, f"{ACC}/orders", json=changed, headers={"ETag": '"v43"'})
    api.add(responses.PUT, f"{ACC}/orders", json=fixture("order_response"),
            match=[matchers.header_matcher({"If-Match": '"v43"'})])

    client.modify_order("3a4-DEF", new_price=1.3)

    first, retry = [body(c) for c in api.calls if c.request.method == "PUT"]
    assert first["quantity"] == 5000
    assert retry["quantity"] == 7000 and retry["stopPrice"] == 1.3


def test_group_acknowledging_fewer_orders_than_sent_is_loud(client, api):
    api.add(responses.POST, f"{ACC}/orders",
            json={"orderResponses": [{"orderId": 1, "updateOrderId": 1}]})
    with pytest.raises(OrderPlacementError, match="WITHOUT its stop loss") as excinfo:
        client.place_order("EUR/USD", "BUY", 1000, "MARKET", stop_loss=1.0, take_profit=1.2,
                           orderCode="entry-7")
    assert excinfo.value.ambiguous is True and excinfo.value.order_code == "entry-7"


def test_close_short_position_buys_it_back(client, api):
    data = fixture("positions")
    data["positions"][0]["side"] = "SELL"
    api.add(responses.GET, f"{ACC}/positions", json=data)
    api.add(responses.POST, f"{ACC}/orders", json=fixture("order_response"))
    client.close_position("63649", quantity=25000)
    order = body(api.calls[-1])
    assert order["side"] == "BUY" and order["quantity"] == 25000
    assert order["positionEffect"] == "CLOSE" and order["positionCode"] == "63649"


@pytest.mark.parametrize("status", [500, 502, 504])
def test_server_error_on_placement_is_flagged_ambiguous(client, api, status):
    """A gateway timeout says nothing about whether the order was placed; calling
    it unambiguous invites a resend with a new orderCode, i.e. a duplicate."""
    api.add(responses.POST, f"{ACC}/orders", status=status, body="upstream timed out")
    with pytest.raises(OrderPlacementError) as excinfo:
        client.place_order("EUR/USD", "BUY", 1000, "MARKET", orderCode="k-5")
    assert excinfo.value.ambiguous is True and "k-5" in str(excinfo.value)
