"""
modules/macro_data.py
Real-yield + yield-curve fetcher for the macro pod.

Real 10Y yield is the dominant structural driver of XAU/USD — a 10bp move in
real yields ≈ 0.3-0.5% in gold, larger than DXY's contribution. The bot was
running with DXY + nominal TNX only, missing the breakeven leg entirely.

Two data sources, same interface:
  - synthetic  (default, no key required): TNX (yfinance) − rolling CPI YoY
    from `data/cpi_cache.json`. Sufficient for *direction*, not basis-points
    accuracy — fine for gating gold bias.
  - fred       (if FRED_API_KEY in env): DGS10 − T10YIE daily, basis-point
    accurate. ~60-second signup at https://fredaccount.stlouisfed.org/apikey

Output schema is identical regardless of source, so `macro_flow` is source-blind.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

_CPI_CACHE_FILE = Path(__file__).resolve().parent.parent / "data" / "cpi_cache.json"

# Conservative default: most recent US CPI YoY readings. Used when the cache
# file is missing/stale; user updates monthly via BLS or the cache file.
_CPI_FALLBACK_YOY = 2.6   # %

# yfinance tickers for the synthetic path
_TICKER_10Y = "^TNX"      # 10-year Treasury yield (×10 — divide by 10 to %)
_TICKER_2Y  = "^IRX"      # 13-week T-bill (proxy short-end; closer to 2Y is unavailable on yfinance)
                          # Real 2Y isn't on yfinance free; we use 5Y FVX below for curve
_TICKER_5Y  = "^FVX"

# Per-call cache so we don't hammer yfinance every signal cycle
_CACHE: Dict[str, Any] = {"_ts": 0.0, "_data": None}
_CACHE_TTL_SEC = 3600     # 1 hour


# ── CPI YoY: file-cached, manually refreshed monthly ───────────────────────────

def _load_cpi_yoy() -> float:
    """Return latest CPI YoY % from `data/cpi_cache.json`.

    Cache schema: {"latest_yoy_pct": 2.7, "as_of": "2026-04-01", "history": {...}}
    Falls back to _CPI_FALLBACK_YOY if file missing.
    """
    try:
        if _CPI_CACHE_FILE.exists():
            with open(_CPI_CACHE_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return float(data.get("latest_yoy_pct", _CPI_FALLBACK_YOY))
    except Exception as exc:
        logger.debug("CPI cache read failed (%s); using fallback %.2f", exc, _CPI_FALLBACK_YOY)
    return _CPI_FALLBACK_YOY


# ── Synthetic path (yfinance) ──────────────────────────────────────────────────

def _fetch_synthetic_yields() -> Optional[Dict[str, Any]]:
    """yfinance: 10Y nominal − CPI YoY = synthetic real yield."""
    try:
        import yfinance as yf
    except Exception as exc:
        logger.debug("yfinance unavailable: %s", exc)
        return None

    def _close_series(ticker: str, period: str = "10d"):
        df = yf.download(ticker, period=period, interval="1d",
                         progress=False, auto_adjust=False)
        if df is None or df.empty:
            return None
        # yfinance now returns MultiIndex columns (Price, Ticker). Flatten so
        # we always pull a 1-D Series of closes.
        close = df["Close"]
        if hasattr(close, "columns"):    # MultiIndex case → single-ticker frame
            close = close.iloc[:, 0]
        return close.astype(float)

    try:
        tnx_close = _close_series(_TICKER_10Y, "10d")
        if tnx_close is None or len(tnx_close) < 6:
            return None

        # yfinance currently returns ^TNX as the actual yield in % (e.g. 4.39 = 4.39%).
        # No /10 scaling.
        cpi_yoy = _load_cpi_yoy()
        nom_now   = float(tnx_close.iloc[-1])
        nom_5d    = float(tnx_close.iloc[-6])
        real_now  = nom_now - cpi_yoy
        real_5d   = nom_5d  - cpi_yoy
        delta_bp  = round((real_now - real_5d) * 100.0, 1)   # %·100 = bps

        # 5s10s curve as best-effort proxy for 2s10s (yfinance has no clean 2Y)
        curve = 0.0
        try:
            fvx_close = _close_series(_TICKER_5Y, "5d")
            if fvx_close is not None and len(fvx_close) > 0:
                curve = round((nom_now - float(fvx_close.iloc[-1])) * 100.0, 1)
        except Exception:
            pass

        return {
            "real_yield_10y": round(real_now, 3),
            "real_yield_5d_change_bp": delta_bp,
            "yield_curve_2s10s": curve,        # 5s10s used as 2s10s proxy
            "tnx_nominal": round(nom_now, 3),
            "cpi_yoy_assumed": round(cpi_yoy, 2),
            "source": "synthetic",
        }
    except Exception as exc:
        logger.warning("Synthetic real-yield fetch failed: %s", exc)
        return None


# ── FRED path (precise, optional) ──────────────────────────────────────────────

def _fred_series(series_id: str, api_key: str, limit: int = 20) -> Optional[list]:
    """Return list of (date, value) tuples newest-last for a FRED series."""
    url = ("https://api.stlouisfed.org/fred/series/observations"
           f"?series_id={series_id}&api_key={api_key}&file_type=json&limit={limit}"
           "&sort_order=desc")
    try:
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        obs = r.json().get("observations", [])
        # Reverse so newest is last
        return [(o["date"], float(o["value"])) for o in reversed(obs)
                if o.get("value") not in (".", "", None)]
    except Exception as exc:
        logger.warning("FRED %s fetch failed: %s", series_id, exc)
        return None


def _fetch_fred_yields(api_key: str) -> Optional[Dict[str, Any]]:
    """Real 10Y = DGS10 (nominal 10Y) − T10YIE (10Y breakeven). Both daily."""
    dgs10  = _fred_series("DGS10", api_key, limit=10)
    t10yie = _fred_series("T10YIE", api_key, limit=10)
    if not dgs10 or not t10yie:
        return None
    try:
        # Align by latest available — assume daily series mostly match dates
        nom_now = dgs10[-1][1]
        be_now  = t10yie[-1][1]
        real_now = nom_now - be_now
        if len(dgs10) >= 6 and len(t10yie) >= 6:
            real_5d = dgs10[-6][1] - t10yie[-6][1]
            delta_bp = round((real_now - real_5d) * 100.0, 1)
        else:
            delta_bp = 0.0

        # 2s10s curve
        dgs2 = _fred_series("DGS2", api_key, limit=2)
        curve = round((nom_now - dgs2[-1][1]) * 100.0, 1) if dgs2 else 0.0

        return {
            "real_yield_10y": round(real_now, 3),
            "real_yield_5d_change_bp": delta_bp,
            "yield_curve_2s10s": curve,
            "tnx_nominal": round(nom_now, 3),
            "breakeven_10y": round(be_now, 3),
            "source": "fred",
        }
    except Exception as exc:
        logger.warning("FRED real-yield calc failed: %s", exc)
        return None


# ── Public entry point ─────────────────────────────────────────────────────────

def fetch_real_yields(force_refresh: bool = False) -> Dict[str, Any]:
    """Return the real-yield dict. 1h cached.

    Schema:
      real_yield_10y         : float  — current 10Y real yield in %
      real_yield_5d_change_bp: float  — Δ over last 5 sessions, in basis points
      yield_curve_2s10s      : float  — 10Y − 2Y in bps (curve steepness)
      source                 : str    — 'fred' | 'synthetic' | 'unavailable'
    """
    now = time.time()
    if not force_refresh and _CACHE["_data"] is not None and (now - _CACHE["_ts"]) < _CACHE_TTL_SEC:
        return _CACHE["_data"]

    fred_key = os.environ.get("FRED_API_KEY", "").strip()
    out: Optional[Dict[str, Any]] = None
    if fred_key:
        out = _fetch_fred_yields(fred_key)
    if out is None:
        out = _fetch_synthetic_yields()
    if out is None:
        out = {
            "real_yield_10y": 0.0,
            "real_yield_5d_change_bp": 0.0,
            "yield_curve_2s10s": 0.0,
            "source": "unavailable",
        }

    _CACHE["_ts"]   = now
    _CACHE["_data"] = out
    return out
