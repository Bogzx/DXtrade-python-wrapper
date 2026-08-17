# 📊 DXTrade Python API Wrapper

![Python](https://img.shields.io/badge/python-3.7+-blue.svg)
![Status](https://img.shields.io/badge/status-not%20functional%20%7C%20untested%20prototype-red)
![Protocol](https://img.shields.io/badge/protocol-unverified-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)

> **Read this first:** this wrapper has never successfully completed a single call
> against a real DXtrade server. It is published as a **writeup of a failed
> reverse-engineering attempt**, not as a usable client. If you need a working
> DXtrade client, see [Use this instead](#-use-this-instead).

## 📋 Table of Contents

- [Project Status](#-project-status-not-functional--untested-prototype)
- [Use this instead](#-use-this-instead)
- [What this repo is actually good for](#-what-this-repo-is-actually-good-for)
- [What was learned](#-what-was-learned)
- [Introduction](#-introduction)
- [Features (as designed, none verified)](#-features-as-designed-none-verified)
- [Alternative Approaches](#-alternative-approaches)
- [Installation](#-installation)
- [Tests](#-tests)
- [Packaging](#-packaging)
- [Disclaimer](#-disclaimer)

## ⚠️ Project Status: Not Functional — Untested Prototype

This code has **demonstrably never been executed successfully against a live
DXtrade server.** An earlier version of this README said "Not Currently
Functional with FTMO," which implied the wrapper worked elsewhere and that FTMO
was the only obstacle. That was wrong, and it wasted readers' time. There are
**four** reasons this does not work, and **three of them are mine.**

### Cause 1 — FTMO discontinued REST access (vendor policy, not fixable)

As of late April 2024, **FTMO discontinued REST API access to the DXtrade
platform for its clients.** See [Trading Update – 25 Apr 2024](https://ftmo.com/en/trading-updates/trading-update-25-apr-2024/).
Authenticating against FTMO returns `403 Forbidden` and no code change will
alter that. FTMO was the headline use case for this repo, and it is closed
permanently.

**This is the cause the old README named — and stopping there is what hid the
next three for two years.** A `403` at login is indistinguishable from a
policy block, so the author attributed the failure to FTMO and never looked
further. The bugs below were found by a code review in 2026, not by running
anything.

### Cause 2 — every REST path after login omitted the `/dxsca-web` prefix (mine)

`login()` correctly used `/dxsca-web/login`. **Every single other endpoint** —
portfolio, positions, orders, history, order placement, modification,
cancellation, position close — was built straight off the bare host, with no
prefix. All eight would have returned `404` **at any broker on earth**, not
just FTMO. Even if FTMO had kept REST access enabled, nothing past
authentication would have worked.

Because login was never reached at FTMO, this bug never got the chance to
surface.

**Fixed (UNVERIFIED):** the prefix is now a constructor argument (`api_prefix`,
default `/dxsca-web`) and every URL is built by a single `_url(path)` helper.
The point of routing through one helper rather than patching eight f-strings is
that this class of bug cannot silently recur.

### Cause 3 — wrong authorization scheme (mine)

The wrapper sent `Authorization: Bearer <token>`. DXtrade SCA uses a custom
scheme: `Authorization: DXAPI <sessionToken>`. Every authenticated request
would have been rejected with `401`, on top of already being sent to a
prefix-less URL.

**Fixed (UNVERIFIED):** now sends `DXAPI`, via the `AUTH_SCHEME` class
attribute.

### Cause 4 — order payload missing required fields (mine)

The order payload omitted both:

* **`account`** — the account the order belongs to, and
* **`orderCode`** — a client-generated idempotency key that DXtrade requires.

Orders would have been rejected at validation even had they reached the correct
URL with a valid session. `orderCode` is the field that makes retrying a
timed-out order safe rather than a way to accidentally open a second position.

**Fixed (UNVERIFIED):** both fields are now populated;
`orderCode` is auto-generated per order and can be overridden by the caller.

### ⚠️ What "Fixed (UNVERIFIED)" means here

It means **fixed by reading, not by running.** Confirming any of these requires
a funded or demo account at a broker that still enables DXtrade SCA REST
access. I do not have one. SCA access is provisioned per firm, so there is no
universal endpoint set to test against either.

Everything in this repository — URL paths, the authorization scheme, payload
field names, the login response shape, the WebSocket handshake, the `/ping`
keepalive endpoint — remains **inferred from community examples and a
third-party development guide.** Some of it is probably still wrong. The
included tests assert that the wrapper *builds the requests it intends to
build*; they cannot and do not assert that a DXtrade server accepts them.

**Do not point this at a funded account.**

### Other known defects, fixed but likewise unverified

| Defect | Was |
| --- | --- |
| Account-code fallback | Returned the invented code `"primary"` when login carried no account, guaranteeing a `404` at a nonexistent account. Now raises `DXTradeAPIError` with an actionable message. |
| No session keepalive | Sessions expired silently with no renewal. Now has a `ping()` method and an optional background keepalive thread. |
| `logout()` was a no-op | Its HTTP call was commented out "pending documentation," so sessions were left alive server-side. Now issues the request, best-effort. |
| Dead WebSocket auth header | Guarded by `"token=" not in self._build_websocket_url()` — tautologically false whenever a token existed, so the header was never sent once. |
| Malformed WebSocket URL | Appended `&account=` unconditionally but `?token=` only when a token existed, producing a query string whose first parameter began with `&`. Now built with `urlencode()`. |

## ✅ Use this instead

If you want to actually trade on DXtrade from Python, use
**[`dxtrade-sdk`](https://pypi.org/project/dxtrade-sdk/)** — it is
pip-installable and tested, which this is not.

```bash
pip install dxtrade-sdk
```

This repository is not a competitor to it and should not be treated as one.

## 🔍 What this repo is actually good for

Not as a library. As a **case study in reverse-engineering an undocumented
broker API from community examples, and getting it wrong.**

The useful content is:

* A complete, commented model of what the DXtrade SCA REST/WebSocket surface
  was *believed* to look like from public information alone (`dxtrade_wrapper.py`).
* A concrete record of **how a vendor-side block masked three independent
  client-side bugs for two years.** The `403` was real, it was correctly
  diagnosed, and diagnosing it correctly was exactly what stopped anyone from
  looking any further.
* A demonstration of why "it fails at step 1 for a known external reason" is a
  dangerous place to stop debugging.

## 🎓 What was learned

1. **A correct diagnosis can still be an incomplete one.** FTMO really did
   disable REST access. That true fact absorbed all the suspicion a `403`
   generates and no one looked past it.
2. **Test at the boundary you control.** Not one line of this needed a broker to
   verify the URL it constructs. The single test asserting that login hits
   `/dxsca-web/login` with a `DXAPI` header would have caught two of the three
   blockers, offline, in the first hour. It now exists — two years late.
3. **One code path, not eight.** The prefix bug was possible only because URL
   construction was duplicated across eight f-strings. One `_url()` helper makes
   the whole class of bug structurally impossible.
4. **Don't ship a plausible fallback for missing data.** The `"primary"` account
   code turned a clear "I don't know the account" into a misleading `404` from
   the server.
5. **Publishing an honest failure beats publishing a hopeful half-success.** A
   README that says "works, mostly, probably" costs every reader the time to
   discover otherwise.

## 🔍 Introduction

`DXTradeDashboardWrapper` was intended to simplify interaction with the DXtrade
trading platform's API, abstracting REST and WebSocket calls behind a dashboard-
oriented interface: authentication, account data, order management, and
real-time streams.

It was written from a Development Guide that itself relied on publicly available
information and community examples, because official, comprehensive DXtrade API
documentation was not accessible. That inference chain is the root cause of
causes 2 through 4 above.

## ✨ Features (as designed, none verified)

Everything below describes **intent**. No item has been confirmed to work.

### 🔐 Authentication
* Login with username, password, and broker domain/vendor
* Session token handling, `DXAPI` scheme *(unverified)*
* `ping()` keepalive plus optional background thread *(unverified endpoint)*

### 📈 Account Data Access
* `get_balance()`, `get_positions()`, `get_orders()`, `get_order_history()`
* Structured returns via dataclasses

### 🛒 Order Management
* `place_order()` — Market, Limit, Stop
* `modify_order()`, `cancel_order()`
* `close_position()`, `modify_position_sl_tp()`
* Linked Stop Loss / Take Profit handling

### 🔄 Real-Time Data
* `connect_websocket()` / `disconnect_websocket()`
* `subscribe_market_data()`, `subscribe_account_updates()`
* Thread-safe queues: `price_update_queue`, `order_update_queue`,
  `account_update_queue`

### 🚦 Error Handling
* Custom exception hierarchy, logging throughout

## 🔄 Alternative Approaches

Some community implementations interact with DXtrade by mimicking the browser:

1. Authenticating via internal endpoints (e.g. `/api/auth/login`) using **session
   cookies** rather than API tokens
2. Scraping a **CSRF token** from the web application page
3. Using the web app's internal WebSocket endpoint (e.g. `/client/connector`)
4. Posting orders to internal endpoints (e.g. `/api/orders/single`) with cookies
   and CSRF token

> **⚠️ Caution:** this is reverse-engineering the web platform. It is **highly
> fragile** and **may violate your broker's Terms of Service**. Not recommended.

## 📦 Installation

There is no package to install — see [Packaging](#-packaging). Clone the repo and
install the dependencies.

### Dependencies

* `requests` — HTTP REST calls
* `websocket-client` — WebSocket connections
* `python-dotenv` — used by `example.py` to load `.env`
  *(previously missing from this list, so following the README broke the
  example at `example.py:8`)*

```bash
pip install requests websocket-client python-dotenv
```

Or:

```bash
pip install -r requirements.txt
```

### Configuration

Copy `.env.example` to `.env` and fill it in. `.env` is gitignored; never commit
real credentials.

```bash
cp .env.example .env
```

## 🧪 Tests

Tests use [`responses`](https://github.com/getsentry/responses) with hand-written
JSON fixtures, so **no broker account is required** to run them — previously the
single largest contributor blocker.

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

**What they prove:** that the wrapper builds the URLs, headers, and payloads it
intends to. They regression-lock all three blocking bugs.

**What they do not prove:** that any DXtrade server accepts those requests. The
fixtures in `tests/fixtures/` are hand-written from the same inferred protocol as
the wrapper — **not recorded from a live session**, because no live session has
ever succeeded. If the inference is wrong, these tests pass while the wrapper
stays broken. Replacing the fixtures with real recorded traffic is the single
most valuable contribution anyone with broker access could make.

## 📦 Packaging

**Deliberately not published to PyPI, and there is no `pyproject.toml`.**

Publishing a non-functional package next to the working, tested `dxtrade-sdk`
would be net-negative: it would compete for installs on name recognition while
being unable to place an order. Packaging becomes worth doing if and only if the
protocol fixes above are verified against a live broker. Until then, the honest
distribution channel for this repo is the README you are reading.

## ⚖️ Disclaimer

This wrapper was built from inferred information and community examples. It is
**not functional**, for the four independent reasons documented above, and its
fixes are unverified.

**Use at your own risk.** Automated trading carries significant financial risk.
The authors and contributors are not responsible for any financial losses or
other damages. Never point unverified trading code at a funded account.

---

*This project is not affiliated with, endorsed by, or connected to Devexperts,
DXTRADE, or FTMO.*
