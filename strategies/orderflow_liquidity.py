"""
strategies/orderflow_liquidity.py
JPMorgan-style order-flow / stop-run reversal.

Detects:
  1. **Liquidity sweep**: price wicks past the previous-day high (PDH) or
     previous-day low (PDL) by >ORDERFLOW_SWEEP_PCT.
  2. **Absorption candle**: same bar shows ≥ session-adaptive volume threshold
     AND a long opposite wick (lower wick on a bottom sweep, upper wick on a
     top sweep) — institutions absorbing the stop-driven flow.

Vote LONG on PDL sweep + lower-wick absorption (retail panic, bank scoops it).
Vote SHORT on PDH sweep + upper-wick absorption.

Uses session-adaptive volume thresholds: 1.5× during Asia hours, 2.0×
elsewhere — institutions need higher volume confirmation in liquid sessions.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

import numpy as np

from strategies.base import StrategyAgent, StrategyVote
from config_strategies import (
    ORDERFLOW_SWEEP_PCT,
    ORDERFLOW_VOL_LOOKBACK,
    ORDERFLOW_VOL_MULT_ASIA,
    ORDERFLOW_VOL_MULT_LONDON,
    ORDERFLOW_WICK_RATIO,
)

try:
    from zoneinfo import ZoneInfo
    _IST = ZoneInfo("Asia/Kolkata")
except Exception:                                # pragma: no cover
    _IST = timezone.utc


def _ist_hour() -> int:
    try:
        return datetime.now(tz=timezone.utc).astimezone(_IST).hour
    except Exception:
        return 12


def _vol_threshold_for_session() -> float:
    """1.5× in Asia (00:00-07:00 IST), 2.0× rest of day."""
    h = _ist_hour()
    if 0 <= h < 7:
        return ORDERFLOW_VOL_MULT_ASIA
    return ORDERFLOW_VOL_MULT_LONDON


class OrderflowLiquidityStrategy(StrategyAgent):
    inspired_by = "JPMorgan order-flow desk (stop-run reversal / absorption)"
    archetype = "FLOW"

    def __init__(self, asset: str = "GENERIC"):
        self.asset = asset.upper()
        self.name = f"orderflow_liquidity_{self.asset.lower()}"

    def vote(self, snapshot: Dict[str, Any], feed: Any) -> StrategyVote:
        try:
            bars = feed.get_bars("5Min") if feed is not None else None
            if bars is None or bars.empty or len(bars) < ORDERFLOW_VOL_LOOKBACK + 5:
                return self._neutral("Insufficient 5m bars")

            highs = bars["high"].astype(float).values
            lows = bars["low"].astype(float).values
            opens = bars["open"].astype(float).values
            closes = bars["close"].astype(float).values
            vols = bars.get("volume", None)
            if vols is not None:
                vols = vols.astype(float).fillna(0).values
            else:
                vols = np.zeros(len(closes))

            pdh = float(snapshot.get("pdh", 0) or 0)
            pdl = float(snapshot.get("pdl", 0) or 0)
            if pdh <= 0 or pdl <= 0:
                # Fall back: derive from the longest bar set we can see
                pdh = float(np.max(highs[-288:])) if len(highs) >= 24 else float(np.max(highs))
                pdl = float(np.min(lows[-288:]))  if len(lows)  >= 24 else float(np.min(lows))

            o, h, l, c = opens[-1], highs[-1], lows[-1], closes[-1]
            rng = h - l
            if rng <= 0:
                return self._neutral("Zero range candle")

            upper_wick = h - max(o, c)
            lower_wick = min(o, c) - l
            upper_wick_ratio = upper_wick / rng
            lower_wick_ratio = lower_wick / rng

            # Average volume over lookback (excluding current bar)
            ref_vols = vols[-(ORDERFLOW_VOL_LOOKBACK + 1):-1]
            avg_vol = float(np.mean(ref_vols)) if ref_vols.size else 0.0
            cur_vol = float(vols[-1])
            vol_threshold_mult = _vol_threshold_for_session()
            vol_ok = avg_vol > 0 and cur_vol >= vol_threshold_mult * avg_vol
            # If we don't have volume data (e.g. yfinance index = volume 0),
            # require the wick + sweep alone but reduce confidence.
            no_vol_data = avg_vol <= 0

            sweep_pdh = h >= pdh * (1 + ORDERFLOW_SWEEP_PCT) and c < pdh
            sweep_pdl = l <= pdl * (1 - ORDERFLOW_SWEEP_PCT) and c > pdl

            meta = {
                "pdh": round(pdh, 2),
                "pdl": round(pdl, 2),
                "current_close": round(c, 2),
                "upper_wick_ratio": round(upper_wick_ratio, 2),
                "lower_wick_ratio": round(lower_wick_ratio, 2),
                "volume_mult": round((cur_vol / avg_vol) if avg_vol > 0 else 0, 2),
                "session_vol_threshold": vol_threshold_mult,
                "ist_hour": _ist_hour(),
            }

            if sweep_pdl and lower_wick_ratio >= ORDERFLOW_WICK_RATIO and (vol_ok or no_vol_data):
                # Stop-hunt below PDL with absorption — long
                base_conf = min(0.95, 0.5 + lower_wick_ratio)
                if no_vol_data:
                    base_conf *= 0.7
                return StrategyVote(
                    name=self.name, inspired_by=self.inspired_by,
                    direction="LONG", confidence=float(base_conf),
                    rationale=(
                        f"PDL sweep ₹{l:.2f} < PDL ₹{pdl:.2f}, lower-wick "
                        f"{lower_wick_ratio:.0%} of range, "
                        f"vol {(cur_vol/avg_vol):.1f}× avg — institutional absorption"
                    ) if not no_vol_data else (
                        f"PDL sweep ₹{l:.2f} < PDL ₹{pdl:.2f}, lower-wick "
                        f"{lower_wick_ratio:.0%} (no volume data; reduced confidence)"
                    ),
                    metadata=meta,
                )

            if sweep_pdh and upper_wick_ratio >= ORDERFLOW_WICK_RATIO and (vol_ok or no_vol_data):
                base_conf = min(0.95, 0.5 + upper_wick_ratio)
                if no_vol_data:
                    base_conf *= 0.7
                return StrategyVote(
                    name=self.name, inspired_by=self.inspired_by,
                    direction="SHORT", confidence=float(base_conf),
                    rationale=(
                        f"PDH sweep ₹{h:.2f} > PDH ₹{pdh:.2f}, upper-wick "
                        f"{upper_wick_ratio:.0%} of range, "
                        f"vol {(cur_vol/avg_vol):.1f}× avg — institutional absorption"
                    ) if not no_vol_data else (
                        f"PDH sweep ₹{h:.2f} > PDH ₹{pdh:.2f}, upper-wick "
                        f"{upper_wick_ratio:.0%} (no volume data; reduced confidence)"
                    ),
                    metadata=meta,
                )

            return self._neutral(
                f"No PDH/PDL sweep with absorption (pdh={pdh:.2f}, pdl={pdl:.2f})",
                **meta,
            )

        except Exception as exc:
            return self._neutral(f"Strategy error: {exc}")
