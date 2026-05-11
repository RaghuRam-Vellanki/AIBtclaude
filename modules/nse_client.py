"""
modules/nse_client.py
Thin wrapper around www.nseindia.com JSON endpoints, using curl_cffi to
impersonate a real Chrome 124 TLS fingerprint. NSE blocks plain
`requests` (and even fully-cookied `requests` sessions) at the JA3 layer,
so curl_cffi is the only reliable way for an automated client to read FII/DII
and the F&O option chain.

The "official" /api/option-chain-indices endpoint returns literal '{}' for
non-browser clients regardless of fingerprint. We use the F&O all-contracts
endpoint /api/liveEquity-derivatives?index=nse50_opt instead and reshape it
into the standard {records: {data: [...] , underlyingValue, expiryDates}} shape.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from curl_cffi import requests as _cffi_requests
    _USE_CURL_CFFI = True
except Exception:                                # pragma: no cover
    import requests as _cffi_requests            # type: ignore
    _USE_CURL_CFFI = False

from config import NSE_CACHE_FILE

logger = logging.getLogger(__name__)

NSE_HOME              = "https://www.nseindia.com"
NSE_OPTION_CHAIN_PAGE = "https://www.nseindia.com/option-chain"
URL_FII_DII           = "https://www.nseindia.com/api/fiidiiTradeReact"
URL_OPTION_CHAIN_OFF  = "https://www.nseindia.com/api/option-chain-indices"
URL_LIVE_FNO          = "https://www.nseindia.com/api/liveEquity-derivatives"
URL_MARKET_STATUS     = "https://www.nseindia.com/api/marketStatus"

# Map our NIFTY-style symbol → NSE F&O index code
_FNO_INDEX_CODE = {
    "NIFTY":     "nse50_opt",
    "BANKNIFTY": "niftybank_opt",
    "FINNIFTY":  "finnifty_opt",
}

FII_DII_TTL_SEC      = 60 * 60          # 1 hour
OPTION_CHAIN_TTL_SEC = 60 * 5           # 5 minutes
MARKET_STATUS_TTL    = 60               # 1 minute
REQUEST_TIMEOUT      = 12


def _new_session():
    """Create either a curl_cffi session (Chrome impersonation) or a plain
    requests session as a fallback."""
    if _USE_CURL_CFFI:
        return _cffi_requests.Session(impersonate="chrome124")
    sess = _cffi_requests.Session()
    sess.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
    })
    return sess


def _parse_nse_date(d: str) -> Optional[datetime]:
    for fmt in ("%d-%b-%Y", "%d-%b-%Y %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(d, fmt)
        except Exception:
            continue
    return None


def _reshape_fno_to_chain(rows: List[Dict[str, Any]],
                          underlying_filter: str = "NIFTY") -> Dict[str, Any]:
    """Convert /api/liveEquity-derivatives rows → option-chain JSON shape."""
    if not rows:
        return {}

    # Filter to the requested underlying and Call/Put options only
    rows = [r for r in rows
            if (r.get("underlying") or "").upper() == underlying_filter.upper()
            and r.get("optionType") in ("Call", "Put")]
    if not rows:
        return {}

    underlying_value = float(rows[0].get("underlyingValue") or 0)

    # Dedup expiries, sort chronologically
    raw_expiries = sorted(
        {r.get("expiryDate") for r in rows if r.get("expiryDate")},
        key=lambda s: _parse_nse_date(s) or datetime.max,
    )

    # Group by (expiryDate, strike) → {CE, PE}
    grouped: Dict[tuple, Dict[str, Any]] = defaultdict(dict)
    for r in rows:
        try:
            strike = float(r["strikePrice"])
        except Exception:
            continue
        side = "CE" if r["optionType"] == "Call" else "PE"
        key = (r["expiryDate"], strike)
        grouped[key][side] = {
            "openInterest":         float(r.get("openInterest") or 0),
            # The endpoint doesn't expose dOI; consumers can compute deltas
            # across cached snapshots themselves. Default to 0 here.
            "changeinOpenInterest": float(r.get("changeInOpenInterest", 0) or 0),
            "totalTradedVolume":    float(r.get("volume") or 0),
            "lastPrice":            float(r.get("lastPrice") or 0),
            "change":               float(r.get("change") or 0),
            "pChange":               float(r.get("pChange") or 0),
        }

    chain_rows: List[Dict[str, Any]] = []
    for (exp, strike), pair in sorted(grouped.items(),
                                       key=lambda kv: (_parse_nse_date(kv[0][0]) or datetime.max, kv[0][1])):
        row = {"expiryDate": exp, "strikePrice": strike}
        if "CE" in pair:
            row["CE"] = pair["CE"]
        if "PE" in pair:
            row["PE"] = pair["PE"]
        chain_rows.append(row)

    return {
        "records": {
            "underlyingValue": underlying_value,
            "expiryDates":     raw_expiries,
            "data":            chain_rows,
            "timestamp":       datetime.utcnow().isoformat() + "Z",
        }
    }


class NSEClient:
    """Single-instance helper. Strategies should reuse one client per process."""

    def __init__(self, cache_file: Path = NSE_CACHE_FILE):
        self._sess = None
        self._bootstrapped = False
        self._lock = threading.RLock()
        self._cache_file = Path(cache_file)
        self._cache_file.parent.mkdir(parents=True, exist_ok=True)

    # ── public API ───────────────────────────────────────────────────────────

    def fii_dii_daily(self) -> List[Dict[str, Any]]:
        """Return list of FII/DII daily flow rows. Cached 1h."""
        payload = self._get_json(URL_FII_DII, ttl_sec=FII_DII_TTL_SEC)
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return []

    def market_status(self) -> Dict[str, Any]:
        payload = self._get_json(URL_MARKET_STATUS, ttl_sec=MARKET_STATUS_TTL)
        return payload or {}

    def option_chain(self, symbol: str = "NIFTY") -> Dict[str, Any]:
        """Return option-chain-shaped JSON.

        Tries the official /api/option-chain-indices first (often returns {}
        for automation), then falls back to /api/liveEquity-derivatives which
        contains *all* F&O contracts including index options. The latter is
        reshaped into the standard chain layout so callers can stay generic.
        Cached 5 min on disk.
        """
        # 1. Try the canonical endpoint
        official = self._get_json(
            f"{URL_OPTION_CHAIN_OFF}?symbol={symbol}",
            ttl_sec=OPTION_CHAIN_TTL_SEC,
        )
        if isinstance(official, dict) and official.get("records"):
            return official

        # 2. Fall back to F&O all-contracts endpoint
        fno_code = _FNO_INDEX_CODE.get(symbol.upper(), "nse50_opt")
        payload = self._get_json(
            f"{URL_LIVE_FNO}?index={fno_code}",
            ttl_sec=OPTION_CHAIN_TTL_SEC,
        )
        if not isinstance(payload, dict) or not payload.get("data"):
            return {}
        chain = _reshape_fno_to_chain(payload["data"], underlying_filter=symbol.upper())
        return chain

    # ── internals ────────────────────────────────────────────────────────────

    def _bootstrap(self) -> bool:
        """Warm cookies on the homepage and option-chain page."""
        with self._lock:
            if self._bootstrapped and self._sess is not None:
                return True
            try:
                sess = _new_session()
                sess.get(NSE_HOME, timeout=REQUEST_TIMEOUT)
                time.sleep(0.5)
                sess.get(NSE_OPTION_CHAIN_PAGE, timeout=REQUEST_TIMEOUT)
                time.sleep(0.4)
                self._sess = sess
                self._bootstrapped = True
                logger.info(
                    "NSEClient bootstrapped (curl_cffi=%s)",
                    _USE_CURL_CFFI,
                )
                return True
            except Exception as exc:
                logger.warning("NSEClient bootstrap failed: %s", exc)
                self._bootstrapped = False
                self._sess = None
                return False

    def _get_json(self, url: str, ttl_sec: int) -> Any:
        cached = self._cache_get(url, ttl_sec)
        if cached is not None:
            return cached

        if not self._bootstrap():
            return None

        try:
            resp = self._sess.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                logger.info("NSE %s → HTTP %d, re-bootstrapping",
                            url.rsplit("/", 1)[-1], resp.status_code)
                self._bootstrapped = False
                self._sess = None
                if not self._bootstrap():
                    return None
                resp = self._sess.get(url, timeout=REQUEST_TIMEOUT)

            # NSE sometimes returns 200 with literal '{}' or empty body
            if not resp.content or len(resp.content) < 5:
                return None
            try:
                data = resp.json()
            except Exception:
                return None
            self._cache_put(url, data, ttl_sec)
            return data
        except Exception as exc:
            logger.warning("NSE fetch %s failed: %s",
                           url.rsplit("/", 1)[-1], exc)
            return None

    # ── disk cache ───────────────────────────────────────────────────────────

    def _cache_load(self) -> Dict[str, Any]:
        try:
            if not self._cache_file.exists():
                return {}
            return json.loads(self._cache_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _cache_save(self, blob: Dict[str, Any]) -> None:
        try:
            self._cache_file.write_text(
                json.dumps(blob, default=str), encoding="utf-8"
            )
        except Exception as exc:
            logger.debug("NSE cache save failed: %s", exc)

    def _cache_get(self, key: str, ttl_sec: int) -> Any:
        with self._lock:
            blob = self._cache_load()
            entry = blob.get(key)
            if not entry:
                return None
            if time.time() - float(entry.get("fetched_at", 0)) > ttl_sec:
                return None
            return entry.get("payload")

    def _cache_put(self, key: str, payload: Any, ttl_sec: int) -> None:
        with self._lock:
            blob = self._cache_load()
            blob[key] = {"fetched_at": time.time(), "ttl": ttl_sec, "payload": payload}
            self._cache_save(blob)


# Module-level singleton — strategies + data feed share one client/process
_default_client: Optional[NSEClient] = None
_default_lock = threading.Lock()


def get_default_client() -> NSEClient:
    global _default_client
    with _default_lock:
        if _default_client is None:
            _default_client = NSEClient()
        return _default_client
