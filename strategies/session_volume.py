"""
strategies/session_volume.py
JPMorgan-style session-adaptive volume signal for 24-hour markets (BTC, XAU).

Different sessions have different baseline volumes; institutions adjust
thresholds by session:
  - Asia (00:00-07:00 IST): low natural volume → 1.5× avg = signal
  - London (12:30-17:00 IST): high natural volume → 2.0× avg = signal
  - NY (18:30-22:00 IST): peak volume → 2.0× avg = signal

Vote direction = sign of close - open of the high-volume bar.
Confidence scales with volume multiple.

Skipped for NIFTY (a session-bound product where the FII/DII flow strategy
already captures session microstructure).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

import numpy as np

from strategies.base import StrategyAgent, StrategyVote
from config_strategies import (
    SESSION_ASIA_HOURS_IST,
    SESSION_LONDON_HOURS_IST,
    SESSION_NY_HOURS_IST,
    SESSION_VOL_LOOKBACK,
)

try:
    from zoneinfo import ZoneInfo
    _IST = ZoneInfo("Asia/Kolkata")
except Exception:                                # pragma: no cover
    _IST = timezone.utc


def _ist_hour() -> int:
    try:
        return datetime.now(tz=timezone.utc).astimezone(_IST).hour
    except Exception:
        return 12


def _session_label_and_threshold() -> tuple[str, float]:
    h = _ist_hour()
    if SESSION_ASIA_HOURS_IST[0] <= h < SESSION_ASIA_HOURS_IST[1]:
        return "asia", 1.5
    if SESSION_LONDON_HOURS_IST[0] <= h < SESSION_LONDON_HOURS_IST[1]:
        return "london", 2.0
    if SESSION_NY_HOURS_IST[0] <= h < SESSION_NY_HOURS_IST[1]:
        return "newyork", 2.0
    return "off-session", 2.5


class SessionVolumeStrategy(StrategyAgent):
    inspired_by = "JPMorgan volume desk (session-adaptive activity threshold)"
    archetype = "FLOW"

    def __init__(self, asset: str = "GENERIC"):
        self.asset = asset.upper()
        self.name = f"session_volume_{self.asset.lower()}"

    def vote(self, snapshot: Dict[str, Any], feed: Any) -> StrategyVote:
        try:
            bars = feed.get_bars("5Min") if feed is not None else None
            if bars is None or bars.empty or len(bars) < SESSION_VOL_LOOKBACK + 2:
                return self._neutral("Insufficient 5m bars")

            opens = bars["open"].astype(float).values
            closes = bars["close"].astype(float).values
            highs = bars["high"].astype(float).values
            lows = bars["low"].astype(float).values
            vols = bars.get("volume", None)
            if vols is None:
                return self._neutral("No volume column")
            vols = vols.astype(float).fillna(0).values

            ref_vols = vols[-(SESSION_VOL_LOOKBACK + 1):-1]
            avg_vol = float(np.mean(ref_vols)) if ref_vols.size else 0.0
            cur_vol = float(vols[-1])
            if avg_vol <= 0:
                return self._neutral("No usable volume baseline")

            session, threshold = _session_label_and_threshold()
            mult = cur_vol / avg_vol

            o, c, h, l = opens[-1], closes[-1], highs[-1], lows[-1]
            rng = h - l
            close_pos = (c - l) / rng if rng > 0 else 0.5
            bullish_close = c > o and close_pos > 0.6
            bearish_close = c < o and close_pos < 0.4

            meta = {
                "session": session,
                "vol_multiple": round(mult, 2),
                "threshold": threshold,
                "ist_hour": _ist_hour(),
                "close_pos_in_range": round(close_pos, 2),
                "bullish_close": bool(bullish_close),
                "bearish_close": bool(bearish_close),
            }

            if mult < threshold:
                # Whisper-grade: 0.7×threshold ≤ mult < threshold AND a
                # directional close still emits a small vote — sub-strong
                # but worth surfacing on quiet sessions.
                if mult >= 0.7 * threshold:
                    if bullish_close:
                        return StrategyVote(
                            name=self.name, inspired_by=self.inspired_by,
                            direction="LONG", confidence=0.25,
                            rationale=(
                                f"Whisper: {session} {mult:.1f}× vol (sub-{threshold}×) "
                                f"with bullish close at {close_pos:.0%}"
                            ),
                            metadata={**meta, "whisper": True},
                        )
                    if bearish_close:
                        return StrategyVote(
                            name=self.name, inspired_by=self.inspired_by,
                            direction="SHORT", confidence=0.25,
                            rationale=(
                                f"Whisper: {session} {mult:.1f}× vol (sub-{threshold}×) "
                                f"with bearish close at {close_pos:.0%}"
                            ),
                            metadata={**meta, "whisper": True},
                        )
                return self._neutral(
                    f"Volume {mult:.1f}× < {threshold}× ({session} session) — no signal",
                    **meta,
                )

            confidence = float(min(0.9, 0.4 + (mult - threshold) * 0.3))
            if bullish_close:
                return StrategyVote(
                    name=self.name, inspired_by=self.inspired_by,
                    direction="LONG", confidence=confidence,
                    rationale=(
                        f"{session.title()} session: {mult:.1f}× vol surge with "
                        f"bullish close at {close_pos:.0%} of range"
                    ),
                    metadata=meta,
                )
            if bearish_close:
                return StrategyVote(
                    name=self.name, inspired_by=self.inspired_by,
                    direction="SHORT", confidence=confidence,
                    rationale=(
                        f"{session.title()} session: {mult:.1f}× vol surge with "
                        f"bearish close at {close_pos:.0%} of range"
                    ),
                    metadata=meta,
                )

            return self._neutral(
                f"Volume {mult:.1f}× ({session}) but close not directional ({close_pos:.0%})",
                **meta,
            )

        except Exception as exc:
            return self._neutral(f"Strategy error: {exc}")
