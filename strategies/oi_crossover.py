"""
strategies/oi_crossover.py
NSE FNO-desk-style OI/Price divergence detector for NIFTY.

Reads aggregate NIFTY futures Open Interest from the NSE F&O snapshot and
compares it against price movement over a rolling lookback window:

  Price ↑ + OI ↓ = SHORT COVERING  → SHORT (rally is exhausted)
  Price ↑ + OI ↑ = FRESH LONGS     → LONG  (trend continuation)
  Price ↓ + OI ↑ = FRESH SHORTS    → SHORT (downtrend gathering)
  Price ↓ + OI ↓ = LONG UNWIND     → LONG  (bottoming, weak hands flushed)

Maintains a small rolling buffer of (timestamp, price, total_oi) snapshots
inside the strategy instance — the dashboard re-instantiates the pod on each
analysis call but the agent process keeps the same instances over time, so
the buffer accumulates between cycles.

If the NSE futures endpoint is unavailable (option-chain blocked) the
strategy votes NEUTRAL with rationale.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any, Deque, Dict

from strategies.base import StrategyAgent, StrategyVote
from config_strategies import (
    OI_DELTA_THRESHOLD,
    OI_LOOKBACK_HOURS,
    OI_PRICE_THRESHOLD,
)


class OICrossoverStrategy(StrategyAgent):
    name = "nifty_oi_crossover"
    inspired_by = "NSE FNO desk (price/OI divergence — short cover, fresh longs)"
    archetype = "FLOW"

    def __init__(self):
        # ring of (epoch_seconds, price, total_oi) — keep ~12 hours
        self._history: Deque[tuple] = deque(maxlen=200)

    def vote(self, snapshot: Dict[str, Any], feed: Any) -> StrategyVote:
        try:
            if not hasattr(feed, "get_futures_oi_snapshot"):
                return self._neutral("Feed has no futures OI helper")

            snap = feed.get_futures_oi_snapshot()
            if not snap or snap.get("total_oi", 0) <= 0:
                return self._neutral("NSE futures OI snapshot unavailable")

            now = time.time()
            spot = float(snap.get("spot") or snapshot.get("current_price") or 0)
            total_oi = float(snap.get("total_oi") or 0)
            self._history.append((now, spot, total_oi))

            # Find the snapshot that's at least OI_LOOKBACK_HOURS old
            target_age = now - OI_LOOKBACK_HOURS * 3600
            past = next((row for row in self._history if row[0] <= target_age), None)
            if past is None:
                # Fall back to oldest entry we have
                if len(self._history) < 2:
                    return self._neutral(
                        f"Need ≥2 OI snapshots over {OI_LOOKBACK_HOURS}h "
                        f"(have {len(self._history)})"
                    )
                past = self._history[0]

            past_ts, past_price, past_oi = past
            age_hours = (now - past_ts) / 3600.0
            if past_price <= 0 or past_oi <= 0:
                return self._neutral("Past snapshot invalid")

            price_chg = (spot - past_price) / past_price
            oi_chg = (total_oi - past_oi) / past_oi

            meta = {
                "spot": round(spot, 2),
                "past_price": round(past_price, 2),
                "price_chg_pct": round(price_chg * 100, 3),
                "total_oi": round(total_oi, 0),
                "past_oi": round(past_oi, 0),
                "oi_chg_pct": round(oi_chg * 100, 2),
                "lookback_hours": round(age_hours, 1),
                "history_len": len(self._history),
            }

            if abs(price_chg) < OI_PRICE_THRESHOLD or abs(oi_chg) < OI_DELTA_THRESHOLD:
                return self._neutral(
                    f"Δprice={price_chg*100:+.2f}% / ΔOI={oi_chg*100:+.2f}% "
                    f"below thresholds (need ±{OI_PRICE_THRESHOLD*100:.1f}% / "
                    f"±{OI_DELTA_THRESHOLD*100:.0f}%)",
                    **meta,
                )

            price_up = price_chg > 0
            oi_up = oi_chg > 0
            magnitude = float(min(1.0, (abs(price_chg) / OI_PRICE_THRESHOLD) * 0.4 +
                                       (abs(oi_chg) / OI_DELTA_THRESHOLD) * 0.4))

            if price_up and not oi_up:
                # Short covering — exhausted rally → SHORT
                return StrategyVote(
                    name=self.name, inspired_by=self.inspired_by,
                    direction="SHORT", confidence=magnitude,
                    rationale=(
                        f"Price +{price_chg*100:.2f}% but OI {oi_chg*100:+.2f}% "
                        f"= SHORT COVERING — fade the exhausted rally"
                    ),
                    metadata={**meta, "regime": "short_covering"},
                )
            if price_up and oi_up:
                # Fresh longs — continuation
                return StrategyVote(
                    name=self.name, inspired_by=self.inspired_by,
                    direction="LONG", confidence=magnitude,
                    rationale=(
                        f"Price +{price_chg*100:.2f}% with OI {oi_chg*100:+.2f}% "
                        f"= FRESH LONGS — trend continuation"
                    ),
                    metadata={**meta, "regime": "fresh_longs"},
                )
            if (not price_up) and oi_up:
                # Fresh shorts — downtrend gathering
                return StrategyVote(
                    name=self.name, inspired_by=self.inspired_by,
                    direction="SHORT", confidence=magnitude,
                    rationale=(
                        f"Price {price_chg*100:.2f}% with OI {oi_chg*100:+.2f}% "
                        f"= FRESH SHORTS — downtrend gathering"
                    ),
                    metadata={**meta, "regime": "fresh_shorts"},
                )
            # price down & OI down = long unwind = bottoming → LONG
            return StrategyVote(
                name=self.name, inspired_by=self.inspired_by,
                direction="LONG", confidence=magnitude,
                rationale=(
                    f"Price {price_chg*100:.2f}% with OI {oi_chg*100:+.2f}% "
                    f"= LONG UNWIND — weak hands flushed, bottoming"
                ),
                metadata={**meta, "regime": "long_unwind"},
            )

        except Exception as exc:
            return self._neutral(f"Strategy error: {exc}")
