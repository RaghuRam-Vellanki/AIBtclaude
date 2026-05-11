"""
modules/event_calendar.py
High-impact macro-event blackout windows.

Reads `data/event_calendar.json` (refreshed manually weekly). Generators call
`is_blackout(asset, now_utc)` first; if True, no signal fires during the
window. Window applies BEFORE and AFTER the release.

Why: bot used to trade through FOMC/CPI/NFP/RBI blind — single largest source
of random-loss tail events. Blackouts kill that exposure deterministically.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Tuple

import pathlib

logger = logging.getLogger(__name__)

_BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
_CALENDAR_FILE = _BASE_DIR / "data" / "event_calendar.json"

_CACHE: List["Event"] = []
_CACHE_MTIME: float = 0.0


@dataclass
class Event:
    ts: datetime           # UTC
    name: str
    block_assets: tuple    # subset of ("xau", "btc", "nifty")
    window_min: int        # minutes on EACH side of ts
    impact: str            # "med" | "high" | "extreme"

    def covers(self, asset: str, now: datetime) -> bool:
        if asset.lower() not in self.block_assets:
            return False
        delta = (now - self.ts).total_seconds() / 60.0    # minutes from event
        return -self.window_min <= delta <= self.window_min


def _load() -> List[Event]:
    """Load and cache events. Re-reads file when mtime changes."""
    global _CACHE, _CACHE_MTIME
    try:
        mtime = _CALENDAR_FILE.stat().st_mtime
    except FileNotFoundError:
        _CACHE = []
        _CACHE_MTIME = 0.0
        return _CACHE
    if mtime == _CACHE_MTIME and _CACHE:
        return _CACHE
    try:
        payload = json.loads(_CALENDAR_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("event_calendar.json unreadable: %s", exc)
        return _CACHE
    out: List[Event] = []
    for raw in payload.get("events", []):
        try:
            ts = datetime.fromisoformat(raw["ts"].replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            out.append(Event(
                ts=ts,
                name=str(raw.get("name", "")),
                block_assets=tuple(a.lower() for a in raw.get("block_assets", [])),
                window_min=int(raw.get("window_min", 30)),
                impact=str(raw.get("impact", "high")),
            ))
        except Exception as exc:
            logger.debug("Skipping malformed event %r: %s", raw, exc)
    _CACHE = out
    _CACHE_MTIME = mtime
    return _CACHE


def is_blackout(asset: str, now: datetime | None = None) -> Tuple[bool, str]:
    """Return (True, reason) if `asset` is currently in any blackout window.

    `reason` is e.g. "FOMC -45min" (negative = before event) or "US CPI +12min".
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    asset = asset.lower()
    events = _load()
    # Pick the closest covering event for the reason string
    covering = [e for e in events if e.covers(asset, now)]
    if not covering:
        return False, ""
    best = min(covering, key=lambda e: abs((now - e.ts).total_seconds()))
    delta_min = int((now - best.ts).total_seconds() / 60)
    sign = "+" if delta_min >= 0 else "-"
    return True, f"{best.name} {sign}{abs(delta_min)}min"


def upcoming(asset: str, hours: int = 24, now: datetime | None = None) -> List[Event]:
    """Return events for `asset` whose ts is within next `hours` hours."""
    if now is None:
        now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=hours)
    asset = asset.lower()
    return [e for e in _load() if asset in e.block_assets and now <= e.ts <= horizon]
