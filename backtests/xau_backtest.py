"""
backtests/xau_backtest.py
Pull ~2 years of 1H gold (GC=F) bars + macro context, run the institutional
pod through BacktestEngine, write logs/backtest_report.html with matplotlib
charts, and print the pattern-conclusion line.

Usage:
    python backtests/xau_backtest.py
    python backtests/xau_backtest.py --period 1y --capital 25000
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

# Make repo root importable when run as a script
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from modules.backtest_engine import BacktestConfig, BacktestEngine, BacktestResult
from modules.data_feed_xau import XAUDataFeed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("xau_backtest")


REPORT_PATH = ROOT / "logs" / "backtest_report.html"


# ── Data loading ─────────────────────────────────────────────────────────────

def _load_yf(symbol: str, interval: str, period: str) -> pd.DataFrame:
    """Use the same yfinance helper the live feed uses, so column normalisation matches."""
    df = XAUDataFeed._yf_download(symbol, interval, period)
    if df.empty:
        logger.warning("No data for %s %s %s", symbol, interval, period)
    return df


def _load_cot_static() -> dict:
    """Single COT snapshot from CFTC; reused across the whole backtest."""
    feed = XAUDataFeed()
    cot = feed.get_cot_gold_net()
    if not cot or (cot.get("commercial_net", 0) == 0 and cot.get("noncommercial_net", 0) == 0):
        logger.warning("COT data unavailable — macro_flow COT signal will read 'unavailable'")
    return cot


# ── Reporting ────────────────────────────────────────────────────────────────

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
    ax.set_title(f"XAU/USD Backtest — Equity Curve  ({result.config.symbol}, {result.config.interval})")
    ax.set_ylabel("Equity (USD)")
    ax.grid(alpha=0.2)
    ax.legend(loc="upper left", fontsize=9)
    fig.autofmt_xdate()
    return _fig_to_b64(fig)


def _plot_per_strategy(result: BacktestResult) -> str:
    fig, ax = plt.subplots(figsize=(11, 4.5))
    palette = {
        "microstructure":  "#42a5f5",
        "regime_hmm":      "#ab47bc",
        "macro_flow":      "#ffa726",
        "cointegration":   "#26c6da",
        "momentum_macro":  "#66bb6a",
    }
    for name, curve in result.per_strategy_curves.items():
        if curve.empty:
            continue
        ax.plot(curve.index, curve.values, lw=1.2,
                color=palette.get(name, "#bdbdbd"), label=name)
    ax.axhline(result.config.initial_capital, color="#787b86", ls=":", lw=0.8, alpha=0.6)
    ax.set_title("Per-Strategy Equity (each pod member run in isolation)")
    ax.set_ylabel("Equity (USD)")
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


def _trades_table_html(result: BacktestResult, head: int = 10) -> str:
    if not result.trades:
        return "<tr><td colspan=7>No trades.</td></tr>"
    sorted_trades = sorted(result.trades, key=lambda t: t.pnl, reverse=True)
    top = sorted_trades[:head]
    bot = sorted_trades[-head:]
    rows = []

    def _row(t, kind):
        return (
            f"<tr><td>{kind}</td>"
            f"<td>{t.entry_time}</td><td>{t.exit_time}</td>"
            f"<td>{t.side.upper()}</td>"
            f"<td>${t.entry_price:,.2f}</td><td>${t.exit_price:,.2f}</td>"
            f"<td class='{'pos' if t.pnl >= 0 else 'neg'}'>${t.pnl:+.2f}</td>"
            f"<td>{t.regime}</td><td>{t.exit_reason}</td></tr>"
        )

    for t in top:
        rows.append(_row(t, "TOP"))
    for t in bot:
        rows.append(_row(t, "BOT"))
    return "".join(rows)


def _regime_table_html(result: BacktestResult) -> str:
    if result.regime_breakdown.empty:
        return "<tr><td colspan=5>No regime data.</td></tr>"
    rows = []
    for _, r in result.regime_breakdown.iterrows():
        cls = "pos" if r["total_pnl"] >= 0 else "neg"
        rows.append(
            f"<tr><td>{r['regime']}</td><td>{int(r['trades'])}</td>"
            f"<td>{int(r['wins'])}</td><td>{r['win_rate']*100:.1f}%</td>"
            f"<td class='{cls}'>${r['total_pnl']:+.2f}</td></tr>"
        )
    return "".join(rows)


def _write_html_report(result: BacktestResult) -> Path:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    eq_b64 = _plot_equity(result)
    ps_b64 = _plot_per_strategy(result)

    html = f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'>
<title>XAU/USD Backtest Report</title>
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
  img{{max-width:100%;border:1px solid #2a2e39;border-radius:4px;background:#fff}}
</style></head><body>

<h1>◆ XAU/USD Institutional Pod — Backtest Report</h1>
<div class='sub'>Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} ·
  Symbol {result.config.symbol} · Interval {result.config.interval} · Period {result.config.period} ·
  Initial capital ${result.config.initial_capital:,.0f}
</div>

<div class='conclusion'><b>Pattern conclusion:</b> {result.pattern_conclusion}</div>

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
    parser = argparse.ArgumentParser(description="XAU/USD pod backtest")
    parser.add_argument("--period",   default="2y",   help="yfinance period (default 2y)")
    parser.add_argument("--interval", default="1h",   help="bar interval (default 1h)")
    parser.add_argument("--capital",  type=float, default=10_000.0)
    parser.add_argument("--symbol",   default="GC=F", help="yfinance gold symbol")
    args = parser.parse_args()

    logger.info("Loading XAU 1H bars (%s %s)...", args.symbol, args.period)
    bars_1h = _load_yf(args.symbol, args.interval, args.period)
    if bars_1h.empty:
        logger.error("No XAU bars returned by yfinance — aborting.")
        sys.exit(1)
    logger.info("XAU bars: %d rows  range %s → %s",
                len(bars_1h), bars_1h.index.min(), bars_1h.index.max())

    logger.info("Loading DXY 1H...")
    dxy_1h = _load_yf("DX-Y.NYB", "1h", args.period)
    if dxy_1h.empty:
        dxy_1h = _load_yf("DX=F", "1h", args.period)
    logger.info("DXY bars: %d rows", len(dxy_1h))

    logger.info("Loading TNX 1D...")
    tnx_1d = _load_yf("^TNX", "1d", args.period)
    logger.info("TNX bars: %d rows", len(tnx_1d))

    logger.info("Fetching latest CFTC COT report (used as static across backtest)...")
    cot_static = _load_cot_static()

    config = BacktestConfig(
        symbol=args.symbol, interval=args.interval, period=args.period,
        initial_capital=args.capital,
    )
    engine = BacktestEngine(config=config)

    logger.info("Running ensemble + 5 isolated per-strategy replays — this can take a minute.")
    result = engine.run(bars_1h, dxy_1h=dxy_1h, tnx_1d=tnx_1d, cot_static=cot_static)

    report = _write_html_report(result)

    print("=" * 70)
    print(f" XAU Backtest Report  →  {report}")
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
