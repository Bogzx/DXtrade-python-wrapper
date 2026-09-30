"""End-to-end Push API test over a real local websocket (a fake DXtrade server).

Exercises the channel threads, PingRequest/Ping, subscription and reconnect with
subscription replay over actual sockets. Needs the `websockets` package.
"""

import asyncio
import json
import queue
import threading
import time

import pytest

from conftest import ACCOUNT, TOKEN

websockets = pytest.importorskip("websockets")
from websockets.asyncio.server import serve  # noqa: E402


class FakePushServer:
    """Speaks just enough of the Push API: PingRequest on connect, echoes quotes."""

    def __init__(self):
        self.received: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self.connections = 0
        self.history = []
        self.port = None
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._server = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    async def _handler(self, ws):
        self.connections += 1
        path = ws.request.path
        await ws.send(json.dumps({"type": "PingRequest", "session": TOKEN,
                                  "timestamp": "2026-09-30T10:00:00Z"}))
        async for raw in ws:
            msg = json.loads(raw)
            self.received.put((path, msg))
            if msg["type"] == "MarketDataSubscriptionRequest":
                await ws.send(json.dumps({
                    "type": "MarketData", "inReplyTo": msg["requestId"], "session": TOKEN,
                    "payload": {"events": [{"symbol": msg["payload"]["symbols"][0],
                                            "type": "Quote", "bid": 1.1, "ask": 1.1002}]}}))

    def _run(self):
        asyncio.set_event_loop(self._loop)

        async def main():
            self._server = await serve(self._handler, "127.0.0.1", 0)
            self.port = self._server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._server.serve_forever()

        try:
            self._loop.run_until_complete(main())
        except asyncio.CancelledError:
            pass

    def start(self):
        self._thread.start()
        self._ready.wait(5)
        return self

    def drop_all(self):
        """Aborts every client TCP connection, as a network blip would."""
        def abort():
            for conn in list(self._server.connections):
                conn.transport.abort()
        self._loop.call_soon_threadsafe(abort)

    def stop(self):
        self._loop.call_soon_threadsafe(self._server.close)

    def wait_for(self, predicate, timeout=5.0, after=0):
        """Returns the first message (index >= after) matching predicate, with its index."""
        deadline = time.monotonic() + timeout
        while True:
            while True:
                try:
                    self.history.append(self.received.get_nowait())
                except queue.Empty:
                    break
            for index, item in enumerate(self.history[after:], start=after):
                if predicate(item):
                    return index, item
            if time.monotonic() > deadline:
                raise AssertionError(f"not received; saw {self.history}")
            time.sleep(0.05)


@pytest.fixture
def server():
    srv = FakePushServer().start()
    yield srv
    srv.stop()


def test_push_session_over_a_real_socket(client, server, monkeypatch):
    monkeypatch.setattr(client, "_websocket_url", f"ws://127.0.0.1:{server.port}/push")
    client.connect_websocket()
    try:
        assert client._ws_connected

        # The server's PingRequest is answered on both channels.
        server.wait_for(lambda i: i[1]["type"] == "Ping" and i[0].startswith("/push?"))
        server.wait_for(lambda i: i[1]["type"] == "Ping" and i[0].startswith("/push/md"))

        client.subscribe_market_data(["EUR/USD"])
        index, (path, sub) = server.wait_for(
            lambda i: i[1]["type"] == "MarketDataSubscriptionRequest")
        assert path.startswith("/push/md?format=JSON")
        assert sub["session"] == TOKEN and sub["payload"]["account"] == ACCOUNT
        quote = client.price_update_queue.get(timeout=5)
        assert quote["instrument"] == "EUR/USD" and quote["bid"] == 1.1

        # After a drop the channel reconnects and replays the subscription.
        server.drop_all()
        _, (_, replay) = server.wait_for(
            lambda i: i[1]["type"] == "MarketDataSubscriptionRequest", timeout=8,
            after=index + 1)
        assert replay["requestId"] != sub["requestId"]
        assert server.connections >= 4
    finally:
        client.disconnect_websocket()
