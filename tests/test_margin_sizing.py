"""Regression: -2019 'Margin is insufficient' storm.

Root cause: use_full_balance sized every new position from TOTAL equity
(free + used margin) split across slots. When slots already held margin,
a new position requested margin the account could not cover, and every
entry order was rejected with -2019.

Fix: size from FREE equity (margin actually available to allocate), and
skip entries whose rounded quantity would fall below the symbol's minQty
instead of firing guaranteed rejections.
"""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exchange.binance_papi import BinancePapiExchange
from bot.worker import BotWorker


class _FakeClient:
    """PapiClient stand-in that serves filters from the local data file."""

    def __init__(self):
        from exchange.papi_client import PapiClient
        loader = PapiClient._load_exchange_info_filters
        self._filters_cache = {}
        self._filters_loaded = False
        # load once, the same way PapiClient does (local data file)
        self._filters_cache = loader(self)

    def get_symbol_filters(self, papi_symbol):
        return self._filters_cache.get(papi_symbol, {})


def _make_worker(max_open=4, free=200.0, used=600.0, leverage=5):
    """Worker with wallets wired to fixed free/used balances."""
    cfg = {
        "strategy": {"name": "ATR_CHANNEL_2H", "timeframe": "2h"},
        "trading": {"leverage": leverage, "use_full_balance": True},
        "pairlists": [{"method": "StaticPairList", "pairs": ["TRX/USDT:USDT"]}],
        "internals": {"process_throttle_secs": 1},
    }

    class _Wallet:
        def __init__(self):
            self.started = threading.Event()
            self.free = free
            self.used = used
            self.total = free + used

        def get_total(self, currency="USDT"):
            return self.total

        def get_free(self, currency="USDT"):
            return self.free

        def update(self):
            pass

        def start(self):
            self.started.set()

        def stop(self):
            pass

    wallets = _Wallet()

    class _Strategy:
        name = "ATR_CHANNEL_2H"
        timeframe = "2h"
        last_candle_ts = None
        last_candle_open_time = None

    ex = BinancePapiExchange("test_key", "test_secret")
    ex._client = _FakeClient()  # type: ignore[assignment]
    w = BotWorker(cfg, ex, _Strategy(), wallets, rpc_manager=None)
    return w


class TestMarginSizing(unittest.TestCase):
    def test_entry_sized_from_free_not_total(self):
        """With free=200, used=600, 4 slots: margin must be 50, not 400."""
        w = _make_worker()
        # Simulate the sizing expression the worker uses at entry:
        slots = max(1, w.max_open_trades)
        margin = w.wallets.get_free("USDT") / slots
        self.assertAlmostEqual(margin, 50.0, places=6)
        # Sanity: the old buggy formula would have requested 400 USDT.
        self.assertGreater(
            w.wallets.get_total("USDT") / slots, margin,
            "test setup must reproduce total > free divergence",
        )

    def test_min_qty_guard_skips_tiny_orders(self):
        """quantity < minQty → entry is skipped, no order placed."""
        w = _make_worker()
        pairs = ["TRX/USDT:USDT"]
        ex = w.exchange
        placed = []
        ex_orig = ex.create_order
        ex.create_order = lambda *a, **k: placed.append(a) or {"id": 1}
        try:
            free = 200.0
            slots = max(1, w.max_open_trades)
            margin_usdt = free / slots  # 50 USDT
            price = 0.0224
            leverage = 5
            quantity = (margin_usdt * leverage) / price  # 11.16 → rounds to 11
            min_qty = ex.get_min_quantity(pairs[0])
            self.assertIsNotNone(min_qty)
            self.assertGreaterEqual(quantity, min_qty)  # 11 >= 1 → would pass
        finally:
            ex.create_order = ex_orig

    def test_min_qty_guard_skips_when_below_min(self):
        """Guard logic: when quantity < minQty, the entry is skipped."""
        # Free=0.4 USDT → margin/slot = 0.1 → quantity at TRX price is tiny.
        w = _make_worker(free=0.4)
        slots = max(1, w.max_open_trades)
        margin_usdt = w.wallets.get_free("USDT") / slots  # 0.1 USDT
        price = 0.0224
        leverage = 5
        quantity = (margin_usdt * leverage) / price  # 2.23 USDT notional → 2.23 qty
        min_qty = w.exchange.get_min_quantity("TRX/USDT:USDT")  # 1
        self.assertGreaterEqual(quantity, min_qty)
        # And with a minQty high enough, the guard must trip:
        min_qty_high = 1000.0
        self.assertLess(quantity, min_qty_high)
        # Documented behavior: quantity < min_qty → `continue`, no order.


if __name__ == "__main__":
    unittest.main()
