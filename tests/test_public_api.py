"""The public surface that a first PyPI release locks in."""

import builtins
import socket

import responses

import dxtrade_wrapper
from conftest import API, fixture, make_wrapper


def test_star_import_does_not_shadow_builtins():
    namespace = {}
    exec("from dxtrade_wrapper import *", namespace)
    shadowed = [name for name in namespace if name != "__builtins__" and hasattr(builtins, name)]
    assert shadowed == []


def test_builtin_connection_error_still_catches_socket_errors_after_star_import():
    namespace = {}
    exec(
        "from dxtrade_wrapper import *\n"
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('127.0.0.1', 1), timeout=1)\n"
        "    caught = None\n"
        "except ConnectionError as exc:\n"
        "    caught = exc\n",
        namespace,
    )
    assert isinstance(namespace["caught"], (ConnectionRefusedError, socket.timeout, OSError))


def test_old_names_still_import():
    from dxtrade_wrapper import ConnectionError as OldConnectionError
    from dxtrade_wrapper import DXTradeDashboardWrapper

    assert DXTradeDashboardWrapper is dxtrade_wrapper.DXTradeClient
    assert OldConnectionError is dxtrade_wrapper.DXTradeConnectionError
    assert issubclass(OldConnectionError, dxtrade_wrapper.DXTradeWrapperError)


def test_every_exported_name_exists():
    for name in dxtrade_wrapper.__all__:
        assert hasattr(dxtrade_wrapper, name), name
    assert "ConnectionError" not in dxtrade_wrapper.__all__


def test_repr_uses_the_new_name_and_hides_secrets():
    client = make_wrapper()
    text = repr(client)
    assert text.startswith("DXTradeClient(")
    assert "s3cret" not in text


def test_logout_closes_the_http_session(api):
    api.add(responses.POST, f"{API}/login", json=fixture("login_success"))
    api.add(responses.POST, f"{API}/logout", json={})
    client = make_wrapper()
    client.login()
    old_session = client._session
    closed = []
    old_session.close = lambda: closed.append(True)

    client.logout()

    assert closed == [True]
    assert client._session is not old_session
