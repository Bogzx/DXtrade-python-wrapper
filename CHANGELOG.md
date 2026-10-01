# Changelog

## 0.2.0 (2026-10-01)

Rebuilt against the public DXtrade REST/Push specification
(https://demo.dx.trade/developers/, OpenAPI at /dxsca-web/swagger/openapi.json).
Spec-conformant; not yet verified against a live broker. First release prepared
for PyPI.

### Changed before the first PyPI release
- The client class is now `DXTradeClient`; `DXTradeDashboardWrapper` remains as an
  alias.
- The network-error class is now `DXTradeConnectionError`. The old name
  `ConnectionError` still imports, but is no longer in `__all__`: a star import used to
  replace the builtin `ConnectionError` in the caller's module, so `except
  ConnectionError` stopped catching socket errors.
- `logout()` closes the HTTP session's connection pool before starting a new one.
- The sdist now ships `tests/conftest.py`, the JSON fixtures, the examples and this
  changelog, so its test suite runs.
- `client.py` (1,430 lines) is split into layers that build on each other:
  `_session.py` (HTTP, login, keepalive), `_account.py` (read-only data),
  `_orders.py` and `_push.py`. `DXTradeClient`, `parse_interval` and `_PushChannel`
  still import from `dxtrade_wrapper.client`; behaviour is unchanged.

### Fixed in independent review
- `modify_order` on the entry (or a pending SL/TP) of an IF-THEN group sent a single
  order, which per the spec turns the group into a single order and drops its stop
  loss and take profit. Group members are now modified as the whole group; OCO members
  and groups with a missing child are refused with nothing sent.
- Modify requests carried `positionCode` on OPEN orders (the order list includes it;
  the spec says it must be omitted unless CLOSE).
- A 412 retry resent the body built from the stale order; it is now rebuilt from the
  re-read order.
- A 5xx on order placement, or a group acknowledged with fewer order responses than
  orders sent, is now `ambiguous=True`.
- Two threads hitting 401 on the same expired token no longer both log in.
- Build requirement raised to setuptools>=77 (needed for the SPDX `license` string).

### Fixed (each would have failed at any broker)
- Stop loss / take profit were never placed: they waited for a `positionCode` the
  order response does not contain. Now one atomic IF-THEN order group.
- `get_balance()` read `/portfolio` for equity/margin (they are in `/metrics`) and
  returned all-zero balances on any parse error. Now `/metrics`, and errors raise.
- Position/order parsers read invented fields (`id`, `entryPrice`, `stopLoss`...)
  from a bare list; responses are `{"positions": [...]}` etc. with `positionCode`,
  `symbol`, `openPrice`, `stopLossPrice`, `legs[0].price`...
- Order history used `/accounts/{a}/history`; the endpoint is `/orders/history`.
- `modify_order` sent `PATCH /orders/{id}` with `price`/`qty`; the spec requires
  `PUT /accounts/{a}/orders` with the full order and `If-Match`.
- `cancel_order` sent no `If-Match` (403 "Conditional request required").
- `close_position` / `modify_position_sl_tp` called `/positions/{id}`, which does not
  exist; closing is a linked CLOSE order, protections are linked STOP/LIMIT orders.
- `ping()` used GET; the spec says POST. A renewed `sessionToken` is now adopted.
- Push API: replaced invented `subscribe` messages, Atmosphere headers and
  token-in-URL (logged at INFO) with the spec envelope, `PingRequest`→`Ping`
  replies, a separate `/md` market-data channel, and reconnects that actually
  replay subscriptions.

### Added
- Typed exceptions with `status_code`, `error_code`, `description`
  (`NotFoundError`, `ConflictError`, `PreconditionFailedError`, `RateLimitError`,
  `ServerError`); `OrderPlacementError.ambiguous` / `.order_code`.
- One automatic re-login on 401; keepalive capped by the server's session timeout.
- Account discovery via `GET /users/{login@domain}`; `get_accounts()`,
  `get_account_metrics()`, `get_portfolio()`, `close_all()` (bulk close).
- `get_positions(include_pnl=True)` (floating P/L from per-position metrics);
  one automatic retry of rate-limited (429) GETs.
- Context manager, `DXTradeClient` alias, `pyproject.toml`, `py.typed`.
- Conformance tests against the official OpenAPI document; Push tests over a real
  local websocket; offline demo.

### Changed
- Module moved into the `dxtrade_wrapper` package (imports unchanged).
- The library no longer attaches log handlers or sets log levels.
- `Order.order_id` is the server `orderCode`; the client id is `client_order_id`.

## 0.1.0 (2025-04)
- Initial reverse-engineered prototype (never completed a call).
