# Panther Trading Bot

Automated BTCUSDT trading bot with session-based strategy routing, risk controls, and backtest tooling. Built for Bybit.

## Quick start

### 1. Install dependencies

```bash
pip install pybit fastapi uvicorn pydantic
# For backtesting with historical data:
pip install ccxt
```

### 2. Set environment variables

```bash
export BYBIT_API_KEY="your-api-key"
export BYBIT_API_SECRET="your-api-secret"
export BYBIT_TESTNET="true"   # use testnet while developing
export BYBIT_DEMO="true"      # demo trading mode
```

### 3. Run the bot

```bash
# Via web dashboard (recommended)
uvicorn web.api.server:app --reload

# Then open http://localhost:8000 in your browser
# Use POST /bot/start to start, POST /bot/stop to stop
```

### 4. Run backtests

```bash
python backtest.py                   # generic backtest with pybit data
python backtest_bybit.py --period 3  # last 3 months via ccxt
python backtest_bybit.py --period 6  # last 6 months via ccxt
```

### 5. Run tests

```bash
python -m unittest discover -s tests -v
```

No external test runner needed — uses Python's built-in `unittest`.

---

## How it works

### Session routing

The bot splits each 24-hour UTC day into 3 sessions. Each session runs exactly one strategy at full capacity:

| Session | UTC hours | Strategy | Purpose |
|---------|-----------|----------|---------|
| ASIA    | 00:00–08:00 | Strategy C (candle3) | Volume generation |
| LONDON  | 08:00–16:00 | Mean Reversion (scalp) | Profit engine |
| NY      | 16:00–24:00 | Trend Breakout (trend) | Profit engine |

### Strategies

**Mean Reversion** (`scalp`) — Bollinger Band bounce with fractal + liquidity sweep confirmation. Enters after price sweeps a fractal level and shows a reaction candle. 1:3 R:R, ATR-based stop loss.

**Trend Breakout** (`trend`) — EMA crossover trend detection with fractal breakout confirmation. Requires strong candle close beyond recent high/low and EMA gap. 1:3 R:R, ATR-based stop loss.

**Strategy C** (`candle3`) — 3 consecutive same-direction 3-minute candles with increasing volume. ATR-based stop loss, no take profit. Holds for one 3m candle (180s) then closes. Designed to hit volume targets, not profit.

### Risk controls

- **Breakeven at 1R**: all strategies move stop loss to entry + fees once price reaches 1R in profit
- **Daily loss limit**: 3% of starting equity (configurable)
- **Consecutive loss limit**: 3 losses in a row pauses trading
- **Order rate limit**: max 20 orders/hour
- **Risk per trade**: 2% of equity (configurable 1–3%)

---

## Project structure

```
├── main.py                  # TradingBot — orchestrator, strategy routing, execution loop
├── backtest.py              # Backtest runner (pybit data source)
├── backtest_bybit.py        # Backtest runner (ccxt data source)
│
├── models/
│   ├── signal.py            # TradeSignal dataclass, Side enum
│   ├── status.py            # BotState, BotMode, BotStatus
│   └── contracts.py         # Protocol interfaces (Strategy, RiskGate, etc.)
│
├── strategies/
│   ├── indicators.py        # SMA, EMA, ATR, VWAP, Bollinger, RSI, Fractals, Sweep
│   ├── mean_reversion.py    # MeanReversionStrategy
│   ├── trend_breakout.py    # TrendBreakoutStrategy
│   ├── strategy_c.py        # StrategyC
│   └── hybrid_a.py          # Legacy helper classes (sweep detector, trend bias)
│
├── execution/
│   ├── session_manager.py   # Session → strategy mapping
│   ├── risk_manager.py      # Daily loss, consecutive loss, rate limits
│   ├── volume_manager.py    # Daily/monthly volume tracking + risk-based sizing
│   ├── position_manager.py  # Open positions, trade records, PnL
│   ├── order_manager.py     # Order placement, retry logic, breakeven
│   ├── exchange_info.py     # Instrument rules (tick size, lot size, min qty)
│   └── qty_utils.py         # Quantity normalization helpers
│
├── exchange/
│   ├── base.py              # ExchangeClient Protocol (interface)
│   ├── bybit_client.py      # Bybit implementation via pybit
│   ├── bybit_stats.py       # Bybit account stats (extracted from client)
│   └── types.py             # OrderResult dataclass
│
├── backtest/
│   ├── data.py              # Data fetching (pybit + ccxt)
│   ├── engine.py            # Backtest simulation engine
│   └── report.py            # Text + JSON report generation
│
├── web/
│   ├── api/server.py        # FastAPI endpoints (start/stop/status)
│   └── frontend/index.html  # Dashboard UI
│
└── tests/
    ├── market_data.py       # Synthetic OHLCV generators (BTC-like price action)
    ├── test_indicators.py   # Indicator unit tests (20 tests)
    ├── test_strategies.py   # Strategy signal tests (13 tests)
    ├── test_execution.py    # Execution layer tests (25 tests)
    ├── test_contracts.py    # Protocol compliance verification (6 tests)
    └── test_integration.py  # Full bot integration + backtest engine (16 tests)
```

---

## Configuration

All config is done via environment variables (for the web server) or directly in code via dataclasses:

| Variable | Default | Description |
|----------|---------|-------------|
| `BYBIT_API_KEY` | — | Bybit API key (required) |
| `BYBIT_API_SECRET` | — | Bybit API secret (required) |
| `BYBIT_TESTNET` | `true` | Use Bybit testnet |
| `BYBIT_DEMO` | `true` | Demo trading mode |
| `BYBIT_CATEGORY` | `linear` | Market category |
| `BYBIT_LEVERAGE` | `50` | Leverage multiplier |
| `BYBIT_MARGIN_MODE` | `isolated` | Margin mode |
| `BOT_FALLBACK_EQUITY` | `500` | Starting equity ($) |
| `BOT_MARGIN_SAFETY_PCT` | `0.20` | Margin safety buffer |
| `HYBRID_COOLDOWN_SEC` | `120` | Cooldown between trades (seconds) |

---

## Contracts (interfaces)

The project uses Python `Protocol` classes for structural typing. This means you don't need to inherit from a base class — just implement the required methods.

Contracts are defined in `models/contracts.py`:

- **`Strategy`** — must have `strategy_id: str` and `generate_signal(candles, size, symbol, timestamp, **kwargs) -> Optional[TradeSignal]`
- **`RiskGate`** — must have `can_trade(equity, timestamp)`, `register_pnl(pnl)`, `register_order(timestamp)`
- **`PositionTracker`** — must have `has_open_position()`, `open_position()`, `close_position()`, `get_position()`, `open_positions_count()`
- **`VolumeTracker`** — must have `daily_volume`, `monthly_volume`, `register_trade()`, `remaining_daily_volume()`, `compute_size()`

The exchange interface is in `exchange/base.py`:

- **`ExchangeClient`** — must have `create_order()`, `close_position()`, `fetch_ohlcv()`, `get_balance()`, `get_last_price()`, `normalize_qty()`, `get_exchange_stats()`

### Adding a new strategy

1. Create a class with `strategy_id` and `generate_signal()` matching the `Strategy` protocol
2. Register it in `main.py` inside `TradingBot.__init__`
3. Add a session routing entry in `execution/session_manager.py`
4. Run `python -m unittest tests.test_contracts -v` to verify compliance

### Adding a new exchange

1. Create a class implementing all methods from `ExchangeClient` in `exchange/base.py`
2. Pass it to `TradingBot(config, your_exchange_client)`
3. No other changes needed — the bot is exchange-agnostic

---

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Redirects to dashboard |
| POST | `/bot/start` | Start bot (body: `{"strategies": ["trend","scalp","candle3"], "test_trade": true}`) |
| POST | `/bot/stop` | Stop bot |
| POST | `/bot/pause` | Pause bot |
| GET | `/bot/status` | Current state, positions, PnL, volume |
