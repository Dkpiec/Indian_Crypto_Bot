"""
Strategy interface for Indian_Crypto_Bot.
Strategies implement this ABC and are loaded dynamically from strategies/ directory.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)


class IStrategy(ABC):
    """Abstract base class for all trading strategies."""

    # Strategy identifier
    name: str = "BaseStrategy"

    # Timeframe for candles
    timeframe: str = "2h"

    # Max concurrent open trades
    max_open_trades: int = 4

    # Stake currency
    stake_currency: str = "USDT"

    # Initial stoploss percentage (e.g., -0.99 = -99%)
    stoploss: float = -0.99

    # Minimum ROI thresholds: {candle_index: roi_percentage}
    minimal_roi: dict = {"0": 100}

    # Strategy-specific parameters (populated from config or defaults)
    params: dict = {}

    # ── Required Methods ──

    @abstractmethod
    def populate_indicators(
        self, dataframe: pd.DataFrame, metadata: dict
    ) -> pd.DataFrame:
        """
        Calculate technical indicators on OHLCV dataframe.
        Must return a new DataFrame with indicator columns added.
        """
        raise NotImplementedError

    @abstractmethod
    def populate_entry_trend(
        self, dataframe: pd.DataFrame, metadata: dict
    ) -> pd.DataFrame:
        """
        Generate entry signals.
        Must set dataframe["enter_long"] = 1 where entry condition is met.
        """
        raise NotImplementedError

    @abstractmethod
    def populate_exit_trend(
        self, dataframe: pd.DataFrame, metadata: dict
    ) -> pd.DataFrame:
        """
        Generate exit signals.
        Must set dataframe["exit_long"] = 1 where exit condition is met.
        """
        raise NotImplementedError

    # ── Optional Hooks ──

    def custom_stoploss(
        self,
        pair: str,
        trade,
        current_time: int,
        current_rate: float,
        **kwargs: Any,
    ) -> Optional[float]:
        """
        Dynamic stoploss. Return a new stoploss percentage or None to use default.
        """
        return None

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        **kwargs: Any,
    ) -> bool:
        """
        Final check before placing entry order. Return False to skip.
        """
        return True

    def confirm_trade_exit(
        self,
        pair: str,
        trade,
        order_type: str,
        amount: float,
        rate: float,
        **kwargs: Any,
    ) -> bool:
        """
        Final check before placing exit order. Return False to skip.
        """
        return True

    # ── Lifecycle ──

    def on_start(self, **kwargs: Any) -> None:
        """Called when bot starts or strategy is reloaded."""
        logger.info(f"Strategy {self.name} started")

    def on_stop(self, **kwargs: Any) -> None:
        """Called when bot shuts down."""
        logger.info(f"Strategy {self.name} stopped")