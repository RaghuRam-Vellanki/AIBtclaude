"""
modules/backtest_engine.py
Vectorized historical replay of the XAU institutional pod.

Replays the same 5 strategies + the deterministic decision aggregator
(no LLM in backtest) over a pandas DataFrame of 1H gold bars. Tracks
trades, equity, per-strategy curves, and a regime-bucketed breakdown
for the pattern-conclusion report.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from config import XAU_MIN_POD_SCORE, XAU_STOP_LOSS_PCT
from modules.signal_generator import TradeSignal
from modules.signal_generator_xau import XAUSignalGenerator
from modules.technical_analysis import (
    calculate_atr,
    calculate_vwap,
    classify_structure,
    price_zscore,
)
from strategies import default_pod
from strategies.base import StrategyAgent, StrategyVote

logger = logging.getLogger(__name__)


# ── Public types ─────────────────────────────────────────────────────────────

@dataclass
class BacktestConfig:
    symbol:          str   = "GC=F"
    interval:        str   = "1h"
    period:          str   = "2y"
    initial_capital: float = 10_000.0
    commission_pct:  float = 0.0003
    slippage_bps:    int   = 2
    warmup_bars:     int   = 220
    min_pod_score:   float = XAU_MIN_POD_SCORE


@dataclass
class BacktestTrade:
    entry_time:  pd.Timestamp
    exit_time:   Optional[pd.Timestamp]
    side:        str        # "long" / "short"
    entry_price: float
    exit_price:  float = 0.0
    stop_loss:   float = 0.0
    take_profit: float = 0.0
    qty:         float = 0.0
    pnl:         float = 0.0
    pnl_pct:     float = 0.0
    exit_reason: str   = ""
    quality:     str   = ""
    pod_sum:     float = 0.0
    regime:      str   = "unknown"


@dataclass
class BacktestResult:
    config:              BacktestConfig
    trades:              List[BacktestTrade]
    equity_curve:        pd.Series
    per_strategy_curves: Dict[str, pd.Series]
    metrics:             Dict[str, float]
    regime_breakdown:    pd.DataFrame
    pattern_conclusion:  str = ""


# ── Synthetic in-sample feed ─────────────────────────────────────────────────

class BacktestFeed:
    """Minimal duck-typed XAUDataFeed substitute for in-sample replay."""

    def __init__(self,
                 bars_1h: pd.DataFrame,
                 dxy_1h: Optional[pd.DataFrame] = None,
                 tnx_1d: Optional[pd.DataFrame] = None,
                 cot_static: Optional[dict] = None):
        self._bars_1h = bars_1h.copy()
        self._dxy_1h  = dxy_1h.copy() if dxy_1h is not None and not dxy_1h.empty else pd.DataFrame()
        self._tnx_1d  = tnx_1d.copy() if tnx_1d is not None and not tnx_1d.empty else pd.DataFrame()
        self._cot     = cot_static or {
            "commercial_net": 0, "noncommercial_net": 0,
            "report_date": "", "noncommercial_net_4w_avg": 0,
        }
        self._cursor: Optional[pd.Timestamp] = None
        self._4h_cache: Optional[pd.DataFrame] = None
        self._4h_cache_idx: int = -1
        self._1d_cache: Optional[pd.DataFrame] = None
        self._1d_cache_idx: int = -1

    def set_cursor(self, ts: pd.Timestamp) -> None:
        self._cursor = ts

    @property
    def latest_price(self) -> float:
        if self._cursor is None or self._bars_1h.empty:
            return 0.0
        sub = self._bars_1h.loc[:self._cursor]
        return float(sub.iloc[-1]["close"]) if not sub.empty else 0.0

    def get_bars(self, timeframe: str = "1Hour") -> pd.DataFrame:
        if self._cursor is None or self._bars_1h.empty:
            return pd.DataFrame()
        full = self._bars_1h.loc[:self._cursor]
        if full.empty:
            return pd.DataFrame()

        if timeframe == "1Hour":
            return full.tail(220)
        if timeframe == "1Min":
            # Synthesise pseudo-1m from latest 1h bar (single-bar approx).
            # Most strategies that need 1m return Neutral for sparse history.
            return pd.DataFrame()
        if timeframe == "15Min":
            return pd.DataFrame()
        if timeframe == "5Min":
            return pd.DataFrame()
        if timeframe == "4Hour":
            return self._resample(full, "4h").tail(110)
        if timeframe == "1Day":
            return self._resample(full, "1D").tail(80)
        return full.tail(220)

    @staticmethod
    def _resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
        if df.empty:
            return df
        agg = df.resample(rule).agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna()
        return agg

    def get_dxy_1h(self) -> pd.DataFrame:
        if self._cursor is None or self._dxy_1h.empty:
            return pd.DataFrame()
        return self._dxy_1h.loc[:self._cursor].tail(220)

    def get_tnx_1d(self) -> pd.DataFrame:
        if self._cursor is None or self._tnx_1d.empty:
            return pd.DataFrame()
        return self._tnx_1d.loc[:self._cursor].tail(80)

    def get_cot_gold_net(self) -> dict:
        return self._cot


# ── Engine ───────────────────────────────────────────────────────────────────

class BacktestEngine:
    """Single-position, hourly-bar replay over the XAU pod."""

    def __init__(self, strategies: Optional[List[StrategyAgent]] = None,
                 config: Optional[BacktestConfig] = None,
                 hmm_refit_every: int = 24):
        self.strategies = strategies if strategies is not None else default_pod()
        self.config     = config or BacktestConfig()
        # Reusable signal generator with use_llm=False — this gives us the deterministic
        # vote-aggregator for free (no Groq calls during backtest).
        self._gen = XAUSignalGenerator(strategies=self.strategies, use_llm=False)
        # Performance: refit HMM every N bars instead of every bar (same regime
        # for ~24h is fine). Reduces a 6-month replay from ~19min to ~90s.
        self._hmm_refit_every = max(1, int(hmm_refit_every))
        self._cached_hmm_model = None     # populated during _replay

    # ── Public entry point ────────────────────────────────────────────────────

    def run(self,
            bars_1h: pd.DataFrame,
            dxy_1h: Optional[pd.DataFrame] = None,
            tnx_1d: Optional[pd.DataFrame] = None,
            cot_static: Optional[dict] = None) -> BacktestResult:
        feed = BacktestFeed(bars_1h, dxy_1h, tnx_1d, cot_static)

        # Main run: full ensemble (all 5 strategies)
        ensemble_curve, trades = self._replay(feed, active_strategy=None)

        # Per-strategy curves (each in isolation; others zeroed)
        per_strategy_curves: Dict[str, pd.Series] = {}
        for strat in self.strategies:
            try:
                curve, _ = self._replay(feed, active_strategy=strat.name)
                per_strategy_curves[strat.name] = curve
            except Exception as exc:
                logger.warning("Per-strategy replay failed for %s: %s", strat.name, exc)
                per_strategy_curves[strat.name] = pd.Series(
                    index=ensemble_curve.index, data=self.config.initial_capital,
                )

        metrics = self._compute_metrics(ensemble_curve, trades)
        regime_breakdown = self._regime_breakdown(trades)
        conclusion = self._pattern_conclusion(metrics, per_strategy_curves, regime_breakdown)

        return BacktestResult(
            config=self.config,
            trades=trades,
            equity_curve=ensemble_curve,
            per_strategy_curves=per_strategy_curves,
            metrics=metrics,
            regime_breakdown=regime_breakdown,
            pattern_conclusion=conclusion,
        )

    # ── Replay loop ───────────────────────────────────────────────────────────

    def _replay(self, feed: BacktestFeed,
                active_strategy: Optional[str]) -> tuple[pd.Series, List[BacktestTrade]]:
        cfg = self.config
        bars = feed._bars_1h    # already chronologically indexed
        equity = cfg.initial_capital
        equity_history: List[tuple[pd.Timestamp, float]] = []
        trades: List[BacktestTrade] = []
        open_trade: Optional[BacktestTrade] = None

        warmup = max(cfg.warmup_bars, 50)
        if len(bars) <= warmup:
            return pd.Series(dtype=float), []

        # Reset HMM cache for this replay
        self._cached_hmm_model = None

        for i in range(warmup, len(bars)):
            ts  = bars.index[i]
            bar = bars.iloc[i]
            feed.set_cursor(ts)

            # Periodically refit HMM and reuse the model on intervening bars.
            if (i - warmup) % self._hmm_refit_every == 0:
                self._cached_hmm_model = self._fit_hmm(bars.iloc[max(0, i - 200):i])

            # ── 1. Manage existing trade (intrabar SL/TP check) ───────────────
            if open_trade is not None:
                hit = self._check_exit(open_trade, bar)
                if hit is not None:
                    exit_price, reason = hit
                    self._close(open_trade, ts, exit_price, reason, cfg)
                    equity += open_trade.pnl
                    trades.append(open_trade)
                    open_trade = None

            # ── 2. Look for a new signal ──────────────────────────────────────
            if open_trade is None:
                snap = self._build_snapshot(feed)
                if snap["current_price"] <= 0:
                    equity_history.append((ts, equity))
                    continue

                # Inject the cached HMM model so regime_hmm strategies skip refit
                if self._cached_hmm_model is not None:
                    snap["_hmm_model"] = self._cached_hmm_model

                votes = self._collect_votes(snap, feed, active_strategy)
                signal = self._aggregate(votes, snap)
                if signal.bias in ("BULLISH", "BEARISH") and signal.signal_quality != "NO_TRADE":
                    open_trade = self._open(signal, snap, votes, ts, equity, cfg)

            equity_history.append((ts, equity + (self._mtm(open_trade, bar) if open_trade else 0.0)))

        # Force-close any dangling trade at last bar
        if open_trade is not None and not bars.empty:
            last_bar = bars.iloc[-1]
            self._close(open_trade, bars.index[-1], float(last_bar["close"]),
                        "end_of_data", cfg)
            equity += open_trade.pnl
            trades.append(open_trade)

        idx, vals = zip(*equity_history) if equity_history else ([bars.index[-1]], [equity])
        return pd.Series(vals, index=pd.DatetimeIndex(idx)), trades

    # ── HMM caching ───────────────────────────────────────────────────────────

    @staticmethod
    def _fit_hmm(bars_window: pd.DataFrame):
        """Fit a 3-state Gaussian HMM on the last ~200 1h bars and return
        the fitted model so regime_hmm strategies can call .predict() instead
        of refitting on every bar."""
        if bars_window is None or bars_window.empty or len(bars_window) < 80:
            return None
        try:
            import warnings as _w
            from hmmlearn.hmm import GaussianHMM
        except Exception:
            return None
        closes = bars_window["close"].astype(float).values
        highs  = bars_window["high"].astype(float).values
        lows   = bars_window["low"].astype(float).values
        log_returns = np.diff(np.log(closes))
        tr = np.maximum.reduce([
            highs[1:] - lows[1:],
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:]  - closes[:-1]),
        ])
        log_tr = np.log(np.maximum(tr, 1e-9))
        X = np.column_stack([log_returns, log_tr])[-200:]
        try:
            with _w.catch_warnings():
                _w.simplefilter("ignore")
                model = GaussianHMM(
                    n_components=3, covariance_type="diag",
                    n_iter=50, random_state=42, tol=1e-3,
                )
                model.fit(X)
                return model
        except Exception:
            return None

    # ── Vote collection / aggregation ─────────────────────────────────────────

    def _collect_votes(self, snap: dict, feed: BacktestFeed,
                       active_strategy: Optional[str]) -> List[StrategyVote]:
        votes: List[StrategyVote] = []
        for strat in self.strategies:
            try:
                vote = strat.vote(snap, feed)
            except Exception as exc:
                vote = StrategyVote(
                    name=strat.name, inspired_by=strat.inspired_by,
                    direction="NEUTRAL", confidence=0.0,
                    rationale=f"Strategy error: {exc}",
                )
            # Per-strategy isolation: zero out everyone but `active_strategy`
            if active_strategy is not None and strat.name != active_strategy:
                vote = StrategyVote(
                    name=strat.name, inspired_by=strat.inspired_by,
                    direction="NEUTRAL", confidence=0.0,
                    rationale="muted (per-strategy isolated run)",
                    metadata=vote.metadata,
                )
            vote.archetype = getattr(strat, "archetype", "FLOW")
            votes.append(vote)
        return votes

    def _aggregate(self, votes: List[StrategyVote], snap: dict) -> TradeSignal:
        # Reuse the deterministic aggregator from the live signal generator —
        # it implements the same chaos-override / macro-override / quality logic.
        return self._gen._deterministic_signal(votes, snap)

    # ── Trade lifecycle ───────────────────────────────────────────────────────

    @staticmethod
    def _open(signal: TradeSignal, snap: dict, votes: List[StrategyVote],
              ts: pd.Timestamp, equity: float, cfg: BacktestConfig) -> BacktestTrade:
        side = "long" if signal.bias == "BULLISH" else "short"
        # Position sized so that risk = risk_pct * equity / stop_distance
        risk_dollars = equity * (signal.risk_pct or 0.005)
        stop_dist = max(abs(signal.entry_price - signal.stop_loss), 1e-6)
        qty = risk_dollars / stop_dist

        # Simulate slippage + commission on entry
        slip = signal.entry_price * (cfg.slippage_bps / 10_000.0)
        entry_eff = signal.entry_price + slip if side == "long" else signal.entry_price - slip
        entry_eff = round(entry_eff, 2)

        regime = next((v.metadata.get("regime", "unknown")
                       for v in votes if v.name == "regime_hmm"), "unknown")

        return BacktestTrade(
            entry_time=ts,
            exit_time=None,
            side=side,
            entry_price=entry_eff,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit_1,
            qty=qty,
            quality=signal.signal_quality,
            pod_sum=round(sum(v.score for v in votes), 3),
            regime=regime,
        )

    @staticmethod
    def _check_exit(t: BacktestTrade, bar: pd.Series) -> Optional[tuple[float, str]]:
        h = float(bar["high"])
        l = float(bar["low"])
        if t.side == "long":
            if l <= t.stop_loss:
                return t.stop_loss, "stop_loss"
            if t.take_profit > 0 and h >= t.take_profit:
                return t.take_profit, "tp1"
        else:
            if h >= t.stop_loss:
                return t.stop_loss, "stop_loss"
            if t.take_profit > 0 and l <= t.take_profit:
                return t.take_profit, "tp1"
        return None

    @staticmethod
    def _close(t: BacktestTrade, ts: pd.Timestamp, exit_price: float,
               reason: str, cfg: BacktestConfig) -> None:
        # commission on both legs
        commission = (t.entry_price + exit_price) * t.qty * cfg.commission_pct
        if t.side == "long":
            gross = (exit_price - t.entry_price) * t.qty
        else:
            gross = (t.entry_price - exit_price) * t.qty
        pnl = gross - commission
        t.exit_price = exit_price
        t.exit_time  = ts
        t.exit_reason = reason
        t.pnl = round(pnl, 2)
        t.pnl_pct = round(pnl / max(t.entry_price * t.qty, 1e-9) * 100, 3)

    @staticmethod
    def _mtm(trade: Optional[BacktestTrade], bar: pd.Series) -> float:
        if trade is None:
            return 0.0
        last = float(bar["close"])
        if trade.side == "long":
            return (last - trade.entry_price) * trade.qty
        return (trade.entry_price - last) * trade.qty

    # ── Snapshot for strategies (lightweight version of agent's builder) ──────

    @staticmethod
    def _build_snapshot(feed: BacktestFeed) -> dict:
        bars_1h = feed.get_bars("1Hour")
        bars_4h = feed.get_bars("4Hour")
        bars_1d = feed.get_bars("1Day")
        if bars_1h.empty:
            return {"current_price": 0}

        current_price = float(bars_1h.iloc[-1]["close"])
        # Approximate session VWAP using last 24 1h bars (one trading day)
        vwap = calculate_vwap(bars_1h.tail(24))
        daily_atr = calculate_atr(bars_1d) if not bars_1d.empty else 0.0
        zscore = price_zscore(bars_1h)
        h4_struct = classify_structure(bars_4h) if not bars_4h.empty else "ranging"
        h1_struct = classify_structure(bars_1h)
        daily_struct = classify_structure(bars_1d) if not bars_1d.empty else "ranging"

        return {
            "timestamp_ist":   bars_1h.index[-1].isoformat(),
            "current_price":   current_price,
            "session_vwap":    vwap,
            "vwap_distance":   round(current_price - vwap, 2) if vwap else 0,
            "daily_atr":       daily_atr,
            "zscore":          zscore,
            "current_session": "Backtest",
            "daily_structure": daily_struct,
            "h4_structure":    h4_struct,
            "h1_structure":    h1_struct,
            "daily_high":      float(bars_1d["high"].iloc[-1]) if not bars_1d.empty else 0,
            "daily_low":       float(bars_1d["low"].iloc[-1])  if not bars_1d.empty else 0,
            "asian_range_high": 0, "asian_range_low": 0,
            "pdh": 0, "pdl": 0, "weekly_open": 0,
            "round_numbers": [],
            "consecutive_losses": 0,
            "daily_pnl_pct": 0.0,
            "last_trade_result": "N/A",
            "account_value": 0.0,
        }

    # ── Metrics + regime breakdown ────────────────────────────────────────────

    @staticmethod
    def _compute_metrics(equity: pd.Series, trades: List[BacktestTrade]) -> Dict[str, float]:
        if equity.empty:
            return {"sharpe": 0, "sortino": 0, "max_dd": 0, "win_rate": 0,
                    "total_return_pct": 0, "trades": 0, "avg_rr": 0}

        returns = equity.pct_change().dropna()
        ann_factor = np.sqrt(24 * 365)  # hourly
        sharpe = float(returns.mean() / returns.std() * ann_factor) if returns.std() > 0 else 0.0
        downside = returns[returns < 0]
        sortino = float(returns.mean() / downside.std() * ann_factor) if len(downside) > 0 and downside.std() > 0 else 0.0
        peak = equity.cummax()
        max_dd = float(((equity - peak) / peak).min() * 100) if (peak > 0).any() else 0.0
        total_return = float((equity.iloc[-1] / equity.iloc[0] - 1) * 100) if equity.iloc[0] > 0 else 0.0

        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl < 0]
        win_rate = len(wins) / len(trades) if trades else 0.0

        rrs = []
        for t in trades:
            stop_dist = abs(t.entry_price - t.stop_loss)
            if stop_dist > 0 and t.pnl > 0 and t.qty > 0:
                rrs.append(abs(t.pnl) / (stop_dist * t.qty))
        avg_rr = float(np.mean(rrs)) if rrs else 0.0

        return {
            "sharpe":           round(sharpe, 2),
            "sortino":          round(sortino, 2),
            "max_dd_pct":       round(max_dd, 2),
            "total_return_pct": round(total_return, 2),
            "win_rate":         round(win_rate, 3),
            "wins":             len(wins),
            "losses":           len(losses),
            "trades":           len(trades),
            "avg_rr":           round(avg_rr, 2),
        }

    @staticmethod
    def _regime_breakdown(trades: List[BacktestTrade]) -> pd.DataFrame:
        if not trades:
            return pd.DataFrame(columns=["regime", "trades", "wins", "win_rate", "total_pnl"])

        rows: Dict[str, Dict[str, float]] = {}
        for t in trades:
            r = t.regime or "unknown"
            slot = rows.setdefault(r, {"trades": 0, "wins": 0, "total_pnl": 0.0})
            slot["trades"] += 1
            slot["wins"]   += 1 if t.pnl > 0 else 0
            slot["total_pnl"] += t.pnl

        out = []
        for r, s in rows.items():
            out.append({
                "regime":    r,
                "trades":    int(s["trades"]),
                "wins":      int(s["wins"]),
                "win_rate":  round(s["wins"] / s["trades"], 3) if s["trades"] else 0,
                "total_pnl": round(s["total_pnl"], 2),
            })
        return pd.DataFrame(out).sort_values("trades", ascending=False).reset_index(drop=True)

    @staticmethod
    def _pattern_conclusion(metrics: Dict[str, float],
                            per_strategy_curves: Dict[str, pd.Series],
                            regime_breakdown: pd.DataFrame) -> str:
        # Pick the best-performing standalone strategy
        rankings: List[tuple[str, float]] = []
        for name, curve in per_strategy_curves.items():
            if curve.empty:
                continue
            ret = (curve.iloc[-1] / curve.iloc[0] - 1) * 100 if curve.iloc[0] > 0 else 0
            rankings.append((name, float(ret)))
        rankings.sort(key=lambda x: x[1], reverse=True)
        top = rankings[0] if rankings else ("none", 0.0)
        worst = rankings[-1] if rankings else ("none", 0.0)

        best_regime_row = ""
        if not regime_breakdown.empty:
            best_regime = regime_breakdown.sort_values("total_pnl", ascending=False).iloc[0]
            best_regime_row = (
                f" Most profitable regime: {best_regime['regime']} "
                f"({int(best_regime['trades'])} trades, "
                f"win rate {best_regime['win_rate']*100:.0f}%, "
                f"P&L ${best_regime['total_pnl']:+.2f})."
            )

        return (
            f"Ensemble Sharpe {metrics.get('sharpe', 0):.2f} | "
            f"Total return {metrics.get('total_return_pct', 0):+.2f}% over "
            f"{metrics.get('trades', 0)} trades, win rate {metrics.get('win_rate', 0)*100:.0f}%, "
            f"max DD {metrics.get('max_dd_pct', 0):.2f}%. "
            f"Top standalone: {top[0]} ({top[1]:+.2f}%); "
            f"weakest standalone: {worst[0]} ({worst[1]:+.2f}%)."
            + best_regime_row
        )
