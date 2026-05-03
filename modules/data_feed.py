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
from urllib.error import URLError
from urllib.request import Request, urlopen

import pandas as pd
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
        refresh_counter = 0
        while True:
            try:
                latest = self._fetch_public_bars("1Min", WINDOW_SIZES["1Min"])
                self._store_frame("1Min", latest)
                if not latest.empty:
                    newest = latest.iloc[-1]
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

                if refresh_counter % 5 == 0:
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
            time.sleep(60)

    def _fetch_public_bars(self, timeframe: str, limit: int) -> pd.DataFrame:
        granularity = _PUBLIC_GRANULARITY[timeframe]
        capped = min(limit, 300)
        url = (
            "https://api.exchange.coinbase.com/products/BTC-USD/candles"
            f"?granularity={granularity}"
        )
        req = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "AIBtclaude-demo/1.0",
            },
        )
        try:
            with urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except URLError as exc:
            logger.error("Public market-data request failed for %s: %s", timeframe, exc)
            return pd.DataFrame()

        if not isinstance(payload, list) or not payload:
            logger.warning("Empty public market-data payload for %s", timeframe)
            return pd.DataFrame()

        rows = []
        for candle in payload[:capped]:
            if len(candle) < 6:
                continue
            ts, low, high, open_, close, volume = candle[:6]
            rows.append(
                {
                    "timestamp": datetime.fromtimestamp(ts, tz=timezone.utc),
                    "open": float(open_),
                    "high": float(high),
                    "low": float(low),
                    "close": float(close),
                    "volume": float(volume),
                }
            )

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows).sort_values("timestamp")
        df = df.set_index("timestamp")
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
