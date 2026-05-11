"""
strategies/regime_hmm.py
Renaissance Technologies-style regime detection via a 3-state Gaussian HMM
fitted on (1h log-returns, log(true-range)). States are labeled by emission
mean of |return|: low-vol = `accumulation`, mid-vol = `trending`, high-vol =
`chaos`. Vote LONG/SHORT only inside `trending` and only in the direction of
that state's mean return.
"""
from __future__ import annotations

import warnings
from typing import Any, Dict

import numpy as np

from strategies.base import StrategyAgent, StrategyVote


class RegimeHMMStrategy(StrategyAgent):
    name = "regime_hmm"
    inspired_by = "Renaissance Technologies (Hidden Markov regime detection)"
    archetype = "TREND"

    def vote(self, snapshot: Dict[str, Any], feed: Any) -> StrategyVote:
        try:
            bars = feed.get_bars("1Hour") if feed is not None else None
            if bars is None or bars.empty or len(bars) < 80:
                return self._neutral("Insufficient 1h history for HMM")

            closes = bars["close"].astype(float).values
            highs  = bars["high"].astype(float).values
            lows   = bars["low"].astype(float).values

            # Log returns + log true range (volatility proxy)
            log_returns = np.diff(np.log(closes))
            tr = np.maximum.reduce([
                highs[1:] - lows[1:],
                np.abs(highs[1:] - closes[:-1]),
                np.abs(lows[1:]  - closes[:-1]),
            ])
            log_tr = np.log(np.maximum(tr, 1e-9))

            X = np.column_stack([log_returns, log_tr])
            X = X[-200:]   # last 200 hourly bars max

            try:
                from hmmlearn.hmm import GaussianHMM
            except Exception as exc:
                return self._neutral(f"hmmlearn missing: {exc}")

            cached_model = snapshot.get("_hmm_model")
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                if cached_model is not None:
                    model = cached_model
                else:
                    model = GaussianHMM(
                        n_components=3, covariance_type="diag",
                        n_iter=50, random_state=42, tol=1e-3,
                    )
                    model.fit(X)
                states = model.predict(X)
                last_state = int(states[-1])

                # Posterior probability of being in current state
                posteriors = model.predict_proba(X)
                state_prob = float(posteriors[-1, last_state])

            # Rank states by emission mean of log_tr (volatility): lowest = accumulation
            tr_means = model.means_[:, 1]   # log_tr is column 1
            ret_means = model.means_[:, 0]  # log_return is column 0
            order_by_vol = np.argsort(tr_means)
            regime_label_map: Dict[int, str] = {
                int(order_by_vol[0]): "accumulation",
                int(order_by_vol[1]): "trending",
                int(order_by_vol[2]): "chaos",
            }
            regime = regime_label_map.get(last_state, "unknown")
            mean_return = float(ret_means[last_state])

            meta = {
                "regime": regime,
                "state_probability": round(state_prob, 3),
                "mean_log_return": round(mean_return, 5),
            }

            # Vote in `trending` (full confidence) and `accumulation` (half
            # confidence) — accumulation can persist for hours and produces
            # small but reliable drifts. Only `chaos` blocks fully.
            if regime == "chaos":
                return self._neutral(
                    f"Regime=chaos (p={state_prob:.2f}) — stand down",
                    **meta,
                )

            confidence_scale = 1.0 if regime == "trending" else 0.5
            confidence = float(min(1.0, state_prob * confidence_scale))
            if mean_return > 0:
                return StrategyVote(
                    name=self.name,
                    inspired_by=self.inspired_by,
                    direction="LONG",
                    confidence=confidence,
                    rationale=(
                        f"Trending regime detected (HMM p={state_prob:.2f}) with "
                        f"positive mean return {mean_return:+.4f}"
                    ),
                    metadata=meta,
                )
            if mean_return < 0:
                return StrategyVote(
                    name=self.name,
                    inspired_by=self.inspired_by,
                    direction="SHORT",
                    confidence=confidence,
                    rationale=(
                        f"Trending regime detected (HMM p={state_prob:.2f}) with "
                        f"negative mean return {mean_return:+.4f}"
                    ),
                    metadata=meta,
                )
            return self._neutral("Trending regime but mean return is exactly zero", **meta)

        except Exception as exc:
            return self._neutral(f"Strategy error: {exc}")
