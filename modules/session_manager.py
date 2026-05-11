"""
session_manager.py
Tracks the current trading session (Asia / London / NY / overlap)
and fires callbacks at session opens.
All times handled in IST (UTC+5:30).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# IST offset from UTC
_IST = timezone(timedelta(hours=5, minutes=30))

# Session windows as (start_hour, start_min, end_hour, end_min) in IST
_SESSIONS: Dict[str, Tuple[int, int, int, int]] = {
    "asia":         (0,  0,  8, 30),
    "pre_london":   (10, 30, 12, 30),
    "london":       (12, 30, 16, 30),
    "pre_ny":       (16, 30, 18, 30),
    "newyork":      (18, 30, 22, 30),
    "late_us":      (22, 30, 23, 59),
}

# Funding rate settlement times in IST (every 8 hours)
_FUNDING_SETTLEMENTS_IST: List[Tuple[int, int]] = [
    (1, 30), (9, 30), (17, 30),
]

# Sessions that trigger a full analysis
ANALYSIS_SESSIONS = ("asia", "london", "newyork")


class SessionManager:
    """
    Tracks current session and notifies when session opens occur.
    """

    def __init__(self, on_session_open: Optional[Callable[[str], None]] = None):
        self._on_session_open = on_session_open
        self._last_session: Optional[str] = None

    def current_session(self, utc_now: Optional[datetime] = None) -> str:
        """Return the name of the current session based on IST time."""
        now_ist = _to_ist(utc_now or datetime.now(timezone.utc))
        h, m = now_ist.hour, now_ist.minute
        for name, (sh, sm, eh, em) in _SESSIONS.items():
            start_mins = sh * 60 + sm
            end_mins   = eh * 60 + em
            current_mins = h * 60 + m
            if start_mins <= current_mins < end_mins:
                return name
        return "late_us"

    def tick(self, utc_now: Optional[datetime] = None) -> None:
        """
        Call this every minute. Fires on_session_open callback when session changes.
        On first tick (last_session is None) we silently adopt the current session
        so the agent's own startup analysis doesn't get duplicated.
        """
        session = self.current_session(utc_now)
        if session != self._last_session:
            is_first_tick = self._last_session is None
            logger.info("Session changed: %s -> %s", self._last_session, session)
            self._last_session = session
            if self._on_session_open and session in ANALYSIS_SESSIONS and not is_first_tick:
                self._on_session_open(session)

    def minutes_until_next_session_open(self, utc_now: Optional[datetime] = None) -> int:
        """How many minutes until the next major session open (Asia/London/NY)."""
        now_ist = _to_ist(utc_now or datetime.now(timezone.utc))
        current_mins = now_ist.hour * 60 + now_ist.minute
        opens_in_mins = []
        for name in ANALYSIS_SESSIONS:
            sh, sm, _, _ = _SESSIONS[name]
            target_mins = sh * 60 + sm
            diff = (target_mins - current_mins) % (24 * 60)
            opens_in_mins.append(diff)
        return min(opens_in_mins)

    def is_funding_settlement_window(self, utc_now: Optional[datetime] = None,
                                      window_minutes: int = 15) -> bool:
        """Return True if we are within window_minutes of a funding settlement."""
        now_ist = _to_ist(utc_now or datetime.now(timezone.utc))
        current_mins = now_ist.hour * 60 + now_ist.minute
        for fh, fm in _FUNDING_SETTLEMENTS_IST:
            settle_mins = fh * 60 + fm
            if abs(current_mins - settle_mins) <= window_minutes:
                return True
        return False

    def next_funding_settlement_ist(self, utc_now: Optional[datetime] = None) -> str:
        """Return a human-readable string of the next funding settlement time in IST."""
        now_ist = _to_ist(utc_now or datetime.now(timezone.utc))
        current_mins = now_ist.hour * 60 + now_ist.minute
        best: Optional[Tuple[int, int]] = None
        best_diff = 9999
        for fh, fm in _FUNDING_SETTLEMENTS_IST:
            diff = (fh * 60 + fm - current_mins) % (24 * 60)
            if diff < best_diff:
                best_diff = diff
                best = (fh, fm)
        if best:
            return f"{best[0]:02d}:{best[1]:02d} IST (in {best_diff} min)"
        return "unknown"

    @staticmethod
    def ist_now() -> datetime:
        return _to_ist(datetime.now(timezone.utc))


def _to_ist(utc_dt: datetime) -> datetime:
    return utc_dt.astimezone(_IST)
