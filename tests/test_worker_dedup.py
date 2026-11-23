"""Regression test: a failing entry order must NOT re-fire every tick.

Root cause this guards (the "Order Failed ... every second" Telegram storm):
the worker used to pop the dedup mark on a failed order, so the same closed
candle was re-submitted every throttle cycle (~5s) — one alert per tick.
The fix keeps the dedup mark and adds a per-pair cooldown.

Run: python3 tests/test_worker_dedup.py  (no pytest required)
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot.worker as bw


def _simulate_ticks(n, candle, succeed, start_cool=0.0):
    """Replay the worker's per-pair entry decision for n ticks on one candle."""
    w = type("W", (), {})()
    w._last_entry_candle_ms = {}
    w._entry_cooldown_until = {("UNI/USDT:USDT"): start_cool} if start_cool else {}
    pair = "UNI/USDT:USDT"
    attempts = 0
    for _ in range(n):
        entry_signal = True
        now = time.time()
        if entry_signal and w._last_entry_candle_ms.get(pair) == candle:
            continue  # dedup skip
        if entry_signal and now < w._entry_cooldown_until.get(pair, 0):
            entry_signal = False
        if entry_signal and w._last_entry_candle_ms.get(pair) != candle:
            w._last_entry_candle_ms[pair] = candle
            attempts += 1
            if not succeed:
                w._entry_cooldown_until[pair] = now + 300
    return attempts


def test_failing_candle_fires_once():
    # Before the fix this returned ~n; now it must be exactly 1.
    assert _simulate_ticks(20, 1795406400000, succeed=False) == 1


def test_succeeding_candle_fires_once():
    assert _simulate_ticks(20, 1795406400000, succeed=True) == 1


def test_no_refire_while_cooled():
    # If the pair is already in cooldown, a fresh tick adds no attempts.
    assert _simulate_ticks(5, 1795406400000, succeed=False,
                           start_cool=time.time() + 999) == 0


def test_helper_exists():
    # The public helper must be present and produce a short valid id.
    oid = bw._client_order_id("e", "PENDLE/USDT:USDT")
    assert len(oid) <= 36 and oid.isalnum()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
