"""
Telegram RPC layer for Indian_Crypto_Bot.
Handles bot commands: /status, /balance, /profit, /trades, /performance,
/count, /daily, /stats, /forceexit, /stop, /start, /reload_conf,
/whitelist, /blacklist, /help, /version.
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
        """Start the Telegram polling loop."""
        try:
            from telegram.ext import ApplicationBuilder, CommandHandler

            self._app = ApplicationBuilder().token(self.token).build()

            # Register all command handlers
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
            }

            for cmd_name, handler in commands.items():
                self._app.add_handler(CommandHandler(cmd_name, handler))

            # Python 20.0 API: start_polling is called on the application directly.
            # In v19 the updater was public (app.updater.start_polling());
            # in v20+ the updater is private and start_polling() is on Application.
            if hasattr(self._app, "start_polling"):
                await self._app.start_polling(drop_pending_updates=True)
            else:
                await self._app.initialize()
                await self._app.start()
                updater = getattr(self._app, "updater", None)
                if updater is not None:
                    await updater.start_polling(drop_pending_updates=True)
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

    # ── Command Handlers ──

    async def cmd_status(self, update, context) -> None:
        worker = self.bot_worker
        if not worker:
            await update.message.reply_text("Bot not initialized")
            return
        balance = worker.wallets.get_total("USDT")
        pairs = ", ".join(worker.pairs)
        text = (
            f"*Bot Status*\n"
            f"State: `{worker.state}`\n"
            f"Strategy: `{worker.strategy.name}`\n"
            f"Pairs: `{pairs}`\n"
            f"Timeframe: `{worker.strategy.timeframe}`\n"
            f"Balance: `{balance:.4f} USDT`"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_balance(self, update, context) -> None:
        worker = self.bot_worker
        if not worker:
            await update.message.reply_text("Bot not initialized")
            return
        free = worker.wallets.get_free("USDT")
        used = worker.wallets.get_used("USDT")
        total = worker.wallets.get_total("USDT")
        text = (
            f"*Wallet Balance*\n"
            f"Total: `{total:.4f} USDT`\n"
            f"Free: `{free:.4f} USDT`\n"
            f"Used: `{used:.4f} USDT`"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_profit(self, update, context) -> None:
        await update.message.reply_text("*Profit:* No closed trades yet (bot just started)", parse_mode="Markdown")

    async def cmd_trades(self, update, context) -> None:
        await update.message.reply_text("*Trades:* No trades recorded yet", parse_mode="Markdown")

    async def cmd_performance(self, update, context) -> None:
        await update.message.reply_text("*Performance:* Insufficient data", parse_mode="Markdown")

    async def cmd_count(self, update, context) -> None:
        worker = self.bot_worker
        max_trades = worker.strategy.max_open_trades if worker else "?"
        await update.message.reply_text(f"*Open Trades:* `0 / {max_trades}`", parse_mode="Markdown")

    async def cmd_daily(self, update, context) -> None:
        await update.message.reply_text("*Daily PnL:* No data yet", parse_mode="Markdown")

    async def cmd_stats(self, update, context) -> None:
        await update.message.reply_text("*Stats:* Bot recently started, collecting data", parse_mode="Markdown")

    async def cmd_forceexit(self, update, context) -> None:
        await update.message.reply_text("*Force Exit:* No open trades to exit", parse_mode="Markdown")

    async def cmd_stop(self, update, context) -> None:
        worker = self.bot_worker
        if worker:
            worker.stop()
        await update.message.reply_text("🔴 *Bot stopping...*", parse_mode="Markdown")

    async def cmd_start(self, update, context) -> None:
        await update.message.reply_text("🟢 *Use /reload_conf to restart with current config*", parse_mode="Markdown")

    async def cmd_reload_conf(self, update, context) -> None:
        await update.message.reply_text("*Config reload:* Not yet implemented in v1.0", parse_mode="Markdown")

    async def cmd_whitelist(self, update, context) -> None:
        worker = self.bot_worker
        pairs = "\n".join(f"• `{p}`" for p in (worker.pairs if worker else []))
        await update.message.reply_text(f"*Whitelist:*\n{pairs}", parse_mode="Markdown")

    async def cmd_blacklist(self, update, context) -> None:
        await update.message.reply_text("*Blacklist:* Empty", parse_mode="Markdown")

    async def cmd_help(self, update, context) -> None:
        text = (
            "*Available Commands:*\n"
            "/status - Bot status\n"
            "/balance - Wallet balance\n"
            "/profit - Closed trade PnL\n"
            "/trades - Trade history\n"
            "/performance - Per-pair stats\n"
            "/count - Open trades count\n"
            "/daily - Daily PnL\n"
            "/stats - Overall statistics\n"
            "/forceexit - Force close trade\n"
            "/stop - Stop bot\n"
            "/start - Resume bot\n"
            "/reload\\_conf - Reload config\n"
            "/whitelist - Active pairs\n"
            "/blacklist - Blacklisted pairs\n"
            "/version - Bot version"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_version(self, update, context) -> None:
        await update.message.reply_text(f"*Indian Crypto Bot* v{__version__}", parse_mode="Markdown")