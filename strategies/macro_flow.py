"""
strategies/macro_flow.py
JPMorgan / Goldman Sachs commodities-desk-style macro flow score for gold.

Phase-3 upgrade: **real 10Y yield direction is now the dominant component**.
A 10bp move in real yields ≈ 0.3-0.5% in gold — larger than DXY's contribution.
Real yield (= nominal 10Y − inflation breakeven) is the structural duration
trade gold is priced against. Falling real yields → long gold.

DXY and COT remain as confirming legs but no longer dominate the vote.

Veto rule: don't go LONG gold into a USD breakout, even if real yields are
falling — the dollar move tends to win on the cross-asset clearing.
"""
from __future__ import annotations

from typing import Any, Dict

import numpy as np

from strategies.base import StrategyAgent, StrategyVote
from modules.macro_data import fetch_real_yields


class MacroFlowStrategy(StrategyAgent):
    name = "macro_flow"
    inspired_by = "JPMorgan / Goldman Sachs (commodities desk macro flow)"
    archetype = "MACRO"

    # Real-yield-move thresholds (basis points over 5 sessions)
    _REAL_YIELD_STRONG_BP = 10.0      # ≥10bp move = strong signal
    _REAL_YIELD_VETO_BP   = 5.0       # ≥5bp move = at least a directional bias

    def vote(self, snapshot: Dict[str, Any], feed: Any) -> StrategyVote:
        try:
            real = fetch_real_yields()
            dxy_score, dxy_detail = self._dxy_signal(feed)
            tnx_score, tnx_detail = self._tnx_signal(feed)
            cot_score, cot_detail = self._cot_signal(feed)

            real_dbp = float(real.get("real_yield_5d_change_bp", 0.0))
            real_lvl = float(real.get("real_yield_10y", 0.0))

            # ── Real-yield primary leg (weight 2 — dominates other legs) ──
            # Falling real yields = bullish gold (duration trade thesis)
            if real_dbp <= -self._REAL_YIELD_STRONG_BP:
                ry_score = +2
            elif real_dbp <= -self._REAL_YIELD_VETO_BP:
                ry_score = +1
            elif real_dbp >= +self._REAL_YIELD_STRONG_BP:
                ry_score = -2
            elif real_dbp >= +self._REAL_YIELD_VETO_BP:
                ry_score = -1
            else:
                ry_score = 0

            # DXY and COT now tie-break only (weight 1 each, capped at ±1)
            composite = ry_score + dxy_score + cot_score   # range −4..+4 effective
            # Keep tnx_detail in metadata for backwards-compat, but its score
            # is subsumed by ry_score (same duration story, in real terms).

            # Veto: LONG gold into a USD breakout is the classic bull trap.
            # If real yields scream LONG but DXY is strongly rising, downgrade.
            if ry_score > 0 and dxy_score < 0:
                composite = max(0, composite - 1)

            if composite >= 2:
                direction = "LONG"
            elif composite <= -2:
                direction = "SHORT"
            elif composite == 1 and ry_score > 0:
                # Real yields confirm; let it stand as a low-conf LONG
                direction = "LONG"
            elif composite == -1 and ry_score < 0:
                direction = "SHORT"
            elif ry_score != 0 or dxy_score != 0:
                # Whisper-grade: at least one leg has a directional lean even
                # though the composite didn't reach the strong-vote threshold.
                # Tie-break by ry_score (dominant macro driver), else dxy.
                sign = ry_score if ry_score != 0 else dxy_score
                whisper_dir = "LONG" if sign > 0 else "SHORT"
                return self._vote(
                    direction=whisper_dir,
                    confidence=0.22,
                    rationale=(
                        f"Whisper: composite {composite:+d} "
                        f"(real-Y {real_lvl:+.2f}% Δ{real_dbp:+.1f}bp/5d, "
                        f"DXY {dxy_detail['slope']})"
                    ),
                    composite=composite, real_yield=real, ry_score=ry_score,
                    dxy=dxy_detail, tnx=tnx_detail, cot=cot_detail, whisper=True,
                )
            else:
                return self._neutral(
                    f"Macro composite {composite:+d} — flat",
                    real_yield=real, dxy=dxy_detail, tnx=tnx_detail,
                    cot=cot_detail, composite=composite,
                )

            confidence = float(min(0.95, abs(composite) / 4.0 + 0.15))
            return self._vote(
                direction=direction,
                confidence=confidence,
                rationale=(
                    f"Macro {direction} ({composite:+d}): "
                    f"real-Y {real_lvl:+.2f}% Δ{real_dbp:+.1f}bp/5d, "
                    f"DXY {dxy_detail['slope']}, "
                    f"COT noncomm {cot_detail['change']} "
                    f"[{real.get('source', '?')}]"
                ),
                composite=composite,
                real_yield=real,
                ry_score=ry_score,
                dxy=dxy_detail,
                tnx=tnx_detail,
                cot=cot_detail,
            )

        except Exception as exc:
            return self._neutral(f"Strategy error: {exc}")

    @staticmethod
    def _dxy_signal(feed: Any) -> tuple[int, Dict[str, Any]]:
        """+1 if DXY falling (gold-bullish), -1 if DXY rising, 0 if flat."""
        try:
            df = feed.get_dxy_1h() if feed is not None else None
            if df is None or df.empty or len(df) < 6:
                return 0, {"slope": "unknown", "value": 0.0}
            closes = df["close"].astype(float).values[-6:]
            slope = float(closes[-1] - closes[0])
            pct   = slope / closes[0] if closes[0] else 0.0
            if pct < -0.0015:        # > 0.15% drop over last 5 hours
                return +1, {"slope": "falling", "value": round(closes[-1], 2), "pct_5h": round(pct * 100, 3)}
            if pct > +0.0015:
                return -1, {"slope": "rising",  "value": round(closes[-1], 2), "pct_5h": round(pct * 100, 3)}
            return 0, {"slope": "flat", "value": round(closes[-1], 2), "pct_5h": round(pct * 100, 3)}
        except Exception:
            return 0, {"slope": "error", "value": 0.0}

    @staticmethod
    def _tnx_signal(feed: Any) -> tuple[int, Dict[str, Any]]:
        """+1 if 10Y yield falling (gold-bullish), -1 if rising."""
        try:
            df = feed.get_tnx_1d() if feed is not None else None
            if df is None or df.empty or len(df) < 6:
                return 0, {"slope": "unknown", "value": 0.0}
            closes = df["close"].astype(float).values[-6:]
            slope = float(closes[-1] - closes[0])
            if slope < -0.05:        # >5 bps drop over 5 sessions
                return +1, {"slope": "falling", "value": round(closes[-1], 3), "delta_5d": round(slope, 3)}
            if slope > +0.05:
                return -1, {"slope": "rising", "value": round(closes[-1], 3), "delta_5d": round(slope, 3)}
            return 0, {"slope": "flat", "value": round(closes[-1], 3), "delta_5d": round(slope, 3)}
        except Exception:
            return 0, {"slope": "error", "value": 0.0}

    @staticmethod
    def _cot_signal(feed: Any) -> tuple[int, Dict[str, Any]]:
        """
        +1 if non-commercial net is rising vs 4-week average (institutional adding longs),
        -1 if falling.
        """
        try:
            cot = feed.get_cot_gold_net() if feed is not None else None
            if not cot or cot.get("commercial_net", 0) == 0 and cot.get("noncommercial_net", 0) == 0:
                return 0, {"change": "unavailable", "noncomm_net": 0}
            current = int(cot.get("noncommercial_net", 0))
            avg     = int(cot.get("noncommercial_net_4w_avg", 0))
            if avg == 0:
                return 0, {"change": "no-avg", "noncomm_net": current}
            change_pct = (current - avg) / abs(avg) if avg else 0
            if change_pct > 0.05:
                return +1, {"change": "rising",  "noncomm_net": current, "vs_4w_avg_pct": round(change_pct * 100, 1)}
            if change_pct < -0.05:
                return -1, {"change": "falling", "noncomm_net": current, "vs_4w_avg_pct": round(change_pct * 100, 1)}
            return 0, {"change": "flat", "noncomm_net": current, "vs_4w_avg_pct": round(change_pct * 100, 1)}
        except Exception:
            return 0, {"change": "error", "noncomm_net": 0}
