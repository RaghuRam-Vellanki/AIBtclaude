"""
backtests/nifty_backtest.py
Pull historical NIFTY 50 (^NSEI) 1h bars + BANKNIFTY/USDINR/VIX context, run
the NIFTY pod through BacktestEngine, write logs/nifty_backtest_report.html
with matplotlib charts, and print the pattern-conclusion line.

Caveat: the `nifty_options_oi` strategy votes NEUTRAL throughout backtests
(historical option-chain snapshots aren't free — the strategy is live-only).
Backtest equity therefore reflects the 4-strategy ensemble.

Usage:
    python backtests/nifty_backtest.py
    python backtests/nifty_backtest.py --period 1y --capital 500000
"""
from __future__ import annotations

import argparse
import base64
import io
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from modules.backtest_engine import BacktestConfig, BacktestEngine, BacktestResult
from modules.data_feed_xau import XAUDataFeed   # _yf_download is reused
from modules.signal_generator_nifty import NIFTYSignalGenerator
from strategies import default_nifty_pod

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("nifty_backtest")


REPORT_PATH = ROOT / "logs" / "nifty_backtest_report.html"


# ── NIFTY-flavoured BacktestFeed ─────────────────────────────────────────────

class NiftyBacktestFeed:
    """Substitute for NIFTYDataFeed during backtests. Mirrors the duck-typed
    surface the NIFTY strategies need."""

    def __init__(self, bars_1h: pd.DataFrame, bn_1h: pd.DataFrame,
                 usdinr_1d: pd.DataFrame, vix_1d: pd.DataFrame,
                 fii_dii_static: dict):
        self._bars_1h  = bars_1h.copy()
        self._bn_1h    = bn_1h.copy() if bn_1h is not None and not bn_1h.empty else pd.DataFrame()
        self._usdinr   = usdinr_1d.copy() if usdinr_1d is not None and not usdinr_1d.empty else pd.DataFrame()
        self._vix      = vix_1d.copy() if vix_1d is not None and not vix_1d.empty else pd.DataFrame()
        self._fii_dii  = fii_dii_static or {
            "fii_cash_today": 0.0, "dii_cash_today": 0.0,
            "fii_cash_5d_avg": 0.0, "dii_cash_5d_avg": 0.0,
            "report_date": "", "available": False,
        }
        self._cursor = None

    def set_cursor(self, ts):
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
        if timeframe == "1Hour":
            return full.tail(220)
        if timeframe in ("1Min", "5Min", "15Min"):
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
        return df.resample(rule).agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna()

    def get_banknifty_1h(self) -> pd.DataFrame:
        if self._cursor is None or self._bn_1h.empty:
            return pd.DataFrame()
        return self._bn_1h.loc[:self._cursor].tail(220)

    def get_usdinr_1d(self) -> pd.DataFrame:
        if self._cursor is None or self._usdinr.empty:
            return pd.DataFrame()
        return self._usdinr.loc[:self._cursor].tail(80)

    def get_vix_1d(self) -> pd.DataFrame:
        if self._cursor is None or self._vix.empty:
            return pd.DataFrame()
        return self._vix.loc[:self._cursor].tail(80)

    def get_fii_dii_summary(self) -> dict:
        return self._fii_dii

    @staticmethod
    def get_option_chain() -> dict:
        # Historical option chains aren't available free — backtest skips this strategy.
        return {}


# ── NIFTY-aware engine wrapper ────────────────────────────────────────────────

class NiftyBacktestEngine(BacktestEngine):
    """Override only what differs from XAU: signal generator, default pod, feed type."""

    def __init__(self, config=None, hmm_refit_every: int = 24):
        super().__init__(strategies=default_nifty_pod(),
                         config=config or BacktestConfig(symbol="^NSEI", interval="1h", period="6mo"),
                         hmm_refit_every=hmm_refit_every)
        # Replace the deterministic signal generator with the NIFTY one
        self._gen = NIFTYSignalGenerator(strategies=self.strategies, use_llm=False)

    def run(self, bars_1h, bn_1h=None, usdinr_1d=None, vix_1d=None,
            fii_dii_static=None) -> BacktestResult:
        feed = NiftyBacktestFeed(bars_1h, bn_1h, usdinr_1d, vix_1d, fii_dii_static)

        ensemble_curve, trades = self._replay(feed, active_strategy=None)

        per_strategy_curves = {}
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


# ── Data loading ─────────────────────────────────────────────────────────────

def _load_yf(symbol: str, interval: str, period: str) -> pd.DataFrame:
    df = XAUDataFeed._yf_download(symbol, interval, period)
    if df.empty:
        logger.warning("No data for %s %s %s", symbol, interval, period)
    return df


def _load_fii_dii_static() -> dict:
    """One snapshot of FII/DII flows at start of backtest. Reused for whole run."""
    try:
        from modules.nse_client import get_default_client
        rows = get_default_client().fii_dii_daily()
        if not rows:
            return {}
        fii_rows = [r for r in rows if str(r.get("category", "")).startswith("FII")]
        dii_rows = [r for r in rows if str(r.get("category", "")).startswith("DII")]

        def _net(r: dict) -> float:
            try:
                return float(str(r.get("netValue", "0")).replace(",", ""))
            except Exception:
                return 0.0

        return {
            "fii_cash_today":  _net(fii_rows[0]) if fii_rows else 0.0,
            "dii_cash_today":  _net(dii_rows[0]) if dii_rows else 0.0,
            "fii_cash_5d_avg": (sum(_net(r) for r in fii_rows) / max(1, len(fii_rows))) if fii_rows else 0.0,
            "dii_cash_5d_avg": (sum(_net(r) for r in dii_rows) / max(1, len(dii_rows))) if dii_rows else 0.0,
            "report_date":     str(fii_rows[0].get("date", "")) if fii_rows else "",
            "available":       bool(fii_rows or dii_rows),
        }
    except Exception as exc:
        logger.warning("FII/DII fetch failed: %s — using neutral default", exc)
        return {}


# ── Reporting (mirrors xau_backtest.py shape) ────────────────────────────────

def _fig_to_b64(fig: plt.Figure) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=110)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _plot_equity(result: BacktestResult) -> str:
    fig, ax = plt.subplots(figsize=(11, 4.5))
    eq = result.equity_curve
    ax.plot(eq.index, eq.values, color="#26a69a", lw=1.6, label="Ensemble (5-strategy pod)")
    ax.axhline(result.config.initial_capital, color="#787b86", ls=":", lw=0.8, alpha=0.6)
    ax.set_title(f"NIFTY 50 Backtest — Equity Curve  ({result.config.symbol}, {result.config.interval})")
    ax.set_ylabel("Equity (INR)")
    ax.grid(alpha=0.2)
    ax.legend(loc="upper left", fontsize=9)
    fig.autofmt_xdate()
    return _fig_to_b64(fig)


def _plot_per_strategy(result: BacktestResult) -> str:
    fig, ax = plt.subplots(figsize=(11, 4.5))
    palette = {
        "nifty_microstructure": "#42a5f5",
        "nifty_regime_hmm":     "#ab47bc",
        "nifty_fii_dii_flow":   "#ffa726",
        "nifty_pairs_arb_bn":   "#26c6da",
        "nifty_options_oi":     "#bdbdbd",
    }
    for name, curve in result.per_strategy_curves.items():
        if curve.empty:
            continue
        ax.plot(curve.index, curve.values, lw=1.2,
                color=palette.get(name, "#bdbdbd"), label=name)
    ax.axhline(result.config.initial_capital, color="#787b86", ls=":", lw=0.8, alpha=0.6)
    ax.set_title("Per-Strategy Equity (each pod member run in isolation)")
    ax.set_ylabel("Equity (INR)")
    ax.grid(alpha=0.2)
    ax.legend(loc="upper left", fontsize=9, ncol=3)
    fig.autofmt_xdate()
    return _fig_to_b64(fig)


def _metrics_table_html(result: BacktestResult) -> str:
    m = result.metrics
    rows = [
        ("Total return", f"{m.get('total_return_pct', 0):+.2f}%"),
        ("Sharpe ratio (annualised)", f"{m.get('sharpe', 0):.2f}"),
        ("Sortino ratio (annualised)", f"{m.get('sortino', 0):.2f}"),
        ("Max drawdown", f"{m.get('max_dd_pct', 0):.2f}%"),
        ("Trades", f"{m.get('trades', 0)}"),
        ("Win rate", f"{m.get('win_rate', 0)*100:.1f}%"),
        ("Wins / Losses", f"{m.get('wins', 0)} / {m.get('losses', 0)}"),
        ("Avg R:R (winners)", f"{m.get('avg_rr', 0):.2f}"),
    ]
    return "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in rows)


def _regime_table_html(result: BacktestResult) -> str:
    if result.regime_breakdown.empty:
        return "<tr><td colspan=5>No regime data.</td></tr>"
    rows = []
    for _, r in result.regime_breakdown.iterrows():
        cls = "pos" if r["total_pnl"] >= 0 else "neg"
        rows.append(
            f"<tr><td>{r['regime']}</td><td>{int(r['trades'])}</td>"
            f"<td>{int(r['wins'])}</td><td>{r['win_rate']*100:.1f}%</td>"
            f"<td class='{cls}'>₹{r['total_pnl']:+.2f}</td></tr>"
        )
    return "".join(rows)


def _trades_table_html(result: BacktestResult, head: int = 10) -> str:
    if not result.trades:
        return "<tr><td colspan=9>No trades.</td></tr>"
    sorted_trades = sorted(result.trades, key=lambda t: t.pnl, reverse=True)
    top = sorted_trades[:head]
    bot = sorted_trades[-head:]
    rows = []

    def _row(t, kind):
        return (
            f"<tr><td>{kind}</td>"
            f"<td>{t.entry_time}</td><td>{t.exit_time}</td>"
            f"<td>{t.side.upper()}</td>"
            f"<td>₹{t.entry_price:,.2f}</td><td>₹{t.exit_price:,.2f}</td>"
            f"<td class='{'pos' if t.pnl >= 0 else 'neg'}'>₹{t.pnl:+.2f}</td>"
            f"<td>{t.regime}</td><td>{t.exit_reason}</td></tr>"
        )

    for t in top:
        rows.append(_row(t, "TOP"))
    for t in bot:
        rows.append(_row(t, "BOT"))
    return "".join(rows)


def _write_html_report(result: BacktestResult) -> Path:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    eq_b64 = _plot_equity(result)
    ps_b64 = _plot_per_strategy(result)

    html = f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'>
<title>NIFTY 50 Backtest Report</title>
<style>
  body{{background:#0d0f14;color:#d1d4dc;font:13px/1.5 -apple-system,monospace;
       max-width:1100px;margin:24px auto;padding:0 20px}}
  h1{{color:#ffb300;margin-bottom:4px}} h2{{color:#64b5f6;margin-top:28px}}
  .sub{{color:#787b86;margin-bottom:20px}}
  table{{border-collapse:collapse;width:100%;margin-top:8px}}
  th,td{{border-bottom:1px solid #2a2e39;padding:6px 9px;text-align:left;font-size:12px}}
  th{{color:#787b86;font-weight:700;text-transform:uppercase;letter-spacing:.5px}}
  .pos{{color:#26a69a}} .neg{{color:#ef5350}}
  .conclusion{{padding:14px;background:#131722;border-left:3px solid #ffb300;
              margin:12px 0;border-radius:4px}}
  .caveat{{padding:10px;background:#1a1410;border-left:3px solid #ff7043;
          margin:12px 0;border-radius:4px;color:#ffab91;font-size:12px}}
  img{{max-width:100%;border:1px solid #2a2e39;border-radius:4px;background:#fff}}
</style></head><body>

<h1>◆ NIFTY 50 Institutional Pod — Backtest Report</h1>
<div class='sub'>Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} ·
  Symbol {result.config.symbol} · Interval {result.config.interval} · Period {result.config.period} ·
  Initial capital ₹{result.config.initial_capital:,.0f}
</div>

<div class='conclusion'><b>Pattern conclusion:</b> {result.pattern_conclusion}</div>

<div class='caveat'><b>Backtest caveat:</b> The <code>nifty_options_oi</code> strategy
votes NEUTRAL throughout this backtest because historical option-chain snapshots
aren't available free. Backtest equity reflects the 4-strategy ensemble. The
options strategy is live-only and contributes during real-time runs.</div>

<h2>Equity Curve — Ensemble</h2>
<img src='data:image/png;base64,{eq_b64}'/>

<h2>Per-Strategy Equity</h2>
<img src='data:image/png;base64,{ps_b64}'/>

<h2>Headline Metrics</h2>
<table><tr><th>Metric</th><th>Value</th></tr>{_metrics_table_html(result)}</table>

<h2>Regime Breakdown (HMM at entry time)</h2>
<table><tr><th>Regime</th><th>Trades</th><th>Wins</th><th>Win Rate</th><th>Total P&amp;L</th></tr>
{_regime_table_html(result)}</table>

<h2>Top + Bottom Trades</h2>
<table>
  <tr><th>Bucket</th><th>Entry Time</th><th>Exit Time</th><th>Side</th>
      <th>Entry</th><th>Exit</th><th>P&amp;L</th><th>Regime</th><th>Reason</th></tr>
  {_trades_table_html(result)}
</table>

</body></html>"""
    REPORT_PATH.write_text(html, encoding="utf-8")
    return REPORT_PATH


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="NIFTY 50 pod backtest")
    parser.add_argument("--period",    default="6mo", help="yfinance period (default 6mo)")
    parser.add_argument("--interval",  default="1h",  help="bar interval (default 1h)")
    parser.add_argument("--capital",   type=float, default=200_000.0)
    parser.add_argument("--symbol",    default="^NSEI",    help="yfinance NIFTY symbol")
    parser.add_argument("--bn-symbol", default="^NSEBANK", help="yfinance BANKNIFTY symbol")
    args = parser.parse_args()

    logger.info("Loading NIFTY 1H bars (%s %s)...", args.symbol, args.period)
    bars_1h = _load_yf(args.symbol, args.interval, args.period)
    if bars_1h.empty:
        logger.error("No NIFTY bars returned by yfinance — aborting.")
        sys.exit(1)
    logger.info("NIFTY bars: %d rows  range %s → %s",
                len(bars_1h), bars_1h.index.min(), bars_1h.index.max())

    logger.info("Loading BANKNIFTY 1H...")
    bn_1h = _load_yf(args.bn_symbol, "1h", args.period)
    logger.info("BANKNIFTY bars: %d rows", len(bn_1h))

    logger.info("Loading USDINR 1D...")
    usdinr_1d = _load_yf("USDINR=X", "1d", args.period)
    logger.info("USDINR bars: %d rows", len(usdinr_1d))

    logger.info("Loading India VIX 1D...")
    vix_1d = _load_yf("^INDIAVIX", "1d", args.period)
    logger.info("VIX bars: %d rows", len(vix_1d))

    logger.info("Fetching FII/DII snapshot from NSE (used as static across backtest)...")
    fii_dii_static = _load_fii_dii_static()

    config = BacktestConfig(
        symbol=args.symbol, interval=args.interval, period=args.period,
        initial_capital=args.capital,
    )
    engine = NiftyBacktestEngine(config=config, hmm_refit_every=24)

    logger.info("Running ensemble + 5 isolated per-strategy replays — this can take a minute.")
    result = engine.run(bars_1h, bn_1h=bn_1h, usdinr_1d=usdinr_1d, vix_1d=vix_1d,
                        fii_dii_static=fii_dii_static)

    report = _write_html_report(result)

    print("=" * 70)
    print(f" NIFTY Backtest Report  →  {report}")
    print("=" * 70)
    print(f" {result.pattern_conclusion}")
    print("-" * 70)
    print(f" Trades: {result.metrics.get('trades', 0)}   "
          f"Win rate: {result.metrics.get('win_rate', 0)*100:.1f}%   "
          f"Sharpe: {result.metrics.get('sharpe', 0):.2f}   "
          f"Max DD: {result.metrics.get('max_dd_pct', 0):.2f}%   "
          f"Total return: {result.metrics.get('total_return_pct', 0):+.2f}%")
    print("=" * 70)


if __name__ == "__main__":
    main()
