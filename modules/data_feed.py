"""
data_feed.py
Alpaca WebSocket (1m bars) + REST (historical OHLCV) for BTC/USD.
Maintains rolling in-memory OHLCV windows per timeframe.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Callable, Deque, Dict, List, Optional
import ssl
from urllib.error import URLError
from urllib.request import Request, urlopen

import pandas as pd

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl.create_default_context()
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.live.crypto import CryptoDataStream
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, DEMO_MODE, SYMBOL

logger = logging.getLogger(__name__)

# ── Rolling window sizes ─────────────────────────────────────────────────────
WINDOW_SIZES: Dict[str, int] = {
    "1Min":  300,   # 5 hours of 1m bars
    "5Min":  200,
    "15Min": 200,
    "1Hour": 200,
    "4Hour": 100,
    "1Day":  60,
}

# Alpaca TimeFrame objects keyed by our string labels
_TF_MAP = {
    "1Min":  TimeFrame(1,  TimeFrameUnit.Minute),
    "5Min":  TimeFrame(5,  TimeFrameUnit.Minute),
    "15Min": TimeFrame(15, TimeFrameUnit.Minute),
    "1Hour": TimeFrame(1,  TimeFrameUnit.Hour),
    "4Hour": TimeFrame(4,  TimeFrameUnit.Hour),
    "1Day":  TimeFrame(1,  TimeFrameUnit.Day),
}

_PUBLIC_GRANULARITY = {
    "1Min": 60,
    "5Min": 300,
    "15Min": 900,
    "1Hour": 3600,
    "1Day": 86400,
}


class DataFeed:
    """
    Manages market data for BTC/USD:
      - Preloads historical bars via REST on startup
      - Streams 1m bars via WebSocket and updates in-memory windows
      - Provides get_bars(timeframe) → pd.DataFrame
    """

    def __init__(self, on_bar_callback: Optional[Callable[[pd.Series], None]] = None):
        self._demo_mode = DEMO_MODE
        self._hist_client = None
        self._stream = None
        if not self._demo_mode:
            self._hist_client = CryptoHistoricalDataClient(
                api_key=ALPACA_API_KEY,
                secret_key=ALPACA_SECRET_KEY,
            )
            self._stream = CryptoDataStream(
                api_key=ALPACA_API_KEY,
                secret_key=ALPACA_SECRET_KEY,
            )
        self._symbol = SYMBOL
        self._bars: Dict[str, Deque[dict]] = {
            tf: deque(maxlen=size) for tf, size in WINDOW_SIZES.items()
        }
        self._on_bar = on_bar_callback
        self._latest_price: float = 0.0

    # ── Public API ────────────────────────────────────────────────────────────

    def get_bars(self, timeframe: str = "1Min") -> pd.DataFrame:
        """Return stored bars as a DataFrame with columns: open, high, low, close, volume."""
        rows = list(self._bars.get(timeframe, []))
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.set_index("timestamp").sort_index()
        return df

    @property
    def latest_price(self) -> float:
        # If WebSocket hasn't delivered a bar yet, fall back to last historical close
        if self._latest_price == 0.0:
            bars = self._bars.get("1Min")
            if bars:
                self._latest_price = float(bars[-1]["close"])
        return self._latest_price

    # ── Startup ───────────────────────────────────────────────────────────────

    def preload_history(self) -> None:
        """Fetch historical bars for all timeframes before starting the stream."""
        if self._demo_mode:
            self._preload_public_history()
            return

        for tf_label, tf_obj in _TF_MAP.items():
            lookback_days = max(5, WINDOW_SIZES[tf_label] * _tf_minutes(tf_label) // 1440 + 2)
            start = datetime.now(timezone.utc) - timedelta(days=lookback_days)
            req = CryptoBarsRequest(
                symbol_or_symbols=self._symbol,
                timeframe=tf_obj,
                start=start,
            )
            try:
                bars = self._hist_client.get_crypto_bars(req)
                df = bars.df
                if df is None or df.empty:
                    logger.warning("No historical bars returned for %s %s", self._symbol, tf_label)
                    continue
                # Flatten multi-index if present
                if isinstance(df.index, pd.MultiIndex):
                    df = df.xs(self._symbol, level="symbol")
                df = df.tail(WINDOW_SIZES[tf_label])
                for ts, row in df.iterrows():
                    self._bars[tf_label].append({
                        "timestamp": ts,
                        "open":   float(row.get("open",  row.get("o", 0))),
                        "high":   float(row.get("high",  row.get("h", 0))),
                        "low":    float(row.get("low",   row.get("l", 0))),
                        "close":  float(row.get("close", row.get("c", 0))),
                        "volume": float(row.get("volume",row.get("v", 0))),
                    })
                logger.info("Preloaded %d bars for %s %s", len(df), self._symbol, tf_label)
            except Exception as exc:
                logger.error("Failed to preload %s %s: %s", self._symbol, tf_label, exc)

    # ── WebSocket stream ──────────────────────────────────────────────────────

    async def _handle_bar(self, bar) -> None:
        """Called by Alpaca SDK for each new 1m bar."""
        entry = {
            "timestamp": bar.timestamp,
            "open":   float(bar.open),
            "high":   float(bar.high),
            "low":    float(bar.low),
            "close":  float(bar.close),
            "volume": float(bar.volume),
        }
        self._bars["1Min"].append(entry)
        self._latest_price = float(bar.close)

        # Roll up higher TFs (simplified: just track latest close price for non-1m)
        self._latest_price = float(bar.close)

        if self._on_bar:
            self._on_bar(pd.Series(entry))

    def start_stream(self) -> None:
        """Subscribe to 1m bars and run the WebSocket stream (blocking)."""
        if self._demo_mode:
            logger.info("Demo mode enabled — polling public BTC/USD market data")
            self._run_demo_polling_loop()
            return

        self._stream.subscribe_bars(self._handle_bar, self._symbol)
        logger.info("WebSocket stream started for %s", self._symbol)
        self._stream.run()

    async def start_stream_async(self) -> None:
        """Async version — run inside an existing event loop."""
        if self._demo_mode:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._run_demo_polling_loop)
            return

        self._stream.subscribe_bars(self._handle_bar, self._symbol)
        await self._stream._run_forever()

    def _preload_public_history(self) -> None:
        for timeframe in ("1Min", "5Min", "15Min", "1Hour", "1Day"):
            df = self._fetch_public_bars(timeframe, WINDOW_SIZES[timeframe])
            self._store_frame(timeframe, df)

        one_hour = self.get_bars("1Hour")
        self._store_frame("4Hour", self._resample_frame(one_hour, "4h").tail(WINDOW_SIZES["4Hour"]))
        if self._bars["1Min"]:
            self._latest_price = float(self._bars["1Min"][-1]["close"])

    def _run_demo_polling_loop(self) -> None:
        """Polls Coinbase every POLL_SEC for fresh BTC ticker + 1m bars.
        Higher TFs refresh every HIGHER_TF_EVERY iterations to limit API hits.
        Also pulls a fresh /ticker on every iteration so latest_price reflects
        the live last-trade price even when the 1m candle hasn't closed yet."""
        POLL_SEC = 15            # was 60 — gives near-real-time feel on dashboard
        HIGHER_TF_EVERY = 20     # 20 * 15s = 5min between higher-TF refreshes
        refresh_counter = 0
        while True:
            try:
                # Live ticker (last trade price) — fresh every 15s
                tick = self._fetch_ticker_price()
                if tick is not None:
                    self._latest_price = tick

                latest = self._fetch_public_bars("1Min", WINDOW_SIZES["1Min"])
                self._store_frame("1Min", latest)
                if not latest.empty:
                    newest = latest.iloc[-1]
                    # Only override ticker if we couldn't fetch one
                    if tick is None:
                        self._latest_price = float(newest["close"])
                    if self._on_bar:
                        self._on_bar(pd.Series({
                            "timestamp": latest.index[-1],
                            "open": float(newest["open"]),
                            "high": float(newest["high"]),
                            "low": float(newest["low"]),
                            "close": float(newest["close"]),
                            "volume": float(newest["volume"]),
                        }))

                if refresh_counter % HIGHER_TF_EVERY == 0:
                    for timeframe in ("5Min", "15Min", "1Hour", "1Day"):
                        frame = self._fetch_public_bars(timeframe, WINDOW_SIZES[timeframe])
                        self._store_frame(timeframe, frame)
                    one_hour = self.get_bars("1Hour")
                    self._store_frame(
                        "4Hour",
                        self._resample_frame(one_hour, "4h").tail(WINDOW_SIZES["4Hour"]),
                    )
            except Exception as exc:
                logger.error("Demo market-data polling failed: %s", exc)

            refresh_counter += 1
            time.sleep(POLL_SEC)

    # ── Funding + OI (perp futures positioning) ───────────────────────────────

    def fetch_funding_rate(self) -> dict:
        """Latest BTCUSDT funding rate from Binance fapi (no auth needed).
        Returns {'lastFundingRate': float decimal/8h, 'markPrice': float, 'ts': int}.

        Cached for 60s to avoid hammering the API.
        """
        cache = getattr(self, "_funding_cache", None)
        now = time.time()
        if cache and now - cache.get("ts_local", 0) < 60.0:
            return cache
        try:
            url = "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT"
            req = Request(url, headers={"Accept": "application/json", "User-Agent": "AIBtclaude/1.0"})
            with urlopen(req, timeout=8, context=_SSL_CTX) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            out = {
                "lastFundingRate": float(payload.get("lastFundingRate", 0) or 0),
                "markPrice":       float(payload.get("markPrice", 0) or 0),
                "indexPrice":      float(payload.get("indexPrice", 0) or 0),
                "nextFundingTime": int(payload.get("nextFundingTime", 0) or 0),
                "ts_local":        now,
            }
            self._funding_cache = out
            return out
        except Exception as exc:
            logger.debug("Binance premiumIndex fetch failed: %s", exc)
            # Return neutral on failure rather than raise — strategy code should
            # gracefully treat 0 as "no signal".
            return {"lastFundingRate": 0.0, "markPrice": 0.0, "indexPrice": 0.0,
                    "nextFundingTime": 0, "ts_local": now}

    def fetch_oi_change(self) -> dict:
        """24h OI change for BTCUSDT perp from Binance futures-data.
        Returns {'oi_24h_pct': float, 'oi_now': float, 'ts': int}.

        Compares oldest vs latest of last 24 hourly OI snapshots.
        Cached for 5min.
        """
        cache = getattr(self, "_oi_cache", None)
        now = time.time()
        if cache and now - cache.get("ts_local", 0) < 300.0:
            return cache
        try:
            url = ("https://fapi.binance.com/futures/data/openInterestHist"
                   "?symbol=BTCUSDT&period=1h&limit=24")
            req = Request(url, headers={"Accept": "application/json", "User-Agent": "AIBtclaude/1.0"})
            with urlopen(req, timeout=8, context=_SSL_CTX) as resp:
                rows = json.loads(resp.read().decode("utf-8"))
            if isinstance(rows, list) and len(rows) >= 2:
                oi_first = float(rows[0].get("sumOpenInterest", 0) or 0)
                oi_last  = float(rows[-1].get("sumOpenInterest", 0) or 0)
                pct = (oi_last - oi_first) / oi_first if oi_first > 0 else 0.0
                out = {"oi_24h_pct": float(pct), "oi_now": oi_last, "ts_local": now}
            else:
                out = {"oi_24h_pct": 0.0, "oi_now": 0.0, "ts_local": now}
            self._oi_cache = out
            return out
        except Exception as exc:
            logger.debug("Binance OI fetch failed: %s", exc)
            return {"oi_24h_pct": 0.0, "oi_now": 0.0, "ts_local": now}

    def _fetch_ticker_price(self) -> Optional[float]:
        """Live BTC/USD spot price.
        Coinbase/Binance/Kraken are blocked from this network — use Coingecko
        first (proven reachable), fall back to CryptoCompare."""
        # Coingecko
        try:
            url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
            req = Request(url, headers={"Accept": "application/json", "User-Agent": "AIBtclaude/1.0"})
            with urlopen(req, timeout=8, context=_SSL_CTX) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            p = data.get("bitcoin", {}).get("usd")
            if p:
                return float(p)
        except Exception as exc:
            logger.debug("Coingecko ticker failed: %s", exc)
        # CryptoCompare fallback
        try:
            url = "https://min-api.cryptocompare.com/data/price?fsym=BTC&tsyms=USD"
            req = Request(url, headers={"Accept": "application/json", "User-Agent": "AIBtclaude/1.0"})
            with urlopen(req, timeout=8, context=_SSL_CTX) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            p = data.get("USD")
            if p:
                return float(p)
        except Exception as exc:
            logger.debug("CryptoCompare ticker failed: %s", exc)
        return None

    def _fetch_public_bars(self, timeframe: str, limit: int) -> pd.DataFrame:
        """OHLCV bars via yfinance (Coinbase/Binance blocked from this network).
        Falls back to CryptoCompare hist endpoints if yfinance fails."""
        try:
            import yfinance as yf
        except ImportError:
            return self._fetch_cryptocompare_bars(timeframe, limit)

        yf_params = {
            "1Min":  {"interval": "1m",  "period": "5d"},
            "5Min":  {"interval": "5m",  "period": "60d"},
            "15Min": {"interval": "15m", "period": "60d"},
            "1Hour": {"interval": "1h",  "period": "730d"},
            "1Day":  {"interval": "1d",  "period": "max"},
        }
        params = yf_params.get(timeframe)
        if not params:
            return self._fetch_cryptocompare_bars(timeframe, limit)
        try:
            df = yf.download(
                "BTC-USD",
                interval=params["interval"],
                period=params["period"],
                progress=False, auto_adjust=False, prepost=False,
            )
        except Exception as exc:
            logger.error("yfinance BTC fetch failed for %s: %s", timeframe, exc)
            return self._fetch_cryptocompare_bars(timeframe, limit)

        if df is None or df.empty:
            return self._fetch_cryptocompare_bars(timeframe, limit)
        if hasattr(df.columns, "get_level_values"):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                "Close": "close", "Volume": "volume"})
        cols = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
        return df[cols].tail(limit)

    def _fetch_cryptocompare_bars(self, timeframe: str, limit: int) -> pd.DataFrame:
        """Fallback BTC/USD OHLCV via CryptoCompare (proven reachable)."""
        cc_endpoints = {
            "1Min":  ("histominute", 1),
            "5Min":  ("histominute", 5),
            "15Min": ("histominute", 15),
            "1Hour": ("histohour", 1),
            "1Day":  ("histoday", 1),
        }
        ep = cc_endpoints.get(timeframe)
        if not ep:
            return pd.DataFrame()
        endpoint, agg = ep
        capped = min(limit, 2000)
        url = (
            f"https://min-api.cryptocompare.com/data/v2/{endpoint}"
            f"?fsym=BTC&tsym=USD&limit={capped}&aggregate={agg}"
        )
        req = Request(url, headers={"Accept": "application/json", "User-Agent": "AIBtclaude/1.0"})
        try:
            with urlopen(req, timeout=20, context=_SSL_CTX) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (URLError, ssl.SSLError, OSError) as exc:
            logger.error("CryptoCompare fetch failed for %s: %s", timeframe, exc)
            return pd.DataFrame()
        items = (payload or {}).get("Data", {}).get("Data", [])
        if not items:
            return pd.DataFrame()
        rows = []
        for r in items:
            try:
                rows.append({
                    "timestamp": datetime.fromtimestamp(int(r["time"]), tz=timezone.utc),
                    "open":   float(r["open"]),
                    "high":   float(r["high"]),
                    "low":    float(r["low"]),
                    "close":  float(r["close"]),
                    "volume": float(r.get("volumeto", 0) or 0),
                })
            except (KeyError, TypeError, ValueError):
                continue
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows).sort_values("timestamp").set_index("timestamp")
        return df.tail(limit)

    def _store_frame(self, timeframe: str, df: pd.DataFrame) -> None:
        bars = self._bars[timeframe]
        bars.clear()
        if df.empty:
            return
        for ts, row in df.iterrows():
            bars.append(
                {
                    "timestamp": ts,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                }
            )

    @staticmethod
    def _resample_frame(df: pd.DataFrame, rule: str) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame()
        resampled = df.resample(rule).agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )
        return resampled.dropna()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _tf_minutes(tf_label: str) -> int:
    mapping = {"1Min": 1, "5Min": 5, "15Min": 15, "1Hour": 60, "4Hour": 240, "1Day": 1440}
    return mapping.get(tf_label, 1)
