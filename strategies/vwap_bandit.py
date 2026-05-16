"""
strategies/vwap_bandit.py
BlackRock Aladdin "VWAP Bandit" execution-pattern detector.

Aladdin's accumulation algos buy in small clips when price trades persistently
*just below* VWAP — typically 0.3-0.6σ below — without the price breaking
down. The pattern is the institution patiently filling without spiking the
tape. We detect it by:

  1. Compute rolling 60-bar VWAP and rolling stdev of (close - VWAP).
  2. Z-score = (close - VWAP) / stdev.
  3. If ≥ DURATION_BARS consecutive bars sit inside the accumulation Z-band
     (default −0.6 < Z < −0.3) AND price hasn't made a fresh low,
     vote LONG (institutional buying).
  4. Mirror for distribution: ≥ DURATION_BARS bars in +0.3 < Z < +0.6 AND
     no fresh high → SHORT.
"""
from __future__ import annotations

from typing import Any, Dict

import numpy as np
import pandas as pd

from strategies.base import StrategyAgent, StrategyVote
from config_strategies import (
    VWAP_BANDIT_DURATION_BARS,
    VWAP_BANDIT_ROLLING_WIN,
    VWAP_BANDIT_ZSCORE_BAND,
)


def _rolling_vwap(close: pd.Series, volume: pd.Series, period: int) -> pd.Series:
    """Volume-weighted rolling mean of close. Falls back to simple rolling
    mean when volume is all-zero (e.g. yfinance for indices)."""
    if volume is None or volume.fillna(0).sum() == 0:
        return close.rolling(period).mean()
    pv = close * volume
    return pv.rolling(period).sum() / volume.rolling(period).sum()


class VWAPBanditStrategy(StrategyAgent):
    inspired_by = "BlackRock Aladdin (VWAP-bandit accumulation/distribution)"
    archetype = "MEAN_REVERT"

    def __init__(self, asset: str = "GENERIC"):
        self.asset = asset.upper()
        self.name = f"vwap_bandit_{self.asset.lower()}"

    def vote(self, snapshot: Dict[str, Any], feed: Any) -> StrategyVote:
        try:
            bars = feed.get_bars("5Min") if feed is not None else None
            if bars is None or bars.empty or len(bars) < VWAP_BANDIT_ROLLING_WIN + VWAP_BANDIT_DURATION_BARS:
                return self._neutral("Insufficient 5m bars for VWAP-bandit")

            close = bars["close"].astype(float)
            vol = bars.get("volume", None)
            if vol is not None:
                vol = vol.astype(float).fillna(0)

            vwap = _rolling_vwap(close, vol if vol is not None else pd.Series(np.zeros(len(close))),
                                 VWAP_BANDIT_ROLLING_WIN)
            spread = close - vwap
            std = spread.rolling(VWAP_BANDIT_ROLLING_WIN).std(ddof=0)
            z = spread / std.replace(0, np.nan)

            recent_z = z.tail(VWAP_BANDIT_DURATION_BARS).values
            recent_close = close.tail(VWAP_BANDIT_DURATION_BARS).values

            band_lo, band_hi = VWAP_BANDIT_ZSCORE_BAND       # e.g. (-0.6, -0.3)
            mirror_lo, mirror_hi = -band_hi, -band_lo        # (+0.3, +0.6)

            if np.all(np.isnan(recent_z)):
                return self._neutral("VWAP Z-series not computable")

            in_accum = np.all((recent_z >= band_lo) & (recent_z <= band_hi))
            in_distr = np.all((recent_z >= mirror_lo) & (recent_z <= mirror_hi))

            # "Holding without fresh low" == today's low > min of broader window
            broader_low = float(close.tail(VWAP_BANDIT_ROLLING_WIN).min())
            broader_high = float(close.tail(VWAP_BANDIT_ROLLING_WIN).max())
            current = float(recent_close[-1])
            holds_low = recent_close.min() > broader_low * 1.001    # no fresh low
            holds_high = recent_close.max() < broader_high * 0.999  # no fresh high

            cur_z = float(recent_z[-1]) if not np.isnan(recent_z[-1]) else 0.0

            meta = {
                "current_z": round(cur_z, 2),
                "vwap": round(float(vwap.iloc[-1]) if not np.isnan(vwap.iloc[-1]) else 0, 2),
                "in_accumulation_band": bool(in_accum),
                "in_distribution_band": bool(in_distr),
                "holds_low": bool(holds_low),
                "holds_high": bool(holds_high),
                "duration_bars": VWAP_BANDIT_DURATION_BARS,
            }

            if in_accum and holds_low:
                conf = float(min(0.85, 0.5 + abs(cur_z) * 0.4))
                return StrategyVote(
                    name=self.name, inspired_by=self.inspired_by,
                    direction="LONG", confidence=conf,
                    rationale=(
                        f"{VWAP_BANDIT_DURATION_BARS} bars in accumulation band "
                        f"({band_lo:+.1f} ≤ Z ≤ {band_hi:+.1f}, last={cur_z:+.2f}) "
                        f"without fresh low — Aladdin-style accumulation"
                    ),
                    metadata=meta,
                )

            if in_distr and holds_high:
                conf = float(min(0.85, 0.5 + abs(cur_z) * 0.4))
                return StrategyVote(
                    name=self.name, inspired_by=self.inspired_by,
                    direction="SHORT", confidence=conf,
                    rationale=(
                        f"{VWAP_BANDIT_DURATION_BARS} bars in distribution band "
                        f"({mirror_lo:+.1f} ≤ Z ≤ {mirror_hi:+.1f}, last={cur_z:+.2f}) "
                        f"without fresh high — Aladdin-style distribution"
                    ),
                    metadata=meta,
                )

            # Whisper-grade: persistent VWAP offset outside the accumulation
            # band but with |Z| ≥ 0.5 and the broader hold-low/hold-high
            # property — early read on accumulation/distribution.
            if cur_z <= -0.5 and holds_low:
                return StrategyVote(
                    name=self.name, inspired_by=self.inspired_by,
                    direction="LONG", confidence=0.25,
                    rationale=(
                        f"Whisper: Z={cur_z:+.2f} below VWAP with no fresh low "
                        f"(pre-accumulation lean)"
                    ),
                    metadata={**meta, "whisper": True},
                )
            if cur_z >= 0.5 and holds_high:
                return StrategyVote(
                    name=self.name, inspired_by=self.inspired_by,
                    direction="SHORT", confidence=0.25,
                    rationale=(
                        f"Whisper: Z={cur_z:+.2f} above VWAP with no fresh high "
                        f"(pre-distribution lean)"
                    ),
                    metadata={**meta, "whisper": True},
                )

            return self._neutral(
                f"No sustained accumulation/distribution (last Z={cur_z:+.2f})",
                **meta,
            )

        except Exception as exc:
            return self._neutral(f"Strategy error: {exc}")
