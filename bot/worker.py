"""
Main trading loop worker for Indian_Crypto_Bot.
Runs the throttle cycle: fetch candles → run strategy → risk check → execute signals → update wallets.
"""

import logging
import signal
import time
import uuid
from typing import Optional

import pandas as pd

from utils.risk_manager import RiskManager

logger = logging.getLogger(__name__)


def _client_order_id(prefix: str, symbol: str) -> str:
    """Build a Binance-valid newClientOrderId: 1-36 chars, alnum only.

    Binance rejects -4015 if the id exceeds 36 characters. The old format
    embedded a 13-digit millisecond candle timestamp, pushing long symbols
    (e.g. PENDLEUSDT) over the limit. This keeps a short, human-readable
    prefix + a truncated symbol tag + 8 hex of uuid4 — well under 36, still
    effectively collision-free for idempotent retries.
    """
    tag = symbol.replace("/", "").replace(":", "").replace(" ", "")[:8]
    uid = uuid.uuid4().hex[:10]
    oid = f"{prefix}{tag}{uid}"
    return oid[:36]


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
        # Supervisor restart flag: when True the main-loop supervisor stops
        # restarting the worker thread instead of backing off forever.
        self._stop_forever = False
        # Backoff between failed loop ticks — prevents a crash-loop when a
        # transient error (network, exchange 5xx) repeats every throttle second.
        self._error_backoff = 0

        # Signal dedup: a strategy signal lives on a *closed* candle. The same
        # candle must only ever be acted on once, otherwise the loop re-fires
        # the same entry every tick for up to 2h (the original bug: 144
        # duplicate attempts on UNI in 33 minutes).
        self._last_entry_candle_ms: dict[str, int] = {}
        self._last_exit_candle_ms: dict[str, int] = {}
        # Per-pair cooldowns after a failed order: a failing pair stops
        # alerting + re-ordering for this many seconds instead of hammering
        # the exchange and Telegram every throttle tick.
        self._entry_cooldown_until: dict[str, float] = {}
        self._exit_cooldown_until: dict[str, float] = {}
        self._order_cooldown_secs = 300.0

        # Pairs from config
        pairlists = config.get("pairlists", [])
        self.pairs = []
        for pl in pairlists:
            if pl.get("method") == "StaticPairList":
                self.pairs.extend(pl.get("pairs", []))
        if not self.pairs:
            self.pairs = ["TRX/USDT:USDT"]

        # Position-sizing knobs. max_open_trades caps how many concurrent
        # positions the bot holds; when use_full_balance is set we split the
        # equity across that many equal slots instead of putting the whole
        # equity into every signal (the -2019 margin storm).
        trading_conf = config.get("trading", {})
        strategy_conf = config.get("strategy", {})
        self.max_open_trades = int(
            trading_conf.get("max_open_trades",
                             strategy_conf.get("max_open_trades", 4))
        )

        # Entry price-gate + limit-order knobs. On a closed-candle entry
        # signal the market has usually moved; instead of a blind MARKET
        # fill we compare the live last price to the signal close:
        #   dev <= entry_min_deviation_pct  -> MARKET (already at the price)
        #   min < dev <= entry_max_deviation_pct -> LIMIT at the signal price
        #   dev  > entry_max_deviation_pct  -> SKIP the entry entirely
        # A resting GTX limit auto-cancels after entry_limit_expires_secs
        # and the next tick re-evaluates. Defaults apply if the keys are
        # absent.
        self.entry_max_deviation_pct = float(
            trading_conf.get("entry_max_deviation_pct", 1.0)
        )
        self.entry_min_deviation_pct = float(
            trading_conf.get("entry_min_deviation_pct", 0.25)
        )
        self.entry_limit_expires_secs = int(
            trading_conf.get("entry_limit_expires_secs", 1800)
        )
        # Track signal limit orders so we can stop re-firing a still-resting
        # (unfilled) limit on the same candle — dedupe on the candle it was
        # placed for.
        self._open_signal_limit_ms: dict[str, int] = {}
        # The client_order_id of the currently-resting signal limit per pair,
        # so a later tick can verify via fetch_open_orders whether it is
        # still on the book (resting) or has filled / been cancelled.
        self._open_signal_limit_client_id: dict[str, str] = {}

        # Signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum, frame):
        logger.info(f"Received signal {signum}, shutting down gracefully")
        self._running = False
        self.state = "STOPPED"

    def _open_position_count(self) -> int:
        """Number of currently open positions with non-zero contract size.

        Reads the exchange's positionRisk; on any failure returns a large
        number so we fail *closed* (no new entries) rather than open blind
        when we can't see current exposure.
        """
        try:
            positions = self.exchange.fetch_positions()
            count = 0
            for p in positions:
                try:
                    size = abs(float(p.get("positionAmt", 0) or 0))
                    if size > 0:
                        count += 1
                except (TypeError, ValueError):
                    continue
            return count
        except Exception as e:
            logger.warning(f"Could not read open positions ({e}); failing closed.")
            return 10**6

    # ------------------------------------------------------------------
    # Entry execution: live-price gate + market/limit selection
    # ------------------------------------------------------------------
    def _resolve_entry_mode(self, pair: str, signal_price: float) -> dict:
        """Pick how to enter at the signal price, using the live last price.

        Returns a dict:
          live_price : float  — latest last price (0.0 if unavailable)
          deviation  : float  — |live - signal| / signal, in percent
          mode       : str    — "MARKET" | "LIMIT" | "SKIP"

        Decision (defaults from the trading config, see __init__):
          dev <= entry_min_deviation_pct    -> MARKET (price at the signal)
          min < dev <= entry_max_deviation  -> LIMIT at the signal price
          dev  > entry_max_deviation        -> SKIP (price moved too far)
        If the live price can't be read we SKIP the entry: the whole point of
        the gate is to avoid a blind MARKET fill when the price has moved, so
        a flaky ticker should fail toward *waiting* (retry on the next tick),
        not toward market-buying into a moved price.
        """
        try:
            ticker = self.exchange.fetch_ticker(pair)
            live_price = float(ticker.get("last", 0.0))
        except Exception as e:
            logger.warning(
                f"Could not read live price for {pair}; SKIPPING entry "
                f"(will retry on the next tick) to avoid a blind MARKET "
                f"fill: {e}"
            )
            live_price = 0.0

        if live_price <= 0.0:
            return {
                "live_price": 0.0,
                "deviation": 0.0,
                "mode": "SKIP",
            }

        deviation = (abs(live_price - signal_price) / signal_price) * 100.0

        if deviation <= self.entry_min_deviation_pct:
            mode = "MARKET"
        elif deviation <= self.entry_max_deviation_pct:
            mode = "LIMIT"
        else:
            mode = "SKIP"

        return {
            "live_price": live_price,
            "deviation": deviation,
            "mode": mode,
        }

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
                        # Success — clear backoff
                        self._error_backoff = 0
                    except Exception as e:
                        logger.error(f"Error in process loop: {e}", exc_info=True)
                        if self.rpc_manager:
                            self.rpc_manager.send_message(f"⚠️ *Error:* `{str(e)[:200]}`")
                        # Back off on failure so a broken exchange connection
                        # does not hammer the API every throttle second.
                        self._error_backoff = min(60, 5 * (self._error_backoff + 1))
                        time.sleep(self._error_backoff)

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
        """Stop the trading loop permanently (supervisor will not restart)."""
        self._running = False
        self.state = "STOPPED"
        self._stop_forever = True

    def trade_now(self) -> dict:
        """Find the most recent entry signal across all pairs and execute a market buy.
        Returns a dict with the result for Telegram notification."""
        import pandas as pd

        best_pair = None
        best_price = None
        best_signal_time = 0
        best_tag = ""

        for pair in self.pairs:
            try:
                candles = self.exchange.fetch_ohlcv(pair, self.strategy.timeframe, limit=200)
                if not candles or len(candles) < 50:
                    continue
                df = pd.DataFrame(
                    candles, columns=["timestamp", "open", "high", "low", "close", "volume"]
                )
                metadata = {"pair": pair}
                df = self.strategy.populate_indicators(df, metadata)
                df = self.strategy.populate_entry_trend(df, metadata)

                # Find the most recent enter_long==1 bar
                entry_bars = df[df["enter_long"] == 1]
                if entry_bars.empty:
                    continue
                last_entry = entry_bars.iloc[-1]
                sig_time = int(last_entry["timestamp"])
                if sig_time > best_signal_time:
                    best_signal_time = sig_time
                    best_pair = pair
                    best_price = float(last_entry["close"])
                    best_tag = last_entry.get("enter_tag", "manual")
            except Exception as e:
                logger.warning(f"trade_now: error scanning {pair}: {e}")
                continue

        if best_pair is None:
            return {"success": False, "reason": "No recent entry signals found across any pair"}

        # /trade_now is an operator override but must still respect the
        # exchange's max_open_trades cap — a manual 4× full-equity burst
        # against the same pair is how the original duplicate storm happened.
        balance = self.wallets.get_total("USDT")
        allowed, reason = self.risk_manager.can_open_trade(balance)
        if not allowed:
            return {"success": False, "reason": f"Risk manager blocked: {reason}", "pair": best_pair}

        # Execute market buy. Full-balance mode splits equity across the
        # max_open_trades slots; the position cap is enforced before ordering.
        trading_conf = self.config.get("trading", {})
        leverage = trading_conf.get("leverage", 5)
        if trading_conf.get("use_full_balance", False):
            # Size from FREE equity, not total. Total reports free + used
            # margin; a manual burst at total/slot cannot be filled while the
            # slots already hold margin (-2019 storm, /trade_now variant).
            slots = max(1, self.max_open_trades)
            margin_usdt = self.wallets.get_free("USDT") / slots
        else:
            margin_usdt = trading_conf.get("margin_per_trade", 20.0)

        open_pos = self._open_position_count()
        if open_pos >= self.max_open_trades:
            return {
                "success": False,
                "reason": f"At max_open_trades ({open_pos} open >= {self.max_open_trades}); no new entry",
                "pair": best_pair,
            }

        quantity = (margin_usdt * leverage) / best_price

        try:
            client_order_id = _client_order_id("m", best_pair)
            order = self.exchange.create_order(
                symbol=best_pair,
                order_type="MARKET",
                side="BUY",
                amount=quantity,
                reduce_only=False,
                client_order_id=client_order_id,
            )
            logger.info(f"TRADE_NOW executed: {best_pair} qty={quantity:.4f}")
            return {
                "success": True,
                "pair": best_pair,
                "price": best_price,
                "quantity": quantity,
                "margin_usdt": margin_usdt,
                "leverage": leverage,
                "tag": best_tag,
                "order": order,
            }
        except Exception as e:
            logger.error(f"TRADE_NOW order failed on {best_pair}: {e}", exc_info=True)
            return {"success": False, "reason": str(e), "pair": best_pair}

    def _process(self) -> None:
        """One full iteration of the trading loop.

        Runs under the shared state lock so /reload_conf cannot swap
        strategy/config/pairs mid-tick."""
        lock = self.rpc_manager.state_lock if self.rpc_manager else None
        if lock is None:
            self._process_locked()
        else:
            with lock:
                self._process_locked()

    def _process_locked(self) -> None:
        """Body of one loop iteration; must be called with the state lock held."""
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
                # Signal-dedup guard: a strategy signal lives on the *closed*
                # candle (iloc[-2] is the last closed bar; iloc[-1] is still
                # forming in real-time feeds — acting on it is what caused
                # the same entry to re-fire every tick). Act on the last
                # closed bar and remember its timestamp so the same candle
                # is never executed twice.
                last_closed = df.iloc[-2] if len(df) >= 2 else df.iloc[-1]
                candle_ms = int(last_closed["timestamp"])
                entry_signal = int(last_closed.get("enter_long", 0)) == 1
                exit_signal = int(last_closed.get("exit_long", 0)) == 1

                if entry_signal and self._last_entry_candle_ms.get(pair) == candle_ms:
                    logger.debug(f"ENTRY signal on {pair} already acted on candle {candle_ms}; skipping duplicate")
                if exit_signal and self._last_exit_candle_ms.get(pair) == candle_ms:
                    logger.debug(f"EXIT signal on {pair} already acted on candle {candle_ms}; skipping duplicate")

                # Per-pair cooldown: a pair whose last order failed stays quiet
                # for _order_cooldown_secs so it does not alert + re-order on
                # every throttle tick. A NEW closed candle still re-triggers.
                if time.time() < self._entry_cooldown_until.get(pair, 0):
                    if entry_signal:
                        logger.debug(f"ENTRY on {pair} in post-failure cooldown; skipping")
                    entry_signal = False

                if entry_signal and self._last_entry_candle_ms.get(pair) != candle_ms:
                    # Risk check before acting on signal
                    balance = self.wallets.get_total("USDT")
                    allowed, reason = self.risk_manager.can_open_trade(balance)
                    if not allowed:
                        logger.warning(f"ENTRY blocked by risk manager on {pair}: {reason}")
                        if self.rpc_manager:
                            self.rpc_manager.send_message(f"⚠️ *Entry Blocked* `{pair}`\n{reason}")
                        continue

                    price = float(last_closed["close"])
                    trading_conf = self.config.get("trading", {})
                    leverage = trading_conf.get("leverage", 5)
                    use_full_balance = trading_conf.get("use_full_balance", False)
                    if use_full_balance:
                        # Split the equity across max_open_trades equal slots
                        # instead of concentrating it into every signal — that
                        # was the -2019 margin-insufficient storm.
                        slots = max(1, self.max_open_trades)
                        # Size from FREE equity only. get_total() reports
                        # account equity (free + used margin). Sizing from
                        # total while other slots hold margin makes Binance
                        # reject every entry with -2019 "Margin is
                        # insufficient" — the account cannot cover 4 full
                        # slots of equity at once. Free is the margin
                        # actually available to allocate to a new position.
                        margin_usdt = self.wallets.get_free("USDT") / slots
                    else:
                        margin_usdt = trading_conf.get("margin_per_trade", 20.0)

                    # Enforce the concurrent-position cap. When the slot is
                    # already full, skip this entry (the signal will not be
                    # retried on the same candle; it may re-fire next close).
                    open_pos = self._open_position_count()
                    if open_pos >= self.max_open_trades:
                        logger.info(
                            f"ENTRY on {pair} skipped: at max_open_trades "
                            f"({open_pos} open >= {self.max_open_trades})."
                        )
                        continue

                    quantity = (margin_usdt * leverage) / price
                    max_qty = trading_conf.get("max_quantity_per_order")  # optional cap
                    if max_qty is not None and quantity > max_qty:
                        quantity = max_qty

                    # Floor guard: if the computed order is below the
                    # symbol's minimum quantity, log and skip instead of
                    # firing a guaranteed rejection. Free-margin sizing
                    # makes this likely on a small account (TRXUSDT
                    # minQty=1 → 1 USDT notional at price 0.0224).
                    min_qty = self.exchange.get_min_quantity(pair)
                    if min_qty and quantity < min_qty:
                        logger.info(
                            f"ENTRY on {pair} skipped: quantity {quantity:.4f} "
                            f"< minQty {min_qty} (free={self.wallets.get_free('USDT'):.2f})."
                        )
                        continue

                    logger.info(
                        f"ENTRY signal on {pair}: {last_closed.get('enter_tag', 'unknown')} "
                        f"| margin={margin_usdt} USDT | qty={quantity:.4f} | candle={candle_ms}"
                    )

                    # Price gate + market/limit selection. The entry signal is
                    # on a *closed* candle, but by the time we act on it the
                    # live last price has usually moved. Instead of a blind
                    # MARKET fill we compare the live price to the signal
                    # close: MARKET when already at the signal, a GTX limit
                    # at the signal price when it's near, and SKIP when the
                    # price has moved more than entry_max_deviation_pct.
                    entry = self._resolve_entry_mode(pair, price)
                    entry_mode = entry["mode"]
                    dev_pct = entry["deviation"]
                    live_price = entry["live_price"]

                    if entry_mode == "SKIP":
                        live_price_display = (
                            f"{live_price:.6f}" if live_price > 0 else "unavailable"
                        )
                        logger.info(
                            f"ENTRY on {pair} SKIPPED: live {live_price_display} "
                            f"is {dev_pct:.3f}% from signal {price:.6f} "
                            f"(max {self.entry_max_deviation_pct}%, or ticker "
                            f"unavailable); price moved."
                        )
                        if self.rpc_manager:
                            self.rpc_manager.send_message(
                                f"⚠️ *Entry skipped* `{pair}`\n"
                                f"Live `{live_price_display}` is `{dev_pct:.3f}%` from "
                                f"signal `{price:.6f}` "
                                f"(> {self.entry_max_deviation_pct}% max deviation, or "
                                f"ticker unavailable); no fill."
                            )
                        # Mark this candle acted-on so it is never re-tried on
                        # a later tick of the SAME candle. Without this, a
                        # ticker blip or a price that wiggles back within the
                        # max would let a *new* limit fire on a stale signal
                        # (Bug: limit placed above the original intended
                        # entry). The signal re-fires cleanly on the NEXT
                        # close with a fresh candle close as the new price
                        # reference.
                        self._last_entry_candle_ms[pair] = candle_ms
                        # Also clear any resting limit mark for this pair so
                        # the SKIP is definitive — a limit that was placed
                        # earlier on this same candle (before the price ran
                        # past the max) is no longer valid; it will auto-
                        # cancel at its GTX expiry.
                        self._open_signal_limit_ms.pop(pair, None)
                        continue

                    # A still-resting, unfilled signal limit on this same
                    # candle must not be re-fired — it's already on the book.
                    # Verify it is *actually* still resting (a GTX limit
                    # auto-cancels after its expiry, or fills early); if it
                    # has disappeared from the book, clear the dedupe mark
                    # so a fresh limit can be placed for this same candle.
                    if entry_mode == "LIMIT" and self._open_signal_limit_ms.get(pair) == candle_ms:
                        cid = self._open_signal_limit_client_id.get(pair, "")
                        still_resting = False
                        try:
                            open_orders = self.exchange.fetch_open_orders(pair)
                            still_resting = any(
                                str(o.get("clientOrderId", "")) == cid
                                for o in open_orders
                                if cid
                            )
                        except Exception as e:
                            # Can't verify — assume still resting to avoid
                            # double-placing; the next tick re-checks.
                            logger.warning(
                                f"fetch_open_orders failed for {pair} "
                                f"(limit verify); holding: {e}"
                            )
                            still_resting = True
                        if still_resting:
                            logger.debug(
                                f"ENTRY on {pair}: signal limit still resting "
                                f"for candle {candle_ms}; holding."
                            )
                            continue
                        # Limit has filled or been cancelled — clear the mark
                        # so we can place a fresh one on this same candle.
                        logger.info(
                            f"ENTRY on {pair}: signal limit for candle "
                            f"{candle_ms} no longer on the book (filled or "
                            f"cancelled); clearing dedupe to allow re-place."
                        )
                        self._open_signal_limit_ms.pop(pair, None)
                        self._open_signal_limit_client_id.pop(pair, None)

                    # Mark the candle acted on BEFORE placing the order so a
                    # mid-order crash/restart cannot re-fire the same signal.
                    self._last_entry_candle_ms[pair] = candle_ms

                    try:
                        if entry_mode == "MARKET":
                            client_order_id = _client_order_id("e", pair)
                            order = self.exchange.create_order(
                                symbol=pair,
                                order_type="MARKET",
                                side="BUY",
                                amount=quantity,
                                reduce_only=False,
                                client_order_id=client_order_id,
                            )
                            logger.info(
                                f"ENTRY (MARKET) on {pair}: {price:.6f} "
                                f"qty={quantity:.4f} dev={dev_pct:.3f}% {order}"
                            )
                        else:  # LIMIT at the signal price, GTX with a short TTL
                            # Re-check the min-qty floor against the LIVE
                            # price, not just the signal close: a limit that
                            # fills below the symbol's minQty would be
                            # rejected with -1111. If the live price has
                            # moved such that the notional falls under the
                            # floor, skip this tick and re-evaluate next.
                            live_qty = (margin_usdt * leverage) / live_price if live_price > 0 else quantity
                            min_qty_live = self.exchange.get_min_quantity(pair)
                            if min_qty_live and live_qty < min_qty_live:
                                logger.info(
                                    f"ENTRY (LIMIT) on {pair} skipped: live "
                                    f"qty {live_qty:.4f} < minQty {min_qty_live} "
                                    f"at live price {live_price:.6f}."
                                )
                                self._last_entry_candle_ms.pop(pair, None)
                                continue
                            client_order_id = _client_order_id("l", pair)
                            order = self.exchange.create_order(
                                symbol=pair,
                                order_type="LIMIT",
                                side="BUY",
                                amount=quantity,
                                price=price,
                                time_in_force="GTX",
                                reduce_only=False,
                                client_order_id=client_order_id,
                            )
                            # Track the candle this limit was placed for so we
                            # don't keep re-firing a still-resting, unfilled
                            # order; after it fills or its GTX expiry cancels
                            # it, the live price is re-checked on the next tick.
                            self._open_signal_limit_ms[pair] = candle_ms
                            self._open_signal_limit_client_id[pair] = client_order_id
                            logger.info(
                                f"ENTRY (LIMIT) on {pair} @ {price:.6f} "
                                f"qty={quantity:.4f} dev={dev_pct:.3f}% "
                                f"GTX ttl={self.entry_limit_expires_secs}s {order}"
                            )

                        if self.rpc_manager:
                            self.rpc_manager.send_message(
                                f"📈 *ENTRY EXECUTED* `{pair}`\n"
                                f"Price: `{price:.6f}`\n"
                                f"Qty: `{quantity:.4f}`\n"
                                f"Type: `{entry_mode}` (dev {dev_pct:.3f}%)\n"
                                f"Margin: `{margin_usdt} USDT`\n"
                                f"Leverage: `{leverage}x`\n"
                                f"Tag: `{last_closed.get('enter_tag', '')}`"
                            )
                    except Exception as e:
                        # A resting LIMIT that failed to place is not yet on
                        # the book — clear both marks so the next tick can
                        # retry.
                        if entry_mode == "LIMIT":
                            self._open_signal_limit_ms.pop(pair, None)
                            self._open_signal_limit_client_id.pop(pair, None)
                        # Keep the dedup mark so the same candle is not
                        # re-fired. Add a per-pair cooldown so a failing pair
                        # stops alerting for a few minutes instead of
                        # hammering the exchange + Telegram every tick.
                        self._entry_cooldown_until[pair] = time.time() + 300
                        logger.error(
                            f"ORDER FAILED on {pair}: {e} "
                            f"(dedup kept; retrying only on next candle close)"
                        )
                        if self.rpc_manager:
                            self.rpc_manager.send_message(f"❌ *Order Failed* `{pair}`\n`{str(e)[:200]}`")

                if exit_signal and time.time() < self._exit_cooldown_until.get(pair, 0):
                    logger.debug(f"EXIT on {pair} in post-failure cooldown; skipping")
                    exit_signal = False

                if exit_signal and self._last_exit_candle_ms.get(pair) != candle_ms:
                    price = float(last_closed["close"])
                    logger.info(f"EXIT signal on {pair}: {last_closed.get('exit_tag', 'unknown')} | candle={candle_ms}")

                    # Close position with market sell
                    self._last_exit_candle_ms[pair] = candle_ms
                    try:
                        positions = self.exchange.fetch_positions(pair)
                        pos_qty = 0.0
                        for p in positions:
                            amt = float(p.get("positionAmt", 0))
                            if amt > 0:
                                pos_qty += amt
                        if pos_qty > 0:
                            client_order_id = _client_order_id("x", pair)
                            order = self.exchange.create_order(
                                symbol=pair,
                                order_type="MARKET",
                                side="SELL",
                                amount=pos_qty,
                                reduce_only=True,
                                client_order_id=client_order_id,
                            )
                            # Record the closed-trade PnL for the circuit
                            # breaker — without this the consecutive-loss
                            # breaker never sees a result and stays dead.
                            self.risk_manager.record_trade_result(
                                pnl=0.0  # approximate; refine with fills later
                            )
                            logger.info(f"EXIT ORDER PLACED: {order}")
                            if self.rpc_manager:
                                self.rpc_manager.send_message(
                                    f"📉 *EXIT EXECUTED* `{pair}`\n"
                                    f"Price: `{price:.6f}`\n"
                                    f"Qty: `{pos_qty:.4f}`\n"
                                    f"Tag: `{last_closed.get('exit_tag', '')}`"
                                )
                        else:
                            logger.info(f"No open position to exit on {pair}")
                            self._last_exit_candle_ms.pop(pair, None)
                    except Exception as e:
                        # Keep the dedup mark on a failed exit too: re-firing a
                        # reduce-only sell on the same candle is pure spam and
                        # risks double-close. Retry only on the next close.
                        self._exit_cooldown_until[pair] = time.time() + 300
                        logger.error(f"EXIT ORDER FAILED on {pair}: {e} (dedup kept; retrying on next candle close)", exc_info=True)
                        if self.rpc_manager:
                            self.rpc_manager.send_message(f"❌ *Exit Failed* `{pair}`\n`{str(e)[:200]}`")

            except Exception as e:
                logger.error(f"Error processing {pair}: {e}", exc_info=True)

        # Update wallet balances
        self.wallets.update()