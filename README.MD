# 📊 DXTrade Python API Wrapper

![Python](https://img.shields.io/badge/python-3.7+-blue.svg)
![Status](https://img.shields.io/badge/status-development-orange)
![License](https://img.shields.io/badge/license-MIT-green)

## 📋 Table of Contents

- [Introduction](#introduction)
- [Project Status](#project-status)
- [Features](#features)
- [Alternative Approaches](#alternative-approaches)
- [Installation](#installation)
- [Disclaimer](#disclaimer)

## 🔍 Introduction

This Python project provides a high-level wrapper class, `DXTradeDashboardWrapper`, designed to simplify interaction with the DXTRADE trading platform's APIs. The primary goal is to abstract the complexities of direct API calls for functionalities commonly needed in trading dashboards, such as:

* Authentication and session management
* Retrieving account balance, positions, and orders
* Placing, modifying, and canceling orders
* Handling real-time data streams (prices, account updates) via WebSockets

The wrapper was developed based on the structure and information outlined in the provided Development Guide, which itself relied on publicly available information and community examples due to challenges in accessing official, comprehensive DXTRADE API documentation.

## ⚠️ Project Status: Not Currently Functional with FTMO ⚠️

**Important Notice:** As of late April 2024, **FTMO has discontinued REST API access for the DXtrade platform for its clients.**

* See FTMO Announcement: [Trading Update – 25 Apr 2024](https://ftmo.com/en/trading-updates/trading-update-25-apr-2024/)

This means that the core functionalities of this wrapper that rely on the REST API (including **login**, fetching account data, and **placing/managing orders**) **will not work** when configured for an FTMO DXtrade account. Attempts to authenticate using the previously known REST endpoints (like `/dxsca-web/login` or `/api/auth/login`) typically result in a `403 Forbidden` error.

While the WebSocket components might theoretically connect if the correct endpoint and authentication mechanism (likely cookie-based now) were determined, the inability to authenticate and manage the account via the REST API renders the wrapper non-functional for its intended purpose with FTMO.

## ✨ Features

The `DXTradeDashboardWrapper` was designed to offer:

### 🔐 Authentication
* Login using username, password, and broker-specific domain/vendor
* Session management and token handling

### 📈 Account Data Access
* Methods like `get_balance()`, `get_positions()`, `get_orders()`
* Structured data returns using Python dataclasses

### 🛒 Order Management
* `place_order()`: Market, Limit, and Stop orders
* `modify_order()`: Change price, quantity, etc.
* `cancel_order()`: Remove pending orders
* `close_position()`: Full or partial position closure
* `modify_position_sl_tp()`: Update Stop Loss / Take Profit levels
* Special handling for linked Stop Loss / Take Profit orders

### 🔄 Real-Time Data
* WebSocket connection management (`connect_websocket()`, `disconnect_websocket()`)
* Data stream subscriptions:
  * `subscribe_market_data()`: Price updates
  * `subscribe_account_updates()`: Balance, order, position changes
* Thread-safe data queues:
  * `price_update_queue`
  * `order_update_queue`
  * `account_update_queue`

### ⚙️ Configuration
* Flexible initialization parameters to adapt to different broker implementations
* Customizable endpoints, paths, and authentication parameters

### 🚦 Error Handling
* Custom exception hierarchy for specific error types
* Comprehensive logging and debugging capabilities

## 🔄 Alternative Approaches

Some alternative implementations found in the community attempt to interact with DXtrade by mimicking the web browser's behavior:

1. Authenticating via internal API endpoints (e.g., `/api/auth/login`) that rely on **session cookies** rather than API tokens
2. Fetching a **CSRF token** by parsing the main web application page
3. Using the **web application's internal WebSocket endpoint** (e.g., `/client/connector`) authenticated via session cookies
4. Sending order/management requests to internal API endpoints (e.g., `/api/orders/single`) including **session cookies and the CSRF token**

> **⚠️ Caution:** This approach is essentially reverse-engineering the web platform. It is **highly fragile** (can break without notice if the platform is updated) and **may violate the broker's Terms of Service**. It is not recommended for reliable or long-term use.

## 📦 Installation

### Dependencies

* `requests`: For making HTTP REST API calls
* `websocket-client`: For handling WebSocket connections

Install using pip:

```bash
pip install requests websocket-client
```

## ⚖️ Disclaimer

This wrapper was developed based on inferred information and community examples. Due to the discontinuation of the FTMO REST API for clients, it is not functional for FTMO accounts. If used with other brokers providing DXtrade, thorough testing is essential to verify compatibility with their specific API endpoints, authentication mechanisms, and data formats.

**Use at your own risk.** Automated trading involves significant financial risks. The authors and contributors of this wrapper are not responsible for any financial losses or other damages that may result from its use. Always test thoroughly in a demo environment before considering use with real funds.

---

*This project is not affiliated with, endorsed by, or connected to Devexperts, DXTRADE, or FTMO.*