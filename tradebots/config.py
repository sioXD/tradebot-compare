"""Zentrale Konfiguration. Alles wird aus Umgebungsvariablen / .env gelesen."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "bots.db"

BOT_NAMES = ["wsb", "truth", "armstrong"]

# LLM (OpenCode Zen ist OpenAI-kompatibel)
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://opencode.ai/zen/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("OPENCODE_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")
# Komma-getrennte Liste: erstes Modell bevorzugt, bei Limit/Fehler -> naechstes
LLM_MODELS = [m.strip() for m in LLM_MODEL.split(",") if m.strip()]
LLM_COOLDOWN_MINUTES = int(os.getenv("LLM_COOLDOWN_MINUTES", "60"))

# Reddit
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "")
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT", "tradebot-compare/0.1 (paper trading research)")

# Feeds
TRUTH_FEED_URL = os.getenv("TRUTH_FEED_URL", "https://trumpstruth.org/feed")
ARMSTRONG_FEED_URL = os.getenv("ARMSTRONG_FEED_URL", "https://www.armstrongeconomics.com/feed/")
HTTP_USER_AGENT = os.getenv("HTTP_USER_AGENT", "Mozilla/5.0 (compatible; tradebot-compare/0.1; RSS reader)")

# Pro-Bot-Defaults (koennen per <BOT>_<KEY> in .env ueberschrieben werden)
_DEFAULTS: dict[str, dict] = {
    "wsb": dict(FETCH_MINUTES=15, SIGNAL_TTL_HOURS=6, MAX_HOLD_DAYS=5, MAX_ITEM_AGE_HOURS=24, UNIVERSE=""),
    "truth": dict(FETCH_MINUTES=10, SIGNAL_TTL_HOURS=4, MAX_HOLD_DAYS=5, MAX_ITEM_AGE_HOURS=18, UNIVERSE=""),
    "armstrong": dict(
        FETCH_MINUTES=60,
        SIGNAL_TTL_HOURS=48,
        MAX_HOLD_DAYS=30,
        MAX_ITEM_AGE_HOURS=72,
        UNIVERSE="SPY,QQQ,DIA,IWM,GLD,SLV,GDX,TLT,IEF,SHY,UUP,FXE,FXY,USO,UNG,EEM,EWG,FXI,XLE,XLF,IBIT",
    ),
}
_GLOBAL = dict(
    MIN_CONFIDENCE=0.65,
    POSITION_PCT=0.10,  # Anteil des Equity pro Position
    MAX_POSITIONS=8,
    STOP_LOSS_PCT=0.08,
    TAKE_PROFIT_PCT=0.20,
    TRADE_CHECK_MINUTES=5,
)


def _get(bot: str, key: str, cast=str):
    raw = os.getenv(f"{bot.upper()}_{key}")
    if raw in (None, ""):
        raw = os.getenv(f"DEFAULT_{key}")
    if raw in (None, ""):
        raw = _DEFAULTS.get(bot, {}).get(key, _GLOBAL.get(key))
    return cast(raw) if raw is not None else None


@dataclass
class BotConfig:
    name: str
    alpaca_key: str
    alpaca_secret: str
    fetch_minutes: int
    trade_check_minutes: int
    min_confidence: float
    position_pct: float
    max_positions: int
    stop_loss_pct: float
    take_profit_pct: float
    max_hold_days: int
    signal_ttl_hours: int
    max_item_age_hours: int
    universe: list[str] = field(default_factory=list)


def bot_config(bot: str) -> BotConfig:
    if bot not in BOT_NAMES:
        raise ValueError(f"Unbekannter Bot '{bot}'. Erlaubt: {BOT_NAMES}")
    p = bot.upper()
    uni = _get(bot, "UNIVERSE") or ""
    return BotConfig(
        name=bot,
        alpaca_key=os.getenv(f"ALPACA_{p}_KEY", ""),
        alpaca_secret=os.getenv(f"ALPACA_{p}_SECRET", ""),
        fetch_minutes=_get(bot, "FETCH_MINUTES", int),
        trade_check_minutes=_get(bot, "TRADE_CHECK_MINUTES", int),
        min_confidence=_get(bot, "MIN_CONFIDENCE", float),
        position_pct=_get(bot, "POSITION_PCT", float),
        max_positions=_get(bot, "MAX_POSITIONS", int),
        stop_loss_pct=_get(bot, "STOP_LOSS_PCT", float),
        take_profit_pct=_get(bot, "TAKE_PROFIT_PCT", float),
        max_hold_days=_get(bot, "MAX_HOLD_DAYS", int),
        signal_ttl_hours=_get(bot, "SIGNAL_TTL_HOURS", int),
        max_item_age_hours=_get(bot, "MAX_ITEM_AGE_HOURS", int),
        universe=[s.strip().upper() for s in uni.split(",") if s.strip()],
    )
