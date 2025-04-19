"""
Optional data models and enums for the CTrader Trading SDK.

This module provides more Pythonic representations of common cTrader API data structures
with type hints and documentation for improved developer experience.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from typing import Dict, List, Optional


class TradeSide(str, Enum):
    """Trade direction enum."""
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    """Order type enum."""
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"
    MARKET_RANGE = "MARKET_RANGE"


class TimeFrame(str, Enum):
    """Historical data timeframe enum."""
    M1 = "M1"     # 1 minute
    M2 = "M2"     # 2 minutes
    M3 = "M3"     # 3 minutes
    M4 = "M4"     # 4 minutes
    M5 = "M5"     # 5 minutes
    M10 = "M10"   # 10 minutes
    M15 = "M15"   # 15 minutes
    M30 = "M30"   # 30 minutes
    H1 = "H1"     # 1 hour
    H4 = "H4"     # 4 hours
    H12 = "H12"   # 12 hours
    D1 = "D1"     # 1 day
    W1 = "W1"     # 1 week
    MN1 = "MN1"   # 1 month


@dataclass
class SymbolInfo:
    """Trading symbol information."""
    symbol_id: int
    symbol_name: str
    asset_class: str
    digits: int
    pip_position: int
    tick_size: float
    min_volume: float
    max_volume: float
    step_volume: float
    margin_hedged: float
    is_tradable: bool
    bid: Optional[float] = None
    ask: Optional[float] = None
    spread: Optional[float] = None


@dataclass
class AccountInfo:
    """Trading account information."""
    account_id: int
    balance: float
    equity: float
    margin_used: float
    free_margin: float
    margin_level: float
    unrealized_gross_profit: float
    unrealized_net_profit: float


@dataclass
class Position:
    """Trading position information."""
    position_id: int
    symbol_id: int
    symbol_name: str
    volume: float
    trade_side: TradeSide
    price: float
    swap: float
    commission: float
    open_timestamp: int
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    
    @property
    def open_time(self) -> datetime:
        """Convert timestamp to datetime."""
        return datetime.fromtimestamp(self.open_timestamp / 1000)


@dataclass
class Order:
    """Trading order information."""
    order_id: int
    symbol_id: int
    symbol_name: str
    volume: float
    trade_side: TradeSide
    order_type: OrderType
    order_status: str
    creation_timestamp: Optional[int] = None
    expiration_timestamp: Optional[int] = None
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    
    @property
    def creation_time(self) -> Optional[datetime]:
        """Convert creation timestamp to datetime."""
        if self.creation_timestamp:
            return datetime.fromtimestamp(self.creation_timestamp / 1000)
        return None
    
    @property
    def expiration_time(self) -> Optional[datetime]:
        """Convert expiration timestamp to datetime."""
        if self.expiration_timestamp:
            return datetime.fromtimestamp(self.expiration_timestamp / 1000)
        return None


@dataclass
class OHLCBar:
    """Historical price bar (Open, High, Low, Close)."""
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    
    @property
    def time(self) -> datetime:
        """Convert timestamp to datetime."""
        return datetime.fromtimestamp(self.timestamp / 1000)


@dataclass
class Tick:
    """Price tick data."""
    symbol_id: int
    bid: float
    ask: float
    timestamp: int
    
    @property
    def time(self) -> datetime:
        """Convert timestamp to datetime."""
        return datetime.fromtimestamp(self.timestamp / 1000)
