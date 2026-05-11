"""
strategies/btc_microstructure.py
Citadel-style mean reversion adapted for BTC's 24×7 schedule.

Same Z-score + wick-imbalance as the XAU MicrostructureStrategy, but:
  - VWAP is rolling 240-bar (24h on 1m bars) since BTC has no daily session
  - Wick gate is softer (BTC tends to spike without textbook absorption)
  - Confidence boosted on extreme Z (|Z| > 4)
"""
from __future__ import annotations

from typing import Any, Dict

import numpy as np

from strategies.base import StrategyAgent, StrategyVote
from config_strategies import BTC_MICRO_WICK_BOOST, BTC_MICRO_ZSCORE_THRESH


class BTCMicrostructureStrategy(StrategyAgent):
    name = "btc_microstructure"
    inspired_by = "Citadel Securities (24×7 BTC mean reversion vs rolling VWAP)"
    archetype = "MEAN_REVERT"

    def vote(self, snapshot: Dict[str, Any], feed: Any) -> StrategyVote:
        try:
            bars = feed.get_bars("1Min") if feed is not None else None
            if bars is None or bars.empty or len(bars) < 240:
                return self._neutral("Insufficient 1m bars (need 240)")

            closes = bars["close"].astype(float).values[-240:]
            highs  = bars["high"].astype(float).values[-240:]
            lows   = bars["low"].astype(float).values[-240:]
            opens  = bars["open"].astype(float).values[-240:]
            vol = bars.get("volume", None)
            if vol is not None:
                vol = vol.astype(float).fillna(0).values[-240:]
            else:
                vol = np.zeros(len(closes))

            # Rolling VWAP over 240 bars (with safe fallback)
            denom = vol.sum()
            if denom > 0:
                rolling_vwap = float(np.sum(closes * vol) / denom)
            else:
                rolling_vwap = float(np.mean(closes))

            spreads = closes - rolling_vwap
            sigma = float(np.std(spreads))
            if sigma == 0:
                return self._neutral("No price dispersion vs rolling VWAP")
            zscore = float(spreads[-1] / sigma)

            # Wick imbalance over last 3 bars
            def wick_imbalance(o: float, h: float, l: float, c: float) -> float:
                rng = h - l
                if rng <= 0:
                    return 0.0
                upper = h - max(o, c)
                lower = min(o, c) - l
                return (upper - lower) / rng

            wicks = [wick_imbalance(opens[i], highs[i], lows[i], closes[i]) for i in (-3, -2, -1)]
            mean_wick = float(np.mean(wicks))

            meta = {
                "zscore": round(zscore, 2),
                "wick_imbalance": round(mean_wick, 2),
                "rolling_vwap": round(rolling_vwap, 2),
                "current_close": round(float(closes[-1]), 2),
            }

            if zscore <= -BTC_MICRO_ZSCORE_THRESH:
                base_conf = float(min(1.0, abs(zscore) / 3.0))
                wick_bonus = max(0.0, -mean_wick) * BTC_MICRO_WICK_BOOST
                confidence = float(min(1.0, base_conf + wick_bonus))
                return StrategyVote(
                    name=self.name, inspired_by=self.inspired_by,
                    direction="LONG", confidence=confidence,
                    rationale=(
                        f"Z={zscore:.2f}σ below 240-bar VWAP "
                        f"(wick={mean_wick:+.2f}) — fade the dump"
                    ),
                    metadata=meta,
                )

            if zscore >= BTC_MICRO_ZSCORE_THRESH:
                base_conf = float(min(1.0, abs(zscore) / 3.0))
                wick_bonus = max(0.0, mean_wick) * BTC_MICRO_WICK_BOOST
                confidence = float(min(1.0, base_conf + wick_bonus))
                return StrategyVote(
                    name=self.name, inspired_by=self.inspired_by,
                    direction="SHORT", confidence=confidence,
                    rationale=(
                        f"Z={zscore:.2f}σ above 240-bar VWAP "
                        f"(wick={mean_wick:+.2f}) — fade the spike"
                    ),
                    metadata=meta,
                )

            return self._neutral(
                f"Z={zscore:+.2f}σ inside ±{BTC_MICRO_ZSCORE_THRESH:.1f} band — no edge",
                **meta,
            )

        except Exception as exc:
            return self._neutral(f"Strategy error: {exc}")
