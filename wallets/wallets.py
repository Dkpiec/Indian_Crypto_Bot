"""
Wallet management for Indian_Crypto_Bot.
Reads balances directly from papi /account endpoint via exchange layer.
No caching layer that can go stale — always fresh on update().
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Wallet:
    """Single currency wallet state."""
    currency: str
    free: float = 0.0
    used: float = 0.0
    total: float = 0.0

    @property
    def available(self) -> float:
        return self.free


class Wallets:
    """Manages wallet balances for all currencies."""

    def __init__(self, exchange, dry_run: bool = False, dry_run_wallet: float = 1000.0):
        self._exchange = exchange
        self._dry_run = dry_run
        self._dry_run_wallet = dry_run_wallet
        self._wallets: dict[str, Wallet] = {}

    def update(self) -> None:
        """Fetch fresh balances from exchange and update internal state."""
        try:
            balance = self._exchange.fetch_balance()
            usdt_data = balance.get("USDT", {})
            if isinstance(usdt_data, dict):
                self._wallets["USDT"] = Wallet(
                    currency="USDT",
                    free=float(usdt_data.get("free", 0)),
                    used=float(usdt_data.get("used", 0)),
                    total=float(usdt_data.get("total", 0)),
                )
                logger.debug(
                    f"Wallets updated: USDT free={self._wallets['USDT'].free:.4f} "
                    f"used={self._wallets['USDT'].used:.4f} total={self._wallets['USDT'].total:.4f}"
                )
            else:
                logger.warning(f"Unexpected USDT balance format: {usdt_data}")
        except Exception as e:
            logger.error(f"Failed to update wallets: {e}")

    def get_wallet(self, currency: str = "USDT") -> Optional[Wallet]:
        """Get wallet for a specific currency."""
        return self._wallets.get(currency)

    def get_free(self, currency: str = "USDT") -> float:
        """Get free (available) balance for a currency."""
        w = self._wallets.get(currency)
        return w.free if w else 0.0

    def get_used(self, currency: str = "USDT") -> float:
        """Get used (locked/margin) balance for a currency."""
        w = self._wallets.get(currency)
        return w.used if w else 0.0

    def get_total(self, currency: str = "USDT") -> float:
        """Get total balance for a currency."""
        w = self._wallets.get(currency)
        return w.total if w else 0.0

    def get_all_balances(self) -> dict[str, dict]:
        """Get all wallet balances as a dict."""
        return {
            currency: {"free": w.free, "used": w.used, "total": w.total}
            for currency, w in self._wallets.items()
        }

    def get_all(self) -> list[Wallet]:
        """Get all wallets as a list."""
        return list(self._wallets.values())