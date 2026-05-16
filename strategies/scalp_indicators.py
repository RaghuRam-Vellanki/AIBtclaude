"""
strategies/scalp_indicators.py
Cross-asset scalping confluence (Investopedia top-indicators-for-scalping).

Four classic scalping signals voting together:
  1. EMA crossover  (5 / 13)
  2. Bollinger Bands(20, 2σ) — squeeze/expansion direction
  3. Stochastic (14,3,3) — oversold/overbought reversals
  4. RSI(14) at 30/70

≥ SCALP_MIN_ALIGNED of 4 must agree → vote with confidence = aligned/4.
Any single indicator can produce SHORT (price > BB upper, RSI > 70,
Stoch > 80, EMA-fast < EMA-slow), LONG (mirror).

Designed to be cheap (pure pandas, no external calls) so it can run on every
analysis cycle for BTC / XAU / NIFTY.
"""
from __future__ import annotations

from typing import Any, Dict

import numpy as np
import pandas as pd

from strategies.base import StrategyAgent, StrategyVote
from config_strategies import (
    SCALP_BB_PERIOD,
    SCALP_BB_STDEV,
    SCALP_EMA_FAST,
    SCALP_EMA_SLOW,
    SCALP_MIN_ALIGNED,
    SCALP_RSI_LOWER,
    SCALP_RSI_PERIOD,
    SCALP_RSI_UPPER,
    SCALP_STOCH_D,
    SCALP_STOCH_K,
    SCALP_STOCH_LOWER,
    SCALP_STOCH_SLOW,
    SCALP_STOCH_UPPER,
)


def _ema(s: pd.Series, period: int) -> pd.Series:
    return s.ewm(span=period, adjust=False).mean()


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0)
    dn = -delta.clip(upper=0)
    avg_up = up.ewm(alpha=1 / period, adjust=False).mean()
    avg_dn = dn.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_up / avg_dn.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _bollinger(close: pd.Series, period: int, stdev: float):
    mid = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    upper = mid + stdev * std
    lower = mid - stdev * std
    return mid, upper, lower


def _stochastic(high: pd.Series, low: pd.Series, close: pd.Series,
                k: int, d: int, slow: int):
    lowest = low.rolling(k).min()
    highest = high.rolling(k).max()
    raw_k = 100 * (close - lowest) / (highest - lowest).replace(0, np.nan)
    fast_k = raw_k.rolling(slow).mean()
    slow_d = fast_k.rolling(d).mean()
    return fast_k, slow_d


class ScalpIndicatorsStrategy(StrategyAgent):
    inspired_by = "Investopedia top-scalping confluence (EMA + BB + Stoch + RSI)"
    archetype = "MOMENTUM"

    def __init__(self, asset: str = "GENERIC"):
        self.asset = asset.upper()
        self.name = f"scalp_indicators_{self.asset.lower()}"

    def vote(self, snapshot: Dict[str, Any], feed: Any) -> StrategyVote:
        try:
            tf = "5Min"
            bars = feed.get_bars(tf) if feed is not None else None
            if bars is None or bars.empty or len(bars) < max(SCALP_BB_PERIOD, SCALP_RSI_PERIOD, SCALP_STOCH_K) + 5:
                return self._neutral("Insufficient 5m bars for scalp confluence")

            close = bars["close"].astype(float)
            high = bars["high"].astype(float)
            low = bars["low"].astype(float)

            ema_fast = _ema(close, SCALP_EMA_FAST)
            ema_slow = _ema(close, SCALP_EMA_SLOW)
            rsi = _rsi(close, SCALP_RSI_PERIOD)
            mid, upper, lower = _bollinger(close, SCALP_BB_PERIOD, SCALP_BB_STDEV)
            k, d = _stochastic(high, low, close,
                               SCALP_STOCH_K, SCALP_STOCH_D, SCALP_STOCH_SLOW)

            last_close = float(close.iloc[-1])
            last_ema_fast = float(ema_fast.iloc[-1])
            last_ema_slow = float(ema_slow.iloc[-1])
            last_rsi = float(rsi.iloc[-1]) if not np.isnan(rsi.iloc[-1]) else 50.0
            last_upper = float(upper.iloc[-1])
            last_lower = float(lower.iloc[-1])
            last_mid = float(mid.iloc[-1])
            last_k = float(k.iloc[-1]) if not np.isnan(k.iloc[-1]) else 50.0
            last_d = float(d.iloc[-1]) if not np.isnan(d.iloc[-1]) else 50.0

            signals: dict[str, str] = {}

            # 1. EMA crossover bias
            if last_ema_fast > last_ema_slow and last_close > last_ema_fast:
                signals["ema"] = "LONG"
            elif last_ema_fast < last_ema_slow and last_close < last_ema_fast:
                signals["ema"] = "SHORT"
            else:
                signals["ema"] = "NEUTRAL"

            # 2. Bollinger Band (mean-reversion bias on touch of outer band)
            if last_close <= last_lower:
                signals["bb"] = "LONG"
            elif last_close >= last_upper:
                signals["bb"] = "SHORT"
            else:
                signals["bb"] = "NEUTRAL"

            # 3. Stochastic (cross out of oversold = LONG, out of overbought = SHORT)
            if last_k < SCALP_STOCH_LOWER and last_k > last_d:
                signals["stoch"] = "LONG"
            elif last_k > SCALP_STOCH_UPPER and last_k < last_d:
                signals["stoch"] = "SHORT"
            else:
                signals["stoch"] = "NEUTRAL"

            # 4. RSI extremes
            if last_rsi < SCALP_RSI_LOWER:
                signals["rsi"] = "LONG"
            elif last_rsi > SCALP_RSI_UPPER:
                signals["rsi"] = "SHORT"
            else:
                signals["rsi"] = "NEUTRAL"

            longs = sum(1 for v in signals.values() if v == "LONG")
            shorts = sum(1 for v in signals.values() if v == "SHORT")
            meta = {
                "ema_fast": round(last_ema_fast, 2),
                "ema_slow": round(last_ema_slow, 2),
                "rsi": round(last_rsi, 1),
                "stoch_k": round(last_k, 1),
                "stoch_d": round(last_d, 1),
                "bb_pos": round((last_close - last_mid) / (last_upper - last_lower + 1e-9) * 100, 1),
                "alignment": signals,
                "longs": longs,
                "shorts": shorts,
            }

            if longs >= SCALP_MIN_ALIGNED and longs > shorts:
                conf = float(longs / 4.0)
                return StrategyVote(
                    name=self.name, inspired_by=self.inspired_by,
                    direction="LONG", confidence=conf,
                    rationale=(
                        f"Scalp confluence {longs}/4 LONG: "
                        f"EMA={signals['ema']}, BB={signals['bb']}, "
                        f"Stoch={signals['stoch']} (K={last_k:.0f}), "
                        f"RSI={signals['rsi']} ({last_rsi:.0f})"
                    ),
                    metadata=meta,
                )
            if shorts >= SCALP_MIN_ALIGNED and shorts > longs:
                conf = float(shorts / 4.0)
                return StrategyVote(
                    name=self.name, inspired_by=self.inspired_by,
                    direction="SHORT", confidence=conf,
                    rationale=(
                        f"Scalp confluence {shorts}/4 SHORT: "
                        f"EMA={signals['ema']}, BB={signals['bb']}, "
                        f"Stoch={signals['stoch']} (K={last_k:.0f}), "
                        f"RSI={signals['rsi']} ({last_rsi:.0f})"
                    ),
                    metadata=meta,
                )

            # Whisper-grade: 2-of-4 alignment (one short of the strong-vote threshold).
            if longs == 2 and longs > shorts:
                return StrategyVote(
                    name=self.name, inspired_by=self.inspired_by,
                    direction="LONG", confidence=0.30,
                    rationale=(
                        f"Whisper: 2/4 LONG (EMA={signals['ema']}, BB={signals['bb']}, "
                        f"Stoch={signals['stoch']}, RSI={signals['rsi']})"
                    ),
                    metadata={**meta, "whisper": True},
                )
            if shorts == 2 and shorts > longs:
                return StrategyVote(
                    name=self.name, inspired_by=self.inspired_by,
                    direction="SHORT", confidence=0.30,
                    rationale=(
                        f"Whisper: 2/4 SHORT (EMA={signals['ema']}, BB={signals['bb']}, "
                        f"Stoch={signals['stoch']}, RSI={signals['rsi']})"
                    ),
                    metadata={**meta, "whisper": True},
                )

            return self._neutral(
                f"Scalp confluence weak (L={longs} S={shorts}, need ≥{SCALP_MIN_ALIGNED})",
                **meta,
            )

        except Exception as exc:
            return self._neutral(f"Strategy error: {exc}")
