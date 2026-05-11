"""
strategies/volatility_regime.py
BlackRock RiskMetrics / Heston-style volatility-regime detector.

Compares implied vol (IV) to realized vol (HV).
  - For NIFTY: IV proxy = India VIX (already in feed); HV = 20-day annualised
    realized vol of ^NSEI close-to-close returns.
  - For XAU: no public IV index; use rolling EWMA realized vol vs longer-window
    realized as the regime proxy.

If IV/HV ratio > VOL_REGIME_IV_HV_RATIO (~1.4): the option market is paying a
premium for protection that historical vol doesn't justify → "vol crush"
expected. Mean-revert bias = vote LONG underlying with a small confidence
(institutions sell put premium = synthetic long delta).

If VIX > VOL_REGIME_MAX_VIX → risk-off, NEUTRAL (don't fight the panic).

Best used as a pod *flavour* — not the primary driver, but a confidence boost
(or veto) for the ensemble.
"""
from __future__ import annotations

from typing import Any, Dict

import numpy as np

from strategies.base import StrategyAgent, StrategyVote
from config_strategies import (
    VOL_REGIME_HV_LOOKBACK,
    VOL_REGIME_IV_HV_RATIO,
    VOL_REGIME_MAX_VIX,
    VOL_REGIME_MIN_VIX,
)


class VolatilityRegimeStrategy(StrategyAgent):
    inspired_by = "BlackRock RiskMetrics / Heston (IV/HV regime, vol crush)"
    archetype = "OPTIONS"

    def __init__(self, asset: str = "GENERIC"):
        self.asset = asset.upper()
        self.name = f"volatility_regime_{self.asset.lower()}"

    def vote(self, snapshot: Dict[str, Any], feed: Any) -> StrategyVote:
        try:
            bars = feed.get_bars("1Day") if feed is not None else None
            if bars is None or bars.empty or len(bars) < VOL_REGIME_HV_LOOKBACK + 2:
                return self._neutral("Insufficient daily bars for HV calc")

            closes = bars["close"].astype(float).values
            log_ret = np.diff(np.log(closes[-(VOL_REGIME_HV_LOOKBACK + 1):]))
            if log_ret.size < 2:
                return self._neutral("Returns vector too short")
            hv_annualised = float(np.std(log_ret, ddof=0) * np.sqrt(252) * 100)

            if self.asset == "NIFTY":
                iv = float(snapshot.get("india_vix", 0) or 0)
                if iv <= 0:
                    # Fall back to feed
                    try:
                        vix_df = feed.get_vix_1d()
                        iv = float(vix_df["close"].iloc[-1]) if vix_df is not None and not vix_df.empty else 0.0
                    except Exception:
                        iv = 0.0
                if iv <= VOL_REGIME_MIN_VIX:
                    return self._neutral(f"VIX={iv:.1f} too low to be meaningful", iv=iv, hv=round(hv_annualised, 1))
                if iv >= VOL_REGIME_MAX_VIX:
                    return self._neutral(f"VIX={iv:.1f} risk-off — stand down", iv=iv, hv=round(hv_annualised, 1))
            else:
                # XAU / BTC: synthetic IV from short-window EWMA realized vol
                shorter = log_ret[-7:]
                iv = float(np.std(shorter, ddof=0) * np.sqrt(252) * 100) if shorter.size >= 3 else 0.0
                if iv <= 0:
                    return self._neutral("Synthetic IV computation failed")

            ratio = iv / hv_annualised if hv_annualised > 0 else 0.0
            meta = {
                "iv": round(iv, 2),
                "hv_annualised": round(hv_annualised, 2),
                "iv_hv_ratio": round(ratio, 2),
                "threshold": VOL_REGIME_IV_HV_RATIO,
            }

            if ratio >= VOL_REGIME_IV_HV_RATIO:
                # Premium overpriced — vol crush expected, mean-revert bias.
                # We map this to a small LONG bias because in equity / index land,
                # vol-crush = market relief = upward drift.
                confidence = float(min(0.7, 0.3 + (ratio - VOL_REGIME_IV_HV_RATIO) * 0.5))
                return StrategyVote(
                    name=self.name, inspired_by=self.inspired_by,
                    direction="LONG" if self.asset != "BTC" else "SHORT",
                    confidence=confidence,
                    rationale=(
                        f"IV/HV={ratio:.2f} > {VOL_REGIME_IV_HV_RATIO} (IV={iv:.1f}, "
                        f"HV={hv_annualised:.1f}) — vol-crush bias"
                    ),
                    metadata=meta,
                )

            if ratio < 0.7:
                # IV well below HV — vol expansion likely, market complacent.
                return self._neutral(
                    f"IV/HV={ratio:.2f} < 0.7 — vol expansion risk, no directional edge",
                    **meta,
                )

            return self._neutral(
                f"IV/HV={ratio:.2f} in normal band (no edge)",
                **meta,
            )

        except Exception as exc:
            return self._neutral(f"Strategy error: {exc}")
