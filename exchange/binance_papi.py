"""
High-level Binance papi exchange interface.
Wraps PapiClient with freqtrade-compatible method signatures.
All methods speak papi natively — no CCXT, no fapi.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .papi_client import PapiClient

logger = logging.getLogger(__name__)


class BinancePapiExchange:
    """Exchange interface compatible with Indian_Crypto_Bot trading loop."""

    def __init__(self, api_key: str, secret_key: str, dry_run: bool = False):
        self.dry_run = dry_run
        self._client = PapiClient(api_key, secret_key) if not dry_run else None
        self._time_offset_ms = 0

    @property
    def name(self) -> str:
        return "binance_papi"

    def startup(self) -> None:
        """Sync time offset with exchange server."""
        if self.dry_run or self._client is None:
            return
        try:
            self._client.ping()
            logger.info("Papi connectivity verified")
        except Exception as e:
            logger.warning(f"Papi ping failed (non-fatal): {e}")

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    # ── Market Data ──

    def fetch_ohlcv(
        self, symbol: str, timeframe: str, limit: int = 500
    ) -> list[list]:
        """
        Fetch OHLCV candles. Returns [[timestamp, open, high, low, close, volume], ...].
        Symbol format: TRXUSDT (no slash, no :USDT suffix for papi).
        """
        if self._client is None:
            return []
        papi_symbol = self._to_papi_symbol(symbol)
        interval = self._to_papi_interval(timeframe)
        raw = self._client.get_klines(papi_symbol, interval, limit=limit)
        return [
            [
                int(c[0]),       # timestamp ms
                float(c[1]),     # open
                float(c[2]),     # high
                float(c[3]),     # low
                float(c[4]),     # close
                float(c[5]),     # volume
            ]
            for c in raw
        ]

    def fetch_ticker(self, symbol: str) -> dict:
        """Fetch latest ticker price."""
        if self._client is None:
            return {"symbol": symbol, "last": 0.0}
        papi_symbol = self._to_papi_symbol(symbol)
        data = self._client.get_ticker_price(papi_symbol)
        if isinstance(data, list):
            data = data[0] if data else {}
        return {"symbol": symbol, "last": float(data.get("price", 0))}

    # ── Account & Balance ──

    def fetch_balance(self) -> dict:
        """
        Fetch account balance from papi /account endpoint.
        Returns CCXT-compatible balance dict with USDT keys.
        """
        if self.dry_run or self._client is None:
            return {"USDT": {"free": 1000.0, "used": 0.0, "total": 1000.0}}

        account = self._client.get_account()
        equity = float(account.get("accountEquity", 0))
        used = float(account.get("accountInitialMargin", 0))
        free = equity - used

        logger.debug(f"Balance: equity={equity}, used={used}, free={free}")
        return {
            "info": account,
            "USDT": {"free": free, "used": used, "total": equity},
            "free": {"USDT": free},
            "used": {"USDT": used},
            "total": {"USDT": equity},
        }

    def fetch_positions(self, symbol: Optional[str] = None) -> list[dict]:
        """Fetch open positions from papi /positionRisk."""
        if self.dry_run or self._client is None:
            return []
        papi_symbol = self._to_papi_symbol(symbol) if symbol else None
        return self._client.get_position_risk(papi_symbol)

    # ── Order Management ──

    def create_order(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: Optional[float] = None,
        time_in_force: Optional[str] = None,
        reduce_only: bool = False,
    ) -> dict:
        """Place an order on papi."""
        if self.dry_run or self._client is None:
            logger.info(f"[DRY RUN] {side} {amount} {symbol} @ {price}")
            return {"id": "dry_run", "status": "simulated"}

        papi_symbol = self._to_papi_symbol(symbol)
        return self._client.create_order(
            symbol=papi_symbol,
            side=side,
            order_type=order_type,
            quantity=amount,
            price=price,
            time_in_force=time_in_force,
            reduce_only=reduce_only,
        )

    def cancel_order(self, order_id: int, symbol: str) -> dict:
        """Cancel an open order."""
        if self.dry_run or self._client is None:
            return {"id": order_id, "status": "cancelled_simulated"}
        papi_symbol = self._to_papi_symbol(symbol)
        return self._client.cancel_order(papi_symbol, order_id=order_id)

    def fetch_order(self, order_id: int, symbol: str) -> dict:
        """Query order status."""
        if self.dry_run or self._client is None:
            return {"id": order_id, "status": "simulated"}
        papi_symbol = self._to_papi_symbol(symbol)
        return self._client.get_order(papi_symbol, order_id=order_id)

    def fetch_open_orders(self, symbol: Optional[str] = None) -> list[dict]:
        """List all open orders."""
        if self.dry_run or self._client is None:
            return []
        papi_symbol = self._to_papi_symbol(symbol) if symbol else None
        return self._client.get_open_orders(papi_symbol)

    # ── Helpers ──

    @staticmethod
    def _to_papi_symbol(symbol: str) -> str:
        """Convert 'TRX/USDT:USDT' → 'TRXUSDT' for papi API calls."""
        return symbol.replace("/", "").replace(":USDT", "").replace(":BTC", "")

    @staticmethod
    def _to_papi_interval(timeframe: str) -> str:
        """Convert freqtrade timeframe strings to papi kline intervals."""
        mapping = {
            "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m",
            "30m": "30m", "1h": "1h", "2h": "2h", "4h": "4h",
            "6h": "6h", "8h": "8h", "12h": "12h", "1d": "1d",
            "3d": "3d", "1w": "1w", "1M": "1M",
        }
        return mapping.get(timeframe, timeframe)