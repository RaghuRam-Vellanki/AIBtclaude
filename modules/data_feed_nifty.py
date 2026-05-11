"""
modules/data_feed_nifty.py
yfinance-based polling feed for NIFTY 50 (^NSEI), plus the macro/flow inputs
the strategy pod consumes: BANKNIFTY (pairs-arb counterpart), USDINR, India
VIX, FII/DII daily flows (NSE JSON), and the option chain (NSE JSON).

Mirrors XAUDataFeed shape so BacktestFeed adapters can reuse the same
duck-typed interface (get_bars + named macro helpers).

Polling thread skips itself when market is closed (NSE: 9:15-15:30 IST,
Mon-Fri, ex-holidays) — saves 16+ hours/day of useless yfinance calls.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Callable, Deque, Dict, Optional

import pandas as pd

from config import NIFTY_BN_YFINANCE_SYMBOL, NIFTY_USDINR_SYMBOL, NIFTY_VIX_SYMBOL, NIFTY_YFINANCE_SYMBOL
from modules import market_calendar
from modules.data_feed_xau import XAUDataFeed   # reuse _yf_download

logger = logging.getLogger(__name__)


WINDOW_SIZES: Dict[str, int] = {
    "1Min":  300,
    "5Min":  200,
    "15Min": 200,
    "1Hour": 200,
    "4Hour": 100,
    "1Day":  60,
}

_YF_PARAMS: Dict[str, Dict[str, str]] = {
    "1Min":  {"interval": "1m",  "period": "5d"},
    "5Min":  {"interval": "5m",  "period": "30d"},
    "15Min": {"interval": "15m", "period": "30d"},
    "1Hour": {"interval": "1h",  "period": "60d"},
    "4Hour": {"interval": "1h",  "period": "120d"},   # resampled from 1h
    "1Day":  {"interval": "1d",  "period": "1y"},
}


class NIFTYDataFeed:
    """yfinance polling for ^NSEI + macro helpers for the NIFTY pod."""

    def __init__(self, on_bar_callback: Optional[Callable[[pd.Series], None]] = None):
        self._symbol = NIFTY_YFINANCE_SYMBOL
        self._bars: Dict[str, Deque[dict]] = {
            tf: deque(maxlen=size) for tf, size in WINDOW_SIZES.items()
        }
        self._on_bar = on_bar_callback
        self._latest_price: float = 0.0
        self._running = True

        # Macro caches
        self._bn_cache: Optional[pd.DataFrame] = None
        self._bn_cache_ts: float = 0.0
        self._inr_cache: Optional[pd.DataFrame] = None
        self._inr_cache_ts: float = 0.0
        self._vix_cache: Optional[pd.DataFrame] = None
        self._vix_cache_ts: float = 0.0
        self._fii_dii_cache: Optional[dict] = None
        self._fii_dii_cache_ts: float = 0.0
        self._option_chain_cache: Optional[dict] = None
        self._option_chain_cache_ts: float = 0.0

        # NSE client lazily initialized
        self._nse_client = None

    # ── Public API ────────────────────────────────────────────────────────────

    def get_bars(self, timeframe: str = "1Hour") -> pd.DataFrame:
        rows = list(self._bars.get(timeframe, []))
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df.set_index("timestamp").sort_index()

    @property
    def latest_price(self) -> float:
        if self._latest_price == 0.0:
            for tf in ("1Min", "5Min", "1Hour"):
                bars = self._bars.get(tf)
                if bars:
                    self._latest_price = float(bars[-1]["close"])
                    break
        return self._latest_price

    def stop(self) -> None:
        self._running = False

    # ── Startup ───────────────────────────────────────────────────────────────

    def preload_history(self) -> None:
        for tf in ("1Day", "1Hour", "15Min", "5Min", "1Min"):
            self._refresh_timeframe(tf)
        self._resample_4hour()
        if self._bars["1Hour"]:
            self._latest_price = float(self._bars["1Hour"][-1]["close"])

    # ── Polling loop ──────────────────────────────────────────────────────────

    def start_polling(self) -> None:
        """Blocking. Call from a daemon thread. Sleeps when market is closed.
        Polls every 20s during open hours for near-real-time price."""
        POLL_SEC = 20
        HIGHER_TF_EVERY = 15  # 15 * 20s = 5min
        logger.info("NIFTY polling loop started (yfinance %s)", self._symbol)
        loop_count = 0
        while self._running:
            try:
                if not market_calendar.is_market_open():
                    delta = market_calendar.time_until_open()
                    sleep_for = min(int(delta.total_seconds()), 1800)  # cap 30 min
                    if sleep_for > 0:
                        logger.info("NIFTY market closed — sleeping %ds (next open in %s)",
                                    sleep_for, delta)
                        time.sleep(sleep_for)
                        continue

                self._refresh_timeframe("1Min")
                if self._bars["1Min"]:
                    newest = self._bars["1Min"][-1]
                    self._latest_price = float(newest["close"])
                    if self._on_bar:
                        self._on_bar(pd.Series(newest))

                if loop_count % HIGHER_TF_EVERY == 0:
                    for tf in ("5Min", "15Min", "1Hour", "1Day"):
                        self._refresh_timeframe(tf)
                    self._resample_4hour()

            except Exception as exc:
                logger.error("NIFTY polling error: %s", exc)

            loop_count += 1
            time.sleep(POLL_SEC)

    # ── yfinance fetch helpers (delegates to XAUDataFeed._yf_download) ─────────

    def _refresh_timeframe(self, tf: str) -> None:
        params = _YF_PARAMS.get(tf)
        if not params:
            return
        df = XAUDataFeed._yf_download(self._symbol, params["interval"], params["period"])
        if df.empty:
            return
        df = df.tail(WINDOW_SIZES[tf])
        bars = self._bars[tf]
        bars.clear()
        for ts, row in df.iterrows():
            bars.append({
                "timestamp": ts,
                "open":   float(row["open"]),
                "high":   float(row["high"]),
                "low":    float(row["low"]),
                "close":  float(row["close"]),
                "volume": float(row["volume"]),
            })

    def _resample_4hour(self) -> None:
        one_hour = self.get_bars("1Hour")
        if one_hour.empty:
            return
        agg = one_hour.resample("4h").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna().tail(WINDOW_SIZES["4Hour"])
        bars = self._bars["4Hour"]
        bars.clear()
        for ts, row in agg.iterrows():
            bars.append({
                "timestamp": ts,
                "open":   float(row["open"]),
                "high":   float(row["high"]),
                "low":    float(row["low"]),
                "close":  float(row["close"]),
                "volume": float(row["volume"]),
            })

    # ── Strategy helpers ─────────────────────────────────────────────────────

    def get_banknifty_1h(self) -> pd.DataFrame:
        """BANKNIFTY 1h bars (~30 sessions). Cached 5 minutes."""
        now = time.time()
        if self._bn_cache is not None and now - self._bn_cache_ts < 300:
            return self._bn_cache
        df = XAUDataFeed._yf_download(NIFTY_BN_YFINANCE_SYMBOL, "1h", "60d")
        self._bn_cache = df
        self._bn_cache_ts = now
        return df

    def get_usdinr_1d(self) -> pd.DataFrame:
        """USDINR daily bars (~60 sessions). Cached 1 hour."""
        now = time.time()
        if self._inr_cache is not None and now - self._inr_cache_ts < 3600:
            return self._inr_cache
        df = XAUDataFeed._yf_download(NIFTY_USDINR_SYMBOL, "1d", "90d")
        self._inr_cache = df
        self._inr_cache_ts = now
        return df

    def get_vix_1d(self) -> pd.DataFrame:
        """India VIX daily bars (~30 sessions). Cached 1 hour."""
        now = time.time()
        if self._vix_cache is not None and now - self._vix_cache_ts < 3600:
            return self._vix_cache
        df = XAUDataFeed._yf_download(NIFTY_VIX_SYMBOL, "1d", "60d")
        self._vix_cache = df
        self._vix_cache_ts = now
        return df

    def _ensure_nse_client(self):
        if self._nse_client is None:
            from modules.nse_client import get_default_client
            self._nse_client = get_default_client()
        return self._nse_client

    def get_fii_dii_summary(self) -> dict:
        """
        Return {
            "fii_cash_today": float (INR crores, net),
            "dii_cash_today": float,
            "fii_cash_5d_avg": float,
            "dii_cash_5d_avg": float,
            "report_date": "DD-MMM-YYYY",
            "available": bool,
        }
        Cached 1 hour. NSE returns 1-2 most recent days only via this endpoint;
        '5d_avg' is best-effort over what NSE serves us.
        """
        now = time.time()
        if self._fii_dii_cache is not None and now - self._fii_dii_cache_ts < 3600:
            return self._fii_dii_cache

        client = self._ensure_nse_client()
        rows = client.fii_dii_daily()
        result = {
            "fii_cash_today": 0.0, "dii_cash_today": 0.0,
            "fii_cash_5d_avg": 0.0, "dii_cash_5d_avg": 0.0,
            "report_date": "", "available": False,
        }
        if rows:
            fii_rows = [r for r in rows if str(r.get("category", "")).startswith("FII")]
            dii_rows = [r for r in rows if str(r.get("category", "")).startswith("DII")]

            def _net(r: dict) -> float:
                try:
                    return float(str(r.get("netValue", "0")).replace(",", ""))
                except Exception:
                    return 0.0

            if fii_rows:
                result["fii_cash_today"] = _net(fii_rows[0])
                result["fii_cash_5d_avg"] = (
                    sum(_net(r) for r in fii_rows) / len(fii_rows)
                )
                result["report_date"] = str(fii_rows[0].get("date", ""))
            if dii_rows:
                result["dii_cash_today"] = _net(dii_rows[0])
                result["dii_cash_5d_avg"] = (
                    sum(_net(r) for r in dii_rows) / len(dii_rows)
                )
            result["available"] = bool(fii_rows or dii_rows)

        self._fii_dii_cache = result
        self._fii_dii_cache_ts = now
        return result

    def get_option_chain(self) -> dict:
        """
        Return raw NSE option chain JSON for current monthly expiry, or {} if
        NSE blocks the endpoint (it frequently returns empty for non-browser
        clients — strategy code must handle this gracefully).
        Cached 5 minutes.
        """
        now = time.time()
        if self._option_chain_cache is not None and now - self._option_chain_cache_ts < 300:
            return self._option_chain_cache

        client = self._ensure_nse_client()
        chain = client.option_chain("NIFTY") or {}
        self._option_chain_cache = chain
        self._option_chain_cache_ts = now
        return chain

    def get_futures_oi_snapshot(self) -> dict:
        """
        Return aggregate NIFTY futures Open Interest from NSE F&O snapshot.
        Sums OI across all expiry months for the current-near-month NIFTY future.
        Cached 5 minutes inside the same option-chain blob (the chain
        derives from the same liveEquity-derivatives JSON), so we don't
        re-hit NSE.
        Output: {underlying, total_oi, total_volume, near_expiry, fetched_at}
        Empty dict if endpoint unavailable.
        """
        chain = self.get_option_chain()
        if not chain or "records" not in chain:
            return {}
        records = chain["records"]
        rows = records.get("data") or []
        if not rows:
            return {}
        # Aggregate OI across both CE/PE for nearest expiry — that's the most
        # actively-traded surface and what HFT desks watch
        nearest = (records.get("expiryDates") or [None])[0]
        total_oi = 0.0
        total_vol = 0.0
        for r in rows:
            if r.get("expiryDate") != nearest:
                continue
            ce = r.get("CE") or {}
            pe = r.get("PE") or {}
            total_oi += float(ce.get("openInterest", 0) or 0) + float(pe.get("openInterest", 0) or 0)
            total_vol += float(ce.get("totalTradedVolume", 0) or 0) + float(pe.get("totalTradedVolume", 0) or 0)
        return {
            "underlying":   "NIFTY",
            "near_expiry":  nearest,
            "spot":         float(records.get("underlyingValue") or 0),
            "total_oi":     total_oi,
            "total_volume": total_vol,
            "fetched_at":   self._option_chain_cache_ts,
        }
