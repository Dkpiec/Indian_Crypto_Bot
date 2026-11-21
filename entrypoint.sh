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
      - PEPE/USDT:USDT
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

internals:
  process_throttle_secs: 5
  heartbeat_interval: 60

dry_run: ${DRY_RUN:-false}
dry_run_wallet: ${DRY_RUN_WALLET:-1000.0}
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

# Start the bot
echo "Starting bot process..."
exec python3 -m bot.main