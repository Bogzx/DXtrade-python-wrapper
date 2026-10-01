"""Shared fixtures. All HTTP is mocked with `responses`; nothing touches a broker.

Fixture JSON in tests/fixtures/ follows the shapes in Devexperts' public
DXtrade REST spec / OpenAPI document. test_spec_conformance.py (opt-in, needs
network) validates both these fixtures and the requests the client sends
against that OpenAPI document.
"""

import json
import pathlib
import sys

import pytest
import responses

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dxtrade_wrapper import DXTradeClient  # noqa: E402

BASE_URL = "https://dxtrade.example-broker.com"
PREFIX = "/dxsca-web"
API = f"{BASE_URL}{PREFIX}"
ACCOUNT = "default:ACC12345"
ACCOUNT_ENC = "default%3AACC12345"
ACC = f"{API}/accounts/{ACCOUNT_ENC}"
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
PASSWORD = "s3cret-Pa55word"
TOKEN = "b3f1c2d4-0000-4a1b-9c8d-EXAMPLE-TOKEN"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def make_wrapper(**kwargs):
    """A client with keepalive disabled so tests spawn no threads."""
    params = dict(
        base_url=BASE_URL,
        username="user",
        password=PASSWORD,
        domain_or_vendor="default",
        keepalive_interval=None,
    )
    params.update(kwargs)
    return DXTradeClient(**params)


def body(call):
    return json.loads(call.request.body) if call.request.body else None


@pytest.fixture
def api():
    """A responses mock that fails the test on any unmocked request."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


@pytest.fixture
def client(api):
    """A logged-in client with the account already known."""
    api.add(responses.POST, f"{API}/login", json=fixture("login_success"))
    wrapper = make_wrapper(account=ACCOUNT)
    wrapper.login()
    return wrapper
