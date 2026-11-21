"""
Risk management utilities for Indian_Crypto_Bot.
Implements drawdown protection, regime filtering, and position sizing.
Phase 4 Market Regime Improvements.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class RiskManager:
    """Centralized risk management with circuit breakers and regime filters."""

    def __init__(
        self,
        max_daily_loss_pct: float = 5.0,
        max_consecutive_losses: int = 3,
        min_adx_for_entry: float = 25.0,
        max_spread_pct: float = 0.5,
        min_volume_usdt: float = 100000.0,
    ):
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_consecutive_losses = max_consecutive_losses
        self.min_adx_for_entry = min_adx_for_entry
        self.max_spread_pct = max_spread_pct
        self.min_volume_usdt = min_volume_usdt

        # Tracking state
        self._daily_pnl: float = 0.0
        self._consecutive_losses: int = 0
        self._circuit_breaker_active: bool = False
        self._last_reset_date: Optional[str] = None

    def reset_daily_if_needed(self) -> None:
        """Reset daily tracking at UTC midnight."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._last_reset_date != today:
            self._daily_pnl = 0.0
            self._circuit_breaker_active = False
            self._last_reset_date = today
            logger.info(f"Risk manager daily reset: {today}")

    def record_trade_result(self, pnl: float) -> None:
        """Record a closed trade's PnL for circuit breaker tracking."""
        self.reset_daily_if_needed()
        self._daily_pnl += pnl
        if pnl < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

        logger.debug(
            f"Trade recorded: pnl={pnl:.4f}, daily={self._daily_pnl:.4f}, "
            f"consec_losses={self._consecutive_losses}"
        )

    def can_open_trade(self, current_balance: float) -> tuple[bool, str]:
        """
        Check if a new trade is allowed under current risk constraints.

        Returns:
            (allowed, reason) tuple.
        """
        self.reset_daily_if_needed()

        # Circuit breaker: daily loss limit
        if current_balance > 0:
            daily_loss_pct = abs(min(self._daily_pnl, 0)) / current_balance * 100
            if daily_loss_pct >= self.max_daily_loss_pct:
                self._circuit_breaker_active = True
                return False, f"Daily loss limit hit: {daily_loss_pct:.2f}% >= {self.max_daily_loss_pct}%"

        # Circuit breaker: consecutive losses
        if self._consecutive_losses >= self.max_consecutive_losses:
            self._circuit_breaker_active = True
            return False, f"Consecutive loss limit: {self._consecutive_losses} >= {self.max_consecutive_losses}"

        if self._circuit_breaker_active:
            return False, "Circuit breaker active (reset at next UTC midnight)"

        return True, "OK"

    def check_regime_filter(self, adx: Optional[float] = None) -> tuple[bool, str]:
        """
        Check if current market regime allows trend-following entries.

        Args:
            adx: Average Directional Index value (None = skip check)

        Returns:
            (allowed, reason) tuple.
        """
        if adx is not None and adx < self.min_adx_for_entry:
            return False, f"Weak trend: ADX={adx:.1f} < {self.min_adx_for_entry}"
        return True, "OK"

    def check_liquidity(
        self, spread_pct: Optional[float] = None, volume_usdt: Optional[float] = None
    ) -> tuple[bool, str]:
        """
        Check if liquidity conditions allow entry.

        Args:
            spread_pct: Current bid-ask spread as percentage
            volume_usdt: 24h volume in USDT

        Returns:
            (allowed, reason) tuple.
        """
        if spread_pct is not None and spread_pct > self.max_spread_pct:
            return False, f"Spread too wide: {spread_pct:.3f}% > {self.max_spread_pct}%"
        if volume_usdt is not None and volume_usdt < self.min_volume_usdt:
            return False, f"Low volume: {volume_usdt:.0f} < {self.min_volume_usdt:.0f} USDT"
        return True, "OK"

    @property
    def status(self) -> dict:
        """Current risk manager state for /status command."""
        return {
            "daily_pnl": round(self._daily_pnl, 4),
            "consecutive_losses": self._consecutive_losses,
            "circuit_breaker_active": self._circuit_breaker_active,
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "max_consecutive_losses": self.max_consecutive_losses,
        }