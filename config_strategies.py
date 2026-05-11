"""
config_strategies.py
Central tunable parameters for the 8 institutional strategies added in Phase 4
(BlackRock-tier expansion). Pure constants — no imports, no side effects —
so any strategy / signal generator / backtest can read these without
triggering loads of yfinance, requests etc.
"""
from __future__ import annotations

# ── Order-flow / liquidity (JPM-style stop-run reversal) ─────────────────────
ORDERFLOW_VOL_MULT_ASIA   = 1.5      # Asia-session volume multiplier vs avg
ORDERFLOW_VOL_MULT_LONDON = 2.0      # London/NY-session volume multiplier
ORDERFLOW_WICK_RATIO      = 0.40     # min wick / candle-range to count as absorption
ORDERFLOW_SWEEP_PCT       = 0.0005   # 0.05% beyond PDH/PDL counts as a sweep
ORDERFLOW_VOL_LOOKBACK    = 30       # bars used to compute "avg" volume

# ── VWAP Bandit (BlackRock Aladdin execution) ────────────────────────────────
VWAP_BANDIT_ZSCORE_BAND   = (-0.6, -0.3)   # accumulation band (LONG); SHORT = mirror
VWAP_BANDIT_DURATION_BARS = 6              # consecutive bars within band
VWAP_BANDIT_ROLLING_WIN   = 60             # bars used in rolling VWAP / std

# ── Volatility regime (BlackRock RiskMetrics / Heston) ───────────────────────
VOL_REGIME_IV_HV_RATIO    = 1.4      # IV/HV ratio above this = sell-vol bias
VOL_REGIME_HV_LOOKBACK    = 20       # daily bars for realized vol
VOL_REGIME_MIN_VIX        = 10.0     # below this VIX is meaningless
VOL_REGIME_MAX_VIX        = 35.0     # above = risk-off, NEUTRAL

# ── OI crossover (NSE FNO desk style for NIFTY) ─────────────────────────────
OI_LOOKBACK_HOURS         = 4        # window for ΔOI / Δprice
OI_PRICE_THRESHOLD        = 0.002    # 0.2% min price move to be "significant"
OI_DELTA_THRESHOLD        = 0.05     # 5% min OI change to be "significant"

# ── Scalping confluence (Investopedia top-indicators piece) ──────────────────
SCALP_EMA_FAST            = 5
SCALP_EMA_SLOW            = 13
SCALP_RSI_PERIOD          = 14
SCALP_RSI_UPPER           = 70
SCALP_RSI_LOWER           = 30
SCALP_STOCH_K             = 14
SCALP_STOCH_D             = 3
SCALP_STOCH_SLOW          = 3
SCALP_STOCH_UPPER         = 80
SCALP_STOCH_LOWER         = 20
SCALP_BB_PERIOD           = 20
SCALP_BB_STDEV            = 2.0
SCALP_MIN_ALIGNED         = 3        # of 4 indicators required to vote

# ── Greeks proxy (BlackRock Aladdin / RiskMetrics on NIFTY chain) ────────────
GREEKS_RISK_FREE_RATE     = 0.07     # India 10-year proxy (~7%)
GREEKS_DAYS_PER_YEAR      = 365
GREEKS_GAMMA_WALL_TOP_N   = 3        # top N strikes by Σγ×OI
GREEKS_PUT_WALL_BUFFER    = 0.005    # 0.5% below put wall = LONG zone
GREEKS_CALL_WALL_BUFFER   = 0.005    # 0.5% above call wall = SHORT zone
GREEKS_DEFAULT_IV         = 0.15     # fallback IV when chain doesn't expose it

# ── Session-adaptive volume thresholds (JPM volume desk) ─────────────────────
SESSION_ASIA_HOURS_IST    = (0, 7)             # 00:00–07:00 IST
SESSION_LONDON_HOURS_IST  = (12, 17)           # 12:30 → simplified 12-17
SESSION_NY_HOURS_IST      = (18, 22)           # 18:30 → simplified 18-22
SESSION_VOL_LOOKBACK      = 20                 # bars for avg volume comparison

# ── BTC microstructure ──────────────────────────────────────────────────────
BTC_MICRO_ZSCORE_THRESH   = 2.0      # |Z| from VWAP triggers vote
BTC_MICRO_WICK_BOOST      = 0.5      # wick imbalance contribution to confidence
