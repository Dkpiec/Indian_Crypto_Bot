"""
Main trading loop worker for Indian_Crypto_Bot.
Runs the throttle cycle: fetch candles → run strategy → risk check → execute signals → update wallets.
"""

import logging
import signal
import time
from typing import Optional

import pandas as pd

from utils.risk_manager import RiskManager

logger = logging.getLogger(__name__)


class BotWorker:
    """Core trading loop with state machine and heartbeat."""

    def __init__(self, config: dict, exchange, strategy, wallets, rpc_manager=None):
        self.config = config
        self.exchange = exchange
        self.strategy = strategy
        self.wallets = wallets
        self.rpc_manager = rpc_manager
        self.risk_manager = RiskManager(
            max_daily_loss_pct=config.get("risk", {}).get("max_daily_loss_pct", 5.0),
            max_consecutive_losses=config.get("risk", {}).get("max_consecutive_losses", 3),
            min_adx_for_entry=config.get("risk", {}).get("min_adx_for_entry", 25.0),
        )

        self.state = "STOPPED"  # STOPPED | RUNNING
        self.throttle_secs = config.get("internals", {}).get("process_throttle_secs", 5)
        self.heartbeat_interval = config.get("internals", {}).get("heartbeat_interval", 60)
        self._last_heartbeat = 0.0
        self._running = False

        # Pairs from config
        pairlists = config.get("pairlists", [])
        self.pairs = []
        for pl in pairlists:
            if pl.get("method") == "StaticPairList":
                self.pairs.extend(pl.get("pairs", []))
        if not self.pairs:
            self.pairs = ["TRX/USDT:USDT"]

        # Signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum, frame):
        logger.info(f"Received signal {signum}, shutting down gracefully")
        self._running = False
        self.state = "STOPPED"

    def start(self) -> None:
        """Start the trading loop."""
        self._running = True
        self.state = "RUNNING"
        self.exchange.startup()
        self.strategy.on_start()
        self.wallets.update()

        logger.info(
            f"Bot started: pairs={self.pairs}, timeframe={self.strategy.timeframe}, "
            f"throttle={self.throttle_secs}s, dry_run={self.config.get('dry_run', False)}"
        )

        if self.rpc_manager:
            self.rpc_manager.send_message(
                f"🟢 *Bot Started*\n"
                f"Exchange: `{self.exchange.name}`\n"
                f"Strategy: `{self.strategy.name}`\n"
                f"Pairs: `{', '.join(self.pairs)}`\n"
                f"Timeframe: `{self.strategy.timeframe}`\n"
                f"Dry Run: `{self.config.get('dry_run', False)}`"
            )

        try:
            while self._running:
                if self.state == "RUNNING":
                    try:
                        self._process()
                    except Exception as e:
                        logger.error(f"Error in process loop: {e}", exc_info=True)
                        if self.rpc_manager:
                            self.rpc_manager.send_message(f"⚠️ *Error:* `{str(e)[:200]}`")

                # Heartbeat
                now = time.time()
                if now - self._last_heartbeat >= self.heartbeat_interval:
                    logger.info(
                        f"Heartbeat: state={self.state}, "
                        f"balance={self.wallets.get_total('USDT'):.4f} USDT"
                    )
                    self._last_heartbeat = now

                time.sleep(self.throttle_secs)

        finally:
            self.strategy.on_stop()
            self.exchange.close()
            logger.info("Bot stopped")
            if self.rpc_manager:
                self.rpc_manager.send_message("🔴 *Bot Stopped*")

    def stop(self) -> None:
        """Stop the trading loop."""
        self._running = False
        self.state = "STOPPED"

    def _process(self) -> None:
        """One full iteration of the trading loop."""
        for pair in self.pairs:
            try:
                # 1. Fetch OHLCV candles
                candles = self.exchange.fetch_ohlcv(
                    pair, self.strategy.timeframe, limit=200
                )
                if not candles or len(candles) < 50:
                    logger.warning(f"Insufficient candle data for {pair}: {len(candles)} bars")
                    continue

                # 2. Build DataFrame
                df = pd.DataFrame(
                    candles, columns=["timestamp", "open", "high", "low", "close", "volume"]
                )
                df["date"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)

                # 3. Run strategy indicators
                metadata = {"pair": pair}
                df = self.strategy.populate_indicators(df, metadata)

                # 4. Generate entry/exit signals
                df = self.strategy.populate_entry_trend(df, metadata)
                df = self.strategy.populate_exit_trend(df, metadata)

                # 5. Check latest bar for signals
                latest = df.iloc[-1]
                if latest.get("enter_long", 0) == 1:
                    # Risk check before acting on signal
                    balance = self.wallets.get_total("USDT")
                    allowed, reason = self.risk_manager.can_open_trade(balance)
                    if not allowed:
                        logger.warning(f"ENTRY blocked by risk manager on {pair}: {reason}")
                        if self.rpc_manager:
                            self.rpc_manager.send_message(f"⚠️ *Entry Blocked* `{pair}`\n{reason}")
                        continue

                    price = latest["close"]
                    margin_usdt = self.config.get("trading", {}).get("margin_per_trade", 20.0)
                    leverage = self.config.get("trading", {}).get("leverage", 10)
                    quantity = (margin_usdt * leverage) / price

                    logger.info(
                        f"ENTRY signal on {pair}: {latest.get('enter_tag', 'unknown')} "
                        f"| margin={margin_usdt} USDT | qty={quantity:.4f}"
                    )

                    try:
                        order = self.exchange.create_order(
                            symbol=pair,
                            order_type="MARKET",
                            side="BUY",
                            amount=quantity,
                        )
                        logger.info(f"ORDER PLACED: {order}")
                        if self.rpc_manager:
                            self.rpc_manager.send_message(
                                f"📈 *ENTRY EXECUTED* `{pair}`\n"
                                f"Price: `{price:.6f}`\n"
                                f"Qty: `{quantity:.4f}`\n"
                                f"Margin: `{margin_usdt} USDT`\n"
                                f"Leverage: `{leverage}x`\n"
                                f"Tag: `{latest.get('enter_tag', '')}`"
                            )
                    except Exception as e:
                        logger.error(f"ORDER FAILED on {pair}: {e}", exc_info=True)
                        if self.rpc_manager:
                            self.rpc_manager.send_message(f"❌ *Order Failed* `{pair}`\n`{str(e)[:200]}`")

                if latest.get("exit_long", 0) == 1:
                    price = latest["close"]
                    logger.info(f"EXIT signal on {pair}: {latest.get('exit_tag', 'unknown')}")

                    # Close position with market sell
                    try:
                        positions = self.exchange.fetch_positions(pair)
                        pos_qty = 0.0
                        for p in positions:
                            amt = float(p.get("positionAmt", 0))
                            if amt > 0:
                                pos_qty += amt
                        if pos_qty > 0:
                            order = self.exchange.create_order(
                                symbol=pair,
                                order_type="MARKET",
                                side="SELL",
                                amount=pos_qty,
                                reduce_only=True,
                            )
                            logger.info(f"EXIT ORDER PLACED: {order}")
                            if self.rpc_manager:
                                self.rpc_manager.send_message(
                                    f"📉 *EXIT EXECUTED* `{pair}`\n"
                                    f"Price: `{price:.6f}`\n"
                                    f"Qty: `{pos_qty:.4f}`\n"
                                    f"Tag: `{latest.get('exit_tag', '')}`"
                                )
                        else:
                            logger.info(f"No open position to exit on {pair}")
                    except Exception as e:
                        logger.error(f"EXIT ORDER FAILED on {pair}: {e}", exc_info=True)
                        if self.rpc_manager:
                            self.rpc_manager.send_message(f"❌ *Exit Failed* `{pair}`\n`{str(e)[:200]}`")

            except Exception as e:
                logger.error(f"Error processing {pair}: {e}", exc_info=True)

        # Update wallet balances
        self.wallets.update()