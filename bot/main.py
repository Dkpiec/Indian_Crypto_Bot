"""
Main entry point for Indian_Crypto_Bot.
Loads config, initializes exchange/strategy/wallets/RPC, starts worker loop.
"""

import asyncio
import logging
import sys

from config.loader import load_config
from exchange.binance_papi import BinancePapiExchange
from strategy.loader import load_strategy
from wallets.wallets import Wallets
from rpc.telegram import TelegramRPC
from bot.worker import BotWorker


def setup_logging() -> None:
    """Configure structured logging with console output."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    # Suppress noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def main() -> None:
    """Initialize and run the trading bot."""
    setup_logging()
    logger = logging.getLogger(__name__)

    logger.info("Indian Crypto Bot starting...")

    # 1. Load configuration
    try:
        config = load_config()
        logger.info("Configuration loaded successfully")
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        sys.exit(1)

    # 2. Initialize exchange
    exchange_config = config.get("exchange", {})
    dry_run = config.get("dry_run", False)
    exchange = BinancePapiExchange(
        api_key=exchange_config.get("api_key", ""),
        secret_key=exchange_config.get("secret_key", ""),
        dry_run=dry_run,
    )

    # 3. Load strategy
    strategy_name = config.get("strategy", {}).get("name", "ATR_CHANNEL_2H")
    try:
        strategy_cls = load_strategy(strategy_name)
        strategy = strategy_cls()
        # Override strategy params from config if provided
        strategy_params = config.get("strategy", {})
        for key in ("timeframe", "max_open_trades", "stake_currency", "stoploss"):
            if key in strategy_params:
                setattr(strategy, key, strategy_params[key])
        logger.info(f"Strategy loaded: {strategy.name} (timeframe={strategy.timeframe})")
    except Exception as e:
        logger.error(f"Failed to load strategy {strategy_name}: {e}")
        sys.exit(1)

    # 4. Initialize wallets
    wallets = Wallets(
        exchange=exchange,
        dry_run=dry_run,
        dry_run_wallet=config.get("dry_run_wallet", 1000.0),
    )

    # 5. Initialize Telegram RPC
    telegram_config = config.get("telegram", {})
    rpc_manager = None
    if telegram_config.get("enabled", True):
        rpc_manager = TelegramRPC(
            token=telegram_config.get("token", ""),
            chat_id=str(telegram_config.get("chat_id", "")),
        )

    # 6. Create and start worker
    worker = BotWorker(
        config=config,
        exchange=exchange,
        strategy=strategy,
        wallets=wallets,
        rpc_manager=rpc_manager,
    )

    # Link worker to RPC so commands can control it
    if rpc_manager:
        rpc_manager.bot_worker = worker

    # Run async Telegram RPC alongside sync worker loop.
    # worker.start() is blocking/sync, so it MUST run in a separate thread;
    # otherwise it blocks the asyncio event loop and Telegram update callbacks
    # (which are coroutines on that loop) can never be processed.
    import threading

    async def run_with_telegram():
        if rpc_manager:
            await rpc_manager.start()
        try:
            # Run blocking worker in a daemon thread so the event loop stays free
            worker_thread = threading.Thread(target=worker.start, daemon=True)
            worker_thread.start()
            # Keep the event loop alive while the worker thread runs
            while worker_thread.is_alive():
                await asyncio.sleep(1)
        finally:
            if rpc_manager:
                await rpc_manager.stop()

    try:
        asyncio.run(run_with_telegram())
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()