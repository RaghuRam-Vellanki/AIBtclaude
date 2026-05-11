"""
strategies/nifty_fii_dii_flow.py
JPMorgan / Goldman Sachs EM-equity-desk style flow score for NIFTY 50.
Combines (a) FII (foreign institutional) cash flow direction, (b) DII
(domestic institutional) cash flow direction, and (c) USDINR direction
(rupee strength → FII-friendly). Each contributes ±1; FII >> DII for
NIFTY direction historically, so FII gets ±1 and DII gets ±1, USDINR ±1.
Range −3..+3.
"""
from __future__ import annotations

from typing import Any, Dict

import numpy as np

from strategies.base import StrategyAgent, StrategyVote


# Daily flow thresholds (INR crores, net buy/sell). 500cr = roughly the
# institutional-conviction threshold; below this is noise, above is a directional bet.
FII_NET_THRESHOLD_CR = 500.0
USDINR_SLOPE_PCT     = 0.20          # 5-day move > 0.2% counts as a directional move


class NIFTYFiiDiiFlowStrategy(StrategyAgent):
    name = "nifty_fii_dii_flow"
    inspired_by = "JPMorgan / Goldman Sachs (EM-equity flow desk)"
    archetype = "FLOW"

    def vote(self, snapshot: Dict[str, Any], feed: Any) -> StrategyVote:
        try:
            fii_score, fii_detail = self._fii_signal(feed)
            dii_score, dii_detail = self._dii_signal(feed)
            inr_score, inr_detail = self._inr_signal(feed)

            composite = fii_score + dii_score + inr_score    # range −3..+3

            base_meta = {
                "composite": composite,
                "fii": fii_detail,
                "dii": dii_detail,
                "inr": inr_detail,
            }

            if composite >= 2:
                direction = "LONG"
            elif composite <= -2:
                direction = "SHORT"
            else:
                return self._neutral(
                    f"Flow composite {composite:+d} — needs |≥2| for trade signal",
                    **base_meta,
                )

            confidence = float(abs(composite) / 3.0)
            return StrategyVote(
                name=self.name,
                inspired_by=self.inspired_by,
                direction=direction,
                confidence=confidence,
                rationale=(
                    f"Flow {direction} ({composite:+d}/3): FII {fii_detail['flow']}, "
                    f"DII {dii_detail['flow']}, INR {inr_detail['slope']}"
                ),
                metadata=base_meta,
            )

        except Exception as exc:
            return self._neutral(f"Strategy error: {exc}")

    # ── components ───────────────────────────────────────────────────────────

    @staticmethod
    def _fii_signal(feed: Any):
        """+1 if FII 5d-avg net cash > +500cr (buyers); -1 if < -500cr; 0 otherwise."""
        try:
            data = feed.get_fii_dii_summary() if feed is not None else None
            if not data or not data.get("available"):
                return 0, {"flow": "unavailable", "today": 0.0, "avg_5d": 0.0}
            avg = float(data.get("fii_cash_5d_avg", 0.0))
            today = float(data.get("fii_cash_today", 0.0))
            if avg > FII_NET_THRESHOLD_CR:
                return +1, {"flow": "buying", "today": today, "avg_5d": avg}
            if avg < -FII_NET_THRESHOLD_CR:
                return -1, {"flow": "selling", "today": today, "avg_5d": avg}
            return 0, {"flow": "neutral", "today": today, "avg_5d": avg}
        except Exception:
            return 0, {"flow": "error", "today": 0.0, "avg_5d": 0.0}

    @staticmethod
    def _dii_signal(feed: Any):
        """+1 if DII 5d-avg net cash >= 0 (absorbing/buying); -1 if negative."""
        try:
            data = feed.get_fii_dii_summary() if feed is not None else None
            if not data or not data.get("available"):
                return 0, {"flow": "unavailable", "today": 0.0, "avg_5d": 0.0}
            avg = float(data.get("dii_cash_5d_avg", 0.0))
            today = float(data.get("dii_cash_today", 0.0))
            if avg >= 0:
                return +1, {"flow": "buying", "today": today, "avg_5d": avg}
            return -1, {"flow": "selling", "today": today, "avg_5d": avg}
        except Exception:
            return 0, {"flow": "error", "today": 0.0, "avg_5d": 0.0}

    @staticmethod
    def _inr_signal(feed: Any):
        """+1 if USDINR falling (rupee strengthening, FII-friendly), -1 if rising."""
        try:
            df = feed.get_usdinr_1d() if feed is not None else None
            if df is None or df.empty or len(df) < 6:
                return 0, {"slope": "unknown", "value": 0.0, "pct_5d": 0.0}
            closes = df["close"].astype(float).values[-6:]
            slope = float(closes[-1] - closes[0])
            pct = (slope / closes[0]) * 100 if closes[0] else 0.0
            if pct < -USDINR_SLOPE_PCT:
                return +1, {"slope": "falling", "value": round(closes[-1], 3), "pct_5d": round(pct, 3)}
            if pct > +USDINR_SLOPE_PCT:
                return -1, {"slope": "rising", "value": round(closes[-1], 3), "pct_5d": round(pct, 3)}
            return 0, {"slope": "flat", "value": round(closes[-1], 3), "pct_5d": round(pct, 3)}
        except Exception:
            return 0, {"slope": "error", "value": 0.0, "pct_5d": 0.0}
