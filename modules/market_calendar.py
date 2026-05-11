"""
modules/market_calendar.py
NSE (Indian) market hours + holiday helpers used by agent_nifty.py and the
dashboard. NIFTY trades 9:15-15:30 IST Mon-Fri, closed on NSE-published
holidays. The agent's polling thread sleeps until next open instead of
hammering yfinance during off-hours.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:                       # pragma: no cover — Python <3.9
    from backports.zoneinfo import ZoneInfo   # type: ignore

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc

MARKET_OPEN_IST  = time(9, 15)
MARKET_CLOSE_IST = time(15, 30)

# NSE annual trading holidays. One manual update per FY is acceptable.
# Source: https://www.nseindia.com/resources/exchange-communication-holidays
NSE_HOLIDAYS_FY26: set[date] = {
    date(2026, 1, 26),   # Republic Day
    date(2026, 3, 14),   # Holi
    date(2026, 3, 31),   # Eid-ul-Fitr (tentative)
    date(2026, 4, 14),   # Dr. Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 8, 15),   # Independence Day (Saturday in 2026 — NSE may keep open)
    date(2026, 10, 2),   # Gandhi Jayanti
    date(2026, 11, 7),   # Diwali Laxmi Pujan / Muhurat trading day
    date(2026, 12, 25),  # Christmas
}


def _to_ist(now: datetime | None) -> datetime:
    if now is None:
        now = datetime.now(tz=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(IST)


def is_nse_holiday(d: date) -> bool:
    return d in NSE_HOLIDAYS_FY26


def is_market_open(now: datetime | None = None) -> bool:
    ist = _to_ist(now)
    if ist.weekday() >= 5:                    # Saturday/Sunday
        return False
    if is_nse_holiday(ist.date()):
        return False
    return MARKET_OPEN_IST <= ist.time() < MARKET_CLOSE_IST


def _next_session_open(ist_now: datetime) -> datetime:
    candidate = ist_now.replace(
        hour=MARKET_OPEN_IST.hour, minute=MARKET_OPEN_IST.minute,
        second=0, microsecond=0,
    )
    if ist_now.time() >= MARKET_OPEN_IST:
        candidate = candidate + timedelta(days=1)
    while candidate.weekday() >= 5 or is_nse_holiday(candidate.date()):
        candidate = candidate + timedelta(days=1)
    return candidate


def time_until_open(now: datetime | None = None) -> timedelta:
    ist = _to_ist(now)
    if is_market_open(ist):
        return timedelta(0)
    return _next_session_open(ist) - ist


def time_until_close(now: datetime | None = None) -> timedelta:
    ist = _to_ist(now)
    if not is_market_open(ist):
        return timedelta(0)
    close_dt = ist.replace(
        hour=MARKET_CLOSE_IST.hour, minute=MARKET_CLOSE_IST.minute,
        second=0, microsecond=0,
    )
    return close_dt - ist


def market_status_dict(now: datetime | None = None) -> dict:
    ist = _to_ist(now)
    open_now = is_market_open(ist)
    if open_now:
        delta = time_until_close(ist)
        label = f"open · closes in {_format_delta(delta)}"
    else:
        delta = time_until_open(ist)
        label = f"closed · opens in {_format_delta(delta)}"
    return {
        "is_open": open_now,
        "ist_time": ist.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "label": label,
        "seconds_until_change": int(delta.total_seconds()),
    }


def _format_delta(td: timedelta) -> str:
    total_min = int(td.total_seconds() // 60)
    if total_min < 60:
        return f"{total_min}m"
    hours, mins = divmod(total_min, 60)
    if hours < 24:
        return f"{hours}h{mins:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"
