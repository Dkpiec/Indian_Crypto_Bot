"""
Telegram RPC layer for Indian_Crypto_Bot.
All commands pull LIVE data from Binance via the exchange layer.
No hardcoded placeholders — every response reflects actual account state.
"""

import logging
import os
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

__version__ = "1.1.0"


class TelegramRPC:
    """Telegram bot command handler and message sender."""

    def __init__(self, token: str, chat_id: str, bot_worker=None):
        self.token = token
        self.chat_id = str(chat_id)
        self.bot_worker = bot_worker
        self._app = None
        # Set by the 409 conflict handler when THIS instance loses the token
        # race to a newer container. Created in start() (needs the running
        # event loop); None until start() runs. When set, the polling loop
        # stops so the newer instance holds the token cleanly.
        self._conflict_yield_event = None
        # Set by stop() so the conflict-watchdog's reclaim loop knows to
        # exit and stop re-arming polling.
        self._stop_requested = False
        # The worker thread and the Telegram event-loop thread both read
        # bot_worker and /reload_conf mutates worker.state and the strategy.
        # This lock serializes state mutation against in-flight ticks so a
        # /reload_conf cannot race a tick reading the old strategy.
        self._state_lock = threading.RLock()

    @property
    def state_lock(self) -> threading.RLock:
        """Shared lock around worker/strategy state. The worker's hot path
        and the mutating Telegram handlers must both hold this."""
        return self._state_lock

    async def start(self) -> None:
        """Start the Telegram polling loop (non-blocking)."""
        if not self.token or not self.token.strip():
            logger.warning("Telegram token is empty; RPC disabled")
            return
        try:
            from telegram.ext import ApplicationBuilder, CommandHandler

            self._app = ApplicationBuilder().token(self.token).build()

            # Register bot menu commands (visible at bottom of Telegram chat)
            menu_commands = [
                ("status", "Bot status + live balance"),
                ("balance", "Live wallet balance"),
                ("profit", "Unrealized PnL + positions"),
                ("trades", "Open positions detail"),
                ("performance", "Per-position ROI"),
                ("count", "Open trades count"),
                ("daily", "Daily summary"),
                ("stats", "Overall statistics"),
                ("forceexit", "Close positions"),
                ("stop", "Stop bot"),
                ("start", "Resume bot"),
                ("trade_now", "Enter nearest signal"),
            ]
            try:
                from telegram import BotCommand
                await self._app.bot.set_my_commands(
                    [BotCommand(cmd, desc) for cmd, desc in menu_commands]
                )
                logger.info(f"Registered {len(menu_commands)} bot menu commands")
            except Exception as e:
                logger.warning(f"Failed to set bot menu commands: {e}")

            commands = {
                "status": self.cmd_status,
                "stats": self.cmd_stats,
                "balance": self.cmd_balance,
                "profit": self.cmd_profit,
                "trades": self.cmd_trades,
                "trades_now": self.cmd_trades_now,
                "performance": self.cmd_performance,
                "count": self.cmd_count,
                "daily": self.cmd_daily,
                "forceexit": self.cmd_forceexit,
                "stop": self.cmd_stop,
                "start": self.cmd_start,
                "reset_breaker": self.cmd_reset_breaker,
                "reload_conf": self.cmd_reload_conf,
                "whitelist": self.cmd_whitelist,
                "blacklist": self.cmd_blacklist,
                "help": self.cmd_help,
                "version": self.cmd_version,
                "trade_now": self.cmd_trade_now,
            }

            for cmd_name, handler in commands.items():
                self._app.add_handler(CommandHandler(cmd_name, handler))

            await self._app.initialize()
            await self._app.start()

            # Guard against the "terminated by other getUpdates request"
            # Conflict: it means ANOTHER instance of this bot (an orphaned
            # Coolify container from a previous deploy that was never killed)
            # is polling the same token. Without handling it, python-
            # telegram-bot's network loop re-raises and logs an unbounded
            # ERROR traceback. Instead, back off quietly and let the newest
            # instance win the token.
            self._poll_backoff_until = 0.0
            # Count consecutive 409s: if this instance keeps losing the token
            # race (an older container is still polling the same token), it
            # must YIELD and stop polling rather than keep hammering getUpdates
            # for 5 minutes (the old backoff behaviour). Holding the token
            # from the losing side is what produces the 409 on the winning
            # (newer) container. Once this instance confirms it is the
            # loser, it stops its own polling loop so the winner holds the
            # token cleanly.
            self._consecutive_conflicts = 0
            # Track the first post-startup conflict so we can treat it as a
            # benign race (the older container finishing its graceful stop)
            # rather than a hard yield. The very first 409 after start is
            # retried silently; only repeated conflicts disable this instance.
            self._saw_first_conflict = False

            # Create the yield event BEFORE start_polling so the error
            # callback can safely set it on the very first 409 without a
            # race (the previous code assigned it after start_polling).
            import asyncio as _asyncio
            self._conflict_yield_event = _asyncio.Event()
            # Reclaim guard: after we stop polling on a 409, we must NOT
            # let a later reclaim attempt re-arm the stop path.
            self._reclaim_started = False

            # Startup pre-delay: before the first live poll, wait a short,
            # bounded window for any older container to finish its graceful
            # Telegram shutdown (the entrypoint gives the dying bot up to
            # ~20s to stop its RPC). This collapses the overlap window that
            # produces the single "initial" 409 on restart, so the new
            # instance usually wins the token on its first poll with no 409
            # logged at all. Configurable via TELEGRAM_STARTUP_DELAY_SECS.
            try:
                startup_delay = float(os.environ.get(
                    "TELEGRAM_STARTUP_DELAY_SECS", "12"))
            except (TypeError, ValueError):
                startup_delay = 12.0
            if startup_delay > 0:
                logger.info(
                    f"Telegram RPC: waiting {startup_delay:.0f}s before "
                    "first poll to let any older container release the token")
                await _asyncio.sleep(startup_delay)

            def _polling_error_callback(err) -> None:
                # PTB raises telegram.error.Conflict on a 409. Detect by
                # type name when available (robust across PTB builds where
                # the message text varies), else fall back to text.
                text = str(err)
                is_conflict = (
                    "Conflict" in type(err).__name__
                    or "Conflict" in text
                    or "terminated by other getUpdates" in text
                )
                if is_conflict:
                    self._consecutive_conflicts += 1
                    if not self._saw_first_conflict:
                        # The very first 409 after startup is almost always
                        # the benign overlap with an older container still
                        # finishing its graceful Telegram shutdown. Treat it
                        # as transient: log at INFO (not WARNING), let PTB's
                        # built-in poll retry re-poll shortly, and do NOT
                        # disable this instance. This avoids the scary
                        # 409 log on every restart.
                        self._saw_first_conflict = True
                        logger.info(
                            "Telegram: transient token conflict on first poll "
                            "(an older instance is releasing the token); "
                            "retrying — will keep this instance active.")
                        return
                    logger.warning(
                        "Telegram Conflict: another bot instance holds this "
                        "token (likely an orphaned container from a previous "
                        "deploy). This instance is yielding the token "
                        "(stopping its own polling) so the newest instance "
                        "wins cleanly."
                    )
                    # Yield: stop this instance's polling. The trading loop
                    # is independent of Telegram, so the bot keeps trading;
                    # only the command interface on THIS (losing) instance
                    # is disabled. The newer instance owns the token and
                    # serves commands.
                    try:
                        if self._conflict_yield_event is not None:
                            self._conflict_yield_event.set()
                    except Exception:
                        pass
                    return
                # A non-409 error means we were able to reach Telegram —
                # reset the consecutive conflict counter.
                self._consecutive_conflicts = 0
                logger.error(f"Telegram polling error: {err}")

            async def _start_polling_once() -> None:
                """Start the updater's polling loop (idempotent enough for
                re-arming after a 409 yield)."""
                app = self._app
                if app is None or app.updater is None:
                    return
                await app.updater.start_polling(
                    drop_pending_updates=False,  # keep the queue intact across reclaims
                    error_callback=_polling_error_callback,
                )

            await _start_polling_once()

            async def _conflict_watchdog() -> None:
                """When this instance loses the token (409), stop its own
                polling so the newer instance can own the token. The
                trading loop is unaffected — only this instance's command
                interface is disabled.

                After yielding, RE-CLAIM: if the other instance was just a
                short-lived orphan (a still-dying container from the
                previous deploy) and the token frees up, this instance
                restarts its polling so the bot keeps serving commands
                instead of staying permanently dead. Back off so we don't
                hammer getUpdates while the real winner is still up.
                """
                evt = self._conflict_yield_event
                if evt is None:
                    return
                while True:
                    try:
                        await evt.wait()
                    except Exception:
                        return
                    # We lost the token — stop polling so the newer instance
                    # can own it cleanly.
                    logger.info(
                        "Conflict watchdog: this instance lost the Telegram "
                        "token; stopping its polling loop so the newer "
                        "instance holds the token."
                    )
                    try:
                        app = self._app
                        if app and app.updater:
                            await app.updater.stop()
                    except Exception as e:
                        logger.warning(f"conflict watchdog: updater.stop() "
                                       f"failed: {e}")

                    # Reclaim loop: try to re-acquire the token with backoff.
                    # A 409 on the next getUpdates means someone else still
                    # holds it — back off and retry. A clean start means we
                    # won and resume serving commands.
                    self._reclaim_started = True
                    delay = 5.0
                    while True:
                        try:
                            await _asyncio.sleep(delay)
                        except _asyncio.CancelledError:
                            raise
                        if self._stop_requested:
                            return
                        try:
                            await _start_polling_once()
                        except Exception as e:
                            # A 409 during (re)start means another instance
                            # still owns the token. Keep retrying with a
                            # longer backoff; do not crash the loop.
                            logger.warning(
                                "Reclaim attempt failed (likely still "
                                f"conflicted): {e}. Retrying in {delay:.0f}s."
                            )
                            delay = min(delay * 2, 60.0)
                            continue
                        # Reclaim succeeded — we are the poller again.
                        logger.info(
                            "Conflict watchdog: re-acquired the Telegram "
                            "token; polling resumed."
                        )
                        # Reset the event so a FUTURE 409 re-triggers the
                        # yield path.
                        try:
                            evt.clear()
                        except Exception:
                            pass
                        break

            _asyncio.ensure_future(_conflict_watchdog())

            logger.info("Telegram RPC started, polling for commands")

        except ImportError:
            logger.error("python-telegram-bot not installed; Telegram RPC disabled")
        except Exception as e:
            logger.error(f"Failed to start Telegram RPC: {e}", exc_info=True)

    async def stop(self) -> None:
        """Stop the Telegram polling loop."""
        # Tell the conflict-watchdog to stop reclaiming the token.
        self._stop_requested = True
        if self._app is not None:
            try:
                await self._app.stop()
                await self._app.shutdown()
                logger.info("Telegram RPC stopped")
            except Exception as e:
                logger.error(f"Error stopping Telegram RPC: {e}")

    def send_message(self, text: str) -> None:
        """Send a message to the configured chat (sync wrapper for non-async contexts)."""
        import requests
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if not resp.json().get("ok"):
                logger.warning(f"Telegram send failed: {resp.text[:200]}")
        except Exception as e:
            logger.error(f"Telegram send error: {e}")

    # ── Live Data Helpers ──

    def _get_live_positions(self) -> list[dict]:
        """Fetch all open positions from Binance with non-zero size."""
        worker = self.bot_worker
        if not worker:
            return []
        try:
            raw = worker.exchange.fetch_positions()
            return [
                p for p in raw
                if abs(float(p.get("positionAmt", 0))) > 0
            ]
        except Exception as e:
            logger.error(f"Failed to fetch positions: {e}")
            return []

    def _get_live_balance(self) -> dict:
        """Fetch fresh balance from Binance."""
        worker = self.bot_worker
        if not worker:
            return {"free": 0, "used": 0, "total": 0}
        try:
            worker.wallets.update()
            return {
                "free": worker.wallets.get_free("USDT"),
                "used": worker.wallets.get_used("USDT"),
                "total": worker.wallets.get_total("USDT"),
            }
        except Exception as e:
            logger.error(f"Failed to fetch balance: {e}")
            return {"free": 0, "used": 0, "total": 0}

    # ── Command Handlers (all pull live Binance data) ──

    async def cmd_status(self, update, context) -> None:
        worker = self.bot_worker
        if not worker:
            await update.message.reply_text("❌ Bot not initialized", parse_mode="Markdown")
            return

        bal = self._get_live_balance()
        positions = self._get_live_positions()
        pairs_preview = ", ".join(worker.pairs[:5])
        if len(worker.pairs) > 5:
            pairs_preview += f" (+{len(worker.pairs)-5} more)"

        trading = worker.config.get("trading", {})
        leverage = trading.get("leverage", 5)
        use_full = trading.get("use_full_balance", False)
        margin_info = f"Full Balance ({bal['total']:.2f} USDT)" if use_full else f"{trading.get('margin_per_trade', 20.0)} USDT"

        text = (
            f"*🤖 Bot Status*\n"
            f"State: `{worker.state}`\n"
            f"Strategy: `{worker.strategy.name}` @ `{worker.strategy.timeframe}`\n"
            f"Pairs ({len(worker.pairs)}): `{pairs_preview}`\n"
            f"Leverage: `{leverage}x` | Margin: `{margin_info}`\n"
            f"Balance: `{bal['total']:.4f} USDT`\n"
            f"Open Positions: `{len(positions)}`\n"
            f"Dry Run: `{worker.config.get('dry_run', False)}`"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_balance(self, update, context) -> None:
        bal = self._get_live_balance()
        positions = self._get_live_positions()
        unrealized = sum(float(p.get("unRealizedProfit", 0)) for p in positions)

        text = (
            f"*💰 Wallet Balance (Live)*\n"
            f"Total Equity: `{bal['total']:.4f} USDT`\n"
            f"Free: `{bal['free']:.4f} USDT`\n"
            f"Used (Margin): `{bal['used']:.4f} USDT`\n"
            f"Unrealized PnL: `{unrealized:.4f} USDT`"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_profit(self, update, context) -> None:
        positions = self._get_live_positions()
        bal = self._get_live_balance()

        unrealized = sum(float(p.get("unRealizedProfit", 0)) for p in positions)
        realized_lines = []
        for p in positions:
            symbol = p.get("symbol", "?")
            amt = float(p.get("positionAmt", 0))
            entry = float(p.get("entryPrice", 0))
            upnl = float(p.get("unRealizedProfit", 0))
            side = "LONG" if amt > 0 else "SHORT"
            realized_lines.append(
                f"• `{symbol}` {side} {abs(amt):.2f} @ `{entry:.6f}` → UPnL `{upnl:.4f}`"
            )

        if not realized_lines:
            pos_text = "No open positions"
        else:
            pos_text = "\n".join(realized_lines)

        text = (
            f"*📊 Profit (Live)*\n"
            f"Unrealized PnL: `{unrealized:.4f} USDT`\n"
            f"Equity: `{bal['total']:.4f} USDT`\n\n"
            f"*Open Positions:*\n{pos_text}"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_trades(self, update, context) -> None:
        """Show current open positions from Binance."""
        positions = self._get_live_positions()
        if not positions:
            await update.message.reply_text("*📋 Trades:* No open positions", parse_mode="Markdown")
            return

        lines = []
        for p in positions:
            symbol = p.get("symbol", "?")
            amt = float(p.get("positionAmt", 0))
            entry = float(p.get("entryPrice", 0))
            mark = float(p.get("markPrice", 0))
            upnl = float(p.get("unRealizedProfit", 0))
            lev = float(p.get("leverage", 0))
            side = "🟢 LONG" if amt > 0 else "🔴 SHORT"
            lines.append(
                f"{side} `{symbol}`\n"
                f"  Qty: `{abs(amt):.4f}` | Lev: `{lev:.0f}x`\n"
                f"  Entry: `{entry:.6f}` | Mark: `{mark:.6f}`\n"
                f"  UPnL: `{upnl:.4f} USDT`"
            )

        text = f"*📋 Open Trades ({len(positions)})*\n\n" + "\n\n".join(lines)
        await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_performance(self, update, context) -> None:
        """Per-position performance from live Binance data."""
        positions = self._get_live_positions()
        if not positions:
            await update.message.reply_text("*📈 Performance:* No open positions to analyze", parse_mode="Markdown")
            return

        lines = []
        for p in positions:
            symbol = p.get("symbol", "?")
            amt = float(p.get("positionAmt", 0))
            entry = float(p.get("entryPrice", 0))
            mark = float(p.get("markPrice", 0))
            upnl = float(p.get("unRealizedProfit", 0))
            roi_pct = ((mark - entry) / entry * 100) if entry > 0 else 0
            if amt < 0:
                roi_pct = -roi_pct
            emoji = "✅" if upnl >= 0 else "❌"
            lines.append(f"{emoji} `{symbol}` ROI: `{roi_pct:+.2f}%` | UPnL: `{upnl:.4f} USDT`")

        text = f"*📈 Performance (Live)*\n\n" + "\n".join(lines)
        await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_count(self, update, context) -> None:
        """Count of open positions from live Binance data."""
        positions = self._get_live_positions()
        worker = self.bot_worker
        max_trades = worker.strategy.max_open_trades if worker else "?"

        lines = []
        for p in positions:
            symbol = p.get("symbol", "?")
            amt = float(p.get("positionAmt", 0))
            side = "LONG" if amt > 0 else "SHORT"
            lines.append(f"• `{symbol}` {side} {abs(amt):.4f}")

        pos_detail = "\n".join(lines) if lines else "None"
        text = (
            f"*🔢 Open Trades Count*\n"
            f"Open: `{len(positions)} / {max_trades}`\n\n"
            f"{pos_detail}"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_daily(self, update, context) -> None:
        """Daily PnL approximated from unrealized profit on open positions."""
        positions = self._get_live_positions()
        bal = self._get_live_balance()
        unrealized = sum(float(p.get("unRealizedProfit", 0)) for p in positions)

        text = (
            f"*📅 Daily Summary (Live)*\n"
            f"Unrealized PnL: `{unrealized:.4f} USDT`\n"
            f"Current Equity: `{bal['total']:.4f} USDT`\n"
            f"Free Balance: `{bal['free']:.4f} USDT`\n"
            f"Open Positions: `{len(positions)}`\n"
            f"_Note: Full daily PnL requires trade history tracking (coming in v1.1)_"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_trades_now(self, update, context) -> None:
        """Show live open positions in a compact table format — instant snapshot."""
        positions = self._get_live_positions()
        bal = self._get_live_balance()
        if not positions:
            await update.message.reply_text(
                f"*📋 Trades Now:* No open positions\nEquity: `{bal['total']:.4f} USDT`",
                parse_mode="Markdown",
            )
            return

        # Compact table: Symbol | Side | Qty | Entry | Mark | UPnL | ROI%
        rows = []
        for p in positions:
            symbol = p.get("symbol", "?")
            amt = float(p.get("positionAmt", 0))
            entry = float(p.get("entryPrice", 0))
            mark = float(p.get("markPrice", 0))
            upnl = float(p.get("unRealizedProfit", 0))
            lev = float(p.get("leverage", 0))
            side = "L" if amt > 0 else "S"
            roi = ((mark - entry) / entry * 100) if entry > 0 else 0
            if amt < 0:
                roi = -roi
            emoji = "🟢" if upnl >= 0 else "🔴"
            rows.append(
                f"{emoji} `{symbol}` {side} {abs(amt):.2f} @ `{entry:.6f}` → `{mark:.6f}` | `{upnl:+.4f}` ({roi:+.2f}%) `{lev:.0f}x`"
            )

        total_upnl = sum(float(p.get("unRealizedProfit", 0)) for p in positions)
        text = (
            f"*📋 Trades Now ({len(positions)} open)*\n"
            f"Total UPnL: `{total_upnl:+.4f} USDT`\n"
            f"Equity: `{bal['total']:.4f} USDT`\n\n"
            + "\n".join(rows)
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_stats(self, update, context) -> None:
        """Detailed stats table with per-position metrics, margin usage, and strategy info."""
        bal = self._get_live_balance()
        positions = self._get_live_positions()
        worker = self.bot_worker
        unrealized = sum(float(p.get("unRealizedProfit", 0)) for p in positions)

        # Per-position table with margin, notional, ROI
        lines = []
        for p in positions:
            symbol = p.get("symbol", "?")
            amt = float(p.get("positionAmt", 0))
            entry = float(p.get("entryPrice", 0))
            mark = float(p.get("markPrice", 0))
            upnl = float(p.get("unRealizedProfit", 0))
            lev = float(p.get("leverage", 0))
            init_margin = float(p.get("initialMargin", 0))
            notional = abs(amt) * mark
            roi = ((mark - entry) / entry * 100) if entry > 0 else 0
            if amt < 0:
                roi = -roi
            side = "LONG" if amt > 0 else "SHORT"
            lines.append(
                f"{side:5s} `{symbol}` | Qty `{abs(amt):.4f}` | Lev `{lev:.0f}x` | "
                f"Entry `{entry:.6f}` Mark `{mark:.6f}` | "
                f"UPnL `{upnl:+.4f}` ({roi:+.2f}%) | Margin `{init_margin:.4f}` | Notional `{notional:.2f}`"
            )

        total_margin = sum(float(p.get("initialMargin", 0)) for p in positions)
        total_notional = sum(abs(float(p.get("positionAmt", 0))) * float(p.get("markPrice", 0)) for p in positions)
        margin_pct = (total_margin / bal["total"] * 100) if bal["total"] > 0 else 0

        text = (
            f"*📊 Stats Table (Live)*\n\n"
            f"*Account Summary:*\n"
            f"• Equity: `{bal['total']:.4f} USDT`\n"
            f"• Free: `{bal['free']:.4f} USDT` | Used Margin: `{bal['used']:.4f} USDT`\n"
            f"• Total UPnL: `{unrealized:+.4f} USDT`\n"
            f"• Total Notional: `{total_notional:.2f} USDT`\n"
            f"• Margin Used: `{total_margin:.4f} USDT` ({margin_pct:.1f}% of equity)\n\n"
            f"*Strategy:*\n"
            f"• `{worker.strategy.name if worker else '?'}` @ `{worker.strategy.timeframe if worker else '?'}`\n"
            f"• Pairs: `{len(worker.pairs) if worker else 0}` | Dry Run: `{worker.config.get('dry_run', False) if worker else '?'}`\n\n"
            f"*Positions ({len(positions)}):*\n"
            + ("\n".join(lines) if lines else "  None open")
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_forceexit(self, update, context) -> None:
        """Force-close all open positions or a specific pair."""
        worker = self.bot_worker
        if not worker:
            await update.message.reply_text("❌ Bot not initialized", parse_mode="Markdown")
            return

        args = context.args if context.args else []
        target_symbol = args[0].upper() if args else None

        positions = self._get_live_positions()
        if not positions:
            await update.message.reply_text("*Force Exit:* No open positions to close", parse_mode="Markdown")
            return

        closed = []
        errors = []
        for p in positions:
            symbol = p.get("symbol", "")
            amt = float(p.get("positionAmt", 0))
            if amt == 0:
                continue
            if target_symbol and symbol != target_symbol:
                continue

            side = "SELL" if amt > 0 else "BUY"
            qty = abs(amt)
            try:
                order = worker.exchange.create_order(
                    symbol=symbol,
                    order_type="MARKET",
                    side=side,
                    amount=qty,
                    reduce_only=True,
                )
                closed.append(f"✅ `{symbol}` {side} {qty:.4f}")
                logger.info(f"Force exit: {symbol} {side} {qty:.4f}")
            except Exception as e:
                errors.append(f"❌ `{symbol}`: {str(e)[:100]}")
                logger.error(f"Force exit failed {symbol}: {e}")

        result_lines = closed + errors
        text = f"*🚪 Force Exit Results*\n\n" + "\n".join(result_lines)
        await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_stop(self, update, context) -> None:
        worker = self.bot_worker
        if worker:
            worker.stop()
        await update.message.reply_text("🔴 *Bot stopping...*", parse_mode="Markdown")

    async def cmd_start(self, update, context) -> None:
        worker = self.bot_worker
        if worker and worker.state == "STOPPED":
            worker.state = "RUNNING"
            worker._running = True
            # Reset the backoff so the resumed loop starts at full speed
            worker._error_backoff = 0
            await update.message.reply_text("🟢 *Bot resumed*", parse_mode="Markdown")
        else:
            await update.message.reply_text("🟢 *Use /reload\\_conf to restart with current config*", parse_mode="Markdown")

    async def cmd_reset_breaker(self, update, context) -> None:
        """Manually clear the risk circuit breaker (3 consecutive losses /
        daily loss cap) without waiting for UTC midnight."""
        worker = self.bot_worker
        if not worker:
            await update.message.reply_text("❌ Bot not initialized", parse_mode="Markdown")
            return
        worker.risk_manager.reset_circuit_breaker()
        status = worker.risk_manager.status
        await update.message.reply_text(
            f"✅ *Circuit breaker reset*\n"
            f"daily_pnl: `{status['daily_pnl']}`\n"
            f"consec_losses: `{status['consecutive_losses']}`\n"
            f"breaker: `{status['circuit_breaker_active']}`",
            parse_mode="Markdown",
        )

    async def cmd_reload_conf(self, update, context) -> None:
        """Reload config from disk and apply updated trading parameters.

        Swaps the worker's config/strategy references atomically under
        ``state_lock`` so an in-flight tick cannot read half-swapped state.
        """
        worker = self.bot_worker
        if not worker:
            await update.message.reply_text("❌ Bot not initialized", parse_mode="Markdown")
            return
        try:
            from config.loader import load_config
            new_config = load_config()

            # Swap everything under the shared lock so a concurrent tick
            # cannot observe a partially-applied reload.
            with self.state_lock:
                trading = new_config.get("trading", {})
                old_margin = worker.config.get("trading", {}).get("margin_per_trade", 20.0)
                old_leverage = worker.config.get("trading", {}).get("leverage", 5)
                new_margin = trading.get("margin_per_trade", old_margin)
                new_leverage = trading.get("leverage", old_leverage)
                use_full_balance = trading.get("use_full_balance", False)

                old_pairs = list(worker.pairs)
                new_pairlists = new_config.get("pairlists", [])
                new_pairs = []
                for pl in new_pairlists:
                    new_pairs.extend(pl.get("pairs", []))
                if new_pairs:
                    worker.pairs = new_pairs

                strat_conf = new_config.get("strategy", {})
                for key in ("timeframe", "max_open_trades", "stake_currency", "stoploss"):
                    if key in strat_conf:
                        setattr(worker.strategy, key, strat_conf[key])

                worker.config = new_config
                # Clear dedup marks on new pairs — a reloaded universe
                # should not inherit stale dedup state.
                worker._last_entry_candle_ms.clear()
                worker._last_exit_candle_ms.clear()

                changes = []
                if new_margin != old_margin:
                    changes.append(f"margin: {old_margin}→{new_margin}")
                if new_leverage != old_leverage:
                    changes.append(f"leverage: {old_leverage}→{new_leverage}")
                if new_pairs != old_pairs:
                    changes.append(f"pairs: {len(old_pairs)}→{len(new_pairs)}")

                summary = ", ".join(changes) if changes else "no changes detected"
                pairs_preview = ", ".join(worker.pairs[:5])
                if len(worker.pairs) > 5:
                    pairs_preview += f" (+{len(worker.pairs)-5} more)"

                if use_full_balance:
                    bal = self._get_live_balance()
                    margin_display = f"Full Balance ({bal['total']:.2f} USDT)"
                else:
                    margin_display = f"{new_margin} USDT"

            text = (
                f"✅ *Config Reloaded*\n"
                f"Changes: `{summary}`\n"
                f"Margin: `{margin_display}`\n"
                f"Leverage: `{new_leverage}x`\n"
                f"Full Balance Mode: `{use_full_balance}`\n"
                f"Pairs ({len(worker.pairs)}): `{pairs_preview}`\n"
                f"Strategy: `{worker.strategy.name}` @ `{worker.strategy.timeframe}`"
            )
            logger.info(f"Config reloaded: {summary}")
            await update.message.reply_text(text, parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Config reload failed: {e}", exc_info=True)
            await update.message.reply_text(f"❌ *Config reload failed:*\n`{str(e)[:300]}`", parse_mode="Markdown")

    async def cmd_whitelist(self, update, context) -> None:
        worker = self.bot_worker
        pairs = "\n".join(f"• `{p}`" for p in (worker.pairs if worker else []))
        await update.message.reply_text(f"*Whitelist ({len(worker.pairs) if worker else 0}):*\n{pairs}", parse_mode="Markdown")

    async def cmd_blacklist(self, update, context) -> None:
        await update.message.reply_text("*Blacklist:* Empty", parse_mode="Markdown")

    async def cmd_help(self, update, context) -> None:
        text = (
            "*Available Commands:*\n"
            "/status - Bot status + live balance\n"
            "/balance - Live wallet balance\n"
            "/profit - Unrealized PnL + positions\n"
            "/trades - Open positions detail\n"
            "/performance - Per-position ROI\n"
            "/count - Open trades count\n"
            "/daily - Daily summary\n"
            "/stats - Overall statistics\n"
            "/forceexit PAIR - Close positions\n"
            "/stop - Stop bot\n"
            "/start - Resume bot\n"
            "/trade\\_now - Enter nearest signal"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_trade_now(self, update, context) -> None:
        """Manually enter on the most recent entry signal across all pairs."""
        worker = self.bot_worker
        if not worker:
            await update.message.reply_text("❌ Bot not initialized", parse_mode="Markdown")
            return
        await update.message.reply_text("🔍 *Scanning all pairs for nearest entry signal...*", parse_mode="Markdown")
        try:
            result = worker.trade_now()
            if result["success"]:
                text = (
                    f"📈 *TRADE NOW EXECUTED*\n"
                    f"Pair: `{result['pair']}`\n"
                    f"Price: `{result['price']:.6f}`\n"
                    f"Qty: `{result['quantity']:.4f}`\n"
                    f"Margin: `{result['margin_usdt']:.2f} USDT`\n"
                    f"Leverage: `{result['leverage']}x`\n"
                    f"Signal: `{result['tag']}`"
                )
                await update.message.reply_text(text, parse_mode="Markdown")
            else:
                pair_info = f" (`{result.get('pair', '')}`)" if result.get("pair") else ""
                await update.message.reply_text(
                    f"⚠️ *Trade Now Failed*{pair_info}\n`{result['reason'][:300]}`",
                    parse_mode="Markdown",
                )
        except Exception as e:
            logger.error(f"/trade_now failed: {e}", exc_info=True)
            await update.message.reply_text(f"❌ *Error:* `{str(e)[:300]}`", parse_mode="Markdown")

    async def cmd_version(self, update, context) -> None:
        await update.message.reply_text(f"*Indian Crypto Bot* v{__version__}", parse_mode="Markdown")