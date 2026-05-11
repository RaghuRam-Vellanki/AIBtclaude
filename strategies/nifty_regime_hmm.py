"""
strategies/nifty_regime_hmm.py
Renaissance Technologies-style regime detection on NIFTY 50 1h bars via a
3-state Gaussian HMM fitted on (log-return, log-true-range). States are
labeled by emission mean of log_tr (vol): lowest = accumulation, middle =
trending, highest = chaos. Vote LONG/SHORT only inside `trending` and only
in the direction of that state's mean return.

Performance hook: backtests fit the HMM many thousands of times on near-
identical windows. Pass a pre-fit `GaussianHMM` via
`snapshot["_hmm_model"]` to skip the per-bar fit (~95% speedup).
"""
from __future__ import annotations

import warnings
from typing import Any, Dict, Optional

import numpy as np

from strategies.base import StrategyAgent, StrategyVote


class NIFTYRegimeHMMStrategy(StrategyAgent):
    name = "nifty_regime_hmm"
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

            log_returns = np.diff(np.log(closes))
            tr = np.maximum.reduce([
                highs[1:] - lows[1:],
                np.abs(highs[1:] - closes[:-1]),
                np.abs(lows[1:]  - closes[:-1]),
            ])
            log_tr = np.log(np.maximum(tr, 1e-9))

            X = np.column_stack([log_returns, log_tr])[-200:]

            try:
                from hmmlearn.hmm import GaussianHMM
            except Exception as exc:
                return self._neutral(f"hmmlearn missing: {exc}")

            cached_model: Optional[GaussianHMM] = snapshot.get("_hmm_model")
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
                posteriors = model.predict_proba(X)
                state_prob = float(posteriors[-1, last_state])

            tr_means = model.means_[:, 1]
            ret_means = model.means_[:, 0]
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
                "cached": cached_model is not None,
            }

            if regime != "trending":
                return self._neutral(
                    f"Regime={regime} (p={state_prob:.2f}) — only trade in trending state",
                    **meta,
                )

            confidence = float(min(1.0, state_prob))
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
