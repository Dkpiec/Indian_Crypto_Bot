# Indian_Crypto_Bot Architecture Document

## 1. Overview
A papi-native crypto trading bot that replaces freqtrade's CCXT/fapi monkey-patching approach with direct `https://papi.binance.com/papi/v1/*` API calls. Designed for portfolio margin accounts where fapi authenticated endpoints are blocked but papi works natively.

**Key Principle:** Zero monkey-patching. Every Binance interaction goes through a dedicated papi exchange layer that speaks the papi protocol directly.

---

## 2. Module Architecture

```
Indian_Crypto_Bot/
├── bot/
│   ├── __init__.py
│   ├── main.py              # Entry point, CLI arg parsing
│   ├── worker.py            # Main loop: throttle, state machine, heartbeat
│   └── freqtradebot.py      # Core trading logic: process(), order execution
├── exchange/
│   ├── __init__.py
│   ├── papi_client.py       # Native papi HTTP client (requests + HMAC signing)
│   ├── papi_endpoints.py    # Endpoint registry with request/response schemas
│   ├── binance_papi.py      # High-level exchange interface (fetch_ohlcv, create_order, etc.)
│   └── types.py             # TypedDicts for papi responses
├── strategy/
│   ├── __init__.py
│   ├── interface.py         # IStrategy ABC: populate_indicators, populate_entry_trend, populate_exit_trend
│   ├── loader.py            # Dynamic strategy discovery and loading from strategies/ dir
│   └── atr_channel_2h.py   # Ported ATR_CHANNEL_2H strategy
├── wallets/
│   ├── __init__.py
│   └── wallets.py           # Wallet class + Wallets manager (reads from papi /account)
├── rpc/
│   ├── __init__.py
│   ├── telegram.py          # Telegram bot: command handlers, message formatting
│   └── rpc_manager.py       # Message routing, async send queue
├── config/
│   ├── __init__.py
│   ├── loader.py            # YAML/JSON config loading + validation
│   └── schema.py            # Pydantic models for config validation
├── backtest/
│   ├── __init__.py
│   ├── engine.py            # Backtesting engine (reuses strategy interface)
│   └── data_loader.py       # Historical OHLCV loader (papi klines or local cache)
├── persistence/
│   ├── __init__.py
│   ├── database.py          # SQLite trade/order storage
│   └── models.py            # Trade, Order ORM models
├── utils/
│   ├── __init__.py
│   ├── logging_setup.py     # Structured logging + rotation
│   ├── datetime_helpers.py  # UTC normalization, timeframe math
│   └── indicators.py        # TA-Lib/pandas-ta wrapper
├── strategies/               # User strategy directory (hot-reloadable)
│   └── ATR_CHANNEL_2H.py
├── config.yaml               # Default config template
├── Dockerfile
├── entrypoint.sh
└── README.md
```

---

## 3. Exchange Layer (Dual-Endpoint Architecture)

### 3.1 Why Dual Endpoints?
**Phase 3 Bug Audit finding:** The papi gateway (`papi.binance.com/papi/v1/`) ONLY serves portfolio-margin authenticated endpoints (account, order management). Public market data endpoints (klines, ticker/price) return 404 on papi and MUST stay on fapi. Additionally, `positionRisk` and `openOrders` also 404 on papi for portfolio margin accounts and are mocked.

This means a pure-papi approach is impossible. The solution is **dual-endpoint routing** — no monkey-patching, just correct URL selection per endpoint type:
- **PAPI** (`https://papi.binance.com/papi/v1`): `/account`, `/order`, `/cancelOrder`, `/getOrder`
- **FAPI** (`https://fapi.binance.com/fapi/v1`): `/klines`, `/ticker/price`, `/ping` (unsigned)
- **MOCKED**: `/positionSide/dual`, `/multiAssetsMargin`, `/leverageBracket`, `/positionRisk`, `/openOrders`

### 3.2 PapiClient Design (Dual-Endpoint)
```python
class PapiClient:
    PAPI_BASE = "https://papi.binance.com/papi/v1"  # Authenticated trading
    FAPI_BASE = "https://fapi.binance.com/fapi/v1"  # Public market data

    def _route_base_url(self, path: str) -> str:
        if path in self.PAPI_ONLY_PATHS:
            return self.PAPI_BASE
        if path in self.FAPI_ONLY_PATHS:
            return self.FAPI_BASE
        return self.PAPI_BASE  # default to papi for unknown

    def signed_request(self, method, path, params=None, signed=True):
        base_url = self._route_base_url(path)
        # Sign only papi requests; fapi public endpoints are unsigned
        ...
        self.session = requests.Session()
        self.rate_limiter = AdaptiveRateLimiter()

    def signed_request(self, method: str, path: str, params: dict = None) -> dict:
        """HMAC-SHA256 signed request to papi endpoint."""
        params = params or {}
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 30000
        query_string = urlencode(params)
        signature = hmac.new(secret, query_string, hashlib.sha256).hexdigest()
        params["signature"] = signature
        response = self.session.request(method, f"{self.BASE_URL}{path}", params=params)
        response.raise_for_status()
        return response.json()
```

### 3.3 Endpoint Mapping Table

| Freqtrade/CCXT Call | Papi Endpoint | Notes |
|---------------------|---------------|-------|
| fetchBalance() | GET /account | Returns {accountEquity, accountInitialMargin, actualEquity} |
| fetchPositions() | GET /positionRisk | Returns array of position objects |
| fetchTicker() | GET /ticker/price | Same as fapi |
| fetchOHLCV() | GET /klines | Same as fapi |
| createOrder() | POST /order | Same params as fapi (symbol, side, type, quantity, price) |
| cancelOrder() | DELETE /order | Same as fapi |
| fetchOrder() | GET /order | Same as fapi |
| fetchOpenOrders() | GET /openOrders | Same as fapi |
| fetchLeverageTiers() | NOT AVAILABLE | Mock with empty dict; not needed for portfolio margin |
| positionSide/dual | NOT AVAILABLE | Mock: {dualSidePosition: false} |
| multiAssetsMargin | NOT AVAILABLE | Mock: {multiAssetsMargin: false} |
| leverageBracket | NOT AVAILABLE | Mock: [] |

### 3.4 Balance Response Transformation
Papi `/account` returns:
```json
{
  "accountEquity": "57.95",
  "accountInitialMargin": "1.08",
  "actualEquity": "57.96",
  "accountStatus": "NORMAL"
}
```

Transformed to internal Wallet format:
```python
Wallet(currency="USDT", free=56.87, used=1.08, total=57.95)
```

---

## 4. Strategy Interface

### 4.1 IStrategy Abstract Base Class
```python
class IStrategy(ABC):
    INTERFACE_VERSION: int = 3
    minimal_roi: dict = {}
    stoploss: float = -0.10
    timeframe: str = "2h"
    max_open_trades: int = 4
    stake_currency: str = "USDT"

    @abstractmethod
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame: ...

    @abstractmethod
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame: ...

    @abstractmethod
    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame: ...

    # Optional hooks
    def custom_stoploss(self, pair, trade, current_time, current_rate, **kwargs) -> float: ...
    def confirm_trade_entry(self, pair, order_type, amount, rate, **kwargs) -> bool: ...
    def confirm_trade_exit(self, pair, trade, order_type, amount, rate, **kwargs) -> bool: ...
```

### 4.2 Strategy Loading
Strategies are discovered dynamically from `strategies/` directory at startup and on `/reload_conf`:
```python
class StrategyLoader:
    def load_strategy(self, name: str) -> IStrategy:
        module = importlib.import_module(f"strategies.{name}")
        cls = getattr(module, name)
        return cls()
```

---

## 5. Wallet System

### 5.1 Wallet Data Structure
```python
@dataclass
class Wallet:
    currency: str
    free: float = 0.0
    used: float = 0.0
    total: float = 0.0
```

### 5.2 Update Flow
1. `Wallets.update()` called every throttle cycle (default 5s)
2. Calls `PapiClient.signed_request("GET", "/account")`
3. Transforms response → `Wallet(USDT, free, used, total)`
4. Stores in `self._wallets: dict[str, Wallet]`
5. RPC `/balance` reads from `self._wallets` directly (no caching layer that can go stale)

### 5.3 Dry-Run Mode
In dry-run, wallets track simulated balance from config `dry_run_wallet` minus open trade stakes plus realized profits. No API calls made.

---

## 6. RPC/Telegram Layer

### 6.1 Command Registry
All commands registered via decorator pattern:
```python
@authorized_only
async def cmd_balance(update: Update, context: CallbackContext): ...
```

### 6.2 Supported Commands
| Command | Description | Data Source |
|---------|-------------|-------------|
| /status | Open trades summary | DB + exchange positions |
| /balance | Wallet balances | Wallets.get_all() |
| /profit | Closed trade PnL | DB aggregation |
| /trades | Recent trade history | DB query |
| /performance | Per-pair win rate & avg profit | DB aggregation |
| /count | Open trade count vs max | DB + config |
| /daily | Daily PnL breakdown | DB aggregation |
| /stats | Overall statistics | DB aggregation |
| /forceexit | Force close a trade | Order execution |
| /stop | Stop bot (graceful) | State machine |
| /start | Resume bot | State machine |
| /reload_conf | Reload config + strategy | Hot-reload |
| /whitelist | Show active pairs | PairListManager |
| /blacklist | Show/manage blacklist | Config |
| /help | Command reference | Static |
| /version | Bot version | __version__ |

### 6.3 Message Formatting
All messages use MarkdownV2 parse mode with proper escaping. Long messages split at 4096 chars. Tables formatted with monospace alignment.

---

## 7. Main Trading Loop

### 7.1 Worker Cycle
```
while state != STOPPED:
    if state == RUNNING:
        bot.process()          # One full iteration
    sleep(throttle_secs)       # Default 5s
    heartbeat()                # Log + systemd notify every 60s
```

### 7.2 bot.process() Flow
```
1. Check new candle available (timeframe boundary)
2. Fetch OHLCV for all whitelist pairs
3. Run strategy.populate_indicators() on each pair
4. Run strategy.populate_entry_trend() → generate entry signals
5. Run strategy.populate_exit_trend() → generate exit signals
6. For each exit signal: execute exit order
7. For each entry signal: check max_open_trades, execute entry order
8. Update wallet balances
9. Update open order statuses
10. Send RPC notifications for any new trades/exits
```

---

## 8. Configuration Schema

```yaml
exchange:
  name: binance
  api_key: "${BINANCE_API_KEY}"      # Env var injection at runtime
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
  stake_amount: unlimited
  stoploss: -0.99

pairlists:
  - method: StaticPairList
    pairs:
      - TRX/USDT:USDT

internals:
  process_throttle_secs: 5
  heartbeat_interval: 60

dry_run: false
dry_run_wallet: 1000.0
```

---

## 9. Deployment Architecture

### 9.1 Dockerfile
- Base: `python:3.11-slim`
- Non-root user (`bot`)
- Health check: `curl localhost:8080/health` (simple HTTP health endpoint)
- Entrypoint injects env vars into config.yaml at runtime

### 9.2 Coolify Integration
- Source: GitHub repo `Dkpiec/Indian_Crypto_Bot`
- Build pack: Dockerfile
- Auto-deploy on push to main
- Credentials stored as Coolify environment variables (never in code)

### 9.3 Credential Preservation
During cutover from freqtrade:
1. Export env vars from existing freqtrade Coolify app
2. Import same vars into new Indian_Crypto_Bot app
3. Verify bot starts and authenticates before stopping old app

---

## 10. Future Extensibility

### 10.1 Adding New Strategies
Drop a new `.py` file in `strategies/` implementing `IStrategy`. Update `config.yaml` strategy name. No core changes needed.

### 10.2 Multi-Exchange Support
`PapiClient` can be abstracted behind an `IExchangeClient` interface. Future exchanges implement the same interface. Current scope: Binance papi only.

### 10.3 WebSocket Support
Phase 1 uses REST polling. Phase 2 can add `papi_ws_client.py` for real-time kline/orderbook streams via `wss://fstream.binance.com/ws` (same auth as REST).