# CTrader Trading SDK - Package Structure

This document describes the organization of the `ctrader_trading_sdk` package.

## Directory Structure

```
ctrader_trading_sdk/
├── ctrader_trading_sdk/
│   ├── __init__.py          # Package initialization, imports, version
│   ├── sdk.py               # Main SDK class implementation
│   ├── exceptions.py        # Custom exception classes
│   └── models.py            # Optional data models and enums
├── tests/
│   ├── __init__.py
│   └── test_sdk.py          # Unit tests for the SDK
├── examples/
│   └── example_usage.py     # Example script demonstrating usage
├── setup.py                 # Package setup script
├── README.md                # Package documentation
├── LICENSE                  # License file
└── PACKAGE_STRUCTURE.md     # This file
```

## Files Description

### Core Package Files

- **`__init__.py`**: Initializes the package and exposes the main classes and exceptions for easy importing.
  
- **`sdk.py`**: Contains the main `CTraderTradingSDK` class implementation, which wraps the OpenApiPy client and provides simplified methods for common trading operations.

- **`exceptions.py`**: Defines custom exception classes for different error scenarios in the SDK.

- **`models.py`**: Contains optional data classes and enums for representing cTrader API data structures in a more Pythonic way.

### Tests

- **`test_sdk.py`**: Contains unit tests for the SDK, using mock objects to simulate API interactions without making actual network requests.

### Examples

- **`example_usage.py`**: Provides a comprehensive example script demonstrating how to use the SDK for various trading operations.

### Package Configuration

- **`setup.py`**: Configures package metadata, dependencies, and installation settings.

- **`README.md`**: Main documentation file with installation instructions, quick start guide, and usage examples.

- **`LICENSE`**: The license file for the package.

## Class Structure

The main class in the SDK is `CTraderTradingSDK`, which is organized into logical sections:

1. **Initialization and state management**: Constructor, internal state flags, and data caches.

2. **Connection and lifecycle management**: Methods for connecting, disconnecting, and handling reconnection.

3. **Authentication**: Methods for handling the OAuth flow and account authentication.

4. **Event handling**: Callbacks for handling various events from the API.

5. **Trading functionality**: Methods for placing orders, managing positions, and retrieving account data.

6. **Market data**: Methods for subscribing to real-time price updates and retrieving historical data.

7. **Helper methods**: Internal utilities for common operations.

The SDK uses custom exception classes for clear error handling and optional data models for type safety and better developer experience.
