"""Typed results and parsers for DXtrade REST payloads.

Field mappings follow the public DXtrade REST API specification
(https://demo.dx.trade/developers/#/DXtrade-REST-API) and its OpenAPI document
(https://demo.dx.trade/dxsca-web/swagger/openapi.json). They have not been
checked against a live broker session.

Parsers are strict: a payload that does not have the documented shape raises
:class:`~dxtrade_wrapper.exceptions.DXTradeAPIError` instead of quietly
returning zeros, because a trader acting on a fake zero balance is worse off
than one who sees an error.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Union

from .exceptions import DXTradeAPIError

JSON = Dict[str, Any]


@dataclass
class Balance:
    """Account balance and margin figures (from ``/accounts/{account}/metrics``)."""

    equity: float
    balance: float
    margin: float
    free_margin: float
    #: equity / margin * 100, computed client-side; None when margin is zero.
    margin_level: Optional[float] = None
    currency: Optional[str] = None
    available_funds: Optional[float] = None
    open_pl: Optional[float] = None
    total_pl: Optional[float] = None
    account: Optional[str] = None


@dataclass
class Position:
    """An open position (``Position`` in the REST spec)."""

    position_id: str
    instrument: str
    quantity: float
    side: str  # 'BUY' or 'SELL'
    entry_price: float
    current_price: Optional[float] = None
    pnl: Optional[float] = None
    open_time: Optional[str] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    account: Optional[str] = None


@dataclass
class Order:
    """A working order (``Order`` in the REST spec).

    ``order_id`` is the server's order code, which ``cancel_order`` and
    ``modify_order`` accept. ``client_order_id`` is the ``orderCode`` the client
    sent when placing it; those methods accept it too.
    """

    order_id: str
    instrument: str
    quantity: float
    side: str  # 'BUY' or 'SELL'
    order_type: str  # 'MARKET', 'LIMIT', 'STOP'
    status: str  # 'ACCEPTED', 'WORKING', 'COMPLETED', 'CANCELED', 'EXPIRED', 'REJECTED'
    price: Optional[float] = None  # limit or stop price
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    created_time: Optional[str] = None
    position_effect: Optional[str] = None  # 'OPEN' or 'CLOSE'
    tif: Optional[str] = None
    position_code: Optional[str] = None
    client_order_id: Optional[str] = None
    server_order_id: Optional[int] = None
    final_status: Optional[bool] = None
    account: Optional[str] = None


def account_code(value: Union[str, Mapping[str, Any], None]) -> Optional[str]:
    """Normalises an account code to the documented ``clearing:account`` string.

    The prose spec says account codes are strings; the OpenAPI schema models
    them as ``{"clearing": ..., "account": ...}``. Both are accepted.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping) and "account" in value:
        clearing = value.get("clearing")
        return f"{clearing}:{value['account']}" if clearing else str(value["account"])
    raise DXTradeAPIError(f"Unrecognised account code: {value!r}")


def _opt_num(value: Any, field: str) -> Optional[float]:
    """DXtrade numbers may arrive as JSON numbers or as strings ("1.5", "+INF")."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise DXTradeAPIError(f"Field {field!r} is not a number: {value!r}") from None


def _num(value: Any, field: str) -> float:
    number = _opt_num(value, field)
    if number is None:
        raise DXTradeAPIError(f"Missing numeric field {field!r} in response")
    return number


def _bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).lower() == "true"


def unwrap_list(data: Any, key: str) -> List[JSON]:
    """Returns ``data[key]`` for a ``{"key": [...]}`` envelope.

    Every DXtrade list endpoint wraps its items (``{"positions": [...]}``,
    ``{"orders": [...]}``...). A bare list is tolerated as well.
    """
    if isinstance(data, list):
        items = data
    elif isinstance(data, Mapping) and isinstance(data.get(key, []), list):
        items = data.get(key, [])
    else:
        raise DXTradeAPIError(f"Expected a {{{key!r}: [...]}} object, got {type(data).__name__}")
    for item in items:
        if not isinstance(item, Mapping):
            raise DXTradeAPIError(f"Expected objects in {key!r}, got {type(item).__name__}")
    return list(items)


def parse_balance(metrics: Mapping[str, Any], currency: Optional[str] = None) -> Balance:
    """Builds a Balance from one ``AccountMetrics`` object."""
    equity = _num(metrics.get("equity"), "equity")
    margin = _num(metrics.get("margin"), "margin")
    # The prose spec spells these openPl/totalPl, the OpenAPI schema openPL/totalPL.
    open_pl = metrics.get("openPL", metrics.get("openPl"))
    total_pl = metrics.get("totalPL", metrics.get("totalPl"))
    return Balance(
        equity=equity,
        balance=_num(metrics.get("balance"), "balance"),
        margin=margin,
        free_margin=_num(metrics.get("marginFree"), "marginFree"),
        margin_level=(equity / margin * 100.0) if margin else None,
        currency=currency,
        available_funds=_opt_num(metrics.get("availableFunds"), "availableFunds"),
        open_pl=_opt_num(open_pl, "openPL"),
        total_pl=_opt_num(total_pl, "totalPL"),
        account=account_code(metrics.get("account")),
    )


def parse_position(data: Mapping[str, Any]) -> Position:
    try:
        return Position(
            position_id=str(data["positionCode"]),
            instrument=str(data["symbol"]),
            quantity=_num(data.get("quantity"), "quantity"),
            side=str(data["side"]),
            entry_price=_num(data.get("openPrice"), "openPrice"),
            open_time=data.get("openTime"),
            stop_loss=_opt_num(data.get("stopLossPrice"), "stopLossPrice"),
            take_profit=_opt_num(data.get("takeProfitPrice"), "takeProfitPrice"),
            account=account_code(data.get("account")),
        )
    except KeyError as exc:
        raise DXTradeAPIError(f"Position is missing field {exc.args[0]!r}") from None


def order_leg(data: Mapping[str, Any]) -> Mapping[str, Any]:
    """The single leg of an order (the spec says ``legs`` has exactly one)."""
    legs = data.get("legs") or [{}]
    return legs[0] if isinstance(legs[0], Mapping) else {}


def parse_order(data: Mapping[str, Any]) -> Order:
    leg = order_leg(data)
    try:
        return Order(
            order_id=str(data["orderCode"]),
            instrument=str(data.get("instrument") or leg.get("instrument", "")),
            quantity=_num(leg.get("quantity", 0), "legs[0].quantity"),
            side=str(data.get("side") or leg.get("side", "")),
            order_type=str(data["type"]),
            status=str(data["status"]),
            price=_opt_num(leg.get("price"), "legs[0].price"),
            created_time=data.get("issueTime"),
            position_effect=leg.get("positionEffect"),
            tif=data.get("tif"),
            position_code=leg.get("positionCode"),
            client_order_id=data.get("clientOrderId"),
            server_order_id=data.get("orderId"),
            final_status=_bool(data.get("finalStatus")),
            account=account_code(data.get("account")),
        )
    except KeyError as exc:
        raise DXTradeAPIError(f"Order is missing field {exc.args[0]!r}") from None
