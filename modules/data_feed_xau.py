"""
data_feed_xau.py
yfinance-based polling feed for XAU/USD (Gold), plus macro inputs used by the
strategy pod: DXY, 10Y Treasury yield, and the CFTC weekly Commitment of Traders
report for COMEX gold futures.

No WebSocket — yfinance is REST-only. The agent runs preload_history() once,
then start_polling() in a daemon thread which refreshes 1m bars every 60s and
the higher timeframes every 5 minutes.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import time
import zipfile
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Deque, Dict, Optional
import ssl
from urllib.request import Request, urlopen

import pandas as pd

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl.create_default_context()

from config import XAU_COT_CACHE, XAU_YFINANCE_SYMBOL

logger = logging.getLogger(__name__)


WINDOW_SIZES: Dict[str, int] = {
    "1Min":  300,
    "5Min":  200,
    "15Min": 200,
    "1Hour": 200,
    "4Hour": 100,
    "1Day":  60,
}

# yfinance interval string + lookback period for each of our timeframes
_YF_PARAMS: Dict[str, Dict[str, str]] = {
    "1Min":  {"interval": "1m",  "period": "2d"},
    "5Min":  {"interval": "5m",  "period": "5d"},
    "15Min": {"interval": "15m", "period": "10d"},
    "1Hour": {"interval": "1h",  "period": "60d"},
    "4Hour": {"interval": "1h",  "period": "120d"},   # resampled from 1h
    "1Day":  {"interval": "1d",  "period": "2y"},
}


class XAUDataFeed:
    """yfinance polling feed for gold + macro inputs for the strategy pod."""

    def __init__(self, on_bar_callback: Optional[Callable[[pd.Series], None]] = None):
        self._symbol = XAU_YFINANCE_SYMBOL
        self._bars: Dict[str, Deque[dict]] = {
            tf: deque(maxlen=size) for tf, size in WINDOW_SIZES.items()
        }
        self._on_bar = on_bar_callback
        self._latest_price: float = 0.0
        self._running = True

        # Macro caches
        self._dxy_cache: Optional[pd.DataFrame] = None
        self._dxy_cache_ts: float = 0.0
        self._tnx_cache: Optional[pd.DataFrame] = None
        self._tnx_cache_ts: float = 0.0
        self._cot_cache: Optional[dict] = None
        self._cot_cache_ts: float = 0.0

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
            bars = self._bars.get("1Min")
            if bars:
                self._latest_price = float(bars[-1]["close"])
            else:
                bars_h = self._bars.get("1Hour")
                if bars_h:
                    self._latest_price = float(bars_h[-1]["close"])
        return self._latest_price

    def stop(self) -> None:
        self._running = False

    # ── Startup ───────────────────────────────────────────────────────────────

    def preload_history(self) -> None:
        """Pull initial bars for all timeframes."""
        for tf in ("1Day", "1Hour", "15Min", "5Min", "1Min"):
            self._refresh_timeframe(tf)
        # 4Hour is resampled from 1Hour
        self._resample_4hour()
        if self._bars["1Hour"]:
            self._latest_price = float(self._bars["1Hour"][-1]["close"])

    # ── Polling loop ──────────────────────────────────────────────────────────

    def start_polling(self) -> None:
        """Blocking loop. Run in a daemon thread.
        Polls every 20s for near-real-time price; higher TFs every 5min.
        Uses gold-api.com for live SPOT XAU/USD (yfinance GC=F is gold FUTURES,
        which trades at a contango premium of ~0.3% — caused price-display bug
        and skewed entry/SL/TP levels). OHLC bars stay on GC=F (close enough for
        technical indicators), but the displayed price + entry quote use spot."""
        POLL_SEC = 20
        HIGHER_TF_EVERY = 15  # 15 * 20s = 5min
        logger.info("XAU polling loop started (yfinance %s for OHLC + gold-api.com for spot)",
                    self._symbol)
        loop_count = 0
        while self._running:
            try:
                # Always refresh 1m bars (futures, used by all strategies)
                self._refresh_timeframe("1Min")
                if self._bars["1Min"]:
                    newest = self._bars["1Min"][-1]

                    # Override displayed/entry price with live SPOT XAU/USD
                    spot = self._fetch_spot_price()
                    self._latest_price = spot if spot else float(newest["close"])

                    if self._on_bar:
                        bar = dict(newest)
                        # Patch the close field with live spot so downstream
                        # callbacks (state writes, signal entry levels) use spot
                        if spot:
                            bar["close"] = spot
                        self._on_bar(pd.Series(bar))

                # Every HIGHER_TF_EVERY cycles (~5 min): refresh higher TFs
                if loop_count % HIGHER_TF_EVERY == 0:
                    for tf in ("5Min", "15Min", "1Hour", "1Day"):
                        self._refresh_timeframe(tf)
                    self._resample_4hour()

            except Exception as exc:
                logger.error("XAU polling error: %s", exc)

            loop_count += 1
            time.sleep(POLL_SEC)

    @staticmethod
    def _fetch_spot_price() -> Optional[float]:
        """Live SPOT XAU/USD price (not futures). gold-api.com is free and reachable."""
        try:
            req = Request(
                "https://api.gold-api.com/price/XAU",
                headers={"Accept": "application/json", "User-Agent": "XAUTradingAgent/1.0"},
            )
            with urlopen(req, timeout=8, context=_SSL_CTX) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            p = payload.get("price")
            if p is not None and float(p) > 0:
                return float(p)
        except Exception as exc:
            logger.debug("gold-api spot fetch failed: %s", exc)
        return None

    # ── yfinance fetch helpers ────────────────────────────────────────────────

    def _refresh_timeframe(self, tf: str) -> None:
        params = _YF_PARAMS.get(tf)
        if not params:
            return
        df = self._yf_download(self._symbol, params["interval"], params["period"])
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

    @staticmethod
    def _yf_download(symbol: str, interval: str, period: str) -> pd.DataFrame:
        """Wrapper around yfinance.download with stdout suppressed and column normalization."""
        try:
            import yfinance as yf
            df = yf.download(
                symbol, interval=interval, period=period,
                progress=False, auto_adjust=False, threads=False,
            )
        except Exception as exc:
            logger.warning("yfinance download failed for %s %s: %s", symbol, interval, exc)
            return pd.DataFrame()

        if df is None or df.empty:
            return pd.DataFrame()

        # Recent yfinance returns multi-index columns even for a single ticker
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]

        df = df.rename(columns={
            "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Adj Close": "adj_close", "Volume": "volume",
        })
        for col in ("open", "high", "low", "close", "volume"):
            if col not in df.columns:
                df[col] = 0.0
        df.index = pd.to_datetime(df.index, utc=True)
        return df[["open", "high", "low", "close", "volume"]].dropna(how="all")

    # ── Macro inputs for strategy pod ─────────────────────────────────────────

    def get_dxy_1h(self) -> pd.DataFrame:
        """DXY 1h bars (5 days). Cached for 5 minutes."""
        now = time.time()
        if self._dxy_cache is not None and now - self._dxy_cache_ts < 300:
            return self._dxy_cache
        df = self._yf_download("DX-Y.NYB", "1h", "10d")
        if df.empty:
            df = self._yf_download("DX=F", "1h", "10d")  # fallback to dollar-index futures
        self._dxy_cache = df
        self._dxy_cache_ts = now
        return df

    def get_tnx_1d(self) -> pd.DataFrame:
        """10Y Treasury yield (^TNX) daily bars, ~30 sessions. Cached 1h."""
        now = time.time()
        if self._tnx_cache is not None and now - self._tnx_cache_ts < 3600:
            return self._tnx_cache
        df = self._yf_download("^TNX", "1d", "60d")
        self._tnx_cache = df
        self._tnx_cache_ts = now
        return df

    def get_cot_gold_net(self) -> dict:
        """
        Parse the latest CFTC weekly Commitments of Traders report for COMEX gold
        futures (CFTC code 088691). Cached for 24 hours; falls back to disk cache.
        Returns:
            {"commercial_net": int, "noncommercial_net": int,
             "report_date": "YYYY-MM-DD", "noncommercial_net_4w_avg": int}
        """
        now = time.time()
        if self._cot_cache is not None and now - self._cot_cache_ts < 86_400:
            return self._cot_cache

        # Try disk cache first
        cache_path = Path(XAU_COT_CACHE)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        if cache_path.exists():
            try:
                disk = json.loads(cache_path.read_text(encoding="utf-8"))
                # Use disk cache if <7 days old
                report_date = datetime.fromisoformat(disk["report_date"])
                if (datetime.now(timezone.utc).date() - report_date.date()).days < 7:
                    self._cot_cache = disk
                    self._cot_cache_ts = now
                    return disk
            except Exception:
                pass

        cot = self._fetch_cot_remote()
        if cot:
            try:
                cache_path.write_text(json.dumps(cot, default=str), encoding="utf-8")
            except Exception:
                pass
            self._cot_cache = cot
            self._cot_cache_ts = now
            return cot

        # Last-resort empty dict
        return {"commercial_net": 0, "noncommercial_net": 0,
                "report_date": "", "noncommercial_net_4w_avg": 0}

    @staticmethod
    def _fetch_cot_remote() -> Optional[dict]:
        """Download and parse the CFTC weekly futures-only ZIP."""
        url = "https://www.cftc.gov/files/dea/history/deafut_xls_2025.zip"
        try:
            req = Request(url, headers={"User-Agent": "AIBtclaude/1.0"})
            with urlopen(req, timeout=30, context=_SSL_CTX) as resp:
                payload = resp.read()
        except Exception as exc:
            logger.warning("CFTC ZIP fetch failed: %s", exc)
            # Fallback to plain-text current report
            return XAUDataFeed._fetch_cot_plaintext()

        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                # Pick the first .xls (XLS97 format actually CSV-as-tab). Modern CFTC
                # ships .txt files inside the zip on some endpoints; handle both.
                names = zf.namelist()
                target = next((n for n in names if n.lower().endswith((".txt", ".csv", ".xls"))), names[0])
                with zf.open(target) as fh:
                    text = fh.read().decode("latin-1")
        except Exception as exc:
            logger.warning("CFTC ZIP parse failed: %s", exc)
            return XAUDataFeed._fetch_cot_plaintext()

        return XAUDataFeed._parse_cot_text(text)

    @staticmethod
    def _fetch_cot_plaintext() -> Optional[dict]:
        """Fallback: latest week plain-text futures-only report."""
        url = "https://www.cftc.gov/dea/newcot/deafut.txt"
        try:
            req = Request(url, headers={"User-Agent": "AIBtclaude/1.0"})
            with urlopen(req, timeout=20, context=_SSL_CTX) as resp:
                text = resp.read().decode("latin-1")
        except Exception as exc:
            logger.warning("CFTC plaintext fetch failed: %s", exc)
            return None
        return XAUDataFeed._parse_cot_text(text)

    @staticmethod
    def _parse_cot_text(text: str) -> Optional[dict]:
        """
        CFTC futures-only short reports list one row per market with
        commercial / non-commercial long/short positions.
        We look for COMEX gold (code 088691) and compute nets.

        The format is comma-separated with the "CFTC Contract Market Code"
        column near the end. We fall back to a name match on "GOLD" when
        the code column is absent.
        """
        try:
            reader = csv.reader(io.StringIO(text))
            header = next(reader, None)
            if not header:
                return None
            # Find indices we need
            def find_col(name_substring: str) -> Optional[int]:
                key = name_substring.lower()
                for i, h in enumerate(header):
                    if key in h.lower():
                        return i
                return None

            name_col = find_col("market and exchange names") or find_col("market_and_exchange") or 0
            date_col = find_col("as_of_date") or find_col("report_date") or find_col("as of date")
            code_col = find_col("contract_market_code") or find_col("cftc")
            noncomm_long_col = find_col("noncomm_positions_long_all") or find_col("noncomm long")
            noncomm_short_col = find_col("noncomm_positions_short_all") or find_col("noncomm short")
            comm_long_col = find_col("comm_positions_long_all") or find_col("comm long")
            comm_short_col = find_col("comm_positions_short_all") or find_col("comm short")

            target_rows = []
            for row in reader:
                if not row:
                    continue
                code_val = row[code_col] if code_col is not None and code_col < len(row) else ""
                name_val = row[name_col] if name_col < len(row) else ""
                is_gold = (code_val and code_val.strip().startswith("088691")) or \
                          ("GOLD" in name_val.upper() and "COMEX" in name_val.upper())
                if is_gold:
                    target_rows.append(row)

            if not target_rows:
                return None

            def safe_int(v: str) -> int:
                try:
                    return int(float(str(v).replace(",", "").strip() or "0"))
                except Exception:
                    return 0

            # The most recent row is what we want (CFTC files are chronological)
            latest = target_rows[-1]

            comm_long = safe_int(latest[comm_long_col])    if comm_long_col is not None else 0
            comm_short = safe_int(latest[comm_short_col])  if comm_short_col is not None else 0
            noncomm_long = safe_int(latest[noncomm_long_col]) if noncomm_long_col is not None else 0
            noncomm_short = safe_int(latest[noncomm_short_col]) if noncomm_short_col is not None else 0

            commercial_net = comm_long - comm_short
            noncommercial_net = noncomm_long - noncomm_short

            # 4-week average of non-commercial net (uses last 4 gold rows)
            recent4 = target_rows[-4:] if len(target_rows) >= 4 else target_rows
            running_avg = 0
            count = 0
            for row in recent4:
                nl = safe_int(row[noncomm_long_col]) if noncomm_long_col is not None else 0
                ns = safe_int(row[noncomm_short_col]) if noncomm_short_col is not None else 0
                running_avg += (nl - ns)
                count += 1
            noncomm_4w_avg = int(running_avg / count) if count else 0

            report_date = latest[date_col] if date_col is not None and date_col < len(latest) else ""

            return {
                "commercial_net": commercial_net,
                "noncommercial_net": noncommercial_net,
                "report_date": report_date,
                "noncommercial_net_4w_avg": noncomm_4w_avg,
            }
        except Exception as exc:
            logger.warning("COT parse error: %s", exc)
            return None
