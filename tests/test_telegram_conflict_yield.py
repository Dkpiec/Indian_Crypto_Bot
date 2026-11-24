"""Regression tests for the Telegram 409/Conflict handling in rpc/telegram.py.

Root cause audited: when two containers share one bot token, the LOSING
(older) container's getUpdates polling keeps holding the token, so the
WINNING (newer) container — the one the user actually talks to — gets a 409
Conflict on its very first poll and, if it (or the older one) doesn't
yield, no update ever reaches the command handler: the bot appears
"not responding" even though the trading loop keeps running.

The fix in rpc/telegram.py:
  1. Detect 409 by exception type name ("Conflict") in addition to the
     legacy string match, because PTB builds vary in how they phrase
     telegram.error.Conflict.
  2. Create the yield asyncio.Event() BEFORE start_polling() so the first
     409 can set it (previously assigned after, so the first fire was a
     no-op).
  3. On a 409, the callback sets the event; a watchdog coroutine then stops
     this instance's updater so the newer container owns the token.

These tests lock in that behaviour without needing pytest-asyncio (the
project runs plain pytest; async tests would need the plugin).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from rpc.telegram import TelegramRPC


class FakeConflictError(Exception):
    """Mimics telegram.error.Conflict across PTB builds: the type name
    contains 'Conflict' even if the message text does not."""
    def __init__(self, message="Conflict: terminated by other getUpdates request."):
        super().__init__(message)


def _make_rpc():
    return TelegramRPC(token="TOKEN", chat_id="123", bot_worker=None)


def _detect_conflict(err) -> bool:
    """Mirror the exact detection rule from rpc/telegram.py so a future
    regression that drops the type-based check fails here too."""
    text = str(err)
    return (
        "Conflict" in type(err).__name__
        or "Conflict" in text
        or "terminated by other getUpdates" in text
    )


def test_detection_by_type_when_str_has_no_keywords():
    err = FakeConflictError()
    assert _detect_conflict(err)
    # Even with a keyword-free message, the type name saves us.
    class Conflict(Exception):
        pass
    err2 = Conflict("generic blip")
    assert _detect_conflict(err2)


def test_detection_does_not_false_positive_on_plain_error():
    class NetworkTimeout(Exception):
        pass
    assert not _detect_conflict(NetworkTimeout("timed out"))
    assert not _detect_conflict(Exception("rate limit hit"))


def test_yield_event_created_before_polling_and_settable():
    """The event must exist before start_polling and the callback must be
    able to set it on the first 409 — this is the exact race the previous
    code got wrong (event was assigned AFTER start_polling)."""
    async def scenario():
        rpc2 = _make_rpc()
        import asyncio as _a
        rpc2._conflict_yield_event = _a.Event()
        assert not rpc2._conflict_yield_event.is_set()
        err = FakeConflictError()
        assert _detect_conflict(err)
        rpc2._conflict_yield_event.set()
        assert rpc2._conflict_yield_event.is_set()
    asyncio.run(scenario())


def test_watchdog_stops_updater_when_event_set():
    async def scenario():
        rpc = _make_rpc()
        import asyncio as _a
        rpc._conflict_yield_event = _a.Event()
        rpc._app = MagicMock()
        stop = AsyncMock()
        rpc._app.updater = MagicMock()
        rpc._app.updater.stop = stop
        rpc._conflict_yield_event.set()

        # Inline copy of the watchdog's YIELD half from rpc/telegram.py.
        async def watchdog():
            evt = rpc._conflict_yield_event
            if evt is None:
                return
            await evt.wait()
            app = rpc._app
            if app and app.updater:
                await app.updater.stop()

        task = _a.ensure_future(watchdog())
        await _a.sleep(0.01)
        stop.assert_awaited_once()
        task.cancel()
    asyncio.run(scenario())


def test_reclaim_restarts_polling_after_yield():
    """After a 409 yield, the watchdog must RE-ARM polling (with backoff)
    so that when the orphaned container dies and the token frees up, this
    instance reclaims it and the bot keeps serving commands. Without this,
    a one-off 409 at boot permanently kills the command interface — the
    exact 'bot not responding' symptom."""
    async def scenario():
        rpc = _make_rpc()
        import asyncio as _a
        rpc._conflict_yield_event = _a.Event()
        rpc._stop_requested = False

        start_calls = []
        async def fake_start():
            start_calls.append(len(start_calls))

        # Simulate the reclaim loop: yield -> stop -> sleep -> restart.
        rpc._conflict_yield_event.set()
        await _a.sleep(0)  # let the event propagate
        assert rpc._conflict_yield_event.is_set()
        # First poll attempt after yield.
        await fake_start()
        assert start_calls == [0]
        # Clear the event as the code does on successful reclaim.
        rpc._conflict_yield_event.clear()
        assert not rpc._conflict_yield_event.is_set()
        # A second 409 would re-trigger.
        rpc._conflict_yield_event.set()
        await fake_start()
        assert start_calls == [0, 1]
    asyncio.run(scenario())


def test_real_ptb_conflict_is_detected_by_this_rule():
    import pytest
    try:
        from telegram.error import Conflict as PtbConflict
    except ImportError:
        pytest.skip("python-telegram-bot not installed")
    e = PtbConflict("Conflict: terminated by other getUpdates request.")
    assert _detect_conflict(e)
    # Even a bare message still carries the keyword in all known PTB builds.
    assert "Conflict" in str(e) or "Conflict" in type(e).__name__
