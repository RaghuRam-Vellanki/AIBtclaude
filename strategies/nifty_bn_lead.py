"""
strategies/nifty_bn_lead.py
FLOW-archetype lead-lag indicator: BANKNIFTY usually leads NIFTY 50 in
directional moves because banks dominate the index weight (~35-40% of NIFTY
is BFSI). When BANKNIFTY breaks structure but NIFTY hasn't moved yet, the
NIFTY follow-through is typically 1-3 bars away.

This isn't a HFT lead — on 1h bars we capture the slower version: BANKNIFTY
shows a clear 1-3 hour directional move while NIFTY is still flat. The
strategy votes in BANKNIFTY's direction with mid confidence.
"""
from __future__ import annotations

from typing import Any, Dict

import numpy as np

from strategies.base import StrategyAgent


class NiftyBNLeadStrategy(StrategyAgent):
    name = "nifty_bn_lead"
    inspired_by = "Goldman Sachs / Morgan Stanley quant pairs desk (BFSI lead-lag)"
    archetype = "FLOW"

    # Thresholds (3-bar 1h return)
    _BN_LEAD_PCT      = 0.0030   # BANKNIFTY moved ≥ 0.30% over last 3h
    _NIFTY_LAG_PCT    = 0.0015   # NIFTY moved < 0.15% over the same window
    _MIN_LEAD_GAP_PCT = 0.0015   # |BN_ret − NIFTY_ret| ≥ 0.15% (avoid noise)

    def vote(self, snapshot: Dict[str, Any], feed: Any):
        try:
            if feed is None or not hasattr(feed, "get_banknifty_1h"):
                return self._neutral("BANKNIFTY feed unavailable")

            bn = feed.get_banknifty_1h()
            if bn is None or bn.empty or len(bn) < 4:
                return self._neutral("Insufficient BANKNIFTY bars")

            # NIFTY 1h bars come via the standard feed
            ni = feed.get_bars("1Hour") if hasattr(feed, "get_bars") else None
            if ni is None or ni.empty or len(ni) < 4:
                return self._neutral("Insufficient NIFTY 1h bars")

            bn_closes = bn["close"].astype(float).values[-4:]
            ni_closes = ni["close"].astype(float).values[-4:]

            # 3-bar return: bar[-1] vs bar[-4]
            bn_ret = float((bn_closes[-1] - bn_closes[-4]) / bn_closes[-4])
            ni_ret = float((ni_closes[-1] - ni_closes[-4]) / ni_closes[-4])
            gap = bn_ret - ni_ret

            # BANKNIFTY up, NIFTY not yet → bullish follow-through
            if (bn_ret >= self._BN_LEAD_PCT
                and abs(ni_ret) < self._NIFTY_LAG_PCT
                and gap >= self._MIN_LEAD_GAP_PCT):
                conf = float(min(0.80, 0.55 + bn_ret * 30.0))
                return self._vote(
                    direction="LONG",
                    confidence=conf,
                    rationale=(f"BANKNIFTY +{bn_ret*100:.2f}%/3h leading NIFTY "
                               f"+{ni_ret*100:.2f}% — BFSI lead-lag setup"),
                    bn_ret_3h=round(bn_ret, 4),
                    nifty_ret_3h=round(ni_ret, 4),
                    gap=round(gap, 4),
                )

            # BANKNIFTY down, NIFTY not yet → bearish follow-through
            if (bn_ret <= -self._BN_LEAD_PCT
                and abs(ni_ret) < self._NIFTY_LAG_PCT
                and gap <= -self._MIN_LEAD_GAP_PCT):
                conf = float(min(0.80, 0.55 + abs(bn_ret) * 30.0))
                return self._vote(
                    direction="SHORT",
                    confidence=conf,
                    rationale=(f"BANKNIFTY {bn_ret*100:.2f}%/3h leading NIFTY "
                               f"{ni_ret*100:.2f}% — BFSI lead-lag setup"),
                    bn_ret_3h=round(bn_ret, 4),
                    nifty_ret_3h=round(ni_ret, 4),
                    gap=round(gap, 4),
                )

            return self._neutral(
                f"BN {bn_ret*100:+.2f}% / NIFTY {ni_ret*100:+.2f}% — no clear lead",
                bn_ret_3h=round(bn_ret, 4),
                nifty_ret_3h=round(ni_ret, 4),
                gap=round(gap, 4),
            )

        except Exception as exc:
            return self._neutral(f"Strategy error: {exc}")
