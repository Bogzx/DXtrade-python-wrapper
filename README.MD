# 📊 DXtrade Python Wrapper

![Python](https://img.shields.io/badge/python-3.9+-blue.svg)
![Status](https://img.shields.io/badge/status-spec--conformant%20%7C%20not%20live--verified-orange)
[![CI](https://github.com/Bogzx/DXtrade-python-wrapper/actions/workflows/ci.yml/badge.svg)](https://github.com/Bogzx/DXtrade-python-wrapper/actions/workflows/ci.yml)
![License](https://img.shields.io/badge/license-MIT-green)

A typed Python client for the [DXtrade](https://dx.trade) broker platform's REST and
Push (WebSocket) APIs. It covers login and session upkeep, balance, positions, orders,
placing orders with stop loss / take profit, modify, cancel, close, and live quotes and
account updates. Prop-firm and retail traders use DXtrade.

> **Status: spec-conformant, not live-verified.** Every request follows Devexperts'
> public [DXtrade API documentation](https://demo.dx.trade/developers/), and the test
> suite checks the client against their published
> [OpenAPI document](https://demo.dx.trade/dxsca-web/swagger/openapi.json). It has
> **not** yet been run against a live broker. Try it on a **demo account** first. Brokers
> decide whether to enable REST access at all (FTMO turned it off in April 2024).

## 📥 Install

```bash
pip install dxtrade-python-wrapper                                  # PyPI, once 0.2.0 is published
pip install git+https://github.com/Bogzx/DXtrade-python-wrapper     # latest main
```

```python
import dxtrade_wrapper
```

Python 3.9+. Runtime dependencies: `requests` and `websocket-client`. Type hints ship with
the package (`py.typed`).

## ⚡ Quick start (no broker account needed)

```bash
git clone https://github.com/Bogzx/DXtrade-python-wrapper.git
cd DXtrade-python-wrapper
python -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"
python examples/offline_demo.py
```

The demo runs the client against a simulated DXtrade server: it logs in, reads the
balance, positions and orders, places an entry with SL/TP, modifies, cancels, and logs
out. Then it prints every HTTP request that would have gone to a real broker.

## 🔌 With a real (demo) account

```python
from dxtrade_wrapper import DXTradeClient, OrderPlacementError

with DXTradeClient(
    base_url="https://dxtrade.your-broker.com",
    username="your-login",
    password="...",
    domain_or_vendor="default",
    account="default:ACC12345",      # optional if you have exactly one account
) as dx:
    print(dx.get_balance())           # equity, balance, margin, free margin, P/L
    print(dx.get_positions())
    print(dx.get_orders())

    try:
        # Entry + stop loss + take profit in ONE request (an IF-THEN order group):
        # a rejected request places nothing. SL/TP activate when the entry fills.
        dx.place_order("EUR/USD", "BUY", 10_000, "MARKET", stop_loss=1.0800, take_profit=1.0950)
    except OrderPlacementError as exc:
        print(exc.status_code, exc.error_code, exc.description, exc.ambiguous)
```

Or put your settings in `.env` (see [`.env.example`](https://github.com/Bogzx/DXtrade-python-wrapper/blob/main/.env.example)),
`pip install -e ".[examples]"` and run [`python example.py`](https://github.com/Bogzx/DXtrade-python-wrapper/blob/main/example.py). It is
read-only unless you pass `--demo-order`.

## 📚 API

| Method | DXtrade endpoint |
|---|---|
| `login()` / `logout()` / `ping()` | `POST /login`, `/logout`, `/ping` (token auth, `Authorization: DXAPI <token>`) |
| `get_accounts()` | `GET /users/{login@domain}` |
| `get_balance()` → `Balance` | `GET /accounts/{account}/metrics` |
| `get_account_metrics()`, `get_portfolio()` | raw metrics / portfolio |
| `get_positions(include_pnl=False)` → `[Position]` | `GET /accounts/{account}/positions` (+ per-position `fpl` from metrics with `include_pnl=True`) |
| `get_orders()` → `[Order]` | `GET /accounts/{account}/orders` |
| `get_order_history(limit, **filters)` | `GET /accounts/{account}/orders/history` (e.g. `in_status="COMPLETED"`, `period="today"`) |
| `place_order(...)` | `POST /accounts/{account}/orders`, as a single order or an IF-THEN group when SL/TP are given |
| `modify_order(order_id, new_price, new_quantity)` | `PUT /accounts/{account}/orders` with `If-Match`; a member of a working IF-THEN group is sent as the whole group, so its SL/TP are kept |
| `cancel_order(order_id)` | `DELETE /accounts/{account}/orders/{code}` with `If-Match` |
| `close_position(position_id, quantity=None)` | MARKET order with `positionEffect=CLOSE` |
| `modify_position_sl_tp(position_id, stop_loss, take_profit)` | moves existing protection orders (PUT) or adds them (POST) |
| `close_all(instrument=None)` | `POST /accounts/{account}/close` (bulk close) |
| `connect_websocket()`, `subscribe_market_data([...])`, `subscribe_account_updates()` | Push API: quotes → `price_update_queue`, orders → `order_update_queue`, portfolio/metrics → `account_update_queue` |

**Sessions.** A background thread pings every `keepalive_interval` seconds (capped at
half the session timeout the server reports). If a request still gets `401` because
the session expired, the client logs in again once and retries. Retrying an order is
safe: it keeps its `orderCode`, and DXtrade rejects a duplicate code (409, error 100)
instead of opening a second position. A read (GET) that is rate-limited (429) waits out
`Retry-After` (up to 5s) and retries once. Orders are never retried automatically on 429.

**Errors.** Everything derives from `DXTradeWrapperError`. Server errors are
`DXTradeAPIError` subclasses carrying `status_code`, `error_code` and `description` from
DXtrade's error body: `AuthenticationError` (401, and a 403 at login, which means the
broker has not enabled REST access), `NotFoundError`, `ConflictError`
(409 business rejection), `PreconditionFailedError` (412), `RateLimitError` (429,
`retry_after`) and `ServerError`. `OrderPlacementError` adds `order_code` and
`ambiguous`. `ambiguous=True` means the outcome is unknown (a timeout after sending, a
5xx, or an order group the server acknowledged only in part), so check for that
`orderCode` before sending again. `DXTradeConnectionError` covers network failures. Any
other HTTP error, such as a 403 "Conditional request required", is a plain
`DXTradeAPIError` with its `status_code`.

**Security.** The password and session token are never logged, and the token is not
put in websocket URLs. The library does not configure logging; call
`logging.basicConfig()` in your app to see its logs.

**Push API URL.** The Push spec leaves the websocket address to each deployment. Pass
`websocket_url=` (ask your broker). Market data uses the same URL with `/md` appended,
as the spec describes.

## 🧪 Tests

```bash
pip install -e ".[test]"
python -m pytest                                  # offline, ~100 tests
DXTRADE_SPEC_TESTS=1 python -m pytest tests/test_spec_conformance.py -v
```

* The main suite mocks HTTP with [`responses`](https://github.com/getsentry/responses),
  using fixtures shaped like the spec's schemas, and runs the Push channels against a
  local websocket server.
* `test_spec_conformance.py` downloads the official OpenAPI document (public, read-only)
  and checks two things. First, every request the client sends uses a documented path,
  method, query parameter and body fields, and the body validates against the schema.
  Second, every fixture matches its response schema. It is opt-in because it needs
  network access. The spec is not vendored.

Known quirks in the OpenAPI document, which the conformance test tolerates:
`AccountCode` and the date/time types are modelled as objects while the prose spec and
its examples use strings, and order groups (IF-THEN/OCO) are documented in prose but
missing from the OpenAPI request body.

**What this does not prove:** that your broker's deployment behaves like the spec.
Recorded traffic from a real demo account, with credentials and tokens removed, is the
most valuable contribution anyone with access could make.

## 🗂 History: why this was broken for two years

The first version was reverse-engineered from community examples. It never completed a
call. FTMO's `403` (REST access disabled for clients in April 2024) looked like the only
problem and hid the others. A 2026 review found the client-side bugs: a missing
`/dxsca-web` prefix on every call after login, `Bearer` instead of `DXAPI`, and orders
missing `account`/`orderCode`. A later check against the public spec found more:
invented field names in every parser, stop loss / take profit silently never placed,
wrong endpoints and methods for history, modify, close and SL/TP, missing `If-Match`
headers, a Push protocol that did not exist, and a balance that turned any parse error
into zeros. [`CHANGELOG.md`](https://github.com/Bogzx/DXtrade-python-wrapper/blob/main/CHANGELOG.md) has the details.

Lessons that still apply:

1. **A correct diagnosis can still be an incomplete one.** The 403 was real, and it
   stopped anyone looking further.
2. **Check whether the docs exist before inferring.** The official spec had been public
   the whole time.
3. **Test at the boundary you control.** Validating outgoing requests against a schema
   needs no broker.
4. **Don't fabricate fallbacks.** An invented account code, zeros on parse failure, and
   SL/TP skipped with only a log line all turn a clear error into a silent wrong answer.

## 📦 Packaging and releases

The distribution is `dxtrade-python-wrapper`; the import name is `dxtrade_wrapper`. The
sdist includes the tests and fixtures, so `pytest` runs from an unpacked sdist. Releases
are built by [`release.yml`](https://github.com/Bogzx/DXtrade-python-wrapper/blob/main/.github/workflows/release.yml)
when a GitHub release is published. It checks that the tag matches `__version__` and has
a dated CHANGELOG entry, attaches the sdist and wheel to the release, and publishes them
with PyPI Trusted Publishing (TestPyPI first) once the repository variable
`PYPI_PUBLISH` is `true`.

`DXTradeDashboardWrapper` (the 0.1 class name) and `ConnectionError` still import, as
aliases of `DXTradeClient` and `DXTradeConnectionError`.

## ⚖️ Disclaimer

Not affiliated with Devexperts, DXtrade or any broker. Automated trading carries
significant financial risk. This client has not been verified against a live server:
use a demo account, and never point untested trading code at a funded account.
