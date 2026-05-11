import os
from dotenv import load_dotenv

load_dotenv()


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}

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

# Demo mode: use public market data + local strategy calls, no broker required.
HAS_ALPACA_CREDS      = bool(ALPACA_API_KEY and ALPACA_SECRET_KEY)
HAS_GROQ_KEY          = bool(GROQ_API_KEY)
DEMO_MODE             = _env_flag("DEMO_MODE", default=not HAS_ALPACA_CREDS)
ALLOW_BEARISH_SIGNALS = DEMO_MODE

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

# ── Quant-tier reset (Phase 5) ───────────────────────────────────────────────
# Volatility-aware stop placement: SL = entry ± k·ATR(14) where k scales by
# signal grade. Floor on TP1 R-multiple kills sub-2:1 trades that need >67%
# win-rate to break even after fees.
ATR_K_BY_QUALITY = {"A+": 2.5, "A": 2.0, "B": 1.5}
MIN_RR_T1 = 2.0                   # reject signal if TP1 < 2.0R from entry

# BTC perpetual funding-rate gates (Binance fapi premiumIndex; 8h interval)
# 0.0008 = 0.08%/8h ≈ 87% APR → leveraged longs at imminent liq risk
BTC_FUNDING_BLOCK     = 0.0008    # block direction when funding agrees this far
BTC_FUNDING_DOWNGRADE = 0.0005    # downgrade A→B if same-direction funding ≥ this

# Position max-hold enforcement (was metadata-only; now closed at market on breach)
MAX_HOLD_HOURS_DEFAULT = 8        # used when TradeSignal.max_hold_time unparseable

# TP-ladder scale-out fractions (sum to 1.0). Symmetric for LONG/SHORT.
TP_LADDER_FRACTIONS = {"tp1": 0.40, "tp2": 0.35, "tp3": 0.25}

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

# ── BTC pod (Phase 4) ────────────────────────────────────────────────────────
BTC_MIN_POD_SCORE     = 0.5
BTC_POD_REPORT_FILE   = BASE_DIR / "logs" / "btc_pod_report.json"

# ── XAU/USD ──────────────────────────────────────────────────────────────────
XAU_SYMBOL                 = "XAU/USD"
XAU_YFINANCE_SYMBOL        = "GC=F"
XAU_RISK_PCT               = 0.01
XAU_STOP_LOSS_PCT          = 0.03            # 3% — gold ATR/price ratio is lower than BTC
XAU_ANALYSIS_INTERVAL      = 3600
XAU_MIN_POD_SCORE          = 0.8             # |sum of 9 votes| must exceed this for a trade
                                              # (rebalanced for Phase 4 expanded pod — 9 strategies)
XAU_PAPER_STARTING_BALANCE = 10_000.0        # USD — local paper sim bank
XAU_APPROVAL_TIMEOUT_SEC   = 300

XAU_SKILL_FILE      = BASE_DIR / "XAU_INSTITUTIONAL_SKILL.md"
XAU_STATE_FILE      = BASE_DIR / "logs" / "xau_state.json"
XAU_PENDING_FILE    = BASE_DIR / "logs" / "xau_pending_signal.json"
XAU_BOT_PID_FILE    = BASE_DIR / "logs" / "xau_bot_pid.txt"
XAU_TRADES_LOG      = BASE_DIR / "logs" / "xau_trades_log.json"
XAU_POD_REPORT_FILE = BASE_DIR / "logs" / "xau_pod_report.json"
XAU_AGENT_LOG       = BASE_DIR / "logs" / "xau_agent.log"
XAU_ANALYZE_TRIGGER = BASE_DIR / "logs" / "xau_analyze_now.json"
XAU_COT_CACHE       = BASE_DIR / "logs" / ".cot_cache.json"

# ── NIFTY 50 ─────────────────────────────────────────────────────────────────
NIFTY_SYMBOL                 = "NIFTY 50"
NIFTY_YFINANCE_SYMBOL        = "^NSEI"
NIFTY_BN_YFINANCE_SYMBOL     = "^NSEBANK"
NIFTY_USDINR_SYMBOL          = "USDINR=X"
NIFTY_VIX_SYMBOL             = "^INDIAVIX"
NIFTY_RISK_PCT               = 0.01
NIFTY_STOP_LOSS_PCT          = 0.015         # 1.5% — index ATR/price ratio is much lower than gold
NIFTY_ANALYSIS_INTERVAL      = 1800          # 30 min — index moves slower than gold
NIFTY_MIN_POD_SCORE          = 0.8           # |sum of 11 votes| must exceed for a trade
                                              # (rebalanced for Phase 4 expanded pod — 11 strategies)
NIFTY_PAPER_STARTING_BALANCE = 200_000.0     # INR — realistic solotrader account
NIFTY_LOT_SIZE               = 75            # NIFTY index futures lot (FY26 spec)
NIFTY_APPROVAL_TIMEOUT_SEC   = 300

NIFTY_SKILL_FILE      = BASE_DIR / "NIFTY_INSTITUTIONAL_SKILL.md"
NIFTY_STATE_FILE      = BASE_DIR / "logs" / "nifty_state.json"
NIFTY_PENDING_FILE    = BASE_DIR / "logs" / "nifty_pending_signal.json"
NIFTY_BOT_PID_FILE    = BASE_DIR / "logs" / "nifty_bot_pid.txt"
NIFTY_TRADES_LOG      = BASE_DIR / "logs" / "nifty_trades_log.json"
NIFTY_POD_REPORT_FILE = BASE_DIR / "logs" / "nifty_pod_report.json"
NIFTY_AGENT_LOG       = BASE_DIR / "logs" / "nifty_agent.log"
NIFTY_ANALYZE_TRIGGER = BASE_DIR / "logs" / "nifty_analyze_now.json"
NSE_CACHE_FILE        = BASE_DIR / "logs" / ".nse_cache.json"
