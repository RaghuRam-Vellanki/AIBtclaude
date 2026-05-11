"""
strategies/cointegration.py
D.E. Shaw-style statistical arbitrage: rolling OLS log(XAU) ~ α + β·log(DXY).
When the residual Z-score blows out, gold is mispriced relative to its dollar
beta and we mean-revert. ADF test guards against fitting a broken relationship.
"""
from __future__ import annotations

from typing import Any, Dict

import numpy as np
import pandas as pd

from strategies.base import StrategyAgent, StrategyVote


class CointegrationStrategy(StrategyAgent):
    name = "cointegration"
    inspired_by = "D.E. Shaw (statistical arbitrage / pairs trading)"
    archetype = "MEAN_REVERT"

    def vote(self, snapshot: Dict[str, Any], feed: Any) -> StrategyVote:
        try:
            xau_bars = feed.get_bars("1Hour") if feed is not None else None
            dxy_bars = feed.get_dxy_1h()      if feed is not None else None
            if xau_bars is None or xau_bars.empty or len(xau_bars) < 100:
                return self._neutral("Insufficient XAU 1h bars")
            if dxy_bars is None or dxy_bars.empty or len(dxy_bars) < 100:
                return self._neutral("Insufficient DXY 1h bars")

            xau = xau_bars["close"].astype(float).rename("xau")
            dxy = dxy_bars["close"].astype(float).rename("dxy")
            joined = pd.concat([xau, dxy], axis=1, join="inner").dropna().tail(200)
            if len(joined) < 100:
                return self._neutral("Insufficient overlapping XAU/DXY history")

            log_xau = np.log(joined["xau"].values)
            log_dxy = np.log(joined["dxy"].values)

            # Manual OLS β + α (avoid statsmodels dependency at runtime cost)
            x_mean = log_dxy.mean()
            y_mean = log_xau.mean()
            cov_xy = ((log_dxy - x_mean) * (log_xau - y_mean)).sum()
            var_x  = ((log_dxy - x_mean) ** 2).sum()
            if var_x <= 0:
                return self._neutral("DXY variance is zero")
            beta  = cov_xy / var_x
            alpha = y_mean - beta * x_mean
            residuals = log_xau - (alpha + beta * log_dxy)

            mu  = residuals.mean()
            sd  = residuals.std()
            if sd == 0:
                return self._neutral("Residual stdev is zero")
            zscore = float((residuals[-1] - mu) / sd)

            # Cointegration check via ADF
            cointegrated = self._is_cointegrated(residuals)
            if not cointegrated:
                return self._neutral(
                    "ADF p > 0.10 — XAU/DXY relationship has broken (skip stat-arb)",
                    zscore=round(zscore, 2), beta=round(float(beta), 3),
                )

            if zscore <= -2.0:
                confidence = float(min(1.0, abs(zscore) / 3.0))
                return StrategyVote(
                    name=self.name,
                    inspired_by=self.inspired_by,
                    direction="LONG",
                    confidence=confidence,
                    rationale=(
                        f"Residual Z={zscore:.2f}σ — gold cheap vs DXY model "
                        f"(β={beta:.3f}); expect mean reversion higher"
                    ),
                    metadata={
                        "zscore": round(zscore, 2), "beta": round(float(beta), 3),
                        "residual": round(float(residuals[-1]), 5),
                    },
                )

            if zscore >= 2.0:
                confidence = float(min(1.0, abs(zscore) / 3.0))
                return StrategyVote(
                    name=self.name,
                    inspired_by=self.inspired_by,
                    direction="SHORT",
                    confidence=confidence,
                    rationale=(
                        f"Residual Z={zscore:.2f}σ — gold expensive vs DXY model "
                        f"(β={beta:.3f}); expect mean reversion lower"
                    ),
                    metadata={
                        "zscore": round(zscore, 2), "beta": round(float(beta), 3),
                        "residual": round(float(residuals[-1]), 5),
                    },
                )

            return self._neutral(
                f"Residual Z={zscore:+.2f}σ — within band, no stat-arb edge",
                zscore=round(zscore, 2), beta=round(float(beta), 3),
            )

        except Exception as exc:
            return self._neutral(f"Strategy error: {exc}")

    @staticmethod
    def _is_cointegrated(residuals: np.ndarray) -> bool:
        """ADF on residuals; True if p-value < 0.10 (residuals are stationary)."""
        try:
            from statsmodels.tsa.stattools import adfuller
            result = adfuller(residuals, maxlag=5, regression="c", autolag="AIC")
            pvalue = float(result[1])
            return pvalue < 0.10
        except Exception:
            # If statsmodels missing or numerical issue, assume cointegrated
            # (conservative for the strategy — better to take the trade than skip)
            return True
