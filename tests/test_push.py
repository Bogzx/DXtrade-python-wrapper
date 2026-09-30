"""Push API (websocket) per the DXtrade Push spec, driven with fakes: no sockets."""

import json

import pytest
import responses

from conftest import ACCOUNT, API, TOKEN, fixture, make_wrapper
from dxtrade_wrapper import WebSocketError
from dxtrade_wrapper.client import _PushChannel


class FakeWS:
    def __init__(self):
        self.sent = []

    def send(self, data):
        self.sent.append(json.loads(data))

    def close(self):
        pass


def attach_channels(client):
    """Registers connected channels backed by FakeWS, as connect_websocket() would."""
    channels = {}
    for name, md in (("events", False), ("md", True)):
        chan = _PushChannel(client, name, client._build_websocket_url(market_data=md))
        chan._ws = FakeWS()
        chan.connected = True
        channels[name] = chan
    client._channels = channels
    return channels


def test_urls_follow_the_spec_and_never_carry_the_token(client):
    events = client._build_websocket_url()
    md = client._build_websocket_url(market_data=True)
    assert events == "wss://dxtrade.example-broker.com/websocket?format=JSON"
    assert md == "wss://dxtrade.example-broker.com/websocket/md?format=JSON"
    assert TOKEN not in events + md
    assert all(TOKEN not in h for h in client._build_websocket_headers())


def test_explicit_websocket_url_wins():
    wrapper = make_wrapper(websocket_url="wss://push.broker.test/dxsca-web/push?x=1")
    assert wrapper._build_websocket_url() == "wss://push.broker.test/dxsca-web/push?format=JSON"
    assert wrapper._build_websocket_url(True) == (
        "wss://push.broker.test/dxsca-web/push/md?format=JSON")


def test_subscribing_before_connect_raises(client):
    with pytest.raises(WebSocketError):
        client.subscribe_market_data(["EUR/USD"])
    with pytest.raises(WebSocketError):
        client.subscribe_account_updates()


def test_market_data_subscription_envelope(client):
    channels = attach_channels(client)
    client.subscribe_market_data(["EUR/USD", "EUR/USD"])

    [msg] = channels["md"]._ws.sent
    assert msg["type"] == "MarketDataSubscriptionRequest"
    assert msg["session"] == TOKEN
    assert msg["requestId"] and msg["timestamp"].endswith("Z")
    assert msg["payload"] == {"symbols": ["EUR/USD"], "account": ACCOUNT,
                              "eventTypes": [{"type": "Quote", "format": "COMPACT"}]}
    assert channels["events"]._ws.sent == []


def test_unsubscribe_references_the_original_request(client):
    channels = attach_channels(client)
    client.subscribe_market_data(["EUR/USD"])
    client.unsubscribe_market_data(["EUR/USD"])
    sub, close = channels["md"]._ws.sent
    assert close["type"] == "MarketDataCloseSubscriptionRequest"
    assert close["refRequestId"] == sub["requestId"]


def test_account_subscriptions_use_portfolio_and_metrics_requests(client):
    channels = attach_channels(client)
    client.subscribe_account_updates()
    client.subscribe_account_updates()  # idempotent
    types = [m["type"] for m in channels["events"]._ws.sent]
    assert types == ["AccountPortfoliosSubscriptionRequest", "AccountMetricsSubscriptionRequest"]
    assert channels["events"]._ws.sent[0]["payload"] == {"requestType": "LIST",
                                                        "accounts": [ACCOUNT]}


def test_server_ping_request_gets_a_ping_reply(client):
    channels = attach_channels(client)
    client._handle_push_message(channels["events"], json.dumps(
        {"type": "PingRequest", "session": TOKEN, "timestamp": "2026-09-30T10:00:00Z"}))
    [reply] = channels["events"]._ws.sent
    assert reply["type"] == "Ping" and reply["session"] == TOKEN


def test_quotes_land_on_the_price_queue(client):
    channels = attach_channels(client)
    client._handle_push_message(channels["md"], json.dumps({
        "type": "MarketData", "inReplyTo": "1", "session": TOKEN,
        "payload": {"events": [{"symbol": "EUR/USD", "type": "Quote", "ask": 1.0555,
                                "bid": 1.0550, "time": "2026-09-30T10:00:00Z"}]}}))
    update = client.price_update_queue.get_nowait()
    assert update["instrument"] == "EUR/USD"
    assert update["spread"] == pytest.approx(0.0005)


def test_portfolio_update_feeds_order_and_account_queues(client):
    channels = attach_channels(client)
    orders = fixture("orders")["orders"]
    client._handle_push_message(channels["events"], json.dumps({
        "type": "AccountPortfolios", "payload": {"portfolios": [
            {"account": ACCOUNT, "version": 43, "balances": [], "positions": [],
             "orders": orders}]}}))
    assert client.account_update_queue.get_nowait()["type"] == "portfolio"
    first = client.order_update_queue.get_nowait()
    assert first["type"] == "order_update" and first["order_id"] == "3a4-ABC"
    assert client.order_update_queue.qsize() == 1


def test_metrics_update_feeds_account_queue(client):
    channels = attach_channels(client)
    client._handle_push_message(channels["events"], json.dumps({
        "type": "AccountMetrics", "payload": fixture("metrics")}))
    update = client.account_update_queue.get_nowait()
    assert update["type"] == "account_update" and update["equity"] == 100250.0


def test_reconnect_replays_subscriptions_with_fresh_request_ids(client):
    channels = attach_channels(client)
    client.subscribe_market_data(["EUR/USD"])
    first = channels["md"]._ws.sent[0]["requestId"]

    channels["md"]._ws = FakeWS()  # a new socket after a drop
    channels["md"]._on_open()

    [replayed] = channels["md"]._ws.sent
    assert replayed["type"] == "MarketDataSubscriptionRequest"
    assert replayed["requestId"] != first
    assert channels["md"].request_ids["md:EUR/USD"] == replayed["requestId"]


def test_expired_session_reject_triggers_relogin_and_resubscribe(client, api):
    channels = attach_channels(client)
    client.subscribe_account_updates()
    api.add(responses.POST, f"{API}/login", json={"sessionToken": "NEW-TOKEN"})

    client._handle_push_message(channels["events"], json.dumps({
        "type": "Reject", "inReplyTo": "x",
        "payload": {"errorCode": "1", "description": "Authorization required"}}))

    resent = channels["events"]._ws.sent[2:]
    assert [m["type"] for m in resent] == ["AccountPortfoliosSubscriptionRequest",
                                          "AccountMetricsSubscriptionRequest"]
    assert all(m["session"] == "NEW-TOKEN" for m in resent)


def test_garbage_messages_are_ignored(client):
    channels = attach_channels(client)
    client._handle_push_message(channels["events"], "not json")
    client._handle_push_message(channels["events"], json.dumps({"type": "Mystery"}))
    assert client.account_update_queue.empty()


def test_disconnect_clears_subscriptions(client):
    attach_channels(client)
    client.subscribe_market_data(["EUR/USD"])
    client.disconnect_websocket()
    assert client._subscriptions == {} and client._channels == {}
    assert not client._subscribed_instruments
