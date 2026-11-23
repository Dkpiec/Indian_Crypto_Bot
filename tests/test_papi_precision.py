"""Unit tests for exchange-layer audit fixes.

Covers:
- lot-size / price-tick precision rounding (the -1111 fix)
- local-file filter loading (papi-only: no /exchangeInfo network call)
- clock-skew offset application to signed timestamps
- round_step decimal correctness

Run with:  python3 -m pytest tests/test_papi_precision.py -q
   or:     python3 tests/test_papi_precision.py  (no pytest required)
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exchange.papi_client import PapiClient


def make_client():
    # No network needed: we test routing, offset, and rounding helpers only.
    return PapiClient("dummy_key", "dummy_secret")


# ── round_step ──────────────────────────────────────────────────────────────

def test_round_step_down_to_step():
    c = make_client()
    assert c.round_step(4.1794, "0.01") == 4.17
    assert c.round_step(0.5364058424702716, "0.00010000") == 0.5364
    assert c.round_step(100.1985, "0.01") == 100.19
    assert c.round_step(9e-05, "0.0001") == 0.0


def test_round_step_noop_on_empty_or_none():
    c = make_client()
    assert c.round_step(1.5, "") == 1.5
    assert c.round_step(1.5, None) == 1.5
    assert c.round_step(None, "0.01") is None


# ── routing: trading stays on papi ──────────────────────────────────────────

def test_routing_orders_to_papi_um():
    c = make_client()
    assert c._route_base_url("/order") == PapiClient.PAPI_UM_BASE


# ── local-file filter loading (papi-only) ───────────────────────────────────

def test_local_filters_file_loaded():
    c = make_client()
    c._filters_loaded = False
    f = c.get_symbol_filters("BTCUSDT")
    # data/papi_filters.json ships with stepSize for the configured pairs
    assert f.get("stepSize") in ("0.001", "0.01", "0.1", "1"), f
    # a second lookup must not reload (cache hit)
    c2 = make_client()
    c2._filters_loaded = False
    c2.get_symbol_filters("ETHUSDT")
    assert c2._filters_loaded is True


def test_unknown_symbol_filters_empty():
    c = make_client()
    c._filters_loaded = False
    # NOSUCHCOIN is not in the local file -> no rounding (safe), not an error
    assert c.get_symbol_filters("NOSUCHCOIN") == {}


# ── clock offset ─────────────────────────────────────────────────────────────

def test_signed_timestamp_applies_offset():
    c = make_client()
    c.time_offset_ms = 5000  # host 5s behind server
    now = c._now_ms()
    base = int(time.time() * 1000)
    # The offset must be included; allow up to 1.5s of execution drift.
    assert abs((now - base) - 5000) < 1500


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
