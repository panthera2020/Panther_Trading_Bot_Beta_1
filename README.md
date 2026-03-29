# Panther Trading Bot Beta 1

Clean-slate trading volume engine for BTCUSDT with risk controls, strategy isolation, and backtest tooling.

## Clean-slate intent (source of truth)
This repo is now aligned to the clean-slate master document:
- Clean-slate master document: https://polyalphablobstorage.blob.core.windows.net/artifacts-execution-3cf79f65-34f9-49eb-9eb3-c1f3af949ee0/20260329_185925_panther_trading_bot_clean_slate_master_document_2026-03-29.md
- Clean-slate intent SSOT: https://polyalphablobstorage.blob.core.windows.net/artifacts-execution-3cf79f65-34f9-49eb-9eb3-c1f3af949ee0/20260329_185818_panther_trading_bot_clean_slate_intent_ssot_2026-03-29.md

### Core objective
- Primary objective: generate sustainable monthly trading volume
- Risk objective: preserve principal and prevent burn-out
- Validation model: isolate strategy performance first, then combine only proven strategies

## Current integrated state
This clean-slate integration includes the previously open PR work merged into one coherent baseline:
- Strategy C crash fixes and volume guard fixes
- Candle3 disable paths where required
- Risk sizing updates (1-3% range implementations, baseline 2%)
- Session and throughput improvements
- Backtest engines added (`backtest.py`, `backtest_bybit.py`)
- MeanReversion upgrades with trend filter and threshold tuning
- Monthly target alignment updates (including $1M target path)

## Repository structure
- `main.py`: bot orchestration, execution loop, strategy routing
- `execution/`: risk, session, volume, order and position controls
- `strategies/`: TrendBreakout, MeanReversion, StrategyC modules
- `backtest.py`: integrated backtest runner
- `backtest_bybit.py`: Bybit-focused backtest workflow
- `models/`, `exchange/`, `web/`: supporting modules

## Run
```bash
python main.py
```

## Backtest
```bash
python backtest.py
python backtest_bybit.py --period 3
python backtest_bybit.py --period 6
```

## Clean-slate operating rule
Do not stack overlapping hotfix PRs directly into `main` again. Land changes through one coherent branch with compile/backtest verification.
