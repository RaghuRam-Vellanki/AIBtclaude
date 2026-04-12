import os
from dotenv import load_dotenv

load_dotenv()

# ── Alpaca ──────────────────────────────────────────────────────────────────
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
PAPER_MODE        = ALPACA_BASE_URL.startswith("https://paper-api")

# ── OpenAI ───────────────────────────────────────────────────────────────────
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL      = "gpt-4o-mini"

# ── Groq (free tier) ──────────────────────────────────────────────────────────
GROQ_API_KEY      = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL        = "llama-3.3-70b-versatile"   # free, better instruction following

# ── Trading ───────────────────────────────────────────────────────────────────
SYMBOL                    = "BTC/USD"
RISK_PCT                  = 0.01        # 1 % risk per trade
DAILY_MAX_LOSS_PCT        = 0.02        # 2 % daily circuit-breaker
MAX_CONSECUTIVE_LOSSES    = 3
ANALYSIS_INTERVAL_SECONDS = 3600       # full re-analysis every 1 hour
MIN_SIGNAL_QUALITY        = 5          # A/A+ only (out of 6)
MIN_STOP_DISTANCE_USD     = 100        # minimum stop in dollars
STOP_LOSS_PCT             = 0.05       # stop loss = 5% from entry price (overrides model suggestion)

# ── Session times (IST = UTC+5:30) ──────────────────────────────────────────
# Stored as (hour, minute) tuples in IST
SESSION_OPENS_IST = {
    "asia":   (0,  0),
    "london": (12, 30),
    "newyork":(18, 30),
}

# ── ATR thresholds ──────────────────────────────────────────────────────────
ATR_REDUCE_25_THRESHOLD  = 2000   # daily ATR > $2 000 → 25 % size reduction
ATR_REDUCE_50_THRESHOLD  = 3500   # daily ATR > $3 500 → 50 % size reduction

# ── Trade approval ───────────────────────────────────────────────────────────
REQUIRE_APPROVAL     = True    # If True, bot waits for dashboard approval before executing
APPROVAL_TIMEOUT_SEC = 300     # Auto-skip if no approval within 5 minutes

# ── Skill file ───────────────────────────────────────────────────────────────
import pathlib
BASE_DIR        = pathlib.Path(__file__).parent
SKILL_FILE      = BASE_DIR / "BTC_INSTITUTIONAL_SKILL.md"
LOG_FILE        = BASE_DIR / "logs" / "trades_log.json"
PENDING_FILE    = BASE_DIR / "logs" / "pending_signal.json"
BOT_PID_FILE    = BASE_DIR / "logs" / "bot_pid.txt"
