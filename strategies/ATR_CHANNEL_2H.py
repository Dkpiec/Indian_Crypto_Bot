"""
ATR_CHANNEL_2H Strategy for Indian_Crypto_Bot.
ATR channel breakout strategy on 2h timeframe.
Uses ATR-based channel breakout for entry/exit signals on 2h timeframe.
"""

import logging
from typing import Any

import numpy as np
import pandas as pd
from pandas import DataFrame

from strategy.interface import IStrategy

logger = logging.getLogger(__name__)


class ATR_CHANNEL_2H(IStrategy):
    """ATR Channel Breakout Strategy on 2h timeframe."""

    name = "ATR_CHANNEL_2H"
    timeframe = "2h"
    max_open_trades = 4
    stake_currency = "USDT"
    stoploss = -0.99
    minimal_roi = {"0": 100}

    # Strategy parameters (defaults, overridable via config)
    params = {
        "atr_period": 14,
        "atr_mult": 1.5,
        "chan_lookback": 40,
        "tp_r": 4.0,
        "sl_atr_mult": 1.0,
        "max_hold_bars": 120,
    }

    def populate_indicators(
        self, dataframe: DataFrame, metadata: dict
    ) -> DataFrame:
        """Calculate ATR and channel boundaries."""
        period = self.params.get("atr_period", 14)
        mult = self.params.get("atr_mult", 1.5)
        lookback = self.params.get("chan_lookback", 40)

        # True Range
        high = dataframe["high"]
        low = dataframe["low"]
        close = dataframe["close"]
        prev_close = close.shift(1)

        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        # ATR
        dataframe["atr"] = true_range.rolling(window=period).mean()

        # Channel midline (SMA of close)
        dataframe["mid"] = close.rolling(window=lookback).mean()

        # Channel upper/lower bands
        dataframe["upper"] = dataframe["mid"] + (dataframe["atr"] * mult)
        dataframe["lower"] = dataframe["mid"] - (dataframe["atr"] * mult)

        # Previous values for crossover detection
        dataframe["prev_close"] = prev_close
        dataframe["prev_upper"] = dataframe["upper"].shift(1)
        dataframe["prev_lower"] = dataframe["lower"].shift(1)

        return dataframe

    def populate_entry_trend(
        self, dataframe: DataFrame, metadata: dict
    ) -> DataFrame:
        """Generate long entry signals on upward channel breakout."""
        dataframe["enter_long"] = 0
        dataframe["enter_tag"] = ""

        # Entry: close crosses above upper channel band
        condition = (
            (dataframe["close"] > dataframe["upper"])
            & (dataframe["prev_close"] <= dataframe["prev_upper"])
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[condition, "enter_long"] = 1
        dataframe.loc[condition, "enter_tag"] = "atr_channel_breakout_up"

        return dataframe

    def populate_exit_trend(
        self, dataframe: DataFrame, metadata: dict
    ) -> DataFrame:
        """Generate long exit signals on downward channel breakout."""
        dataframe["exit_long"] = 0
        dataframe["exit_tag"] = ""

        # Exit: close crosses below lower channel band
        condition = (
            (dataframe["close"] < dataframe["lower"])
            & (dataframe["prev_close"] >= dataframe["prev_lower"])
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[condition, "exit_long"] = 1
        dataframe.loc[condition, "exit_tag"] = "atr_channel_breakout_down"

        return dataframe

    def custom_stoploss(
        self,
        pair: str,
        trade: Any,
        current_time: int,
        current_rate: float,
        **kwargs: Any,
    ) -> float | None:
        """ATR-based trailing stoploss."""
        sl_mult = self.params.get("sl_atr_mult", 1.0)
        atr = kwargs.get("atr")
        if atr is not None and atr > 0:
            # Stoploss as percentage below current rate
            stop_pct = (atr * sl_mult) / current_rate
            return -stop_pct
        return None