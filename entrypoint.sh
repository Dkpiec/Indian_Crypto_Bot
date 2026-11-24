#!/bin/bash
set -e

echo "=== Indian Crypto Bot Starting ==="
echo "Time: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"

# Generate config.yaml from environment variables at runtime
CONFIG_PATH="${CONFIG_PATH:-/app/config.yaml}"

# Always regenerate config from env vars to ensure credentials are injected
# (the committed config.yaml has empty placeholders)
echo "Generating config.yaml from environment variables..."
cat > "$CONFIG_PATH" <<EOF
exchange:
  name: binance
  api_key: "${BINANCE_API_KEY}"
  secret_key: "${BINANCE_SECRET_KEY}"
  trading_mode: futures
  margin_mode: cross

telegram:
  enabled: true
  token: "${FREQTRADE_TELEGRAM_TOKEN}"
  chat_id: "${FREQTRADE_TELEGRAM_CHAT_ID}"

strategy:
  name: ATR_CHANNEL_2H
  timeframe: 2h
  max_open_trades: 4
  stake_currency: USDT
  stoploss: -0.99

pairlists:
  - method: StaticPairList
    pairs:
      - BTC/USDT:USDT
      - ETH/USDT:USDT
      - SOL/USDT:USDT
      - BNB/USDT:USDT
      - XRP/USDT:USDT
      - DOGE/USDT:USDT
      - ADA/USDT:USDT
      - AVAX/USDT:USDT
      - DOT/USDT:USDT
      - LINK/USDT:USDT
      - MATIC/USDT:USDT
      - UNI/USDT:USDT
      - ATOM/USDT:USDT
      - LTC/USDT:USDT
      - FIL/USDT:USDT
      - APT/USDT:USDT
      - ARB/USDT:USDT
      - OP/USDT:USDT
      - NEAR/USDT:USDT
      - SUI/USDT:USDT
      - TRX/USDT:USDT
      - WIF/USDT:USDT
      - FET/USDT:USDT
      - RENDER/USDT:USDT
      - INJ/USDT:USDT
      - TIA/USDT:USDT
      - SEI/USDT:USDT
      - JUP/USDT:USDT
      - PENDLE/USDT:USDT

trading:
  margin_per_trade: 20.0
  leverage: 5
  use_full_balance: true
  entry_max_deviation_pct: ${ENTRY_MAX_DEVIATION_PCT:-1.0}
  entry_min_deviation_pct: ${ENTRY_MIN_DEVIATION_PCT:-0.25}
  entry_limit_expires_secs: ${ENTRY_LIMIT_EXPIRES_SECS:-1800}

internals:
  process_throttle_secs: 5
  heartbeat_interval: 60

dry_run: "${DRY_RUN:-false}"
dry_run_wallet: "${DRY_RUN_WALLET:-1000.0}"
EOF
echo "Config generated at $CONFIG_PATH"

# Validate required env vars for live trading
if [ "${DRY_RUN:-false}" = "false" ]; then
    if [ -z "$BINANCE_API_KEY" ]; then
        echo "ERROR: BINANCE_API_KEY not set (required for live trading)"
        exit 1
    fi
    if [ -z "$BINANCE_SECRET_KEY" ]; then
        echo "ERROR: BINANCE_SECRET_KEY not set (required for live trading)"
        exit 1
    fi
    echo "Live trading mode: credentials validated"
else
    echo "Dry run mode: no credentials required"
fi

if [ -z "$FREQTRADE_TELEGRAM_TOKEN" ]; then
    echo "WARNING: FREQTRADE_TELEGRAM_TOKEN not set; Telegram disabled"
fi

# Start health check endpoint in background
python3 -c "
from http.server import HTTPServer, BaseHTTPRequestHandler
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'ok')
    def log_message(self, *a): pass
HTTPServer(('0.0.0.0', 8080), H).serve_forever()
" &
HEALTH_PID=$!
echo "Health check running on :8080 (PID=$HEALTH_PID)"

# Start the bot.
#
# Do NOT `exec` the bot: run it in the foreground of THIS shell under a trap
# so that on SIGTERM/SIGINT (Coolify restart / container stop) we:
#   1. forward the signal to the bot process so its Python signal handler
#      (worker._handle_signal -> _stop_requested=True) fires,
#   2. wait up to GRACE_SECS for the bot to release the Telegram token
#      (bot.main cancels the RPC in its finally block),
#   3. kill the bot + health-checker if they are still alive.
#
# This is what stops the old container's Telegram polling loop from outliving
# the new one and causing a 409 Conflict on the shared bot token at the next
# deploy. `exec` (the old behaviour) replaced the shell, so a trap could not
# survive and the bot was only killed by the container runtime's SIGKILL.
echo "Starting bot process..."
python3 -m bot.main &
BOT_PID=$!

# Grace period: give the bot time to stop its Telegram polling loop before
# we escalate to SIGKILL. 20s covers the 10s RPC-stop timeout + margin.
GRACE_SECS="${STOP_GRACE_SECS:-20}"

_forward_signal() {
    local sig="$1"
    echo "entrypoint: forwarding ${sig} to bot (PID ${BOT_PID}) and health-checker (PID ${HEALTH_PID})"
    kill -"${sig}" "${BOT_PID}" 2>/dev/null
    kill -"${sig}" "${HEALTH_PID}" 2>/dev/null
}

trap '_forward_signal TERM; for _ in $(seq 1 '"$GRACE_SECS"'); do kill -0 "${BOT_PID}" 2>/dev/null || break; sleep 1; done; kill -KILL "${BOT_PID}" 2>/dev/null; kill -KILL "${HEALTH_PID}" 2>/dev/null; echo "entrypoint: shutdown complete, exiting"; exit 143' TERM
trap '_forward_signal INT;  for _ in $(seq 1 '"$GRACE_SECS"'); do kill -0 "${BOT_PID}" 2>/dev/null || break; sleep 1; done; kill -KILL "${BOT_PID}" 2>/dev/null; kill -KILL "${HEALTH_PID}" 2>/dev/null; echo "entrypoint: shutdown complete, exiting"; exit 130' INT

# Wait for the bot to finish (clean stop via SIGTERM handler, or crash).
wait "${BOT_PID}"
BOT_EXIT=$?
kill "${HEALTH_PID}" 2>/dev/null
exit "${BOT_EXIT}"