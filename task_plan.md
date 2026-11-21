# Papi-Native Crypto Bot — Task Plan

## Goal
Build a custom papi-native crypto trading bot that replaces freqtrade, with native `https://papi.binance.com/papi/v1/*` integration (no monkey-patching), ATR_CHANNEL_2H strategy, full Telegram bot commands, and GitHub→Coolify deployment workflow. Existing freqtrade setup decommissioned only after verification; credentials preserved throughout.

## Next Step
Phase 1: Begin freqtrade reverse engineering — map main loop, exchange abstraction, wallet system, RPC/Telegram layer, and strategy interface.

## Current Phase
Phase 1

## Scope (User-Confirmed 2026-11-21)
1. **Strategy:** Port `ATR_CHANNEL_2H` exactly as-is; architecture must support adding new strategies in future without refactoring core.
2. **Features:** Full feature set matching or exceeding freqtrade: live trading, backtesting, Telegram bot commands, pairlist management, protection handlers, wallet tracking, performance analytics. Upgrade where possible (see Phase 4).
3. **Existing Setup:** Decommission current freqtrade Coolify app AFTER new bot is verified working. Preserve all credentials (`BINANCE_API_KEY`, `BINANCE_SECRET_KEY`, `FREQTRADE_TELEGRAM_TOKEN`, `FREQTRADE_TELEGRAM_CHAT_ID`) in Coolify env vars — never delete.
4. **Telegram:** Reuse existing `@Cryptodkpiec_bot` token and chat ID `357652168`.
5. **Deployment Workflow:** GitHub push → automated tests → Coolify container deploy (same repo→container pattern as current setup).

---

## Phases

### Phase 1: Freqtrade Reverse Engineering & Architecture Design
**Goal:** Understand freqtrade's core loop, exchange abstraction, wallet system, RPC/Telegram layer, and strategy interface well enough to build a clean replacement.

- [ ] 1.1 Map freqtrade's main loop: `FreqtradeBot.process()` → exchange fetch → strategy populate → signal generation → order execution → wallet update cycle
- [ ] 1.2 Document exchange abstraction layer: how CCXT is wrapped, how `fetchBalance`, `fetchTicker`, `fetchOHLCV`, `createOrder`, `cancelOrder` are called and cached
- [ ] 1.3 Document Wallets class: how balances are stored, updated, and served to RPC `/balance` command
- [ ] 1.4 Document RPC/Telegram layer: command registration, message formatting, async polling loop
- [ ] 1.5 Document strategy interface: `populate_indicators`, `populate_entry_trend`, `populate_exit_trend`, `custom_stoploss`, `confirm_trade_entry/exit` hooks
- [ ] 1.6 Document config schema: required fields, pairlist config, exchange config, telegram config, protection handlers
- [ ] 1.7 Identify all Binance-specific code paths that assume fapi (not papi) — these are the bugs we're eliminating
- [ ] 1.8 Produce architecture doc for new bot: module boundaries, papi endpoint mapping table, and strategy plugin interface spec
- **Status:** complete
- **Deliverable:** `architecture.md` written (11,518 bytes) — covers module structure, papi exchange layer, endpoint mapping, strategy interface, wallet system, RPC/Telegram commands, trading loop, config schema, deployment architecture, and future extensibility.

### Phase 2: Core Bot Implementation
**Goal:** Build a working papi-native bot with ATR_CHANNEL_2H strategy, Telegram integration, and live trading capability.

- [ ] 2.1 Create project structure: `papi-bot/` with `exchange/`, `strategy/`, `rpc/`, `wallets/`, `config/`, `backtest/`, `utils/` modules
- [ ] 2.2 Implement papi-native exchange layer: direct `https://papi.binance.com/papi/v1/*` calls for ALL endpoints (account, balance, positionRisk, leverageBracket, order, ticker, klines) — NO monkey-patching, NO fapi fallback
- [ ] 2.3 Implement wallet system: reads from papi `/account` endpoint, caches with TTL, serves to RPC layer
- [ ] 2.4 Implement strategy plugin interface: base class with `populate_indicators`, `populate_entry_trend`, `populate_exit_trend` methods; load strategies from `strategies/` directory dynamically
- [ ] 2.5 Port `ATR_CHANNEL_2H` strategy exactly as-is to new interface
- [ ] 2.6 Implement main trading loop: candle fetch → indicator calc → signal gen → order execution → wallet sync → sleep cycle
- [ ] 2.7 Implement Telegram RPC layer: reuse existing bot token; implement `/status`, `/balance`, `/profit`, `/trades`, `/performance`, `/count`, `/daily`, `/stats`, `/forceexit`, `/stop`, `/start`, `/reload_conf`, `/whitelist`, `/blacklist`, `/help`, `/version` commands
- [ ] 2.8 Implement config loader: YAML/JSON config with exchange credentials, telegram settings, strategy params, pairlist, risk management
- [ ] 2.9 Implement Dockerfile: Python 3.11+, minimal deps, non-root user, health check
- [ ] 2.10 Implement entrypoint.sh: credential injection at runtime (env vars → config), start bot process
- **Status:** pending
- **Deliverable:** Working bot that trades TRX/USDT:USDT on papi with Telegram commands functional.

### Phase 3: Bug Audit & Future-Proofing
**Goal:** Identify all present bugs in the new implementation and anticipate future failure modes.

#### Present Bugs to Audit
- [ ] 3.1 Papi endpoint coverage: verify EVERY endpoint the bot calls exists on papi (no silent 404s like freqtrade had)
- [ ] 3.2 Balance parsing: verify papi `/account` response fields map correctly to wallet display (equity, margin, free, used)
- [ ] 3.3 Order placement: verify papi order endpoint accepts same params as fapi (symbol, side, type, quantity, price, timeInForce)
- [ ] 3.4 WebSocket compatibility: if using WS for real-time data, verify papi WS endpoint exists and auth works
- [ ] 3.5 Rate limiting: verify papi rate limits match bot's request frequency; implement adaptive throttling
- [ ] 3.6 Credential rotation: verify bot handles API key expiry/rotation gracefully without crash
- [ ] 3.7 Telegram reconnection: verify bot recovers from Telegram API timeouts/network blips without losing state
- [ ] 3.8 Config validation: verify missing/invalid config fields produce clear errors, not silent failures
- [ ] 3.9 Strategy hot-reload: verify `/reload_conf` actually reloads strategy params without restart
- [ ] 3.10 Concurrent safety: verify no race conditions between trading loop and RPC command handlers

#### Future Bugs to Anticipate
- [ ] 3.11 Binance API versioning: papi endpoints may change; implement version detection and graceful degradation
- [ ] 3.12 Portfolio margin account changes: user's account type may change; bot should detect and adapt
- [ ] 3.13 New strategy addition: verify adding a second strategy doesn't break existing one or require core changes
- [ ] 3.14 Backtest data source: verify backtesting uses same papi-compatible data source as live (no fapi dependency)
- [ ] 3.15 Multi-pair scaling: verify bot handles 1 pair and 50 pairs with same code path, no hardcoded assumptions
- [ ] 3.16 Timezone handling: verify all timestamps are UTC-normalized; no DST bugs in candle alignment
- [ ] 3.17 Database migration: if using SQLite for trades, verify schema migrations work across versions
- [ ] 3.18 Memory leaks: verify long-running bot doesn't accumulate unbounded state (candle history, trade log)
- [ ] 3.19 Log rotation: verify logs don't fill disk; implement size-based rotation
- [ ] 3.20 Graceful shutdown: verify SIGTERM stops bot cleanly, cancels open orders if configured, saves state
- **Status:** pending
- **Deliverable:** `bug_audit.md` with each item marked PASS/FAIL/WARNING and remediation notes.

### Phase 4: Market Regime Improvements
**Goal:** Enhance the bot beyond freqtrade's defaults based on current market conditions and best practices.

- [ ] 4.1 Adaptive timeframe selection: auto-switch between 1h/2h/4h based on volatility regime (ATR percentile)
- [ ] 4.2 Dynamic position sizing: Kelly criterion or volatility-targeted sizing instead of fixed stake
- [ ] 4.3 Regime filter: add trend strength filter (ADX > 25) to avoid ranging markets for trend-following strategies
- [ ] 4.4 Drawdown protection: circuit breaker that pauses trading after X% daily loss or Y consecutive losses
- [ ] 4.5 Funding rate awareness: for perpetual swaps, factor funding rate into entry/exit signals
- [ ] 4.6 Liquidity check: skip entries when spread > threshold or volume < minimum
- [ ] 4.7 Smart order routing: use limit orders with post-only flag to avoid taker fees; fall back to market only on urgency
- [ ] 4.8 Performance attribution: track win rate, avg R:R, max drawdown per strategy separately for multi-strategy future
- [ ] 4.9 Alert escalation: critical errors (auth fail, exchange down) send immediate Telegram alert, not just log
- [ ] 4.10 Backtest realism: include slippage model, fee tiers, partial fills in backtesting engine
- **Status:** pending
- **Deliverable:** `improvements.md` with rationale, implementation status, and expected impact for each.

### Phase 5: Testing & Verification
**Goal:** Prove the bot works correctly before decommissioning freqtrade.

- [ ] 5.1 Unit tests: exchange layer, wallet parsing, strategy signals, config loading
- [ ] 5.2 Integration test: dry-run mode with real papi data, no real orders
- [ ] 5.3 Backtest validation: run ATR_CHANNEL_2H on historical data, compare results with freqtrade backtest
- [ ] 5.4 Telegram command test: verify all 20+ commands return correct data
- [ ] 5.5 Live paper test: run bot in dry-run on Coolify for 24h, verify no crashes, correct balance display, proper signal generation
- [ ] 5.6 Live small-stake test: enable real trading with minimum stake ($10), verify order execution, position tracking, PnL calculation
- [ ] 5.7 Failover test: kill bot mid-trade, restart, verify it recovers state correctly
- [ ] 5.8 Credential preservation test: verify Coolify env vars survive app deletion/recreation
- **Status:** pending
- **Deliverable:** Test report with pass/fail for each item; screenshots of Telegram commands working.

### Phase 6: Deployment & Cutover
**Goal:** Deploy new bot to Coolify, verify production readiness, decommission freqtrade.

- [ ] 6.1 Push final code to GitHub repo `Dkpiec/Indian_Crypto_Bot` (https://github.com/Dkpiec/Indian_Crypto_Bot)
- [ ] 6.2 Create new Coolify application pointing to new repo
- [ ] 6.3 Copy credentials from existing freqtrade app to new app (API keys, Telegram token/chat ID)
- [ ] 6.4 Deploy and verify container starts, Telegram bot responds, balance shows correctly
- [ ] 6.5 Run 48h live monitoring period with alerts enabled
- [ ] 6.6 After verification: stop (don't delete) existing freqtrade app as backup
- [ ] 6.7 Update memory/docs with new bot location, repo URL, Coolify app UUID
- [ ] 6.8 Delete freqtrade app ONLY after 7 days of stable operation (user confirms)
- **Status:** pending
- **Deliverable:** New bot running in production; freqtrade stopped but recoverable; cutover checklist signed off.

---

## Approval Gate
**THIS PLAN REQUIRES USER APPROVAL BEFORE EXECUTION BEGINS.**
User will review this plan, the bug audit (Phase 3), and improvements (Phase 4) before any code is written.
Credentials will be preserved throughout. Existing freqtrade setup remains running until Phase 6.6.

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| Port only ATR_CHANNEL_2H initially | User confirmed; architecture supports future strategies via plugin interface |
| Full feature set + upgrades | User wants good features, even upgrades beyond freqtrade |
| Remove freqtrade after verification | User approved removal but credentials must be preserved |
| Reuse existing Telegram bot | User confirmed reuse of @Cryptodkpiec_bot and chat ID 357652168 |
| Native papi integration, no monkey-patching | Eliminates root cause of all freqtrade papi issues experienced in current setup |
| GitHub→Coolify deployment workflow | Matches existing proven workflow; user confirmed this pattern |

## Errors Encountered
| Error | Resolution |
|-------|------------|
| (none yet) | — |