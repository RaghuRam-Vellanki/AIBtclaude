"""
strategies/nifty_pairs_arb_bn.py
D.E. Shaw-style statistical arbitrage on NIFTY vs BANKNIFTY. Banks are ~37%
of NIFTY 50 by weight, so the two indices cointegrate tightly. Rolling OLS
log(^NSEI) ~ alpha + beta * log(^NSEBANK). When the residual Z-score blows
out, NIFTY is mispriced relative to its bank-driven model and we mean-revert.
ADF on the residuals guards against trading a broken relationship.
"""
from __future__ import annotations

from typing import Any, Dict

import numpy as np
import pandas as pd

from strategies.base import StrategyAgent, StrategyVote


class NIFTYPairsArbStrategy(StrategyAgent):
    name = "nifty_pairs_arb_bn"
    inspired_by = "D.E. Shaw (statistical arbitrage / pairs trading)"
    archetype = "MEAN_REVERT"

    def vote(self, snapshot: Dict[str, Any], feed: Any) -> StrategyVote:
        try:
            nifty_bars = feed.get_bars("1Hour") if feed is not None else None
            bn_bars    = feed.get_banknifty_1h() if feed is not None else None
            if nifty_bars is None or nifty_bars.empty or len(nifty_bars) < 100:
                return self._neutral("Insufficient NIFTY 1h bars")
            if bn_bars is None or bn_bars.empty or len(bn_bars) < 100:
                return self._neutral("Insufficient BANKNIFTY 1h bars")

            n = nifty_bars["close"].astype(float).rename("nifty")
            b = bn_bars["close"].astype(float).rename("bn")
            joined = pd.concat([n, b], axis=1, join="inner").dropna().tail(200)
            if len(joined) < 100:
                return self._neutral("Insufficient overlapping NIFTY/BN history")

            log_n = np.log(joined["nifty"].values)
            log_b = np.log(joined["bn"].values)

            x_mean = log_b.mean()
            y_mean = log_n.mean()
            cov_xy = ((log_b - x_mean) * (log_n - y_mean)).sum()
            var_x  = ((log_b - x_mean) ** 2).sum()
            if var_x <= 0:
                return self._neutral("BANKNIFTY variance is zero")
            beta  = cov_xy / var_x
            alpha = y_mean - beta * x_mean
            residuals = log_n - (alpha + beta * log_b)

            mu  = residuals.mean()
            sd  = residuals.std()
            if sd == 0:
                return self._neutral("Residual stdev is zero")
            zscore = float((residuals[-1] - mu) / sd)

            cointegrated = self._is_cointegrated(residuals)
            if not cointegrated:
                return self._neutral(
                    "ADF p > 0.10 — NIFTY/BANKNIFTY relationship has broken (skip stat-arb)",
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
                        f"Residual Z={zscore:.2f}σ — NIFTY cheap vs BANKNIFTY model "
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
                        f"Residual Z={zscore:.2f}σ — NIFTY expensive vs BANKNIFTY model "
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
        try:
            from statsmodels.tsa.stattools import adfuller
            result = adfuller(residuals, maxlag=5, regression="c", autolag="AIC")
            pvalue = float(result[1])
            return pvalue < 0.10
        except Exception:
            return True
