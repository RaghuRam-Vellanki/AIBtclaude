"""
strategies/microstructure.py
Citadel Securities-style mean reversion: when price extends >2σ from session
VWAP and the most recent 1m bars show wick rejection (long lower wicks for a
bottom, long upper wicks for a top), fade the move.
"""
from __future__ import annotations

from typing import Any, Dict

import numpy as np

from strategies.base import StrategyAgent, StrategyVote


class MicrostructureStrategy(StrategyAgent):
    name = "microstructure"
    inspired_by = "Citadel Securities (HFT market making / mean reversion)"
    archetype = "MEAN_REVERT"

    def vote(self, snapshot: Dict[str, Any], feed: Any) -> StrategyVote:
        try:
            bars = feed.get_bars("1Min") if feed is not None else None
            if bars is None or bars.empty or len(bars) < 30:
                return self._neutral("Insufficient 1m bars")

            vwap = float(snapshot.get("session_vwap", 0) or 0)
            if vwap <= 0:
                return self._neutral("Session VWAP unavailable")

            closes = bars["close"].astype(float).values[-60:]
            highs  = bars["high"].astype(float).values[-60:]
            lows   = bars["low"].astype(float).values[-60:]
            opens  = bars["open"].astype(float).values[-60:]

            import math as _math
            spreads = closes - vwap
            spread_std = float(spreads.std())
            if spread_std == 0 or _math.isnan(spread_std):
                return self._neutral("No price dispersion vs VWAP")

            zscore = float(spreads[-1] / spread_std)
            if _math.isnan(zscore) or _math.isinf(zscore):
                return self._neutral("Z-score not computable")

            # Wick imbalance over last 3 bars: (upper_wick - lower_wick) / range
            def wick_imbalance(o: float, h: float, l: float, c: float) -> float:
                rng = h - l
                if rng <= 0:
                    return 0.0
                upper = h - max(o, c)
                lower = min(o, c) - l
                return (upper - lower) / rng

            wicks = [wick_imbalance(opens[i], highs[i], lows[i], closes[i]) for i in (-3, -2, -1)]
            mean_wick = float(np.mean(wicks))

            # LONG when price is statistically far below VWAP. Wick imbalance is
            # used as a confidence multiplier rather than a hard gate — a -4σ
            # extension is itself a signal even without textbook wick rejection.
            if zscore <= -2.0:
                base_conf = float(min(1.0, abs(zscore) / 3.0))
                wick_boost = max(0.0, -mean_wick) * 0.5  # supportive lower-wick = bonus
                confidence = float(min(1.0, base_conf + wick_boost))
                return StrategyVote(
                    name=self.name,
                    inspired_by=self.inspired_by,
                    direction="LONG",
                    confidence=confidence,
                    rationale=(
                        f"Z={zscore:.2f}σ below VWAP "
                        f"(wick imbalance {mean_wick:+.2f}) — mean-revert long"
                    ),
                    metadata={"zscore": round(zscore, 2), "wick_imbalance": round(mean_wick, 2)},
                )

            if zscore >= 2.0:
                base_conf = float(min(1.0, abs(zscore) / 3.0))
                wick_boost = max(0.0, mean_wick) * 0.5
                confidence = float(min(1.0, base_conf + wick_boost))
                return StrategyVote(
                    name=self.name,
                    inspired_by=self.inspired_by,
                    direction="SHORT",
                    confidence=confidence,
                    rationale=(
                        f"Z={zscore:.2f}σ above VWAP "
                        f"(wick imbalance {mean_wick:+.2f}) — mean-revert short"
                    ),
                    metadata={"zscore": round(zscore, 2), "wick_imbalance": round(mean_wick, 2)},
                )

            # Whisper-grade fade: |Z|≥0.3 with confirming wick tilt → low-conf vote
            # so the cluster aggregator has something to align on quiet days.
            if abs(zscore) >= 0.3:
                wick_agrees = (zscore < 0 and mean_wick < 0) or (zscore > 0 and mean_wick > 0)
                whisper = round(0.15 + min(0.20, abs(zscore) * 0.10) + (0.05 if wick_agrees else 0.0), 2)
                direction = "LONG" if zscore < 0 else "SHORT"
                return StrategyVote(
                    name=self.name, inspired_by=self.inspired_by,
                    direction=direction, confidence=whisper,
                    rationale=(
                        f"Whisper: Z={zscore:+.2f}σ from VWAP "
                        f"(wick {mean_wick:+.2f}) — sub-threshold fade"
                    ),
                    metadata={"zscore": round(zscore, 2), "wick_imbalance": round(mean_wick, 2),
                              "whisper": True},
                )

            return self._neutral(
                f"Z={zscore:+.2f}σ from VWAP, wick imbalance {mean_wick:+.2f} — no edge",
                zscore=round(zscore, 2),
                wick_imbalance=round(mean_wick, 2),
            )

        except Exception as exc:
            return self._neutral(f"Strategy error: {exc}")
