"""The Push API: websocket channels, subscriptions and incoming message routing."""

import json
import threading
from datetime import datetime, timezone
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import websocket

from .exceptions import (
    DXTradeAPIError,
    WebSocketError,
)
from .models import (
    parse_balance,
    parse_order,
)
from ._state import JSON
from ._orders import _OrdersLayer

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _PushLayer(_OrdersLayer):
    def logout(self) -> None:
        """Closes the Push channels, then ends the REST session."""
        self.disconnect_websocket()
        super().logout()

    # ------------------------------------------------------------------
    # Push API (WebSocket)
    # ------------------------------------------------------------------

    def _build_websocket_url(self, market_data: bool = False) -> str:
        """Push API URL. The session token is NOT in the URL: per the Push spec it
        travels in each message's ``session`` field (and URLs end up in logs)."""
        if self._websocket_url:
            base = self._websocket_url.split("?")[0].rstrip("/")
        else:
            scheme = "wss://" if self._base_url.startswith("https://") else "ws://"
            host = self._base_url.split("://", 1)[1]
            base = f"{scheme}{host}/{self._websocket_path}".rstrip("/")
        if market_data:
            base += "/md"
        return f"{base}?{urlencode({'format': 'JSON'})}"

    def _build_websocket_headers(self) -> List[str]:
        return [f"User-Agent: dxtrade-wrapper/{_version()}"]

    def _envelope(self, msg_type: str, payload: Optional[JSON] = None,
                  request_id: Optional[str] = None, **extra: Any) -> JSON:
        message: JSON = {
            "type": msg_type,
            "requestId": request_id or f"dxpw-{next(self._request_ids)}",
            "timestamp": _utc_now(),
            "session": self._auth_token,
        }
        if payload is not None:
            message["payload"] = payload
        message.update(extra)
        return message

    def connect_websocket(self) -> None:
        """Opens the Push API business-events and market-data websockets.

        Each channel reconnects with exponential backoff and replays its
        subscriptions after reconnecting.
        """
        self._check_authenticated()
        with self._lock:
            for name, md in (("events", False), ("md", True)):
                channel = self._channels.get(name)
                if channel is None or not channel.running:
                    channel = _PushChannel(self, name, self._build_websocket_url(market_data=md))
                    self._channels[name] = channel
                    channel.start()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not self._ws_connected:
            time.sleep(0.05)
        if not self._ws_connected:
            self._logger.warning("Push API connection not established yet; still retrying")

    @property
    def _ws_connected(self) -> bool:
        channel = self._channels.get("events")
        return bool(channel and channel.connected)

    def disconnect_websocket(self) -> None:
        with self._lock:
            channels, self._channels = list(self._channels.values()), {}
        for channel in channels:
            channel.stop()
        self._subscriptions.clear()
        self._subscribed_instruments = set()
        self._account_updates_subscribed = False

    def _subscribe(self, key: str, channel: str, msg_type: str, payload: JSON) -> None:
        self._subscriptions[key] = (channel, msg_type, payload)
        chan = self._channels.get(channel)
        if chan is None:
            raise WebSocketError("WebSocket not connected. Call connect_websocket() first.")
        if chan.connected:
            chan.send_subscription(key)

    def _unsubscribe(self, key: str, close_type: str) -> None:
        entry = self._subscriptions.pop(key, None)
        if entry is None:
            return
        chan = self._channels.get(entry[0])
        request_id = chan.request_ids.pop(key, None) if chan else None
        if chan and chan.connected and request_id:
            chan.send(self._envelope(close_type, refRequestId=request_id))

    def subscribe_market_data(self, instruments: List[str]) -> None:
        """Quotes for the given symbols (MarketDataSubscriptionRequest on /md).

        Each quote arrives on ``price_update_queue`` as
        ``{"type": "price_update", "instrument", "bid", "ask", "spread", "time"}``.
        """
        if not self._channels:
            raise WebSocketError("WebSocket not connected. Call connect_websocket() first.")
        for symbol in instruments:
            if symbol in self._subscribed_instruments:
                continue
            payload: JSON = {"symbols": [symbol],
                             "eventTypes": [{"type": "Quote", "format": "COMPACT"}]}
            if self._account_id:
                payload["account"] = self._account_id
            self._subscribe(f"md:{symbol}", "md", "MarketDataSubscriptionRequest", payload)
            self._subscribed_instruments.add(symbol)

    def unsubscribe_market_data(self, instruments: List[str]) -> None:
        if not self._channels:
            raise WebSocketError("WebSocket not connected. Call connect_websocket() first.")
        for symbol in instruments:
            if symbol in self._subscribed_instruments:
                self._unsubscribe(f"md:{symbol}", "MarketDataCloseSubscriptionRequest")
                self._subscribed_instruments.discard(symbol)

    def subscribe_account_updates(self) -> None:
        """Portfolio and metrics updates for the account.

        ``AccountPortfolios`` messages put each order on ``order_update_queue`` and
        the portfolio on ``account_update_queue``; ``AccountMetrics`` messages put
        equity/margin updates on ``account_update_queue``.
        """
        if not self._channels:
            raise WebSocketError("WebSocket not connected. Call connect_websocket() first.")
        if self._account_updates_subscribed:
            return
        accounts = [self._get_account_id()]
        self._subscribe("portfolio", "events", "AccountPortfoliosSubscriptionRequest",
                        {"requestType": "LIST", "accounts": accounts})
        self._subscribe("metrics", "events", "AccountMetricsSubscriptionRequest",
                        {"requestType": "LIST", "accounts": accounts, "includePositions": "true"})
        self._account_updates_subscribed = True

    def _handle_push_message(self, channel: "_PushChannel", raw: str) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            self._logger.error("Push API sent non-JSON data (%d bytes)", len(raw))
            return
        msg_type = message.get("type")
        payload = message.get("payload") or {}
        if msg_type == "PingRequest":
            # The server closes the session if it gets no Ping back.
            channel.send({"type": "Ping", "timestamp": _utc_now(), "session": self._auth_token})
        elif msg_type == "MarketData":
            for event in payload.get("events") or []:
                if event.get("type", "Quote") != "Quote":
                    continue
                update: JSON = {"type": "price_update", "instrument": event.get("symbol"),
                                "time": event.get("time")}
                for field in ("bid", "ask"):
                    if event.get(field) is not None:
                        update[field] = float(event[field])
                if "bid" in update and "ask" in update:
                    update["spread"] = update["ask"] - update["bid"]
                self.price_update_queue.put(update)
        elif msg_type == "AccountPortfolios":
            for portfolio in payload.get("portfolios") or []:
                self.account_update_queue.put({"type": "portfolio", **portfolio})
                for order in portfolio.get("orders") or []:
                    try:
                        parsed = parse_order(order).__dict__
                    except DXTradeAPIError:
                        parsed = {}
                    self.order_update_queue.put({"type": "order_update", **parsed, "raw": order})
        elif msg_type == "AccountMetrics":
            for metrics in payload.get("metrics") or []:
                try:
                    balance = parse_balance(metrics).__dict__
                except DXTradeAPIError:
                    balance = {}
                self.account_update_queue.put({"type": "account_update", **balance,
                                               "raw": metrics})
        elif msg_type == "Reject":
            self._logger.error(
                "Push API rejected request %s: error %s %s", message.get("inReplyTo"),
                payload.get("errorCode"), payload.get("description"),
            )
            if str(payload.get("errorCode")) == "1" and self._auto_relogin:
                # Session expired: log in again and resubscribe on the new token.
                try:
                    self._relogin()
                    channel.resubscribe()
                except Exception as exc:  # noqa: BLE001
                    self._logger.error("Re-login after Push reject failed: %s", exc)
        else:
            self._logger.debug("Unhandled Push API message type %s", msg_type)



class _PushChannel:
    """One Push API websocket with reconnect/backoff and subscription replay."""

    def __init__(self, client: "_PushLayer", name: str, url: str):
        self.client = client
        self.name = name
        self.url = url
        self.connected = False
        self.running = False
        self.request_ids: Dict[str, str] = {}
        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        self.running = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"DXTradePush-{self.name}",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        self._stop.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001, S110 - closing a dead socket
                pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self.connected = False

    def _run(self) -> None:
        failures = 0
        log = self.client._logger
        while not self._stop.is_set():
            opened = threading.Event()

            def on_open(ws: websocket.WebSocketApp, opened: threading.Event = opened) -> None:
                opened.set()
                self._on_open()

            self._ws = websocket.WebSocketApp(
                self.url,
                header=self.client._build_websocket_headers(),
                on_open=on_open,
                on_message=lambda ws, msg: self.client._handle_push_message(self, msg),
                on_error=lambda ws, err: log.warning("Push %s error: %s", self.name, err),
                on_close=lambda ws, code, msg: self._on_close(code, msg),
            )
            log.info("Connecting Push API %s channel: %s", self.name, self.url)
            try:
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as exc:  # noqa: BLE001 - keep reconnecting
                log.error("Push %s channel crashed: %s", self.name, exc)
            self.connected = False
            if self._stop.is_set():
                break
            failures = 0 if opened.is_set() else failures + 1
            delay = min(2 ** failures, 30)
            log.warning("Push %s channel closed; reconnecting in %ss", self.name, delay)
            self._stop.wait(delay)

    def _on_open(self) -> None:
        self.connected = True
        self.client._logger.info("Push API %s channel connected", self.name)
        self.resubscribe()

    def _on_close(self, code: Any, msg: Any) -> None:
        self.connected = False
        self.client._logger.info("Push %s channel closed (%s %s)", self.name, code, msg)

    def resubscribe(self) -> None:
        for key, (channel, _, _) in list(self.client._subscriptions.items()):
            if channel == self.name:
                self.send_subscription(key)

    def send_subscription(self, key: str) -> None:
        channel, msg_type, payload = self.client._subscriptions[key]
        message = self.client._envelope(msg_type, payload)
        self.request_ids[key] = message["requestId"]
        self.send(message)

    def send(self, message: JSON) -> None:
        if self._ws is None:
            raise WebSocketError(f"Push {self.name} channel is not open")
        try:
            self._ws.send(json.dumps(message))
        except Exception as exc:  # noqa: BLE001
            raise WebSocketError(f"Failed to send {message.get('type')}: {exc}") from exc



def _version() -> str:
    from . import __version__

    return __version__
