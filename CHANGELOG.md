# Changelog

## 0.2.0 (unreleased)

Rebuilt against the public DXtrade REST/Push specification
(https://demo.dx.trade/developers/, OpenAPI at /dxsca-web/swagger/openapi.json).
Spec-conformant; not yet verified against a live broker.

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
