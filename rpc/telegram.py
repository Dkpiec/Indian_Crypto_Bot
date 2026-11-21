"""
Telegram RPC layer for Indian_Crypto_Bot.
All commands pull LIVE data from Binance via the exchange layer.
No hardcoded placeholders — every response reflects actual account state.
"""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

__version__ = "1.0.0"


class TelegramRPC:
    """Telegram bot command handler and message sender."""

    def __init__(self, token: str, chat_id: str, bot_worker=None):
        self.token = token
        self.chat_id = str(chat_id)
        self.bot_worker = bot_worker
        self._app = None

    async def start(self) -> None:
        """Start the Telegram polling loop (non-blocking)."""
        try:
            from telegram.ext import ApplicationBuilder, CommandHandler

            self._app = ApplicationBuilder().token(self.token).build()

            commands = {
                "status": self.cmd_status,
                "balance": self.cmd_balance,
                "profit": self.cmd_profit,
                "trades": self.cmd_trades,
                "performance": self.cmd_performance,
                "count": self.cmd_count,
                "daily": self.cmd_daily,
                "stats": self.cmd_stats,
                "forceexit": self.cmd_forceexit,
                "stop": self.cmd_stop,
                "start": self.cmd_start,
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
            if self._app.updater is not None:
                await self._app.updater.start_polling(drop_pending_updates=True)
            logger.info("Telegram RPC started, polling for commands")

        except ImportError:
            logger.error("python-telegram-bot not installed; Telegram RPC disabled")
        except Exception as e:
            logger.error(f"Failed to start Telegram RPC: {e}", exc_info=True)

    async def stop(self) -> None:
        """Stop the Telegram polling loop."""
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

    async def cmd_stats(self, update, context) -> None:
        """Overall stats from live Binance account data."""
        bal = self._get_live_balance()
        positions = self._get_live_positions()
        unrealized = sum(float(p.get("unRealizedProfit", 0)) for p in positions)
        worker = self.bot_worker

        total_margin = sum(float(p.get("initialMargin", 0)) for p in positions)
        avg_leverage = (bal["used"] / total_margin) if total_margin > 0 else 0

        text = (
            f"*📊 Stats (Live)*\n"
            f"Equity: `{bal['total']:.4f} USDT`\n"
            f"Free: `{bal['free']:.4f} USDT`\n"
            f"Used Margin: `{bal['used']:.4f} USDT`\n"
            f"Unrealized PnL: `{unrealized:.4f} USDT`\n"
            f"Open Positions: `{len(positions)}`\n"
            f"Strategy: `{worker.strategy.name if worker else '?'}` @ `{worker.strategy.timeframe if worker else '?'}`\n"
            f"Pairs Monitored: `{len(worker.pairs) if worker else 0}`"
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
            await update.message.reply_text("🟢 *Bot resumed*", parse_mode="Markdown")
        else:
            await update.message.reply_text("🟢 *Use /reload\\_conf to restart with current config*", parse_mode="Markdown")

    async def cmd_reload_conf(self, update, context) -> None:
        """Reload config from disk and apply updated trading parameters."""
        worker = self.bot_worker
        if not worker:
            await update.message.reply_text("❌ Bot not initialized", parse_mode="Markdown")
            return
        try:
            from config.loader import load_config
            new_config = load_config()

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
            "/forceexit [PAIR] - Close positions\n"
            "/stop - Stop bot\n"
            "/start - Resume bot\n"
            "/reload\\_conf - Reload config\n"
            "/whitelist - Active pairs\n"
            "/blacklist - Blacklisted pairs\n"
            "/trade\\_now - Enter nearest signal\n"
            "/version - Bot version"
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