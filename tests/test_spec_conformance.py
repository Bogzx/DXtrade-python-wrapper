"""Checks the client and the fixtures against Devexperts' published OpenAPI document.

Opt-in because it downloads the spec (a public, read-only GET):

    DXTRADE_SPEC_TESTS=1 python -m pytest tests/test_spec_conformance.py -v

The spec is not vendored into this repo. Set DXTRADE_OPENAPI_FILE to use a local
copy instead of downloading.

What it proves: every request the client makes targets a documented path and
method, sends only declared fields, and validates against the schema; every
fixture the other tests rely on matches the documented response schema.
What it cannot prove: that a given broker's deployment behaves like the spec.

Known spec quirks handled here (documented in the README):
* ``AccountCode`` is an object in the OpenAPI schema but a "clearing:account"
  string in the prose spec and all its examples; both are accepted.
* ``SessionToken.timeout`` and the date/time types (``UTCDateTime``...) are
  property-less ``object``s in the OpenAPI schema; the prose spec defines
  strings. Strings are accepted.
* Order *groups* (IF-THEN / OCO) are documented in the prose spec but the
  OpenAPI request body only models a single order, so each order in a group is
  validated as a SingleOrderRequest.
"""

import copy
import json
import os
import pathlib
import re
import urllib.request

import pytest
import responses

from conftest import ACC, ACCOUNT, API, body, fixture, make_wrapper

SPEC_URL = "https://demo.dx.trade/dxsca-web/swagger/openapi.json"

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DXTRADE_SPEC_TESTS") or os.environ.get("DXTRADE_OPENAPI_FILE")),
    reason="set DXTRADE_SPEC_TESTS=1 to download and check against the official OpenAPI spec",
)

jsonschema = pytest.importorskip("jsonschema")


@pytest.fixture(scope="module")
def spec():
    local = os.environ.get("DXTRADE_OPENAPI_FILE")
    if local:
        doc = json.loads(pathlib.Path(local).read_text(encoding="utf-8"))
    else:
        with urllib.request.urlopen(SPEC_URL, timeout=30) as resp:  # noqa: S310
            doc = json.load(resp)
    doc = copy.deepcopy(doc)
    schemas = doc["components"]["schemas"]
    schemas["AccountCode"] = {"anyOf": [{"type": "string"}, schemas["AccountCode"]]}
    schemas["SessionToken"]["properties"]["timeout"] = {}
    # Date/time types are property-less "object"s in the OpenAPI document (a Java
    # type artefact); the prose spec defines them as RFC 3339 strings.
    for name, schema in schemas.items():
        if schema.get("type") == "object" and not schema.get("properties"):
            schemas[name] = {"anyOf": [{"type": "string"}, schema]}
    return doc


def validator(spec, name):
    schema = {"$ref": f"#/components/schemas/{name}", "components": spec["components"]}
    return jsonschema.Draft7Validator(schema)


def declared(spec, name):
    return set(spec["components"]["schemas"][name].get("properties", {}))


def find_operation(spec, method, url):
    path = url.split("?")[0][len(API):]
    for template, ops in spec["paths"].items():
        pattern = "^" + re.sub(r"\{[^/]+\}", "[^/]+", template) + "$"
        if re.match(pattern, path) and method.lower() in ops:
            return template, ops[method.lower()]
    return None, None


def run_scenario():
    """Drives every REST method against mocks and returns the captured calls."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as api:
        etag = {"ETag": '"v1"'}
        api.add(responses.POST, f"{API}/login", json=fixture("login_success"))
        api.add(responses.GET, f"{API}/users/user%40default", json=fixture("users"))
        api.add(responses.GET, f"{ACC}/metrics", json=fixture("metrics"))
        api.add(responses.GET, f"{ACC}/portfolio",
                json={"portfolios": [{"account": ACCOUNT, "version": 1, "balances": [],
                                      "positions": [], "orders": []}]})
        api.add(responses.GET, f"{ACC}/positions", json=fixture("positions"))
        api.add(responses.GET, f"{ACC}/orders", json=fixture("orders"), headers=etag)
        api.add(responses.GET, f"{ACC}/orders/history", json=fixture("history"))
        api.add(responses.POST, f"{ACC}/orders", json=fixture("order_response"))
        api.add(responses.PUT, f"{ACC}/orders", json=fixture("order_response"))
        api.add(responses.DELETE, re.compile(rf"{re.escape(ACC)}/orders/.+"), body="")
        api.add(responses.POST, f"{ACC}/close", body="")
        api.add(responses.POST, f"{API}/ping", json={})
        api.add(responses.POST, f"{API}/logout", body="")

        client = make_wrapper()
        client.login()
        client.get_accounts()
        client.get_balance()
        client.get_portfolio()
        client.get_positions()
        client.get_orders()
        client.get_order_history(limit=10, in_status="COMPLETED")
        client.place_order("EUR/USD", "BUY", 1000, "MARKET")
        client.place_order("EUR/USD", "BUY", 1000, "LIMIT", price=1.05)
        client.place_order("EUR/USD", "BUY", 1000, "MARKET", stop_loss=1.0, take_profit=1.2)
        client.modify_order("3a4-DEF", new_price=1.3)
        api.replace(responses.GET, f"{ACC}/orders", json=fixture("orders_group"), headers=etag)
        client.modify_order("grp-entry", new_price=1.04)
        api.replace(responses.GET, f"{ACC}/orders", json=fixture("orders"), headers=etag)
        client.cancel_order("3a4-DEF")
        client.close_position("63649")
        client.modify_position_sl_tp("63649", stop_loss=1.08, take_profit=1.1)
        client.close_all()
        client.ping()
        client.logout()
        return list(api.calls)


def test_every_request_targets_a_documented_operation(spec):
    for call in run_scenario():
        template, op = find_operation(spec, call.request.method, call.request.url)
        assert op is not None, f"{call.request.method} {call.request.url} not in the spec"
        documented = {p["name"] for p in op.get("parameters", []) if p.get("in") == "query"}
        query = call.request.url.split("?")[1] if "?" in call.request.url else ""
        for pair in filter(None, query.split("&")):
            name = pair.split("=")[0]
            assert name in documented, f"undocumented query param {name} on {template}"


def test_every_request_body_matches_its_schema(spec):
    checked = 0
    for call in run_scenario():
        payload = body(call)
        if payload is None:
            continue
        template, op = find_operation(spec, call.request.method, call.request.url)
        schema_ref = op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        name = schema_ref.split("/")[-1]
        members = payload["orders"] if "contingencyType" in payload else [payload]
        if "contingencyType" in payload:
            assert set(payload) == {"orders", "contingencyType"}
            assert payload["contingencyType"] in ("IF-THEN", "OCO")
        for index, member in enumerate(members):
            validator(spec, name).validate(member)
            # Prose rules the schema cannot express (Single Order Request):
            # positionCode only with CLOSE (IF-THEN children carry neither), and
            # no order type in replace (PUT) requests.
            is_then_child = payload.get("contingencyType") == "IF-THEN" and index > 0
            if "positionCode" in member:
                assert member.get("positionEffect") == "CLOSE", f"{template}: {member}"
            elif member.get("positionEffect") == "CLOSE":
                assert is_then_child, f"{template}: CLOSE without positionCode: {member}"
            if call.request.method == "PUT":
                assert "type" not in member, f"{template}: type in a replace request"
            unknown = set(member) - declared(spec, name)
            assert not unknown, f"{template}: fields not in {name}: {unknown}"
            checked += 1
    assert checked >= 8


@pytest.mark.parametrize(
    "fixture_name, schema",
    [
        ("login_success", "SessionToken"),
        ("users", "UserDetailsList"),
        ("metrics", "AccountMetricsList"),
        ("positions", "PositionList"),
        ("orders", "OrderList"),
        ("orders_group", "OrderList"),
        ("history", "OrderList"),
        ("order_response", "OrderResponse"),
        ("group_response", "OrderResponseList"),
        ("error_bad_credentials", "ApiError"),
    ],
)
def test_fixtures_match_documented_response_schemas(spec, fixture_name, schema):
    data = fixture(fixture_name)
    validator(spec, schema).validate(data)
    assert not set(data) - declared(spec, schema)


def test_parsers_only_read_documented_fields(spec):
    """Guards against the old bug class: parsing invented names (entryPrice, id...)."""
    import inspect

    from dxtrade_wrapper import models

    checks = (
        ("Position", models.parse_position, r'data(?:\.get\(|\[)"(\w+)"'),
        ("AccountMetrics", models.parse_balance, r'metrics\.get\("(\w+)"'),
    )
    for schema, parser, pattern in checks:
        fields = set(re.findall(pattern, inspect.getsource(parser)))
        assert fields, f"no fields found for {schema}"
        # openPl/totalPl are the prose-spec spellings, read as fallbacks.
        extra = fields - declared(spec, schema) - {"openPl", "totalPl"}
        assert not extra, f"{schema} parser reads undocumented fields {extra}"
