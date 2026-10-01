"""Placing, modifying, cancelling and closing orders, including IF-THEN SL/TP groups."""

import uuid
from typing import Any, Callable, List, Mapping, Optional, Tuple

import requests

from .exceptions import (
    AuthenticationError,
    DXTradeAPIError,
    DXTradeConnectionError,
    NotFoundError,
    OrderPlacementError,
    PreconditionFailedError,
    ServerError,
)
from .models import (
    Position,
    account_code,
    order_leg,
)
from ._state import JSON
from ._account import _AccountLayer

_ORDER_TYPES = ("MARKET", "LIMIT", "STOP")
_SIDES = ("BUY", "SELL")
_TIFS = ("GTC", "DAY", "GTD")


def _opposite(side: str) -> str:
    return "SELL" if side.upper() == "BUY" else "BUY"



class _OrdersLayer(_AccountLayer):
    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def _generate_order_code(self) -> str:
        """Client order id: unique per account, <= 64 chars of the allowed set."""
        return f"dxpw-{uuid.uuid4().hex[:24]}"

    def _post_order(self, body: JSON, order_code: str, context: str) -> Any:
        """POSTs an order or order group, raising OrderPlacementError on failure."""
        path = self._account_path("orders")
        self._logger.debug("%s: %s", context, body)
        try:
            response = self._request("POST", path, context, json_body=body)
        except DXTradeConnectionError as exc:
            cause = exc.__cause__
            sent = not isinstance(cause, requests.exceptions.ConnectTimeout)
            raise OrderPlacementError(
                f"{context}: {exc}. " + (
                    f"The order may have been placed: check for orderCode {order_code!r} "
                    "before retrying, or resend with the same orderCode."
                    if sent else "The request was not sent."
                ),
                order_code=order_code, ambiguous=sent,
            ) from exc
        except DXTradeAPIError as exc:
            if isinstance(exc, AuthenticationError) and not self._is_authenticated:
                raise
            # A 5xx (often a gateway timeout) does not say whether the order reached
            # the matching engine, so the outcome is unknown, like a read timeout.
            unknown = isinstance(exc, ServerError)
            message = str(exc)
            if unknown:
                message += (f". The order may have been placed: check for orderCode "
                            f"{order_code!r} before retrying, or resend with the same orderCode.")
            raise OrderPlacementError(
                message, order_code=order_code, ambiguous=unknown, status_code=exc.status_code,
                error_code=exc.error_code, description=exc.description, body=exc.body,
            ) from exc
        return self._json(response, context)

    def place_order(
        self,
        instrument: str,
        side: str,
        quantity: float,
        order_type: str,
        price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        position_effect: str = "OPEN",
        tif: str = "GTC",
        **kwargs: Any,
    ) -> Any:
        """Places an order; with stop_loss / take_profit, atomically.

        Args:
            instrument: Symbol, e.g. "EUR/USD".
            side: "BUY" or "SELL".
            quantity: Units (not lots).
            order_type: "MARKET", "LIMIT" or "STOP".
            price: Limit price (LIMIT) or stop price (STOP).
            stop_loss, take_profit: Protection prices. They are sent in the same
                request as an IF-THEN order group, so there is no window where the
                entry is live and its protections have not been sent yet. If the
                request is rejected, none of it is placed. The protections only
                become active when the entry fills; the spec does not say how a
                protection rejected at that point (e.g. a stop already through the
                market) is reported, so check get_orders() after placing. If the
                server acknowledges fewer orders than were sent, this raises an
                ambiguous OrderPlacementError. The spec allows order groups only on
                position-based accounts; elsewhere the server rejects the request.
            position_effect: "OPEN" or "CLOSE" (CLOSE needs positionCode=...).
            tif: "GTC", "DAY" or "GTD" (GTD needs expireDate=...; MARKET only GTC).
            **kwargs: Extra SingleOrderRequest fields (positionCode, expireDate,
                metadata, ...). ``orderCode=`` supplies your own idempotency key.

        Returns:
            The server response: ``{"orderId", "updateOrderId"}`` for one order, or
            ``{"orderResponses": [...]}`` for an order group.

        Raises:
            ValueError: Invalid arguments (nothing is sent).
            OrderPlacementError: The server rejected it, or the outcome is unknown
                (see ``OrderPlacementError.ambiguous``).
        """
        self._check_authenticated()
        side, order_type = side.upper(), order_type.upper()
        position_effect, tif = position_effect.upper(), tif.upper()
        if side not in _SIDES:
            raise ValueError("Side must be 'BUY' or 'SELL'")
        if order_type not in _ORDER_TYPES:
            raise ValueError("Order type must be 'MARKET', 'LIMIT', or 'STOP'")
        if tif not in _TIFS:
            raise ValueError("tif must be 'GTC', 'DAY' or 'GTD'")
        if order_type == "MARKET" and tif != "GTC":
            raise ValueError("MARKET orders only support tif='GTC'")
        if tif == "GTD" and not kwargs.get("expireDate"):
            raise ValueError("tif='GTD' requires expireDate=...")
        if order_type in ("LIMIT", "STOP") and price is None:
            raise ValueError(f"Price is required for {order_type} orders")
        if position_effect not in ("OPEN", "CLOSE"):
            raise ValueError("position_effect must be 'OPEN' or 'CLOSE'")
        if position_effect == "CLOSE" and not kwargs.get("positionCode"):
            raise ValueError("position_effect='CLOSE' requires positionCode=...")
        protected = stop_loss is not None or take_profit is not None
        if protected and position_effect != "OPEN":
            raise ValueError(
                "stop_loss/take_profit can only be attached to an OPEN order; "
                "use modify_position_sl_tp() for an existing position"
            )
        if quantity is None or quantity <= 0:
            raise ValueError("quantity must be positive")

        account = self._get_account_id()
        order_code = kwargs.pop("orderCode", None) or self._generate_order_code()
        primary: JSON = {
            "account": account,
            "orderCode": order_code,
            "type": order_type,
            "instrument": instrument,
            "quantity": quantity,
            "positionEffect": position_effect,
            "side": side,
            "tif": tif,
        }
        if order_type == "LIMIT":
            primary["limitPrice"] = price
        elif order_type == "STOP":
            primary["stopPrice"] = price
        primary.update(kwargs)

        if not protected:
            return self._post_order(primary, order_code, "Place order")

        # IF-THEN group (REST spec, "Order Group Request"): THEN orders close the
        # position the IF order opens, have the opposite side, no quantity and GTC.
        group_orders = [primary]
        for kind, value in (("STOP", stop_loss), ("LIMIT", take_profit)):
            if value is None:
                continue
            leg: JSON = {
                "account": account,
                "orderCode": self._generate_order_code(),
                "type": kind,
                "instrument": instrument,
                "quantity": 0,
                "positionEffect": "CLOSE",
                "side": _opposite(side),
                "tif": "GTC",
                ("stopPrice" if kind == "STOP" else "limitPrice"): value,
            }
            group_orders.append(leg)
        body = {"orders": group_orders, "contingencyType": "IF-THEN"}
        result = self._post_order(body, order_code, "Place order with protection")
        responses = result.get("orderResponses") if isinstance(result, Mapping) else result
        if isinstance(responses, list) and len(responses) < len(group_orders):
            # The spec answers a group with one OrderResponse per order. Fewer means
            # a protection may be missing while the entry stands: say so loudly.
            raise OrderPlacementError(
                f"Place order with protection: sent {len(group_orders)} orders but the "
                f"server acknowledged {len(responses)}. The entry {order_code!r} may be "
                "working or filled WITHOUT its stop loss / take profit: check get_orders() "
                "and get_positions() now.",
                order_code=order_code, ambiguous=True, body=result,
            )
        return result

    def _find_open_order(self, orders: List[JSON], order_id: str) -> JSON:
        for order in orders:
            ids = {str(order.get("orderCode")), str(order.get("clientOrderId")),
                   str(order.get("orderId"))}
            if str(order_id) in ids:
                return order
        raise NotFoundError(f"No working order {order_id!r} on account {self._get_account_id()}")

    def _replace_request(
        self,
        order: Mapping[str, Any],
        price: Optional[float] = None,
        quantity: Optional[float] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> JSON:
        """A complete SingleOrderRequest for PUT (Modify Order).

        The spec requires the whole order to be resubmitted and the ``type`` to be
        omitted from replace requests.
        """
        leg = order_leg(order)
        body: JSON = {
            "account": account_code(order.get("account")) or self._get_account_id(),
            "orderCode": order.get("clientOrderId") or order.get("orderCode"),
            "instrument": order.get("instrument") or leg.get("instrument"),
            "positionEffect": leg.get("positionEffect"),
            "side": order.get("side") or leg.get("side"),
            "tif": order.get("tif"),
        }
        # The Open Orders listing gives OPEN legs a positionCode too (the order's
        # own id), but the spec says it "must be omitted" unless the effect is CLOSE.
        if leg.get("positionEffect") == "CLOSE" and leg.get("positionCode"):
            body["positionCode"] = leg["positionCode"]
        if order.get("expireDate"):
            body["expireDate"] = order["expireDate"]
        attached = leg.get("positionEffect") == "CLOSE" and leg.get("positionCode")
        if quantity is not None:
            body["quantity"] = quantity
        elif not attached:
            body["quantity"] = leg.get("quantity")
        order_type = str(order.get("type", "")).upper()
        current_price = leg.get("price")
        if order_type == "LIMIT":
            body["limitPrice"] = price if price is not None else current_price
        elif order_type == "STOP":
            body["stopPrice"] = price if price is not None else current_price
        elif price is not None:
            raise ValueError("A MARKET order has no price to modify")
        if extra:
            body.update(extra)
        return {k: v for k, v in body.items() if v is not None}

    def _conditional(
        self,
        method: str,
        path: str,
        context: str,
        etag: Optional[str],
        build: Optional[Callable[[List[JSON]], JSON]] = None,
        prefetched: Optional[Tuple[List[JSON], Optional[str]]] = None,
    ) -> Any:
        """PUT/DELETE with If-Match; on 412 re-reads the orders and retries once.

        ``build(orders)`` makes the request body from the current open orders. It
        runs again on the retry, so a 412 never resends a body built from the
        stale state the server just refused. ``etag`` is a caller-supplied value
        (never retried: the caller asked for that exact version).
        """
        auto = etag is None
        for attempt in (1, 2):
            tag, body = etag, None
            if auto or build is not None:
                orders, fetched = prefetched or self._open_orders_raw()
                prefetched = None
                tag = etag or fetched
                if build is not None:
                    body = build(orders)
            if not tag:
                raise DXTradeAPIError(
                    f"{context}: the server sent no ETag with the order list, and the spec "
                    "requires If-Match for this request. Pass etag=... explicitly."
                )
            try:
                response = self._request(method, path, context, json_body=body,
                                         headers={"If-Match": tag})
                return self._json(response, context)
            except PreconditionFailedError:
                if not auto or attempt == 2:
                    raise
                self._logger.info("%s: orders changed, re-reading and retrying", context)
        return None  # pragma: no cover

    def _find_linked(self, orders: List[JSON], link: Mapping[str, Any]) -> Optional[JSON]:
        wanted = {str(link.get("linkedOrder")), str(link.get("linkedClientOrderId"))} - {"None"}
        for order in orders:
            if {str(order.get("orderCode")), str(order.get("clientOrderId"))} & wanted:
                return order
        return None

    def _working_group(self, orders: List[JSON], order: Mapping[str, Any]) -> Optional[List[JSON]]:
        """The working IF-THEN group ``order`` belongs to, parent first, or None.

        Per the spec's Modify Order example 1, a single-order PUT for the parent of
        an IF-THEN group turns the group into a single order: its stop loss and take
        profit disappear. So group members are always modified as a whole group.
        """
        links = order.get("links") or []
        if any(link.get("linkType") == "OCO" for link in links):
            raise DXTradeAPIError(
                "Order is part of an OCO group; modifying OCO groups is not supported "
                "(a single-order PUT would break the group). Nothing was sent."
            )
        parent: Optional[Mapping[str, Any]] = order
        if not any(link.get("linkType") == "CHILD" for link in links):
            parent_link = next((lk for lk in links if lk.get("linkType") == "PARENT"), None)
            # A filled parent is no longer a working order: the children are then
            # plain protections on the position and are modified on their own.
            parent = self._find_linked(orders, parent_link) if parent_link else None
        if parent is None:
            return None
        group = [dict(parent)]
        for link in parent.get("links") or []:
            if link.get("linkType") != "CHILD":
                continue
            child = self._find_linked(orders, link)
            if child is None:
                raise DXTradeAPIError(
                    f"Order group of {parent.get('clientOrderId') or parent.get('orderCode')!r} "
                    f"lists child {link.get('linkedClientOrderId') or link.get('linkedOrder')!r}, "
                    "which is not a working order; refusing to send a group PUT that would "
                    "drop it. Nothing was sent."
                )
            group.append(child)
        return group

    def _modify_body(
        self,
        orders: List[JSON],
        order: Mapping[str, Any],
        price: Optional[float] = None,
        quantity: Optional[float] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> JSON:
        """The PUT body changing ``order``: a single order, or its whole IF-THEN group
        (REST spec, Modify Order example 2b) with only ``order`` changed."""
        group = self._working_group(orders, order)
        if group is None:
            return self._replace_request(order, price, quantity, extra)
        target = str(order.get("orderCode"))
        members = []
        for index, member in enumerate(group):
            if str(member.get("orderCode")) == target:
                body = self._replace_request(member, price, quantity, extra)
            else:
                body = self._replace_request(member)
            if index > 0:
                # THEN orders as in the spec's group examples: zero quantity, no positionCode.
                body.pop("positionCode", None)
                body["quantity"] = 0
            members.append(body)
        return {"orders": members, "contingencyType": "IF-THEN"}

    def modify_order(
        self,
        order_id: str,
        new_price: Optional[float] = None,
        new_quantity: Optional[float] = None,
        etag: Optional[str] = None,
        **kwargs: Any,
    ) -> Any:
        """Modifies a working order: PUT /accounts/{account}/orders with If-Match.

        The current order is read first, because the spec requires the complete
        order in a modify request; only the given fields change.

        Args:
            order_id: Server orderCode, client orderCode, or numeric orderId.
            new_price: New limit/stop price.
            new_quantity: New quantity in units.
            etag: If-Match value; read from GET orders when omitted.
            **kwargs: Other SingleOrderRequest fields to change (e.g. tif, expireDate).
        """
        if new_price is None and new_quantity is None and not kwargs:
            raise ValueError("At least one parameter to modify must be provided")
        prefetched = self._open_orders_raw()
        self._find_open_order(prefetched[0], order_id)  # fail fast on an unknown id

        def build(orders: List[JSON]) -> JSON:
            order = self._find_open_order(orders, order_id)
            return self._modify_body(orders, order, new_price, new_quantity, kwargs)

        return self._conditional("PUT", self._account_path("orders"), "Modify order",
                                 etag, build, prefetched=prefetched)

    def cancel_order(self, order_id: str, etag: Optional[str] = None) -> Any:
        """Cancels a working order: DELETE /accounts/{account}/orders/{code} with If-Match."""
        self._check_authenticated()
        path = self._account_path(f"orders/{self._seg(order_id)}")
        result = self._conditional("DELETE", path, "Cancel order", etag)
        return result if result is not None else {"success": True, "order_id": order_id}

    def _find_position(self, position_id: str) -> Position:
        for position in self.get_positions():
            if position.position_id == str(position_id):
                return position
        raise NotFoundError(f"No open position {position_id!r}")

    @staticmethod
    def _find_protection(orders: List[JSON], kind: str, position_code: str) -> Optional[JSON]:
        return next(
            (o for o in orders
             if str(o.get("type", "")).upper() == kind
             and order_leg(o).get("positionCode") == position_code
             and order_leg(o).get("positionEffect") == "CLOSE"),
            None,
        )

    def close_position(self, position_id: str, quantity: Optional[float] = None) -> Any:
        """Closes a position fully or partially with a MARKET order linked to it.

        DXtrade has no DELETE-position endpoint: closing is a MARKET order with
        positionEffect=CLOSE and the position's code, on the opposite side.
        """
        self._check_authenticated()
        position = self._find_position(position_id)
        qty = position.quantity if quantity is None else quantity
        if qty <= 0 or qty > position.quantity:
            raise ValueError(f"quantity must be in (0, {position.quantity}]")
        return self.place_order(
            instrument=position.instrument,
            side=_opposite(position.side),
            quantity=qty,
            order_type="MARKET",
            position_effect="CLOSE",
            positionCode=position.position_id,
        )

    def close_all(
        self,
        instrument: Optional[str] = None,
        close_positions: bool = True,
        cancel_orders: bool = True,
        comment: Optional[str] = None,
    ) -> Any:
        """Bulk close: POST /accounts/{account}/close.

        Flattens the account (or one instrument) in a single request, e.g. before
        a news event or a prop-firm cut-off.
        """
        self._check_authenticated()
        if not (close_positions or cancel_orders):
            raise ValueError("Set close_positions and/or cancel_orders")
        body: JSON = {"closePositions": close_positions, "cancelOrders": cancel_orders}
        if instrument:
            body["instrument"] = instrument
        if comment:
            body["comment"] = comment
        response = self._request("POST", self._account_path("close"), "Bulk close",
                                 json_body=body)
        return self._json(response, "Bulk close")

    def modify_position_sl_tp(
        self,
        position_id: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> JSON:
        """Sets or moves a position's stop loss and/or take profit.

        Per the spec, protections are CLOSE orders linked by positionCode: an
        existing STOP (SL) or LIMIT (TP) is modified via PUT, a missing one is
        placed via POST.

        Returns:
            ``{"stop_loss": response, "take_profit": response}`` for what changed.
        """
        self._check_authenticated()
        if stop_loss is None and take_profit is None:
            raise ValueError("At least one of stop_loss or take_profit must be provided")
        position = self._find_position(position_id)
        results: JSON = {}
        for key, kind, value in (("stop_loss", "STOP", stop_loss),
                                 ("take_profit", "LIMIT", take_profit)):
            if value is None:
                continue
            prefetched = self._open_orders_raw()
            existing = self._find_protection(prefetched[0], kind, position.position_id)
            if existing is not None:
                code = str(existing.get("orderCode"))

                def build(orders: List[JSON], code: str = code, value: float = value) -> JSON:
                    current = self._find_open_order(orders, code)
                    return self._modify_body(orders, current, price=value)

                results[key] = self._conditional(
                    "PUT", self._account_path("orders"), f"Modify {key}", None, build,
                    prefetched=prefetched,
                )
            else:
                order_code = self._generate_order_code()
                body = {
                    "account": self._get_account_id(),
                    "orderCode": order_code,
                    "type": kind,
                    "instrument": position.instrument,
                    "positionEffect": "CLOSE",
                    "positionCode": position.position_id,
                    "side": _opposite(position.side),
                    "tif": "GTC",
                    ("stopPrice" if kind == "STOP" else "limitPrice"): value,
                }
                results[key] = self._post_order(body, order_code, f"Place {key}")
        return results
