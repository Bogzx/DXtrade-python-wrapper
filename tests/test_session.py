"""Login, typed errors, session refresh, keepalive, logout, credential hygiene."""

import logging

import pytest
import requests
import responses

from conftest import ACC, ACCOUNT, API, PASSWORD, TOKEN, fixture, make_wrapper
from dxtrade_wrapper import (
    AuthenticationError,
    ConnectionError,
    DXTradeAPIError,
    NotFoundError,
    RateLimitError,
    ServerError,
)
from dxtrade_wrapper.client import parse_interval


def test_bad_credentials_raise_authentication_error_with_server_detail(api):
    api.add(responses.POST, f"{API}/login", json=fixture("error_bad_credentials"), status=401)
    wrapper = make_wrapper()
    with pytest.raises(AuthenticationError) as excinfo:
        wrapper.login()
    exc = excinfo.value
    assert exc.status_code == 401
    assert exc.error_code == "3"
    assert "Incorrect username or password" in str(exc)
    assert wrapper.is_authenticated is False


def test_403_login_explains_rest_access_is_disabled(api):
    api.add(responses.POST, f"{API}/login", json={}, status=403)
    with pytest.raises(AuthenticationError, match="not enabled DXtrade REST"):
        make_wrapper().login()


def test_login_without_token_is_an_error(api):
    api.add(responses.POST, f"{API}/login", json={"timeout": "00:30:00"})
    with pytest.raises(AuthenticationError, match="sessionToken"):
        make_wrapper().login()


def test_network_failure_is_a_connection_error(api):
    api.add(responses.POST, f"{API}/login", body=requests.exceptions.ConnectTimeout("boom"))
    with pytest.raises(ConnectionError):
        make_wrapper().login()


def test_calls_before_login_raise():
    with pytest.raises(AuthenticationError, match="login"):
        make_wrapper(account=ACCOUNT).get_positions()


@pytest.mark.parametrize(
    "status, exc_type",
    [(404, NotFoundError), (429, RateLimitError), (500, ServerError), (400, DXTradeAPIError)],
)
def test_http_errors_map_to_typed_exceptions(client, api, status, exc_type, monkeypatch):
    monkeypatch.setattr("dxtrade_wrapper.client.time.sleep", lambda seconds: None)
    api.add(responses.GET, f"{ACC}/positions",
            json={"errorCode": "2", "description": "Entity not found at server"},
            status=status, headers={"Retry-After": "3"})
    with pytest.raises(exc_type) as excinfo:
        client.get_positions()
    assert excinfo.value.status_code == status
    assert excinfo.value.error_code == "2"
    if status == 429:
        assert excinfo.value.retry_after == 3.0


def test_expired_session_is_renewed_once_and_the_call_retried(client, api):
    api.add(responses.GET, f"{ACC}/positions", json={"errorCode": "1"}, status=401)
    api.add(responses.POST, f"{API}/login", json={"sessionToken": "NEW-TOKEN"})
    api.add(responses.GET, f"{ACC}/positions", json=fixture("positions"))

    positions = client.get_positions()

    assert len(positions) == 1
    assert [c.request.url.rsplit("/", 1)[-1] for c in api.calls[1:]] == [
        "positions", "login", "positions"]
    assert api.calls[-1].request.headers["Authorization"] == "DXAPI NEW-TOKEN"


def test_relogin_is_attempted_only_once(client, api):
    api.add(responses.GET, f"{ACC}/positions", json={"errorCode": "1"}, status=401)
    api.add(responses.POST, f"{API}/login", json={"sessionToken": "NEW"})
    api.add(responses.GET, f"{ACC}/positions", json={"errorCode": "1"}, status=401)
    with pytest.raises(AuthenticationError):
        client.get_positions()
    assert len(api.calls) == 4  # login, positions, login, positions


def test_auto_relogin_can_be_disabled(api):
    api.add(responses.POST, f"{API}/login", json=fixture("login_success"))
    api.add(responses.GET, f"{ACC}/positions", json={"errorCode": "1"}, status=401)
    wrapper = make_wrapper(account=ACCOUNT, auto_relogin=False)
    wrapper.login()
    with pytest.raises(AuthenticationError):
        wrapper.get_positions()
    assert len(api.calls) == 2


def test_ping_is_a_post_and_adopts_a_renewed_token(client, api):
    api.add(responses.POST, f"{API}/ping", json={"sessionToken": "RENEWED", "timeout": "00:10:00"})
    assert client.ping() is True
    assert api.calls[-1].request.method == "POST"
    assert client._session.headers["Authorization"] == "DXAPI RENEWED"
    assert client._session_timeout == 600


def test_ping_reports_failure_without_raising(client, api):
    api.add(responses.POST, f"{API}/ping", json={}, status=401)
    assert client.ping() is False


def test_ping_is_noop_when_not_authenticated():
    assert make_wrapper().ping() is False


def test_keepalive_interval_is_capped_by_session_timeout(api):
    api.add(responses.POST, f"{API}/login", json={"sessionToken": "T", "timeout": "00:01:00"})
    api.add(responses.POST, f"{API}/logout")
    wrapper = make_wrapper(keepalive_interval=300.0)
    wrapper.login()
    try:
        assert wrapper._effective_keepalive() == 30.0
        assert wrapper._keepalive_thread.is_alive() and wrapper._keepalive_thread.daemon
    finally:
        wrapper.logout()
    assert wrapper._keepalive_thread is None


@pytest.mark.parametrize(
    "raw, seconds",
    [("00:30:00", 1800), ("01:05:08", 3908), ("PT15M", 900), ("120", 120), (None, None),
     ("soon", None)],
)
def test_parse_interval(raw, seconds):
    assert parse_interval(raw) == seconds


def test_logout_posts_and_clears_state(client, api):
    api.add(responses.POST, f"{API}/logout")
    client.logout()
    assert api.calls[-1].request.url == f"{API}/logout"
    assert client.is_authenticated is False
    assert "Authorization" not in client._session.headers


def test_logout_survives_server_rejection(client, api):
    api.add(responses.POST, f"{API}/logout", status=500)
    client.logout()
    assert client.is_authenticated is False


def test_context_manager_logs_in_and_out(api):
    api.add(responses.POST, f"{API}/login", json=fixture("login_success"))
    api.add(responses.POST, f"{API}/logout")
    with make_wrapper() as wrapper:
        assert wrapper.is_authenticated
    assert not wrapper.is_authenticated


def test_repr_hides_password_and_token(client):
    text = repr(client)
    assert PASSWORD not in text and TOKEN not in text


def test_credentials_never_reach_the_logs(api, caplog):
    """Password and session token must not appear at any log level."""
    caplog.set_level(logging.DEBUG, logger="dxtrade_wrapper")
    api.add(responses.POST, f"{API}/login", json=fixture("login_success"))
    api.add(responses.GET, f"{ACC}/positions", json={"errorCode": "1"}, status=401)
    api.add(responses.POST, f"{API}/login", json=fixture("error_bad_credentials"), status=401)
    wrapper = make_wrapper(account=ACCOUNT)
    wrapper.login()
    wrapper._build_websocket_url()
    with pytest.raises(AuthenticationError) as excinfo:
        wrapper.get_positions()

    for text in (caplog.text, str(excinfo.value)):
        assert PASSWORD not in text
        assert TOKEN not in text


def test_library_does_not_configure_logging():
    """The old code attached a StreamHandler and forced INFO on import/construct."""
    make_wrapper()
    handlers = logging.getLogger("dxtrade_wrapper").handlers
    assert all(isinstance(h, logging.NullHandler) for h in handlers)


def test_rate_limited_read_is_retried_once_after_retry_after(client, api, monkeypatch):
    sleeps = []
    monkeypatch.setattr("dxtrade_wrapper.client.time.sleep", sleeps.append)
    api.add(responses.GET, f"{ACC}/positions", status=429, headers={"Retry-After": "2"})
    api.add(responses.GET, f"{ACC}/positions", json=fixture("positions"))
    assert len(client.get_positions()) == 1
    assert sleeps == [2.0]


def test_long_retry_after_is_not_waited_out(client, api, monkeypatch):
    monkeypatch.setattr("dxtrade_wrapper.client.time.sleep", lambda s: pytest.fail("slept"))
    api.add(responses.GET, f"{ACC}/positions", status=429, headers={"Retry-After": "60"})
    with pytest.raises(RateLimitError) as excinfo:
        client.get_positions()
    assert excinfo.value.retry_after == 60.0


def test_rate_limited_order_is_never_retried_automatically(client, api, monkeypatch):
    from dxtrade_wrapper import OrderPlacementError

    monkeypatch.setattr("dxtrade_wrapper.client.time.sleep", lambda s: pytest.fail("slept"))
    api.add(responses.POST, f"{ACC}/orders", status=429, headers={"Retry-After": "1"})
    with pytest.raises(OrderPlacementError) as excinfo:
        client.place_order("EUR/USD", "BUY", 1000, "MARKET")
    assert excinfo.value.status_code == 429
    assert len(api.calls) == 2


def test_concurrent_401s_on_one_stale_token_log_in_once(client, api):
    """A thread whose request failed on a token another thread already replaced
    must not log in again (that could invalidate the fresh session)."""
    stale = client._auth_token
    client._auth_token = "ALREADY-RENEWED"
    client._relogin(stale_token=stale)
    assert [c.request.url for c in api.calls].count(f"{API}/login") == 1  # the fixture's


def test_relogin_keeps_the_client_authenticated_while_logging_in(client, api):
    seen = []
    original = client.login

    def spy():
        seen.append(client.is_authenticated)
        original()

    client.login = spy
    api.add(responses.POST, f"{API}/login", json={"sessionToken": "NEW"})
    client._relogin(stale_token=client._auth_token)
    assert seen == [True] and client._auth_token == "NEW"
