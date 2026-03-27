from __future__ import annotations

import os
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.responses import RedirectResponse

from exchange.bybit_client import BybitClient, BybitConfig
from main import BotConfig, TradingBot

logger = logging.getLogger(__name__)

app = FastAPI()
frontend_dir = Path(__file__).resolve().parents[1] / "frontend"
app.mount("/app", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/app")


class BotActionResponse(BaseModel):
    status: str


class StartBotRequest(BaseModel):
    strategies: list[str] | None = None
    test_trade: bool = True


api_key = os.getenv("BYBIT_API_KEY", "")
api_secret = os.getenv("BYBIT_API_SECRET", "")
if not api_key or not api_secret:
    # FIXED: Log a warning instead of crashing the entire app at import time.
    # This allows the server to start for health checks and configuration,
    # and the bot simply won't be able to trade until keys are provided.
    logger.error(
        "BYBIT_API_KEY/BYBIT_API_SECRET not set. "
        "The bot will not be able to connect to the exchange. "
        "Set these environment variables and restart."
    )
    exchange_client = None
    bot = None
else:
    exchange_client = BybitClient(
        BybitConfig(
            api_key=api_key,
            api_secret=api_secret,
            testnet=os.getenv("BYBIT_TESTNET", "true").lower() in {"1", "true", "yes"},
            demo=os.getenv("BYBIT_DEMO", "true").lower() in {"1", "true", "yes"},
            category=os.getenv("BYBIT_CATEGORY", "linear"),
            recv_window=int(os.getenv("BYBIT_RECV_WINDOW", "10000")),
            timeout=int(os.getenv("BYBIT_TIMEOUT", "20")),
            margin_mode=os.getenv("BYBIT_MARGIN_MODE", "isolated"),
            position_mode=os.getenv("BYBIT_POSITION_MODE", "oneway"),
            leverage=int(os.getenv("BYBIT_LEVERAGE", "50")),
        )
    )
    bot = TradingBot(
        BotConfig(
            use_hybrid_trend=os.getenv("USE_HYBRID_TREND", "true").lower() in {"1", "true", "yes"},
            max_spread=float(os.getenv("HYBRID_MAX_SPREAD", "2.0")),
            vol_spike_mult=float(os.getenv("HYBRID_VOL_SPIKE_MULT", "3.0")),
            entry_wait_minutes=int(os.getenv("HYBRID_ENTRY_WAIT_MIN", "5")),
            cooldown_seconds=int(os.getenv("HYBRID_COOLDOWN_SEC", "120")),
            # NEW: equity is just a fallback default; the bot fetches real balance on startup
            equity=float(os.getenv("BOT_FALLBACK_EQUITY", "500")),  # Small account safe default
            margin_safety_pct=float(os.getenv("BOT_MARGIN_SAFETY_PCT", "0.20")),
            balance_cache_ttl=float(os.getenv("BOT_BALANCE_CACHE_TTL", "15.0")),
        ),
        exchange_client,
    )


def _require_bot():
    """Raise an error if the bot is not configured."""
    if bot is None:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=503,
            detail="Bot not configured. Set BYBIT_API_KEY and BYBIT_API_SECRET environment variables.",
        )
    return bot


@app.post("/bot/start", response_model=BotActionResponse)
def start_bot(payload: StartBotRequest) -> BotActionResponse:
    b = _require_bot()
    strategies = payload.strategies or ["trend", "scalp", "candle3"]
    b.start(strategies=strategies, run_test_trade=payload.test_trade)
    return BotActionResponse(status="ok")


@app.post("/bot/stop", response_model=BotActionResponse)
def stop_bot() -> BotActionResponse:
    b = _require_bot()
    b.stop()
    return BotActionResponse(status="ok")


@app.post("/bot/pause", response_model=BotActionResponse)
def pause_bot() -> BotActionResponse:
    b = _require_bot()
    b.pause()
    return BotActionResponse(status="ok")


@app.post("/bot/terminate", response_model=BotActionResponse)
def terminate_bot() -> BotActionResponse:
    b = _require_bot()
    b.terminate()
    return BotActionResponse(status="ok")


@app.get("/bot/status")
def bot_status() -> dict:
    if bot is None:
        return {
            "state": "not_configured",
            "mode": "idle",
            "last_error": "BYBIT_API_KEY/BYBIT_API_SECRET not set",
        }
    status = bot.status()
    return {
        "state": status.state.value,
        "mode": status.mode.value,
        "daily_volume": status.daily_volume,
        "daily_target": status.daily_target,
        "monthly_volume": status.monthly_volume,
        "exchange_volume": status.exchange_volume,
        "strategy_volume": status.strategy_volume,
        "open_positions": status.open_positions,
        "last_error": status.last_error,
        "balance": status.balance,
        "trade_stats": status.trade_stats,
        "open_trades": status.open_trades,
        "closed_trades": status.closed_trades,
        "execution_events": status.execution_events,
    }