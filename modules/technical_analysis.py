"""
technical_analysis.py
Computes all institutional indicators:
  - VWAP (anchored to session open)
  - Fair Value Gaps (FVG)
  - Equal Highs / Equal Lows (liquidity clusters)
  - ATR (14-period)
  - Key levels (PDH, PDL, round numbers, swing H/L)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class FVG:
    top:       float
    bottom:    float
    direction: str    # "bullish" or "bearish"
    timeframe: str
    timestamp: datetime
    filled:    bool = False

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def size(self) -> float:
        return self.top - self.bottom


@dataclass
class LiquidityCluster:
    price:     float
    direction: str    # "buy_stops" (above price) or "sell_stops" (below price)
    touches:   int
    timeframe: str


@dataclass
class KeyLevels:
    pdh:          Optional[float] = None   # Previous day high
    pdl:          Optional[float] = None   # Previous day low
    weekly_open:  Optional[float] = None
    round_numbers: List[float] = field(default_factory=list)
    swing_highs:  List[float] = field(default_factory=list)
    swing_lows:   List[float] = field(default_factory=list)


# ── VWAP ─────────────────────────────────────────────────────────────────────

def calculate_vwap(bars: pd.DataFrame) -> float:
    """
    Volume-weighted average price from the provided bars.
    bars must have columns: high, low, close, volume.
    Returns the current VWAP value.
    """
    if bars.empty or len(bars) < 2:
        return 0.0
    typical_price = (bars["high"] + bars["low"] + bars["close"]) / 3
    cum_tpv = (typical_price * bars["volume"]).cumsum()
    cum_vol = bars["volume"].cumsum()
    vwap_series = cum_tpv / cum_vol
    return float(vwap_series.iloc[-1])


# ── Fair Value Gaps ───────────────────────────────────────────────────────────

def detect_fvg(bars: pd.DataFrame, timeframe: str = "15Min",
               min_size: float = 100.0) -> List[FVG]:
    """
    Detect Fair Value Gaps (3-candle imbalance patterns).

    Bullish FVG: gap between candle[i-2].high and candle[i].low
                 (price skipped upward leaving unfilled zone)
    Bearish FVG: gap between candle[i-2].low and candle[i].high
                 (price skipped downward leaving unfilled zone)

    Returns list of FVG objects (unfilled only).
    """
    if bars.empty or len(bars) < 3:
        return []

    gaps: List[FVG] = []
    closes = bars["close"].values
    highs  = bars["high"].values
    lows   = bars["low"].values
    timestamps = bars.index.tolist() if hasattr(bars.index, "tolist") else list(range(len(bars)))
    current_price = float(closes[-1])

    for i in range(2, len(bars)):
        # Bullish FVG: candle[i-2] high < candle[i] low
        bull_top    = float(lows[i])
        bull_bottom = float(highs[i - 2])
        if bull_bottom < bull_top and (bull_top - bull_bottom) >= min_size:
            # Check if already filled (current price has traded through)
            filled = current_price <= bull_bottom
            gaps.append(FVG(
                top=bull_top, bottom=bull_bottom,
                direction="bullish", timeframe=timeframe,
                timestamp=timestamps[i], filled=filled,
            ))

        # Bearish FVG: candle[i-2] low > candle[i] high
        bear_bottom = float(highs[i])
        bear_top    = float(lows[i - 2])
        if bear_top > bear_bottom and (bear_top - bear_bottom) >= min_size:
            filled = current_price >= bear_top
            gaps.append(FVG(
                top=bear_top, bottom=bear_bottom,
                direction="bearish", timeframe=timeframe,
                timestamp=timestamps[i], filled=filled,
            ))

    # Return only unfilled, most recent 5
    unfilled = [g for g in gaps if not g.filled]
    return unfilled[-5:]


# ── Equal Highs / Equal Lows (Liquidity Clusters) ────────────────────────────

def find_equal_highs_lows(bars: pd.DataFrame, timeframe: str = "1Hour",
                           tolerance: float = 0.003) -> List[LiquidityCluster]:
    """
    Identify equal highs (buy-stop clusters) and equal lows (sell-stop clusters).
    tolerance: fraction of price to consider "equal" (default 0.3%)
    """
    if bars.empty or len(bars) < 5:
        return []

    clusters: List[LiquidityCluster] = []
    highs = bars["high"].values
    lows  = bars["low"].values

    def group_levels(levels: np.ndarray, direction: str) -> List[LiquidityCluster]:
        result = []
        used = [False] * len(levels)
        for i in range(len(levels)):
            if used[i]:
                continue
            group = [levels[i]]
            for j in range(i + 1, len(levels)):
                if used[j]:
                    continue
                if abs(levels[j] - levels[i]) / levels[i] <= tolerance:
                    group.append(levels[j])
                    used[j] = True
            if len(group) >= 2:
                avg_price = float(np.mean(group))
                result.append(LiquidityCluster(
                    price=avg_price,
                    direction=direction,
                    touches=len(group),
                    timeframe=timeframe,
                ))
        return result

    clusters.extend(group_levels(highs, "buy_stops"))
    clusters.extend(group_levels(lows,  "sell_stops"))
    # Sort by number of touches (most-tested = most liquid)
    clusters.sort(key=lambda c: c.touches, reverse=True)
    return clusters[:6]  # top 6 clusters


# ── ATR ───────────────────────────────────────────────────────────────────────

def calculate_atr(bars: pd.DataFrame, period: int = 14) -> float:
    """14-period Average True Range."""
    if bars.empty or len(bars) < period + 1:
        return 0.0
    high  = bars["high"]
    low   = bars["low"]
    close = bars["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    return float(atr) if not np.isnan(atr) else 0.0


# ── Key Levels ────────────────────────────────────────────────────────────────

def get_key_levels(bars_1h: pd.DataFrame, bars_daily: pd.DataFrame,
                   current_price: float) -> KeyLevels:
    """Extract PDH, PDL, weekly open, round numbers, and swing points."""
    levels = KeyLevels()

    # PDH / PDL from daily bars
    if not bars_daily.empty and len(bars_daily) >= 2:
        yesterday = bars_daily.iloc[-2]
        levels.pdh = float(yesterday["high"])
        levels.pdl = float(yesterday["low"])

    # Weekly open (first bar of the week — Monday)
    if not bars_daily.empty:
        df = bars_daily.copy()
        df.index = pd.to_datetime(df.index)
        week_bars = df[df.index.weekday == 0]  # Monday
        if not week_bars.empty:
            levels.weekly_open = float(week_bars.iloc[-1]["open"])

    # Round numbers within ±5% of current price
    if current_price > 0:
        step = 1000 if current_price > 10000 else 500
        lo = int(current_price * 0.95 / step) * step
        hi = int(current_price * 1.05 / step + 1) * step
        levels.round_numbers = list(range(lo, hi + step, step))

    # Swing highs/lows from 1H bars (simple pivot detection)
    if not bars_1h.empty and len(bars_1h) >= 5:
        highs = bars_1h["high"].values
        lows  = bars_1h["low"].values
        for i in range(2, len(highs) - 2):
            if highs[i] > highs[i-1] and highs[i] > highs[i-2] and \
               highs[i] > highs[i+1] and highs[i] > highs[i+2]:
                levels.swing_highs.append(float(highs[i]))
            if lows[i] < lows[i-1] and lows[i] < lows[i-2] and \
               lows[i] < lows[i+1] and lows[i] < lows[i+2]:
                levels.swing_lows.append(float(lows[i]))
        levels.swing_highs = sorted(set(levels.swing_highs))[-5:]
        levels.swing_lows  = sorted(set(levels.swing_lows))[:5]

    return levels


# ── Market Structure ──────────────────────────────────────────────────────────

def classify_structure(bars: pd.DataFrame) -> str:
    """
    Classify market structure from OHLC bars.
    Returns: "uptrend", "downtrend", or "ranging"
    """
    if bars.empty or len(bars) < 10:
        return "ranging"

    highs  = bars["high"].values
    lows   = bars["low"].values
    closes = bars["close"].values

    # Simple HH/HL or LH/LL check on last 10 candles
    n = min(10, len(bars))
    recent_highs = highs[-n:]
    recent_lows  = lows[-n:]

    higher_highs = sum(recent_highs[i] > recent_highs[i-1] for i in range(1, n))
    lower_lows   = sum(recent_lows[i]  < recent_lows[i-1]  for i in range(1, n))
    lower_highs  = sum(recent_highs[i] < recent_highs[i-1] for i in range(1, n))
    higher_lows  = sum(recent_lows[i]  > recent_lows[i-1]  for i in range(1, n))

    if higher_highs >= 6 and higher_lows >= 5:
        return "uptrend"
    if lower_lows >= 6 and lower_highs >= 5:
        return "downtrend"
    return "ranging"


# ── Asian Range ───────────────────────────────────────────────────────────────

def get_asian_range(bars_1m: pd.DataFrame) -> Tuple[float, float]:
    """
    Extract today's Asian session range from 1m bars (00:00–08:30 IST).
    IST = UTC+5:30
    Returns (high, low).
    """
    if bars_1m.empty:
        return 0.0, 0.0

    df = bars_1m.copy()
    df.index = pd.to_datetime(df.index, utc=True)
    # Convert to IST (UTC+5:30)
    df.index = df.index + pd.Timedelta(hours=5, minutes=30)

    today = df.index[-1].date()
    session_start = pd.Timestamp(f"{today} 00:00", tz="UTC") + pd.Timedelta(hours=5, minutes=30)
    session_end   = pd.Timestamp(f"{today} 08:30", tz="UTC") + pd.Timedelta(hours=5, minutes=30)

    asia_bars = df[(df.index >= session_start) & (df.index <= session_end)]
    if asia_bars.empty:
        return 0.0, 0.0

    return float(asia_bars["high"].max()), float(asia_bars["low"].min())


# ── Z-score (Quant layer) ─────────────────────────────────────────────────────

def price_zscore(bars: pd.DataFrame, period: int = 20) -> float:
    """Z-score of current close vs rolling mean — measures extension from mean."""
    if bars.empty or len(bars) < period:
        return 0.0
    closes = bars["close"].values
    mu = np.mean(closes[-period:])
    sigma = np.std(closes[-period:])
    if sigma == 0:
        return 0.0
    return float((closes[-1] - mu) / sigma)
