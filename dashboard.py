"""
dashboard.py — BTC/USD + XAU/USD + NIFTY 50 Trading Agent Dashboard
Features:
  - Asset switch (BTC/USD ↔ XAU/USD ↔ NIFTY 50) — same layout, separate state files
  - TradingView live chart, switches symbol with the dropdown
  - Start / Stop the bot for the selected asset (separate processes & PIDs)
  - Trade approval flow (per asset)
  - Pod Report card (XAU + NIFTY) showing the 5 strategy votes
  - FII/DII chip strip + market-closed banner (NIFTY only)
  - Live logs from the selected asset's agent log

Open: http://localhost:8080
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

from config import (
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    BTC_POD_REPORT_FILE,
    DEMO_MODE,
    GROQ_API_KEY,
    NIFTY_AGENT_LOG,
    NIFTY_ANALYZE_TRIGGER,
    NIFTY_BN_YFINANCE_SYMBOL,
    NIFTY_BOT_PID_FILE,
    NIFTY_PENDING_FILE,
    NIFTY_POD_REPORT_FILE,
    NIFTY_STATE_FILE,
    NIFTY_TRADES_LOG,
    NIFTY_YFINANCE_SYMBOL,
    XAU_AGENT_LOG,
    XAU_BOT_PID_FILE,
    XAU_PENDING_FILE,
    XAU_POD_REPORT_FILE,
    XAU_STATE_FILE,
    XAU_TRADES_LOG,
)
from modules import market_calendar
from modules.news_feed import get_headlines
from modules.nse_client import get_default_client

load_dotenv()

app = FastAPI(title="Multi-Asset Trading Dashboard")

BASE      = Path(__file__).parent
LOGS_DIR  = BASE / "logs"

# BTC paths (legacy, unchanged from v1)
BTC_STATE_F   = LOGS_DIR / "state.json"
BTC_TRADES_F  = LOGS_DIR / "trades_log.json"
BTC_LOG_F     = LOGS_DIR / "agent.log"
BTC_PENDING_F = LOGS_DIR / "pending_signal.json"
BTC_PID_F     = LOGS_DIR / "bot_pid.txt"
BTC_TRIGGER_F = LOGS_DIR / "analyze_now.json"
BTC_POD_F     = Path(BTC_POD_REPORT_FILE)
BTC_AGENT_PY  = BASE / "agent.py"

# XAU paths (read from config so they stay in sync)
XAU_STATE_F   = Path(XAU_STATE_FILE)
XAU_TRADES_F  = Path(XAU_TRADES_LOG)
XAU_LOG_F     = Path(XAU_AGENT_LOG)
XAU_PENDING_F = Path(XAU_PENDING_FILE)
XAU_PID_F     = Path(XAU_BOT_PID_FILE)
XAU_POD_F     = Path(XAU_POD_REPORT_FILE)
XAU_TRIGGER_F = LOGS_DIR / "xau_analyze_now.json"
XAU_AGENT_PY  = BASE / "agent_xau.py"

# NIFTY paths
NIFTY_STATE_F   = Path(NIFTY_STATE_FILE)
NIFTY_TRADES_F  = Path(NIFTY_TRADES_LOG)
NIFTY_LOG_F     = Path(NIFTY_AGENT_LOG)
NIFTY_PENDING_F = Path(NIFTY_PENDING_FILE)
NIFTY_PID_F     = Path(NIFTY_BOT_PID_FILE)
NIFTY_POD_F     = Path(NIFTY_POD_REPORT_FILE)
NIFTY_TRIGGER_F = Path(NIFTY_ANALYZE_TRIGGER)
NIFTY_AGENT_PY  = BASE / "agent_nifty.py"


def _paths_for(asset: str) -> dict:
    """Resolve all per-asset paths from a single ?asset= query param."""
    a = (asset or "").lower()
    if a == "xau":
        return {
            "asset":   "xau",
            "state":   XAU_STATE_F,
            "trades":  XAU_TRADES_F,
            "log":     XAU_LOG_F,
            "pending": XAU_PENDING_F,
            "pid":     XAU_PID_F,
            "trigger": XAU_TRIGGER_F,
            "agent":   XAU_AGENT_PY,
            "pod":     XAU_POD_F,
        }
    if a == "nifty":
        return {
            "asset":   "nifty",
            "state":   NIFTY_STATE_F,
            "trades":  NIFTY_TRADES_F,
            "log":     NIFTY_LOG_F,
            "pending": NIFTY_PENDING_F,
            "pid":     NIFTY_PID_F,
            "trigger": NIFTY_TRIGGER_F,
            "agent":   NIFTY_AGENT_PY,
            "pod":     NIFTY_POD_F,
        }
    return {
        "asset":   "btc",
        "state":   BTC_STATE_F,
        "trades":  BTC_TRADES_F,
        "log":     BTC_LOG_F,
        "pending": BTC_PENDING_F,
        "pid":     BTC_PID_F,
        "trigger": BTC_TRIGGER_F,
        "agent":   BTC_AGENT_PY,
        "pod":     BTC_POD_F,
    }


def _asset_label(asset: str) -> str:
    a = (asset or "").lower()
    if a == "xau":   return "XAU/USD"
    if a == "nifty": return "NIFTY 50"
    return "BTC/USD"


def _sanitize_floats(obj):
    """Recursively replace NaN/Inf floats with None so JSONResponse can serialize.
    FastAPI uses strict JSON; NaN slips through json.loads but breaks json.dumps."""
    import math
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_floats(v) for v in obj]
    return obj


def _missing_bot_credentials(asset: str) -> list[str]:
    """All three assets can run signal-only without broker creds.
    BTC: DEMO_MODE (Coinbase public polling).
    XAU + NIFTY: paper-sim with Groq fallback to deterministic aggregator."""
    return []


def _read_pid(pid_path: Path) -> int | None:
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text().strip())
    except Exception:
        pid_path.unlink(missing_ok=True)
        return None


def _is_pid_alive(pid: int) -> bool:
    """Cross-platform PID liveness check. Windows os.kill() can raise
    SystemError when PID doesn't exist (vs OSError on POSIX) — catch both."""
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, SystemError, PermissionError):
        return False
    except Exception:
        return False


def _live_pid(pid_path: Path) -> int | None:
    pid = _read_pid(pid_path)
    if pid is None:
        return None
    if _is_pid_alive(pid):
        return pid
    pid_path.unlink(missing_ok=True)
    return None


# ── Bot process control ───────────────────────────────────────────────────────

@app.post("/api/bot/start")
def bot_start(asset: str = Query(default="btc")):
    paths = _paths_for(asset)
    missing = _missing_bot_credentials(paths["asset"])
    if missing:
        return JSONResponse(
            {"status": "config_error",
             "detail": "Missing required environment variables",
             "missing": missing},
            status_code=400,
        )

    pid = _live_pid(paths["pid"])
    if pid is not None:
        return JSONResponse({"status": "already_running", "pid": pid})

    paths["log"].parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(paths["log"], "a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(paths["agent"])],
        stdout=log_handle,
        stderr=log_handle,
        cwd=str(BASE),
    )
    paths["pid"].write_text(str(proc.pid))
    return JSONResponse({"status": "started", "pid": proc.pid, "asset": paths["asset"]})


@app.post("/api/bot/stop")
def bot_stop(asset: str = Query(default="btc")):
    paths = _paths_for(asset)
    pid = _live_pid(paths["pid"])
    if pid is None:
        return JSONResponse({"status": "not_running"})
    try:
        if sys.platform == "win32":
            subprocess.call(["taskkill", "/F", "/PID", str(pid)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.kill(pid, signal.SIGTERM)
        paths["pid"].unlink(missing_ok=True)
        return JSONResponse({"status": "stopped", "pid": pid, "asset": paths["asset"]})
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)})


@app.post("/api/bot/analyze")
def bot_analyze(asset: str = Query(default="btc")):
    paths = _paths_for(asset)
    pid = _live_pid(paths["pid"])
    if pid is None:
        return JSONResponse({"status": "not_running"}, status_code=409)
    paths["trigger"].parent.mkdir(parents=True, exist_ok=True)
    paths["trigger"].write_text('{"trigger": true}', encoding="utf-8")
    return JSONResponse({"status": "triggered", "pid": pid, "asset": paths["asset"]})


# ── Signal approval ───────────────────────────────────────────────────────────

@app.post("/api/signal/approve")
def approve(asset: str = Query(default="btc")):
    paths = _paths_for(asset)
    if not paths["pending"].exists():
        return JSONResponse({"status": "no_pending"})
    data = json.loads(paths["pending"].read_text(encoding="utf-8"))
    data["status"] = "approved"
    paths["pending"].write_text(json.dumps(data, indent=2), encoding="utf-8")
    return JSONResponse({"status": "approved"})


@app.post("/api/signal/skip")
def skip(asset: str = Query(default="btc")):
    paths = _paths_for(asset)
    if not paths["pending"].exists():
        return JSONResponse({"status": "no_pending"})
    data = json.loads(paths["pending"].read_text(encoding="utf-8"))
    data["status"] = "skipped"
    paths["pending"].write_text(json.dumps(data, indent=2), encoding="utf-8")
    return JSONResponse({"status": "skipped"})


@app.get("/api/pending")
def get_pending(asset: str = Query(default="btc")):
    paths = _paths_for(asset)
    if not paths["pending"].exists():
        return JSONResponse({"status": "none"})
    try:
        return JSONResponse(json.loads(paths["pending"].read_text(encoding="utf-8")))
    except Exception:
        return JSONResponse({"status": "none"})


# ── Data endpoints ────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return HTMLResponse(HTML)


@app.get("/api/state")
def get_state(asset: str = Query(default="btc")):
    paths = _paths_for(asset)
    label = _asset_label(paths["asset"])
    if paths["state"].exists():
        try:
            state = json.loads(paths["state"].read_text(encoding="utf-8"))
            if _live_pid(paths["pid"]) is None:
                state["bot_status"] = "offline"
            state.setdefault("asset", label)
            return JSONResponse(_sanitize_floats(state))
        except Exception:
            pass
    return JSONResponse({
        "asset": label,
        "bot_status": "offline", "latest_price": 0, "session": "",
    })


@app.get("/api/market_status")
def get_market_status(asset: str = Query(default="nifty")):
    """NIFTY market hours status. BTC/XAU run 24×5/24×7 — return {is_open: True}."""
    if (asset or "").lower() != "nifty":
        return JSONResponse({"is_open": True, "label": "always-on"})
    return JSONResponse(market_calendar.market_status_dict())


@app.get("/api/trades")
def get_trades(asset: str = Query(default="btc")):
    paths = _paths_for(asset)
    if paths["trades"].exists():
        try:
            return JSONResponse(json.loads(paths["trades"].read_text(encoding="utf-8")))
        except Exception:
            pass
    return JSONResponse([])


@app.get("/api/logs")
def get_logs(asset: str = Query(default="btc"),
             lines: int = Query(default=80, le=300)):
    paths = _paths_for(asset)
    if not paths["log"].exists():
        return JSONResponse({"lines": []})
    try:
        with open(paths["log"], encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        return JSONResponse({"lines": [l.rstrip() for l in all_lines[-lines:]]})
    except Exception as e:
        return JSONResponse({"lines": [str(e)]})


@app.get("/api/pod_report")
def get_pod_report(asset: str = Query(default="xau")):
    """Per-asset pod-vote breakdown (XAU/NIFTY/BTC)."""
    paths = _paths_for(asset)
    pod_path = paths.get("pod")
    if pod_path is None:
        return JSONResponse({"votes": [], "pod_sum": 0, "generated_at": None,
                             "error": f"no pod path for asset={asset}"})
    if not pod_path.exists():
        return JSONResponse({"votes": [], "pod_sum": 0, "generated_at": None,
                             "error": f"file not found: {pod_path.name}"})
    try:
        data = json.loads(pod_path.read_text(encoding="utf-8"))
        return JSONResponse(_sanitize_floats(data))
    except Exception as exc:
        return JSONResponse({"votes": [], "pod_sum": 0, "generated_at": None,
                             "error": f"{type(exc).__name__}: {exc}"})


# ── News, OI, chart-data endpoints ────────────────────────────────────────────

@app.get("/api/news")
def get_news(asset: str = Query(default="nifty"),
             limit: int = Query(default=12, le=30)):
    """RSS-aggregated headlines, cached 5 min on disk."""
    try:
        return JSONResponse(get_headlines(asset, limit=limit, ttl_sec=300))
    except Exception as exc:
        return JSONResponse({"items": [], "error": str(exc), "fetched_at": None})


def _option_chain_analytics(payload: dict) -> dict:
    """Compute PCR, max-pain and top-OI strike rows from raw NSE option chain JSON."""
    if not payload or "records" not in payload:
        return {}
    records = payload.get("records") or {}
    rows = records.get("data") or []
    spot = float(records.get("underlyingValue") or 0)
    expiries = records.get("expiryDates") or []
    nearest_expiry = expiries[0] if expiries else None

    # Filter to nearest expiry only
    if nearest_expiry:
        rows = [r for r in rows if r.get("expiryDate") == nearest_expiry]

    total_call_oi = 0
    total_put_oi  = 0
    total_call_chg = 0
    total_put_chg  = 0
    strikes: list[dict] = []
    for r in rows:
        ce = r.get("CE") or {}
        pe = r.get("PE") or {}
        strike = float(r.get("strikePrice") or 0)
        c_oi  = float(ce.get("openInterest")  or 0)
        c_chg = float(ce.get("changeinOpenInterest") or 0)
        p_oi  = float(pe.get("openInterest")  or 0)
        p_chg = float(pe.get("changeinOpenInterest") or 0)
        total_call_oi  += c_oi
        total_put_oi   += p_oi
        total_call_chg += c_chg
        total_put_chg  += p_chg
        strikes.append({
            "strike":   strike,
            "call_oi":  c_oi,
            "call_chg": c_chg,
            "put_oi":   p_oi,
            "put_chg":  p_chg,
        })

    # Max-pain: strike with min total option-writer payoff
    max_pain_strike = 0
    if strikes:
        best_pain = float("inf")
        for s in strikes:
            K = s["strike"]
            pain = sum(
                max(0.0, K - x["strike"]) * x["call_oi"] +
                max(0.0, x["strike"] - K) * x["put_oi"]
                for x in strikes
            )
            if pain < best_pain:
                best_pain = pain
                max_pain_strike = K

    pcr = (total_put_oi / total_call_oi) if total_call_oi else 0.0

    # Top 5 strikes around spot by total OI
    if spot:
        strikes_sorted = sorted(strikes, key=lambda s: abs(s["strike"] - spot))[:9]
        strikes_sorted.sort(key=lambda s: s["strike"])
    else:
        strikes_sorted = strikes[:9]

    return {
        "spot":            spot,
        "expiry":          nearest_expiry,
        "pcr":             round(pcr, 3),
        "max_pain":        max_pain_strike,
        "max_pain_dist":   round(((max_pain_strike - spot) / spot * 100), 2) if spot else 0,
        "total_call_oi":   total_call_oi,
        "total_put_oi":    total_put_oi,
        "total_call_chg":  total_call_chg,
        "total_put_chg":   total_put_chg,
        "atm_strikes":     strikes_sorted,
    }


@app.get("/api/option_chain")
def get_option_chain(symbol: str = Query(default="NIFTY")):
    """Live NSE option chain analytics (PCR, max-pain, top OI strikes)."""
    try:
        client = get_default_client()
        raw = client.option_chain(symbol)
        analytics = _option_chain_analytics(raw)
        if not analytics:
            return JSONResponse({"available": False, "reason": "NSE option-chain feed unavailable (rate-limited or blocked)"})
        analytics["available"] = True
        return JSONResponse(analytics)
    except Exception as exc:
        return JSONResponse({"available": False, "reason": str(exc)})


# In-process chart-bar cache (yfinance fetch is slow).
_CHART_CACHE: dict[str, dict] = {}
_CHART_LOCK = threading.Lock()


def _yf_bars(symbol: str, period: str = "5d", interval: str = "5m") -> list[dict]:
    """Fetch OHLC bars from yfinance, normalised to a list of dicts with epoch seconds."""
    import yfinance as yf
    df = yf.download(
        symbol, period=period, interval=interval,
        progress=False, auto_adjust=False, prepost=False,
    )
    if df is None or df.empty:
        return []
    if hasattr(df.columns, "get_level_values"):
        df.columns = df.columns.get_level_values(0)
    bars: list[dict] = []
    for idx, row in df.iterrows():
        try:
            ts = int(idx.timestamp())
            bars.append({
                "time":   ts,
                "open":   float(row["Open"]),
                "high":   float(row["High"]),
                "low":    float(row["Low"]),
                "close":  float(row["Close"]),
                "volume": float(row.get("Volume") or 0),
            })
        except Exception:
            continue
    return bars


def _btc_bars(granularity: int = 300, limit: int = 350) -> list[dict]:
    """BTC OHLC for the chart. yfinance first (Coinbase blocked from this network),
    CryptoCompare as fallback. Granularity in seconds → maps to yfinance interval."""
    yf_map = {
        60:    ("1m",  "5d"),
        300:   ("5m",  "60d"),
        900:   ("15m", "60d"),
        3600:  ("1h",  "730d"),
        86400: ("1d",  "max"),
    }
    interval, period = yf_map.get(granularity, ("5m", "60d"))
    bars: list[dict] = []
    try:
        import yfinance as yf
        df = yf.download("BTC-USD", interval=interval, period=period,
                         progress=False, auto_adjust=False, prepost=False)
        if df is not None and not df.empty:
            if hasattr(df.columns, "get_level_values"):
                df.columns = df.columns.get_level_values(0)
            for idx, row in df.tail(limit).iterrows():
                try:
                    bars.append({
                        "time":   int(idx.timestamp()),
                        "open":   float(row["Open"]),
                        "high":   float(row["High"]),
                        "low":    float(row["Low"]),
                        "close":  float(row["Close"]),
                        "volume": float(row.get("Volume") or 0),
                    })
                except Exception:
                    continue
    except Exception:
        pass

    if bars:
        bars.sort(key=lambda x: x["time"])
        return bars

    # CryptoCompare fallback
    cc_map = {
        60:    ("histominute", 1),
        300:   ("histominute", 5),
        900:   ("histominute", 15),
        3600:  ("histohour",   1),
        86400: ("histoday",    1),
    }
    endpoint, agg = cc_map.get(granularity, ("histominute", 5))
    try:
        import requests as _rq
        url = (f"https://min-api.cryptocompare.com/data/v2/{endpoint}"
               f"?fsym=BTC&tsym=USD&limit={min(limit, 2000)}&aggregate={agg}")
        resp = _rq.get(url, timeout=8, headers={"User-Agent": "AIBtclaude/1.0"})
        items = (resp.json() or {}).get("Data", {}).get("Data", [])
        for r in items:
            try:
                bars.append({
                    "time":   int(r["time"]),
                    "open":   float(r["open"]),
                    "high":   float(r["high"]),
                    "low":    float(r["low"]),
                    "close":  float(r["close"]),
                    "volume": float(r.get("volumeto", 0) or 0),
                })
            except Exception:
                continue
    except Exception:
        return []
    bars.sort(key=lambda x: x["time"])
    return bars


# Back-compat alias for any older call sites
_coinbase_bars = _btc_bars


@app.get("/api/chart_data")
def get_chart_data(asset: str = Query(default="nifty"),
                   timeframe: str = Query(default="5m")):
    """OHLC bars for the in-page Lightweight-Charts canvas (BTC/XAU/NIFTY)."""
    a = (asset or "nifty").lower()

    # Coinbase intraday for BTC (faster + works on weekends)
    if a == "btc":
        coinbase_gran = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}.get(timeframe, 300)
        cache_key = f"BTC-USD|{timeframe}"
        with _CHART_LOCK:
            cached = _CHART_CACHE.get(cache_key)
            if cached and time.time() - cached["ts"] < 30:
                return JSONResponse({
                    "symbol": "BTC-USD", "timeframe": timeframe,
                    "bars": cached["bars"], "cached": True,
                    "fetched_at": cached["fetched_at"],
                })
        bars = _btc_bars(coinbase_gran)
        if not bars:
            return JSONResponse({"bars": [], "error": "BTC bars fetch returned no data"})
        blob = {"ts": time.time(),
                "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "bars": bars}
        with _CHART_LOCK:
            _CHART_CACHE[cache_key] = blob
        return JSONResponse({
            "symbol": "BTC-USD", "timeframe": timeframe,
            "bars": bars, "cached": False, "fetched_at": blob["fetched_at"],
        })

    # yfinance for XAU + NIFTY (and BANKNIFTY pairs feed)
    if a == "xau":
        symbol = "GC=F"
    elif a == "nifty":
        symbol = NIFTY_YFINANCE_SYMBOL
    elif a == "banknifty":
        symbol = NIFTY_BN_YFINANCE_SYMBOL
    else:
        return JSONResponse({"bars": [], "error": "asset not supported"})

    period_map = {"1m": "2d", "5m": "5d", "15m": "1mo", "1h": "3mo", "1d": "2y"}
    period = period_map.get(timeframe, "5d")
    cache_key = f"{symbol}|{timeframe}"

    with _CHART_LOCK:
        cached = _CHART_CACHE.get(cache_key)
        if cached and time.time() - cached["ts"] < 45:
            return JSONResponse({
                "symbol":    symbol,
                "timeframe": timeframe,
                "bars":      cached["bars"],
                "cached":    True,
                "fetched_at": cached["fetched_at"],
            })

    try:
        bars = _yf_bars(symbol, period=period, interval=timeframe)
    except Exception as exc:
        return JSONResponse({"bars": [], "error": str(exc)})

    if not bars:
        return JSONResponse({"bars": [], "error": "no data from yfinance"})

    # XAU: yfinance only has GC=F (gold FUTURES). User-displayed price comes
    # from the agent's spot feed (gold-api.com). Apply a basis offset so chart
    # bars line up with the displayed spot price (offset = spot - futures_close).
    chart_label = symbol
    if a == "xau":
        spot = _fetch_xau_spot()
        if spot and bars:
            futures_last = float(bars[-1].get("close", 0) or 0)
            if futures_last > 0:
                basis = spot - futures_last
                if abs(basis) > 0.01:
                    bars = [
                        {**b,
                         "open":  b.get("open", 0)  + basis,
                         "high":  b.get("high", 0)  + basis,
                         "low":   b.get("low", 0)   + basis,
                         "close": b.get("close", 0) + basis}
                        for b in bars
                    ]
                    chart_label = "XAU/USD (spot, basis-adj)"

    blob = {
        "ts": time.time(),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "bars": bars,
    }
    with _CHART_LOCK:
        _CHART_CACHE[cache_key] = blob

    return JSONResponse({
        "symbol":     chart_label,
        "timeframe":  timeframe,
        "bars":       bars,
        "cached":     False,
        "fetched_at": blob["fetched_at"],
    })


def _fetch_xau_spot() -> Optional[float]:
    """Live spot XAU/USD via gold-api.com (free)."""
    import urllib.request as _urlreq
    try:
        req = _urlreq.Request(
            "https://api.gold-api.com/price/XAU",
            headers={"Accept": "application/json", "User-Agent": "XAUTradingAgent/1.0"},
        )
        with _urlreq.urlopen(req, timeout=6) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        p = payload.get("price")
        if p and float(p) > 0:
            return float(p)
    except Exception:
        pass
    return None


@app.get("/api/trade_zones")
def get_trade_zones(asset: str = Query(default="btc")):
    """Return current trade entry/SL/TP1/TP2/TP3 levels for chart overlay."""
    paths = _paths_for(asset)
    if not paths["state"].exists():
        return JSONResponse({"available": False, "reason": "no state file"})
    try:
        state = json.loads(paths["state"].read_text(encoding="utf-8"))
    except Exception as exc:
        return JSONResponse({"available": False, "reason": f"state parse error: {exc}"})

    trade = state.get("current_trade") or {}
    if not trade or not trade.get("entry_price"):
        # Fall back: surface the last_signal levels if a signal exists
        last_sig = state.get("last_signal") or {}
        if last_sig and last_sig.get("entry_price") and last_sig.get("stop_loss"):
            return JSONResponse({
                "available":      True,
                "is_pending":     True,
                "bias":           last_sig.get("bias"),
                "entry_price":    last_sig.get("entry_price"),
                "stop_loss":      last_sig.get("stop_loss"),
                "take_profit_1":  last_sig.get("take_profit_1") or 0,
                "take_profit_2":  last_sig.get("take_profit_2") or 0,
                "take_profit_3":  last_sig.get("take_profit_3") or 0,
                "risk_per_unit":  last_sig.get("risk_per_unit") or
                                   abs((last_sig.get("entry_price", 0) or 0) -
                                       (last_sig.get("stop_loss", 0) or 0)),
                "current_price":  state.get("latest_price", 0),
                "open_time":      None,
            })
        return JSONResponse({"available": False, "reason": "no active trade"})

    return JSONResponse({
        "available":       True,
        "is_pending":      False,
        "bias":            trade.get("bias"),
        "entry_price":     trade.get("entry_price"),
        "stop_loss":       trade.get("stop_loss"),
        "take_profit_1":   trade.get("take_profit_1") or 0,
        "take_profit_2":   trade.get("take_profit_2") or 0,
        "take_profit_3":   trade.get("take_profit_3") or 0,
        "risk_per_unit":   trade.get("risk_per_unit") or
                            abs((trade.get("entry_price", 0) or 0) -
                                (trade.get("stop_loss", 0) or 0)),
        "current_price":   trade.get("current_price") or state.get("latest_price", 0),
        "open_time":       trade.get("open_time"),
        "unrealized_pl":   trade.get("unrealized_pl"),
        "unrealized_pl_pct": trade.get("unrealized_pl_pct"),
    })


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Trading Agent</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d0f14;--surf:#131722;--bdr:#2a2e39;
  --txt:#d1d4dc;--sub:#787b86;
  --G:#26a69a;--R:#ef5350;--Y:#f9a825;--B:#2196f3;--P:#9c27b0;--gold:#ffb300;
}
body{background:var(--bg);color:var(--txt);font:13px/1.4 -apple-system,monospace;
  height:100vh;overflow:hidden;display:flex;flex-direction:column}

header{display:flex;align-items:center;gap:16px;padding:7px 14px;
  background:var(--surf);border-bottom:1px solid var(--bdr);flex-shrink:0;flex-wrap:wrap}
.logo{font-size:14px;font-weight:700;letter-spacing:1px}
.logo.btc{color:var(--B)}
.logo.xau{color:var(--gold)}
.logo.nifty{color:#26a69a}
.hs{display:flex;flex-direction:column}
.hs .l{font-size:9px;color:var(--sub);text-transform:uppercase;letter-spacing:.5px}
.hs .v{font-size:13px;font-weight:600}
#h-price{font-size:20px!important}
.dot{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:4px}
.dg{background:var(--G);animation:blink 2s infinite}
.dy{background:var(--Y);animation:blink 2s infinite}
.dr{background:var(--R)}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.ppos{color:var(--G)} .pneg{color:var(--R)}

#asset-switch{background:var(--bg);color:var(--txt);border:1px solid var(--bdr);
  padding:5px 10px;border-radius:4px;font:12px monospace;font-weight:700;cursor:pointer}
#asset-switch:focus{outline:none;border-color:var(--B)}

.btn{padding:5px 14px;border-radius:4px;border:none;font:12px monospace;
  cursor:pointer;font-weight:700;letter-spacing:.5px}
.btn-start{background:rgba(38,166,154,.2);color:var(--G);border:1px solid var(--G)}
.btn-stop {background:rgba(239,83,80,.2); color:var(--R);border:1px solid var(--R)}
.btn:hover{opacity:.8}

.main{display:grid;grid-template-columns:1fr 320px;flex:1;min-height:0}

.cs{display:flex;flex-direction:column;border-right:1px solid var(--bdr)}
.ctbar{display:flex;align-items:center;justify-content:space-between;
  padding:5px 12px;background:var(--surf);border-bottom:1px solid var(--bdr);flex-shrink:0}
.ctbar span{font-size:11px;color:var(--sub)}
.lvbar{display:flex;gap:8px;padding:4px 12px;background:var(--bg);
  border-bottom:1px solid var(--bdr);flex-shrink:0;flex-wrap:wrap;min-height:28px;align-items:center}
.lp{font-size:11px;padding:2px 7px;border-radius:3px;font-weight:600}
.lp-e{background:rgba(33,150,243,.2);color:#64b5f6}
.lp-s{background:rgba(239,83,80,.2);color:#ef9a9a}
.lp-t1{background:rgba(38,166,154,.2);color:#80cbc4}
.lp-t2{background:rgba(156,39,176,.2);color:#ce93d8}
.lp-pnl{background:rgba(255,255,255,.06);color:var(--sub)}
#tv-container{flex:1;min-height:0;position:relative}
#lw-chart{width:100%;height:100%;background:var(--bg)}
#lw-chart-overlay{position:absolute;top:8px;left:12px;font-size:11px;color:var(--sub);
  pointer-events:none;text-shadow:0 0 4px var(--bg);z-index:5}
.tf-bar{display:flex;gap:4px;margin-left:14px}
.tf-btn{background:transparent;border:1px solid var(--bdr);color:var(--sub);
  padding:2px 8px;border-radius:3px;font:11px monospace;cursor:pointer}
.tf-btn.active{background:rgba(33,150,243,.15);color:#64b5f6;border-color:#2196f3}
.tf-btn:hover{color:var(--txt)}

/* Active Trade Zones card */
#zones-card{display:none}
.zone-row{display:flex;justify-content:space-between;align-items:center;
  padding:5px 8px;margin-bottom:3px;background:var(--surf);border-radius:3px;
  border-left:4px solid var(--sub);font-size:11px}
.zone-row.tp3{border-left-color:#2196f3}
.zone-row.tp2{border-left-color:#ff9800}
.zone-row.tp1{border-left-color:#26a69a}
.zone-row.entry{border-left-color:#ffffff;background:rgba(255,255,255,.05)}
.zone-row.sl{border-left-color:#ef5350;background:rgba(239,83,80,.06)}
.zone-row .zlabel{font-weight:700;font-size:10px;letter-spacing:.5px;width:50px}
.zone-row .zprice{font-weight:600;font-size:12px;flex:1;text-align:right;margin-right:8px}
.zone-row .zdist{font-size:10px;color:var(--sub);width:80px;text-align:right}

.rp{display:flex;flex-direction:column;overflow-y:auto}
.card{border-bottom:1px solid var(--bdr);padding:11px}
.ct{font-size:10px;font-weight:700;color:var(--sub);text-transform:uppercase;
  letter-spacing:1px;margin-bottom:9px;display:flex;justify-content:space-between;align-items:center}
.tr{display:flex;justify-content:space-between;align-items:center;
  padding:3px 0;border-bottom:1px solid rgba(42,46,57,.5)}
.tr:last-child{border:none}
.tl{color:var(--sub);font-size:12px} .tv{font-weight:600;font-size:12px;text-align:right}
.empty{text-align:center;color:var(--sub);padding:14px 0;font-size:12px}

#approval-card{background:rgba(249,168,37,.07);border:1px solid var(--Y)!important;display:none}
#approval-card .ct{color:var(--Y)}
.apnl{display:flex;gap:8px;margin-top:10px}
.btn-exec{background:rgba(38,166,154,.25);color:var(--G);border:1px solid var(--G);
  padding:7px 20px;border-radius:4px;font:13px monospace;font-weight:700;cursor:pointer;flex:1}
.btn-skip{background:rgba(239,83,80,.2);color:var(--R);border:1px solid var(--R);
  padding:7px 20px;border-radius:4px;font:13px monospace;font-weight:700;cursor:pointer;flex:1}
.btn-exec:hover{background:rgba(38,166,154,.4)}
.btn-skip:hover{background:rgba(239,83,80,.35)}
#countdown{font-size:11px;color:var(--Y);text-align:center;margin-top:6px}

.badge{padding:2px 8px;border-radius:10px;font-size:11px;font-weight:700}
.bg{background:rgba(38,166,154,.15);color:var(--G);border:1px solid var(--G)}
.br{background:rgba(239,83,80,.15);color:var(--R);border:1px solid var(--R)}
.bm{background:rgba(120,123,134,.15);color:var(--sub);border:1px solid var(--sub)}

.sig-q{font-size:28px;font-weight:800}
.sig-reason{font-size:11px;color:var(--sub);line-height:1.55;max-height:80px;
  overflow-y:auto;border-top:1px solid var(--bdr);margin-top:7px;padding-top:7px;
  white-space:pre-wrap;word-break:break-word}

.sgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
.sbox{background:var(--surf);border-radius:4px;padding:7px 4px;text-align:center;border:1px solid var(--bdr)}
.sbox .n{font-size:17px;font-weight:700} .sbox .lb{font-size:10px;color:var(--sub);margin-top:2px}

#log-box{height:120px;overflow-y:auto;font-size:11px;line-height:1.65;color:var(--sub)}
.ll{padding:1px 0;border-bottom:1px solid rgba(42,46,57,.3)}
.ll.w{color:var(--Y)} .ll.e{color:var(--R)} .ll.t{color:var(--G);font-weight:600}

/* News card */
#news-card{display:none}
.news-item{padding:6px 0;border-bottom:1px solid rgba(42,46,57,.4);font-size:11px;line-height:1.45}
.news-item:last-child{border:none}
.news-item a{color:var(--txt);text-decoration:none;display:block;margin-bottom:2px}
.news-item a:hover{color:#64b5f6}
.news-item .src{color:var(--sub);font-size:10px;display:flex;justify-content:space-between}
.news-item .src .when{color:var(--sub);opacity:.7}
#news-list{max-height:220px;overflow-y:auto}

/* Option chain card */
#oi-card{display:none}
.oi-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:8px}
.oi-stats .sbox .n{font-size:14px}
.oi-table{width:100%;border-collapse:collapse;font-size:10px}
.oi-table th{color:var(--sub);font-weight:600;text-align:right;padding:3px 4px;
  border-bottom:1px solid var(--bdr);font-size:9px;text-transform:uppercase;letter-spacing:.5px}
.oi-table th:nth-child(3){text-align:center}
.oi-table td{padding:3px 4px;text-align:right;border-bottom:1px solid rgba(42,46,57,.4)}
.oi-table td.strike{text-align:center;font-weight:600;color:var(--txt)}
.oi-table td.atm{background:rgba(33,150,243,.1)}
.oi-pos{color:var(--G)} .oi-neg{color:var(--R)}

/* Pod chips */
#pod-card{display:none}
.pod-chip{display:flex;justify-content:space-between;align-items:center;
  padding:6px 8px;margin-bottom:5px;background:var(--surf);border-radius:4px;
  border-left:3px solid var(--sub);font-size:11px}
.pod-chip .pn{font-weight:700;color:var(--txt);font-size:11px}
.pod-chip .pi{color:var(--sub);font-size:10px;font-style:italic}
.pod-chip.long{border-left-color:var(--G)}
.pod-chip.short{border-left-color:var(--R)}
.pod-chip.neutral{border-left-color:var(--sub);opacity:.7}
.pod-rationale{font-size:10px;color:var(--sub);margin-top:3px;line-height:1.4}
.pod-meta{display:flex;flex-wrap:wrap;gap:3px;margin-top:5px}
.meta-chip{font-size:9px;padding:2px 5px;border-radius:3px;background:rgba(33,150,243,0.08);
           color:var(--sub);border:1px solid rgba(33,150,243,0.18);font-family:'JetBrains Mono',monospace;
           white-space:nowrap;line-height:1.2}
.meta-chip b{color:var(--B);font-weight:600;margin-right:2px}
#pod-sum{font-size:11px;color:var(--sub)}

::-webkit-scrollbar{width:3px}::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--bdr);border-radius:2px}
</style>
</head>
<body>

<header>
  <div class="logo btc" id="logo">&#9651; BTC/USD AGENT</div>
  <select id="asset-switch" onchange="onAssetChange()">
    <option value="btc" selected>BTC/USD</option>
    <option value="xau">XAU/USD (Gold)</option>
    <option value="nifty">NIFTY 50 (India)</option>
  </select>
  <div class="hs"><span class="l">Price</span><span class="v" id="h-price">—</span></div>
  <div class="hs"><span class="l">Session</span><span class="v" id="h-session">—</span></div>
  <div class="hs"><span class="l">Account</span><span class="v" id="h-bal">—</span></div>
  <div class="hs"><span class="l">Daily P&amp;L</span><span class="v" id="h-pnl">—</span></div>
  <div class="hs"><span class="l">Bot Status</span><span class="v" id="h-status"><span class="dot dy"></span>connecting...</span></div>
  <div class="hs" id="market-status-wrap" style="display:none"><span class="l">Market</span><span class="v" id="h-market">—</span></div>
  <div style="margin-left:auto;display:flex;align-items:center;gap:10px">
    <span style="font-size:10px;color:var(--sub)" id="h-ts"></span>
    <button class="btn" id="analyze-btn" onclick="analyzeNow()" style="background:rgba(33,150,243,.2);color:#64b5f6;border:1px solid #2196f3">Analyze Now</button>
    <button class="btn btn-start" id="bot-btn" onclick="toggleBot()">Start Bot</button>
  </div>
</header>

<div class="main">
  <div class="cs">
    <div class="ctbar">
      <span id="ctbar-label">BTC/USD &nbsp;·&nbsp; TradingView &nbsp;·&nbsp; 1H</span>
      <div class="tf-bar" id="tf-bar" style="display:none">
        <button class="tf-btn" data-tf="1m" onclick="setTimeframe('1m')">1m</button>
        <button class="tf-btn active" data-tf="5m" onclick="setTimeframe('5m')">5m</button>
        <button class="tf-btn" data-tf="15m" onclick="setTimeframe('15m')">15m</button>
        <button class="tf-btn" data-tf="1h" onclick="setTimeframe('1h')">1h</button>
        <button class="tf-btn" data-tf="1d" onclick="setTimeframe('1d')">1D</button>
      </div>
      <span id="tv-s">loading chart...</span>
    </div>
    <div class="lvbar" id="lvbar"><span style="color:var(--sub);font-style:italic;font-size:11px">No active trade</span></div>
    <div id="tv-container">
      <div id="lw-chart"></div>
      <div id="lw-chart-overlay"></div>
    </div>
  </div>

  <div class="rp">

    <div class="card" id="approval-card">
      <div class="ct">⚡ TRADE DECISION REQUIRED <span id="ap-badge"></span></div>
      <div id="ap-body"></div>
      <div class="apnl">
        <button class="btn-exec" onclick="approveSignal()">✓ Execute Trade</button>
        <button class="btn-skip" onclick="skipSignal()">✕ Skip</button>
      </div>
      <div id="countdown"></div>
    </div>

    <!-- FII/DII chip strip (NIFTY only) -->
    <div class="card" id="fiidii-card" style="display:none">
      <div class="ct">📈 FII / DII FLOW (₹ crores) <span id="fiidii-date" style="color:var(--sub);font-size:10px"></span></div>
      <div id="fiidii-body" class="sgrid">
        <div class="sbox"><div class="n" id="fii-today">—</div><div class="lb">FII Cash Net</div></div>
        <div class="sbox"><div class="n" id="dii-today">—</div><div class="lb">DII Cash Net</div></div>
        <div class="sbox"><div class="n" id="fii-5d">—</div><div class="lb">FII 5D Avg</div></div>
      </div>
    </div>

    <!-- Active Trade Zones (chart-overlay companion) -->
    <div class="card" id="zones-card">
      <div class="ct">📐 ACTIVE TRADE ZONES <span id="zones-meta" style="color:var(--sub);font-size:10px"></span></div>
      <div id="zones-body"></div>
    </div>

    <!-- Option chain analytics (NIFTY only) -->
    <div class="card" id="oi-card">
      <div class="ct">⛓ NSE OPTION CHAIN <span id="oi-meta" style="color:var(--sub);font-size:10px"></span></div>
      <div id="oi-body"><div class="empty">Loading option chain...</div></div>
    </div>

    <!-- Pod Report (XAU + NIFTY) -->
    <div class="card" id="pod-card">
      <div class="ct">🏛 INSTITUTIONAL POD <span id="pod-sum">—</span></div>
      <div id="pod-body"><div class="empty">No vote yet — click Analyze Now</div></div>
    </div>

    <!-- Live news card (NIFTY default; works for all assets) -->
    <div class="card" id="news-card">
      <div class="ct">📰 LIVE NEWS <span id="news-meta" style="color:var(--sub);font-size:10px"></span></div>
      <div id="news-list"><div class="empty">Loading headlines...</div></div>
    </div>

    <div class="card">
      <div class="ct">Current Trade <span id="trade-badge"></span></div>
      <div id="trade-body"><div class="empty">No active trade</div></div>
    </div>

    <div class="card">
      <div class="ct">Last AI Signal <span id="sig-ts" style="color:var(--sub);font-size:10px"></span></div>
      <div id="sig-body"><div class="empty">No signal yet</div></div>
    </div>

    <div class="card">
      <div class="ct">Session Stats</div>
      <div class="sgrid">
        <div class="sbox"><div class="n" id="st-t">0</div><div class="lb">Trades</div></div>
        <div class="sbox"><div class="n" id="st-w">—</div><div class="lb">Win Rate</div></div>
        <div class="sbox"><div class="n" id="st-p">—</div><div class="lb">Total P&amp;L</div></div>
      </div>
    </div>

    <div class="card" style="flex:1;min-height:0">
      <div class="ct">Live Logs</div>
      <div id="log-box"></div>
    </div>

  </div>
</div>

<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<script>
// ── current asset (driven by dropdown) ──
var currentAsset = 'btc';
var currentTF = '5m';
// Asset → display label for chart overlay
var ASSET_LABEL = { btc: 'BTC/USD', xau: 'XAU/USD', nifty: 'NIFTY 50' };
// Asset → currency symbol for trade-zone formatting
var ASSET_CCY   = { btc: '$',       xau: '$',       nifty: '₹'         };

// ── Lightweight Charts handle (NIFTY) ──
var lwChart = null, lwSeries = null, lwResizeObserver = null, lwTimer = null;

function ensureLWChart(){
  if(lwChart) return lwChart;
  var container = el('lw-chart');
  if(!container || !window.LightweightCharts) return null;
  lwChart = LightweightCharts.createChart(container, {
    layout:    { background:{type:'solid', color:'#0d0f14'}, textColor:'#787b86' },
    grid:      { vertLines:{color:'#1a1d27'}, horzLines:{color:'#1a1d27'} },
    rightPriceScale: { borderColor:'#2a2e39' },
    timeScale: { borderColor:'#2a2e39', timeVisible:true, secondsVisible:false, rightOffset:6 },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    autoSize:  true,
  });
  lwSeries = lwChart.addCandlestickSeries({
    upColor:'#26a69a', downColor:'#ef5350',
    borderUpColor:'#26a69a', borderDownColor:'#ef5350',
    wickUpColor:'#26a69a', wickDownColor:'#ef5350',
  });
  if(window.ResizeObserver){
    lwResizeObserver = new ResizeObserver(function(){
      if(lwChart) lwChart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
    });
    lwResizeObserver.observe(container);
  }
  return lwChart;
}

function loadChart(asset, timeframe){
  ensureLWChart();
  if(!lwSeries) return;
  el('tv-s').textContent = 'loading bars...';
  el('tv-s').style.color = 'var(--sub)';
  // Reset markers + price lines for the new asset
  if(lwSeries.setMarkers) lwSeries.setMarkers([]);
  _zoneLines.forEach(function(l){ try{ lwSeries.removePriceLine(l); }catch(e){} });
  _zoneLines = [];
  fetch('/api/chart_data?asset='+encodeURIComponent(asset)+'&timeframe='+encodeURIComponent(timeframe||currentTF))
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(!d.bars || !d.bars.length){
        el('tv-s').textContent = 'no data';
        el('tv-s').style.color = 'var(--R)';
        el('lw-chart-overlay').textContent = d.error || 'no data';
        return;
      }
      var clean = d.bars.filter(function(b){
        return b && b.time && isFinite(b.open) && isFinite(b.close);
      }).map(function(b){
        return { time:b.time, open:b.open, high:b.high, low:b.low, close:b.close };
      });
      lwSeries.setData(clean);
      lwChart.timeScale().fitContent();
      el('tv-s').textContent = 'live ✓ ('+clean.length+' bars · '+timeframe+')';
      el('tv-s').style.color = '#26a69a';
      var last = clean[clean.length-1];
      var ccy = ASSET_CCY[asset] || '$';
      var locale = asset==='nifty' ? 'en-IN' : 'en-US';
      el('lw-chart-overlay').textContent =
        (ASSET_LABEL[asset]||asset.toUpperCase()) + ' · ' + ccy + Number(last.close).toLocaleString(locale);
      // Re-apply trade overlay if there's an active trade
      fetch('/api/trade_zones?asset='+encodeURIComponent(asset))
        .then(function(r){return r.json();})
        .then(function(tz){
          if(tz.available){
            renderTradeOverlay(tz);
            updateZones(tz);
          } else {
            updateZones(null);
          }
        }).catch(function(){});
    })
    .catch(function(e){
      el('tv-s').textContent = 'chart error';
      el('tv-s').style.color = 'var(--R)';
      el('lw-chart-overlay').textContent = String(e);
    });
}

function setTimeframe(tf){
  currentTF = tf;
  document.querySelectorAll('.tf-btn').forEach(function(b){
    b.classList.toggle('active', b.getAttribute('data-tf')===tf);
  });
  loadChart(currentAsset, tf);
}

// ── Trade-zone overlay (BlackRock-style entry/SL/TP1/TP2/TP3) ──
var _zoneLines = [];
function renderTradeOverlay(trade){
  if(!lwSeries) return;
  // Remove previous lines
  _zoneLines.forEach(function(l){ try{ lwSeries.removePriceLine(l); }catch(e){} });
  _zoneLines = [];
  if(!trade || !trade.entry_price){
    if(lwSeries.setMarkers) lwSeries.setMarkers([]);
    return;
  }
  var LineStyle = (window.LightweightCharts && window.LightweightCharts.LineStyle) || {Solid:0, Dashed:2};
  var cfg = [
    {price: trade.entry_price,    color:'#ffffff', title:'⚪ ENTRY', dashed:false, lw:2},
    {price: trade.stop_loss,      color:'#ef5350', title:'🟥 SL ' , dashed:true,  lw:2},
    {price: trade.take_profit_1,  color:'#26a69a', title:'🟩 TP1', dashed:true,  lw:1},
    {price: trade.take_profit_2,  color:'#ff9800', title:'🟧 TP2', dashed:true,  lw:1},
    {price: trade.take_profit_3,  color:'#2196f3', title:'🟦 TP3', dashed:true,  lw:1},
  ];
  cfg.filter(function(c){ return c.price && c.price > 0; }).forEach(function(c){
    try{
      _zoneLines.push(lwSeries.createPriceLine({
        price:           c.price,
        color:           c.color,
        lineWidth:       c.lw,
        lineStyle:       c.dashed ? LineStyle.Dashed : LineStyle.Solid,
        axisLabelVisible: true,
        title:           c.title,
      }));
    }catch(e){ console.log('priceLine err', e); }
  });

  // Marker at entry bar
  if(trade.open_time && lwSeries.setMarkers){
    try{
      var ts = Math.floor(new Date(trade.open_time).getTime()/1000);
      lwSeries.setMarkers([{
        time:     ts,
        position: trade.bias==='BULLISH' ? 'belowBar' : 'aboveBar',
        color:    trade.bias==='BULLISH' ? '#26a69a' : '#ef5350',
        shape:    trade.bias==='BULLISH' ? 'arrowUp' : 'arrowDown',
        text:     (trade.bias==='BULLISH' ? 'LONG @ ' : 'SHORT @ ') + Number(trade.entry_price).toLocaleString(),
      }]);
    }catch(e){}
  }
}

function updateZones(tz){
  var card = el('zones-card');
  var body = el('zones-body');
  var meta = el('zones-meta');
  if(!tz || !tz.available || !tz.entry_price){
    card.style.display = 'none';
    return;
  }
  card.style.display = 'block';
  meta.textContent = (tz.is_pending ? 'PENDING SIGNAL · ' : '') + (tz.bias || '');
  meta.style.color = tz.bias==='BULLISH' ? 'var(--G)' : tz.bias==='BEARISH' ? 'var(--R)' : 'var(--sub)';

  var ccy = ASSET_CCY[currentAsset] || '$';
  var locale = currentAsset==='nifty' ? 'en-IN' : 'en-US';
  var entry = Number(tz.entry_price);
  var risk = Number(tz.risk_per_unit) || Math.abs(entry - Number(tz.stop_loss||entry));
  var isLong = tz.bias === 'BULLISH';

  function fmtPrice(v){ return ccy + Number(v).toLocaleString(locale, {minimumFractionDigits:2, maximumFractionDigits:2}); }
  function pctVsEntry(v){
    if(!entry) return '—';
    var p = (v - entry) / entry * 100;
    return (p>=0?'+':'') + p.toFixed(2) + '%';
  }
  function rMul(v){
    if(!risk || !entry) return '—';
    var dir = isLong ? 1 : -1;
    var r = ((v - entry) / risk) * dir;
    return (r>=0?'+':'') + r.toFixed(1) + 'R';
  }

  function row(cls, label, price){
    if(!price || price <= 0) return '';
    return '<div class="zone-row '+cls+'">'+
             '<span class="zlabel">'+label+'</span>'+
             '<span class="zprice">'+fmtPrice(price)+'</span>'+
             '<span class="zdist">'+pctVsEntry(price)+' '+rMul(price)+'</span>'+
           '</div>';
  }

  // Order: TP3 / TP2 / TP1 / ENTRY / SL  for LONG (top-down profit→loss)
  // Mirror for SHORT so the visual direction reflects price expectation
  var rows;
  if(isLong){
    rows = [
      row('tp3',   'TP3',   tz.take_profit_3),
      row('tp2',   'TP2',   tz.take_profit_2),
      row('tp1',   'TP1',   tz.take_profit_1),
      row('entry', 'ENTRY', tz.entry_price),
      row('sl',    'SL',    tz.stop_loss),
    ];
  } else {
    rows = [
      row('sl',    'SL',    tz.stop_loss),
      row('entry', 'ENTRY', tz.entry_price),
      row('tp1',   'TP1',   tz.take_profit_1),
      row('tp2',   'TP2',   tz.take_profit_2),
      row('tp3',   'TP3',   tz.take_profit_3),
    ];
  }
  body.innerHTML = rows.join('');
}

function curSym(){ return currentAsset==='nifty' ? '₹' : '$'; }

function onAssetChange(){
  currentAsset = el('asset-switch').value;
  var logo = el('logo');
  // Asset-specific default timeframes
  var defaultTF = {btc:'5m', xau:'15m', nifty:'15m'}[currentAsset] || '5m';
  currentTF = defaultTF;
  document.querySelectorAll('.tf-btn').forEach(function(b){
    b.classList.toggle('active', b.getAttribute('data-tf')===currentTF);
  });

  if(currentAsset==='xau'){
    logo.textContent = '◆ XAU/USD AGENT';
    logo.className = 'logo xau';
    el('ctbar-label').innerHTML = 'XAU/USD (Gold) &nbsp;·&nbsp; yfinance GC=F';
    el('pod-card').style.display = 'block';
    el('fiidii-card').style.display = 'none';
    el('oi-card').style.display = 'none';
    el('news-card').style.display = 'block';
    el('market-status-wrap').style.display = 'none';
    el('tf-bar').style.display = 'flex';
    document.title = 'XAU/USD Agent';
  } else if(currentAsset==='nifty'){
    logo.textContent = '◆ NIFTY 50 AGENT';
    logo.className = 'logo nifty';
    el('ctbar-label').innerHTML = 'NIFTY 50 (NSE) &nbsp;·&nbsp; yfinance ^NSEI';
    el('pod-card').style.display = 'block';
    el('fiidii-card').style.display = 'block';
    el('oi-card').style.display = 'block';
    el('news-card').style.display = 'block';
    el('market-status-wrap').style.display = 'flex';
    el('tf-bar').style.display = 'flex';
    document.title = 'NIFTY 50 Agent';
  } else {
    logo.textContent = '△ BTC/USD AGENT';
    logo.className = 'logo btc';
    el('ctbar-label').innerHTML = 'BTC/USD &nbsp;·&nbsp; Coinbase &nbsp;·&nbsp; demo / signal-only';
    el('pod-card').style.display = 'block';
    el('fiidii-card').style.display = 'none';
    el('oi-card').style.display = 'none';
    el('news-card').style.display = 'block';
    el('market-status-wrap').style.display = 'none';
    el('tf-bar').style.display = 'flex';
    document.title = 'BTC/USD Agent';
  }

  // All assets now use Lightweight Charts (BTC=Coinbase, XAU/NIFTY=yfinance)
  if(lwTimer){ clearInterval(lwTimer); lwTimer = null; }
  el('lw-chart-overlay').textContent = (ASSET_LABEL[currentAsset]||currentAsset.toUpperCase()) + ' · loading…';
  loadChart(currentAsset, currentTF);
  lwTimer = setInterval(function(){ loadChart(currentAsset, currentTF); }, 30000);

  // Reset cached UI fields and refetch immediately
  el('approval-card').style.display = 'none';
  el('zones-card').style.display = 'none';
  if(countdownInterval){ clearInterval(countdownInterval); countdownInterval = null; }
  lastLogCount = 0;
  el('log-box').innerHTML = '';
  poll();
}

// ── utils ──
function p$(v){
  if(v===null||v===undefined) return '—';
  return curSym() + Number(v).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
}
function pCr(v){
  if(v===null||v===undefined) return '—';
  var n = Number(v);
  return (n>=0?'+':'') + n.toLocaleString('en-IN',{maximumFractionDigits:0});
}
function pp(v){ if(v===null||v===undefined)return'—'; var n=Number(v); return(n>=0?'+':'')+n.toFixed(2)+'%'; }
function bdg(b){ if(b==='BULLISH')return'<span class="badge bg">LONG</span>'; if(b==='BEARISH')return'<span class="badge br">SHORT</span>'; return'<span class="badge bm">'+(b||'—')+'</span>'; }
function ago(iso){ if(!iso)return'—'; var s=Math.floor((Date.now()-new Date(iso))/1000); if(s<60)return s+'s ago'; if(s<3600)return Math.floor(s/60)+'m ago'; return Math.floor(s/3600)+'h ago'; }

// Format strategy metadata into compact key:value chips so the user sees
// the actual parameters/logic each strategy is computing, not just rationale.
function fmtMetaVal(v){
  if(v === null || v === undefined) return '—';
  if(typeof v === 'number'){
    if(Math.abs(v) >= 1000) return v.toLocaleString('en-IN',{maximumFractionDigits:0});
    if(Math.abs(v) >= 10)   return v.toFixed(2);
    if(Math.abs(v) >= 1)    return v.toFixed(3);
    return v.toFixed(4);
  }
  if(typeof v === 'boolean') return v ? 'Y' : 'N';
  if(typeof v === 'object'){
    // FII/DII-style nested object — render top-level key:val pairs
    var parts = [];
    for(var k in v){ parts.push(k+'='+fmtMetaVal(v[k])); }
    return parts.join(', ');
  }
  return String(v);
}
function renderPodMeta(meta){
  if(!meta || typeof meta !== 'object') return '';
  var keys = Object.keys(meta);
  if(!keys.length) return '';
  var chips = keys.map(function(k){
    var val = fmtMetaVal(meta[k]);
    return '<span class="meta-chip"><b>'+k+'</b>:'+val+'</span>';
  }).join('');
  return '<div class="pod-meta">'+chips+'</div>';
}
function istTime(d){
  d = d || new Date();
  return d.toLocaleTimeString('en-IN', {timeZone: 'Asia/Kolkata', hour12: false}) + ' IST';
}
function istDateTime(iso){
  if(!iso) return '—';
  var d = new Date(iso);
  if(isNaN(d.getTime())) return iso;
  return d.toLocaleString('en-IN', {timeZone: 'Asia/Kolkata', hour12: false}) + ' IST';
}
function qc(q){ return q==='A+'?'#26a69a':q==='A'?'#2196f3':q==='B'?'#f9a825':'#787b86'; }
function el(id){ return document.getElementById(id); }
function aq(url){ return url + (url.indexOf('?')>=0 ? '&' : '?') + 'asset=' + currentAsset; }
function post(url){
  return fetch(aq(url),{method:'POST'}).then(function(r){
    return r.json().then(function(data){
      if(!r.ok){ throw data; }
      return data;
    });
  });
}

var botRunning = false;
var analyzeInFlight = false;     // waiting for agent to start analyzing
var analyzeSeenWorking = false;  // true once state flipped to "analyzing"
var analyzeRequestTs = 0;

function analyzeNow(){
  var btn = el('analyze-btn');
  btn.textContent = 'Requesting...';
  btn.disabled = true;
  post('/api/bot/analyze').then(function(d){
    btn.textContent = 'Analyzing...';
    analyzeInFlight = true;
    analyzeSeenWorking = false;
    analyzeRequestTs = Date.now();
    // Speed up polling while we wait for the agent to flip into "analyzing"
    poll();
    setTimeout(poll, 1500);
    setTimeout(poll, 3500);
    setTimeout(poll, 6000);
    // Hard timeout — release the button after 90s if state never flips back
    setTimeout(function(){
      if(analyzeInFlight){
        analyzeInFlight = false;
        analyzeSeenWorking = false;
        btn.textContent = 'Analyze Now'; btn.disabled = false;
      }
    }, 90000);
  }).catch(function(err){
    btn.textContent = (err && err.status==='not_running') ? 'Bot Offline' : 'Error';
    setTimeout(function(){ btn.textContent='Analyze Now'; btn.disabled=false; }, 3000);
    poll();
  });
}

function toggleBot(){
  if(botRunning){
    post('/api/bot/stop').then(function(d){ console.log('stop:',d); poll(); })
                        .catch(function(d){ console.log('stop err:',d); poll(); });
  } else {
    post('/api/bot/start').then(function(d){ console.log('start:',d); poll(); })
                          .catch(function(d){ console.log('start err:',d); poll(); });
  }
}

function approveSignal(){
  post('/api/signal/approve').then(function(){ el('approval-card').style.display='none'; poll(); });
}
function skipSignal(){
  post('/api/signal/skip').then(function(){ el('approval-card').style.display='none'; poll(); });
}

var countdownInterval = null;
function startCountdown(expiresTs){
  if(countdownInterval) clearInterval(countdownInterval);
  countdownInterval = setInterval(function(){
    var sec = Math.floor(expiresTs - Date.now()/1000);
    if(sec <= 0){ el('countdown').textContent='Expired — bot will skip'; clearInterval(countdownInterval); return; }
    var m = Math.floor(sec/60), s = sec%60;
    el('countdown').textContent = 'Auto-skips in '+m+'m '+s+'s';
  }, 1000);
}

function updateState(s){
  el('h-price').textContent   = s.latest_price ? p$(s.latest_price) : '—';
  el('h-session').textContent = (s.session||'—').toUpperCase();
  el('h-bal').textContent     = s.account ? p$(s.account.balance) : '—';

  var pnl = s.daily_pnl_pct;
  el('h-pnl').textContent  = pp(pnl);
  el('h-pnl').className    = 'v '+(pnl>0?'ppos':pnl<0?'pneg':'');
  el('h-ts').textContent   = istTime();

  var st = el('h-status');
  var bs = s.bot_status||'offline';
  botRunning = (bs==='running'||bs==='awaiting_approval'||bs==='starting'||bs==='analyzing');
  if(bs==='running'){
    st.innerHTML='<span class="dot dg"></span>running';st.style.color='var(--G)';
  } else if(bs==='analyzing'){
    st.innerHTML='<span class="dot dy"></span>analyzing…';st.style.color='var(--Y)';
  } else if(bs==='awaiting_approval'){
    st.innerHTML='<span class="dot dy"></span>awaiting approval';st.style.color='var(--Y)';
  } else if(bs==='starting'){
    st.innerHTML='<span class="dot dy"></span>starting...';st.style.color='var(--Y)';
  } else if(bs==='halted'){
    st.innerHTML='<span class="dot dr"></span>halted';st.style.color='var(--R)';
  } else {
    st.innerHTML='<span class="dot dr"></span>'+bs;st.style.color='var(--sub)';
  }
  el('bot-btn').textContent = botRunning ? 'Stop Bot' : 'Start Bot';
  el('bot-btn').className   = 'btn '+(botRunning?'btn-stop':'btn-start');

  // If we're waiting on an Analyze Now: flip back only AFTER state has been
  // seen as "analyzing" at least once (avoids the early release while the
  // agent's 2s tick hasn't picked up the trigger yet).
  if(analyzeInFlight){
    var btn = el('analyze-btn');
    if(bs==='analyzing'){
      analyzeSeenWorking = true;
      btn.textContent = 'Analyzing...';
      btn.disabled = true;
    } else if(analyzeSeenWorking){
      analyzeInFlight = false;
      analyzeSeenWorking = false;
      btn.textContent = 'Analyze Now';
      btn.disabled = false;
    } else if(Date.now() - analyzeRequestTs > 12000){
      // 12s safety — if state never flipped (e.g. agent crashed), unblock
      analyzeInFlight = false;
      btn.textContent = 'Analyze Now';
      btn.disabled = false;
    }
  }

  var t = s.current_trade;
  var lb = el('lvbar');
  if(t && t.entry_price){
    var pc = t.unrealized_pl_pct||0;
    lb.innerHTML =
      '<span class="lp lp-e">Entry '+p$(t.entry_price)+'</span>'+
      '<span class="lp lp-s">SL '+p$(t.stop_loss)+'</span>'+
      '<span class="lp lp-t1">TP1 '+p$(t.take_profit_1)+'</span>'+
      '<span class="lp lp-t2">TP2 '+p$(t.take_profit_2)+'</span>'+
      '<span class="lp lp-pnl">P&amp;L <span class="'+(pc>=0?'ppos':'pneg')+'">'+pp(pc)+'</span></span>';
  } else {
    lb.innerHTML='<span style="color:var(--sub);font-style:italic;font-size:11px">No active trade</span>';
  }

  var tb = el('trade-body');
  if(t && t.entry_price){
    el('trade-badge').innerHTML = bdg(t.bias);
    var pc2=t.unrealized_pl_pct||0, pd=t.unrealized_pl||0, cls=pc2>=0?'ppos':'pneg';
    var sd=t.entry_price&&t.stop_loss?Math.abs(((t.stop_loss-t.entry_price)/t.entry_price)*100).toFixed(1)+'%':'—';
    tb.innerHTML=
      row('Entry',p$(t.entry_price))+
      row('Stop Loss','<span class="pneg">'+p$(t.stop_loss)+' <small style="opacity:.6">('+sd+')</small></span>')+
      row('TP1','<span class="ppos">'+p$(t.take_profit_1)+'</span>')+
      row('TP2','<span style="color:var(--P)">'+p$(t.take_profit_2)+'</span>')+
      row('Current Price',p$(t.current_price||s.latest_price))+
      row('Unrealized P&L','<span class="'+cls+'">'+pp(pc2)+' ('+(pd>=0?'+':'')+'$'+Number(pd).toFixed(2)+')</span>')+
      row('Notional',p$(t.notional))+
      row('Open',ago(t.open_time))+
      row('Strategy','<span style="color:var(--B);font-size:11px">'+(t.strategy||'—')+'</span>')+
      row('Trade ID','<span style="color:var(--sub);font-size:11px">'+(t.trade_id||'—')+'</span>');
  } else {
    el('trade-badge').innerHTML='';
    tb.innerHTML='<div class="empty">No active trade</div>';
  }

  var sig = s.last_signal;
  if(sig && sig.signal_quality){
    el('sig-ts').textContent = ago(sig.timestamp);
    var hasLevels = sig.entry_price && sig.stop_loss;
    var levelsHtml = hasLevels
      ? row('Entry','<b>'+p$(sig.entry_price)+'</b>')+
        row('Stop Loss','<span class="pneg">'+p$(sig.stop_loss)+'</span>')+
        row('TP1 / TP2 / TP3',
          '<span class="ppos">'+p$(sig.take_profit_1)+'</span> / '+
          '<span style="color:var(--P)">'+p$(sig.take_profit_2)+'</span>' +
          (sig.take_profit_3?' / <span style="color:#2196f3">'+p$(sig.take_profit_3)+'</span>':''))+
        (sig.risk_per_unit?row('Risk / Unit',p$(sig.risk_per_unit)):'')+
        row('VWAP Dist','<span style="font-size:11px">'+(sig.vwap_distance||'—')+'</span>')+
        row('Max Hold',sig.max_hold_time||'—')
      : '<div style="color:var(--sub);font-size:11px;padding:6px 0">'+
        'NO_TRADE — pod did not produce directional edge this cycle</div>';
    el('sig-body').innerHTML=
      '<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">'+
        '<div class="sig-q" style="color:'+qc(sig.signal_quality)+'">'+sig.signal_quality+'</div>'+
        '<div>'+bdg(sig.bias)+'<br><small style="color:var(--sub)">'+(sig.signal_score||'—')+' aligned</small></div>'+
        '<div style="margin-left:auto;text-align:right">'+
          '<div style="color:var(--B);font-size:11px">'+(sig.strategy||'—')+'</div>'+
          '<div style="color:var(--sub);font-size:10px">'+(sig.session||'—')+' · '+(sig.risk_reward_t1||'—')+' R:R</div>'+
        '</div>'+
      '</div>'+
      levelsHtml+
      '<div class="sig-reason">'+(sig.entry_trigger||'')+(sig.invalidation?'\n\n⚠ Invalidation: '+sig.invalidation:'')+'</div>';
  } else {
    el('sig-ts').textContent='';
    el('sig-body').innerHTML='<div class="empty">No signal yet — click Analyze Now</div>';
  }

  var st2=s.stats||{};
  el('st-t').textContent=st2.total_trades||0;
  var wr=st2.win_rate;
  el('st-w').textContent=wr!==undefined?(wr*100).toFixed(0)+'%':'—';
  el('st-w').style.color=wr>=.5?'var(--G)':wr>0?'var(--R)':'';
  el('st-p').textContent=st2.total_pnl_pct!==undefined?pp(st2.total_pnl_pct):'—';
  el('st-p').style.color=(st2.total_pnl_pct||0)>0?'var(--G)':(st2.total_pnl_pct||0)<0?'var(--R)':'';
}

function row(l,v){ return '<div class="tr"><span class="tl">'+l+'</span><span class="tv">'+v+'</span></div>'; }

var lastPendingStatus = '';
function updatePending(p){
  var card = el('approval-card');
  if(!p || p.status!=='pending'){
    card.style.display='none';
    if(countdownInterval){ clearInterval(countdownInterval); countdownInterval=null; }
    return;
  }
  card.style.display='block';
  var sig=p.signal||{};
  el('ap-badge').innerHTML=bdg(sig.bias);

  var pc=sig.stop_loss&&sig.entry_price?Math.abs(((sig.stop_loss-sig.entry_price)/sig.entry_price)*100).toFixed(1)+'%':'—';
  el('ap-body').innerHTML=
    '<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">'+
      '<div style="font-size:26px;font-weight:800;color:'+qc(sig.signal_quality)+'">'+sig.signal_quality+'</div>'+
      '<div><div style="color:var(--B);font-size:12px">'+(sig.strategy||'—')+'</div>'+
           '<div style="color:var(--sub);font-size:11px">'+(sig.signal_score||'—')+' · '+(sig.session||'—')+'</div></div>'+
      '<div style="margin-left:auto;text-align:right">'+
        '<div style="font-size:11px">Notional: <b>'+p$(p.notional)+'</b></div>'+
        '<div style="font-size:11px">R:R <b>'+(sig.risk_reward_t1||'—')+'</b></div>'+
      '</div>'+
    '</div>'+
    row('Entry','<b>'+p$(sig.entry_price)+'</b>')+
    row('Stop Loss','<span class="pneg">'+p$(sig.stop_loss)+' ('+pc+')</span>')+
    row('TP1','<span class="ppos">'+p$(sig.take_profit_1)+'</span>')+
    row('TP2','<span style="color:var(--P)">'+p$(sig.take_profit_2)+'</span>')+
    (sig.take_profit_3?row('TP3','<span style="color:#2196f3">'+p$(sig.take_profit_3)+'</span>'):'')+
    (sig.risk_per_unit?row('Risk / Unit',p$(sig.risk_per_unit)):'')+
    (sig.invalidation?'<div style="font-size:11px;color:var(--sub);margin-top:6px;padding:4px;background:rgba(239,83,80,.06);border-left:2px solid #ef5350;border-radius:3px">⚠ '+sig.invalidation+'</div>':'')+
    (sig.entry_trigger?'<div style="font-size:11px;color:var(--sub);margin-top:6px;padding:4px;background:rgba(255,255,255,.04);border-radius:3px">↪ '+sig.entry_trigger+'</div>':'');

  if(p.status==='pending' && lastPendingStatus!=='pending'){
    startCountdown(p.expires_ts);
  }
  lastPendingStatus=p.status;
}

function updatePodReport(p){
  // BTC pod was added in Phase 4; show pod report for all 3 assets.
  var body = el('pod-body');
  var sumEl = el('pod-sum');
  var votes = (p && p.votes) || [];
  if(!votes.length){
    body.innerHTML = '<div class="empty">No vote yet — click Analyze Now</div>';
    sumEl.textContent = '—';
    return;
  }
  var sum = (p.pod_sum===undefined||p.pod_sum===null) ? 0 : Number(p.pod_sum);
  sumEl.textContent = 'Σ ' + (sum>=0?'+':'') + sum.toFixed(2);
  sumEl.style.color = sum>0.5?'var(--G)':sum<-0.5?'var(--R)':'var(--sub)';

  body.innerHTML = votes.map(function(v){
    var cls = v.direction==='LONG'?'long':v.direction==='SHORT'?'short':'neutral';
    var dirBadge = bdg(v.direction==='LONG'?'BULLISH':v.direction==='SHORT'?'BEARISH':'NEUTRAL');
    var conf = (v.confidence!==undefined ? (v.confidence*100).toFixed(0) : '0') + '%';
    var paramsHtml = renderPodMeta(v.metadata);
    return '<div class="pod-chip '+cls+'">'+
             '<div style="flex:1;min-width:0">'+
               '<div class="pn">'+v.name+'</div>'+
               '<div class="pi">'+(v.inspired_by||'').split('(')[0].trim()+'</div>'+
               (v.rationale?'<div class="pod-rationale">'+v.rationale+'</div>':'')+
               paramsHtml+
             '</div>'+
             '<div style="text-align:right;flex-shrink:0;margin-left:6px">'+dirBadge+
               '<div style="font-size:10px;color:var(--sub);margin-top:2px">'+conf+'</div>'+
             '</div>'+
           '</div>';
  }).join('');

  // NIFTY: extract FII/DII numbers from the fii_dii_flow vote's metadata
  if(currentAsset === 'nifty'){
    var flow = votes.filter(function(v){ return v.name === 'nifty_fii_dii_flow'; })[0];
    var meta = (flow && flow.metadata) ? flow.metadata : null;
    var fii = meta && meta.fii ? meta.fii : null;
    var dii = meta && meta.dii ? meta.dii : null;
    if(fii){
      el('fii-today').textContent = pCr(fii.today);
      el('fii-today').style.color = (fii.today >= 0) ? 'var(--G)' : 'var(--R)';
      el('fii-5d').textContent = pCr(fii.avg_5d);
      el('fii-5d').style.color = (fii.avg_5d >= 0) ? 'var(--G)' : 'var(--R)';
    }
    if(dii){
      el('dii-today').textContent = pCr(dii.today);
      el('dii-today').style.color = (dii.today >= 0) ? 'var(--G)' : 'var(--R)';
    }
  }
}

function updateMarketStatus(ms){
  if(currentAsset !== 'nifty' || !ms) return;
  var open = !!ms.is_open;
  var label = ms.label || (open ? 'open' : 'closed');
  var hm = el('h-market');
  hm.innerHTML = (open ? '<span class="dot dg"></span>' : '<span class="dot dr"></span>') + label;
  hm.style.color = open ? 'var(--G)' : 'var(--R)';
}

function escapeHtml(s){
  return String(s||'').replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}

function timeAgo(iso){
  if(!iso) return '';
  var s = Math.floor((Date.now() - new Date(iso))/1000);
  if(s < 60)   return s+'s';
  if(s < 3600) return Math.floor(s/60)+'m';
  if(s < 86400)return Math.floor(s/3600)+'h';
  return Math.floor(s/86400)+'d';
}

function updateNews(d){
  var list = el('news-list');
  var items = (d && d.items) || [];
  el('news-meta').textContent = items.length ? (items.length+' headlines · '+timeAgo(d.fetched_at)+' ago') : '';
  if(!items.length){
    list.innerHTML = '<div class="empty">No headlines available</div>';
    return;
  }
  list.innerHTML = items.slice(0, 14).map(function(it){
    return '<div class="news-item">'+
             '<a href="'+escapeHtml(it.link)+'" target="_blank" rel="noopener">'+escapeHtml(it.title)+'</a>'+
             '<div class="src"><span>'+escapeHtml(it.source||'')+'</span><span class="when">'+timeAgo(it.published)+'</span></div>'+
           '</div>';
  }).join('');
}

function updateOI(d){
  var body = el('oi-body');
  var meta = el('oi-meta');
  if(!d || !d.available){
    body.innerHTML = '<div class="empty">'+(d && d.reason ? escapeHtml(d.reason) : 'option-chain unavailable')+'</div>';
    meta.textContent = '';
    return;
  }
  meta.textContent = (d.expiry || '') + (d.spot ? ' · spot ₹'+Number(d.spot).toLocaleString('en-IN') : '');
  var pcr = Number(d.pcr||0);
  var pcrColor = pcr > 1.0 ? 'var(--G)' : pcr < 0.7 ? 'var(--R)' : 'var(--Y)';
  var mp = Number(d.max_pain||0), mpDist = Number(d.max_pain_dist||0);

  var rows = (d.atm_strikes||[]).map(function(s){
    var atm = Math.abs(s.strike - d.spot) < 50 ? ' atm' : '';
    function chgCls(v){ return v>0 ? 'oi-pos' : v<0 ? 'oi-neg' : ''; }
    function fmt(v){ return Number(v||0).toLocaleString('en-IN', {maximumFractionDigits:0}); }
    function fmtChg(v){ return (v>=0?'+':'') + fmt(v); }
    return '<tr>'+
      '<td class="'+chgCls(s.call_chg)+'">'+fmtChg(s.call_chg)+'</td>'+
      '<td>'+fmt(s.call_oi)+'</td>'+
      '<td class="strike'+atm+'">'+fmt(s.strike)+'</td>'+
      '<td>'+fmt(s.put_oi)+'</td>'+
      '<td class="'+chgCls(s.put_chg)+'">'+fmtChg(s.put_chg)+'</td>'+
      '</tr>';
  }).join('');

  body.innerHTML =
    '<div class="oi-stats">'+
      '<div class="sbox"><div class="n" style="color:'+pcrColor+'">'+pcr.toFixed(2)+'</div><div class="lb">PCR</div></div>'+
      '<div class="sbox"><div class="n">'+(mp?Number(mp).toLocaleString("en-IN"):'—')+'</div><div class="lb">Max Pain</div></div>'+
      '<div class="sbox"><div class="n" style="color:'+(mpDist>=0?'var(--G)':'var(--R)')+'">'+(mpDist>=0?'+':'')+mpDist.toFixed(2)+'%</div><div class="lb">vs Spot</div></div>'+
    '</div>'+
    '<table class="oi-table">'+
      '<thead><tr><th>ΔOI</th><th>Call OI</th><th>Strike</th><th>Put OI</th><th>ΔOI</th></tr></thead>'+
      '<tbody>'+rows+'</tbody>'+
    '</table>';
}

var lastLogCount=0;
function updateLogs(lines){
  if(!lines||lines.length===lastLogCount)return;
  lastLogCount=lines.length;
  var box=el('log-box');
  var atBot=box.scrollHeight-box.scrollTop-box.clientHeight<30;
  box.innerHTML=lines.map(function(line){
    var lo=line.toLowerCase(), cls='';
    if(lo.indexOf('warning')>=0||lo.indexOf('warn')>=0)cls='w';
    if(lo.indexOf('error')>=0||lo.indexOf('fail')>=0)cls='e';
    if(lo.indexOf('trade opened')>=0||lo.indexOf('order placed')>=0||lo.indexOf('trade closed')>=0||lo.indexOf('paper trade opened')>=0)cls='t';
    return'<div class="ll '+cls+'">'+line.replace(/</g,'&lt;').replace(/>/g,'&gt;')+'</div>';
  }).join('');
  if(atBot)box.scrollTop=box.scrollHeight;
}

var pollTick = 0;

function poll(){
  // Cheap, every 3s
  fetch(aq('/api/state')).then(function(r){return r.json();}).then(updateState).catch(function(e){console.log('state err',e);});
  fetch(aq('/api/pending')).then(function(r){return r.json();}).then(updatePending).catch(function(){});
  fetch(aq('/api/trade_zones')).then(function(r){return r.json();}).then(function(tz){
    if(tz && tz.available){
      renderTradeOverlay(tz);
      updateZones(tz);
    } else {
      renderTradeOverlay(null);
      updateZones(null);
    }
  }).catch(function(){});
  if(currentAsset==='nifty'){
    fetch(aq('/api/market_status')).then(function(r){return r.json();}).then(updateMarketStatus).catch(function(){});
  }

  // Logs every ~6s (every 2nd tick)
  if(pollTick % 2 === 0){
    fetch(aq('/api/logs?lines=80')).then(function(r){return r.json();}).then(function(d){updateLogs(d.lines||[]);}).catch(function(){});
  }
  // Pod report every ~9s (every 3rd tick)
  if(pollTick % 3 === 0){
    fetch(aq('/api/pod_report')).then(function(r){return r.json();}).then(updatePodReport).catch(function(){});
  }
  // News: first tick + every ~60s (every 20th tick = 60s)
  if(pollTick === 0 || pollTick % 20 === 0){
    fetch(aq('/api/news?limit=14')).then(function(r){return r.json();}).then(updateNews).catch(function(){});
  }
  // OI: NIFTY only, every ~30s (every 10th tick = 30s)
  if(currentAsset==='nifty' && (pollTick === 0 || pollTick % 10 === 0)){
    fetch('/api/option_chain?symbol=NIFTY').then(function(r){return r.json();}).then(updateOI).catch(function(){});
  }
  pollTick++;
}

// Initial chart load (BTC default) before first poll
onAssetChange();
// Poll cheap endpoints (state/zones/pending) every 3s.
// Heavy endpoints (logs/pod/news/oi) are gated by per-tick counters
// in poll() so we don't hammer them.
setInterval(poll, 3000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    print("=" * 60)
    print("  Multi-Asset Trading Dashboard - http://localhost:8080")
    print("  Assets: BTC/USD (Alpaca live)  |  XAU/USD (paper-sim pod)")
    print("          NIFTY 50 (paper-sim pod)")
    print("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=8080, reload=False)
