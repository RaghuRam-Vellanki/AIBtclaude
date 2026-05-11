"""
strategies/btc_funding_skew.py
CARRY-archetype strategy: trade BTC against extreme perpetual-swap funding
when open-interest agrees a positioning squeeze is building.

Setups:
  LONG  — funding < −3bp/8h (shorts paying longs)  AND  OI rising ≥ 5%/24h
          ⇒ short squeeze fuel: market is structurally short and adding more
          short exposure, but funding is paying them to close. One spark and
          they cover into thin liquidity.

  SHORT — funding > +8bp/8h (longs paying shorts)  AND  OI rising ≥ 5%/24h
          ⇒ long-liquidation cascade risk: leverage is one-sided, longs
          paying carry for the privilege. Small wick down → cascade liqs.

This is an independent CARRY-cluster vote. It is **not** the same as the
funding-block gate in signal_generator_btc.py — that gate vetoes trades when
funding is extreme + same-direction (avoid being the late long). This
strategy actively positions for the opposite side of the crowd.
"""
from __future__ import annotations

from typing import Any, Dict

from strategies.base import StrategyAgent


class BTCFundingSkewStrategy(StrategyAgent):
    name = "btc_funding_skew"
    inspired_by = "Multicoin Capital / crypto-CTA desks (perp carry / OI squeeze)"
    archetype = "CARRY"

    # Thresholds (per 8h funding rate, decimal)
    _FUNDING_LONG_TRIGGER  = -0.0003   # shorts paying longs
    _FUNDING_SHORT_TRIGGER = +0.0008   # longs paying shorts (matches BTC_FUNDING_BLOCK)
    _OI_PCT_TRIGGER        = 0.05      # 24h OI change ≥ 5%

    def vote(self, snapshot: Dict[str, Any], feed: Any):
        try:
            funding_8h = float(snapshot.get("funding_rate", 0.0) or 0.0)
            # Prefer fresh OI from the feed; fall back to snapshot if not present.
            oi_24h_pct = 0.0
            if feed is not None and hasattr(feed, "fetch_oi_change"):
                try:
                    oi_24h_pct = float(feed.fetch_oi_change().get("oi_24h_pct", 0.0))
                except Exception:
                    oi_24h_pct = float(snapshot.get("oi_24h_pct", 0.0) or 0.0)
            else:
                oi_24h_pct = float(snapshot.get("oi_24h_pct", 0.0) or 0.0)

            # Short-squeeze setup: funding paying longs to hold + OI rising
            if funding_8h <= self._FUNDING_LONG_TRIGGER and oi_24h_pct >= self._OI_PCT_TRIGGER:
                # Confidence scales with how extreme funding is (more negative → higher)
                conf = min(0.85, 0.5 + abs(funding_8h) * 1000.0)
                return self._vote(
                    direction="LONG",
                    confidence=conf,
                    rationale=(f"Funding {funding_8h*100:+.3f}%/8h (shorts paying), "
                               f"OI +{oi_24h_pct*100:.1f}%/24h — short-squeeze setup"),
                    funding_8h=funding_8h, oi_24h_pct=oi_24h_pct,
                    trigger="short_squeeze",
                )

            # Long-liquidation cascade setup: funding too positive + OI rising
            if funding_8h >= self._FUNDING_SHORT_TRIGGER and oi_24h_pct >= self._OI_PCT_TRIGGER:
                conf = min(0.85, 0.5 + funding_8h * 1000.0)
                return self._vote(
                    direction="SHORT",
                    confidence=conf,
                    rationale=(f"Funding {funding_8h*100:+.3f}%/8h (longs paying), "
                               f"OI +{oi_24h_pct*100:.1f}%/24h — long-liq cascade risk"),
                    funding_8h=funding_8h, oi_24h_pct=oi_24h_pct,
                    trigger="long_liq_cascade",
                )

            return self._neutral(
                f"Funding {funding_8h*100:+.4f}%/8h, OI {oi_24h_pct*100:+.1f}%/24h — no skew",
                funding_8h=funding_8h, oi_24h_pct=oi_24h_pct,
            )

        except Exception as exc:
            return self._neutral(f"Strategy error: {exc}")
