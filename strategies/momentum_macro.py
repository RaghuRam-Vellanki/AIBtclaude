"""
strategies/momentum_macro.py
Goldman Sachs-style trend continuation with VWAP execution alignment.

Vote LONG when 4h structure is uptrend, price is above session VWAP, and the
current 1h bar has confirmed continuation (close > VWAP + 0.5*ATR_1h).
Mirror for SHORT.
"""
from __future__ import annotations

from typing import Any, Dict

from strategies.base import StrategyAgent, StrategyVote


class MomentumMacroStrategy(StrategyAgent):
    name = "momentum_macro"
    inspired_by = "Goldman Sachs (macro momentum + VWAP execution)"
    archetype = "TREND"

    def vote(self, snapshot: Dict[str, Any], feed: Any) -> StrategyVote:
        try:
            price       = float(snapshot.get("current_price", 0) or 0)
            vwap        = float(snapshot.get("session_vwap", 0) or 0)
            daily_atr   = float(snapshot.get("daily_atr", 0) or 0)
            h4_struct   = snapshot.get("h4_structure", "ranging")
            h1_struct   = snapshot.get("h1_structure", "ranging")

            if price <= 0 or vwap <= 0:
                return self._neutral("Price or VWAP unavailable")

            # Treat 1H ATR as ~daily_atr / sqrt(24); use daily_atr * 0.04 as a robust proxy.
            atr_1h_proxy = daily_atr * 0.04 if daily_atr > 0 else max(price * 0.0015, 1.0)
            buffer       = atr_1h_proxy * 0.5

            # Aligned-up: 4H uptrend OR (1H uptrend AND price above VWAP).
            # Either timeframe trending in the same direction as the VWAP
            # offset is enough to vote — full alignment just gets higher
            # confidence.
            full_up   = (h4_struct == "uptrend"   and price >= vwap + buffer)
            full_down = (h4_struct == "downtrend" and price <= vwap - buffer)
            partial_up   = (h1_struct == "uptrend"   and price >= vwap + buffer)
            partial_down = (h1_struct == "downtrend" and price <= vwap - buffer)

            if full_up or partial_up:
                confidence = 0.9 if (full_up and h1_struct == "uptrend") else (0.6 if full_up else 0.4)
                return StrategyVote(
                    name=self.name,
                    inspired_by=self.inspired_by,
                    direction="LONG",
                    confidence=confidence,
                    rationale=(
                        f"4H={h4_struct}, 1H={h1_struct} + price ${price:,.2f} "
                        f"above VWAP ${vwap:,.2f} by ${price - vwap:,.2f}"
                    ),
                    metadata={
                        "h4_structure": h4_struct, "h1_structure": h1_struct,
                        "vwap_distance": round(price - vwap, 2),
                    },
                )

            if full_down or partial_down:
                confidence = 0.9 if (full_down and h1_struct == "downtrend") else (0.6 if full_down else 0.4)
                return StrategyVote(
                    name=self.name,
                    inspired_by=self.inspired_by,
                    direction="SHORT",
                    confidence=confidence,
                    rationale=(
                        f"4H={h4_struct}, 1H={h1_struct} + price ${price:,.2f} "
                        f"below VWAP ${vwap:,.2f} by ${vwap - price:,.2f}"
                    ),
                    metadata={
                        "h4_structure": h4_struct, "h1_structure": h1_struct,
                        "vwap_distance": round(price - vwap, 2),
                    },
                )

            return self._neutral(
                f"4H={h4_struct}, 1H={h1_struct}, no VWAP alignment",
                h4_structure=h4_struct, h1_structure=h1_struct,
            )

        except Exception as exc:
            return self._neutral(f"Strategy error: {exc}")
