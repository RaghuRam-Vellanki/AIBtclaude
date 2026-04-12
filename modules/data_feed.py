"""
data_feed.py
Alpaca WebSocket (1m bars) + REST (historical OHLCV) for BTC/USD.
Maintains rolling in-memory OHLCV windows per timeframe.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Callable, Deque, Dict, List, Optional

import pandas as pd
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.live.crypto import CryptoDataStream
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, SYMBOL

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


class DataFeed:
    """
    Manages market data for BTC/USD:
      - Preloads historical bars via REST on startup
      - Streams 1m bars via WebSocket and updates in-memory windows
      - Provides get_bars(timeframe) → pd.DataFrame
    """

    def __init__(self, on_bar_callback: Optional[Callable[[pd.Series], None]] = None):
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
        self._stream.subscribe_bars(self._handle_bar, self._symbol)
        logger.info("WebSocket stream started for %s", self._symbol)
        self._stream.run()

    async def start_stream_async(self) -> None:
        """Async version — run inside an existing event loop."""
        self._stream.subscribe_bars(self._handle_bar, self._symbol)
        await self._stream._run_forever()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _tf_minutes(tf_label: str) -> int:
    mapping = {"1Min": 1, "5Min": 5, "15Min": 15, "1Hour": 60, "4Hour": 240, "1Day": 1440}
    return mapping.get(tf_label, 1)
