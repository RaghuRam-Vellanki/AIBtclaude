"""
dashboard.py
Web dashboard for the BTC/USD trading agent.
Shows live chart, current trade, AI signal, and logs.

Run alongside the bot:
  python dashboard.py
Then open: http://localhost:8080
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

load_dotenv()

app = FastAPI(title="BTC Trading Dashboard")

LOGS_DIR   = Path("logs")
STATE_FILE = LOGS_DIR / "state.json"
TRADES_FILE = LOGS_DIR / "trades_log.json"
AGENT_LOG  = LOGS_DIR / "agent.log"

# Simple in-memory chart cache (avoid hammering Alpaca on every page refresh)
_chart_cache: dict[str, Any] = {"data": [], "fetched_at": 0.0}
_CHART_TTL = 60  # seconds


# ── API endpoints ─────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return HTMLResponse(DASHBOARD_HTML)


@app.get("/api/state")
def get_state():
    if STATE_FILE.exists():
        try:
            return JSONResponse(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return JSONResponse({"bot_status": "offline", "latest_price": 0, "session": "—"})


@app.get("/api/trades")
def get_trades():
    if TRADES_FILE.exists():
        try:
            return JSONResponse(json.loads(TRADES_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return JSONResponse([])


@app.get("/api/logs")
def get_logs(lines: int = Query(default=60, le=200)):
    if not AGENT_LOG.exists():
        return JSONResponse({"lines": []})
    try:
        with open(AGENT_LOG, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        return JSONResponse({"lines": [l.rstrip() for l in all_lines[-lines:]]})
    except Exception as exc:
        return JSONResponse({"lines": [f"Error reading log: {exc}"]})


@app.get("/api/chart")
def get_chart():
    global _chart_cache
    now = time.time()
    if now - _chart_cache["fetched_at"] < _CHART_TTL and _chart_cache["data"]:
        return JSONResponse(_chart_cache["data"])

    try:
        from alpaca.data.historical import CryptoHistoricalDataClient
        from alpaca.data.requests import CryptoBarsRequest
        from alpaca.data.timeframe import TimeFrame

        client = CryptoHistoricalDataClient(
            api_key=os.getenv("ALPACA_API_KEY", ""),
            secret_key=os.getenv("ALPACA_SECRET_KEY", ""),
        )
        end   = datetime.now(timezone.utc)
        start = end - timedelta(days=5)

        req = CryptoBarsRequest(
            symbol_or_symbols="BTC/USD",
            timeframe=TimeFrame.Hour,
            start=start,
            end=end,
        )
        bars = client.get_crypto_bars(req)
        df   = bars.df

        if hasattr(df.index, "levels"):
            df = df.loc["BTC/USD"]
        df = df.reset_index()

        candles = []
        for _, row in df.iterrows():
            ts = row["timestamp"]
            if hasattr(ts, "timestamp"):
                t = int(ts.timestamp())
            else:
                t = int(ts)
            candles.append({
                "time":  t,
                "open":  float(row["open"]),
                "high":  float(row["high"]),
                "low":   float(row["low"]),
                "close": float(row["close"]),
            })

        _chart_cache = {"data": candles, "fetched_at": now}
        return JSONResponse(candles)

    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── HTML dashboard ─────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BTC/USD Trading Agent</title>
<script src="https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:       #0d0f14;
    --surface:  #151820;
    --border:   #252a35;
    --muted:    #4a5568;
    --text:     #e2e8f0;
    --subtext:  #718096;
    --green:    #48bb78;
    --red:      #f56565;
    --yellow:   #ecc94b;
    --blue:     #63b3ed;
    --purple:   #b794f4;
  }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Courier New', monospace;
    font-size: 13px;
    min-height: 100vh;
  }

  /* ── Header ── */
  header {
    display: flex;
    align-items: center;
    gap: 24px;
    padding: 10px 20px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
  }
  .logo { font-size: 15px; font-weight: bold; color: var(--blue); letter-spacing: 1px; }
  .price-big { font-size: 22px; font-weight: bold; }
  .badge {
    padding: 2px 10px; border-radius: 12px; font-size: 11px; font-weight: bold;
  }
  .badge-green  { background: rgba(72,187,120,.2); color: var(--green); border: 1px solid var(--green); }
  .badge-red    { background: rgba(245,101,101,.2); color: var(--red);   border: 1px solid var(--red);  }
  .badge-yellow { background: rgba(236,201,75,.2);  color: var(--yellow);border: 1px solid var(--yellow);}
  .badge-blue   { background: rgba(99,179,237,.2);  color: var(--blue);  border: 1px solid var(--blue); }
  .badge-muted  { background: rgba(74,85,104,.2);   color: var(--muted); border: 1px solid var(--muted);}

  .header-stat { display: flex; flex-direction: column; }
  .header-stat .label { font-size: 10px; color: var(--subtext); text-transform: uppercase; }
  .header-stat .value { font-size: 13px; font-weight: bold; }

  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 5px; }
  .dot-green  { background: var(--green); box-shadow: 0 0 6px var(--green); animation: pulse 2s infinite; }
  .dot-red    { background: var(--red); }
  .dot-yellow { background: var(--yellow); animation: pulse 2s infinite; }

  @keyframes pulse {
    0%, 100% { opacity: 1; } 50% { opacity: .4; }
  }

  /* ── Layout ── */
  .main-grid {
    display: grid;
    grid-template-columns: 1fr 340px;
    grid-template-rows: auto auto;
    gap: 12px;
    padding: 12px;
    height: calc(100vh - 58px);
  }

  /* ── Cards ── */
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 14px;
    overflow: hidden;
  }
  .card-title {
    font-size: 11px; font-weight: bold; color: var(--subtext);
    text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px;
    display: flex; align-items: center; justify-content: space-between;
  }

  /* ── Chart ── */
  #chart-card {
    grid-row: 1 / 3;
    display: flex;
    flex-direction: column;
  }
  #chart-container {
    flex: 1;
    min-height: 0;
  }

  /* ── Right panel ── */
  .right-panel {
    display: flex;
    flex-direction: column;
    gap: 12px;
    overflow-y: auto;
  }

  /* ── Trade panel ── */
  .trade-row {
    display: flex; justify-content: space-between;
    padding: 5px 0; border-bottom: 1px solid var(--border);
  }
  .trade-row:last-child { border-bottom: none; }
  .trade-label { color: var(--subtext); }
  .trade-value { font-weight: bold; text-align: right; }

  .no-trade {
    text-align: center; color: var(--muted);
    padding: 20px 0; font-size: 12px;
  }

  .pnl-pos { color: var(--green); }
  .pnl-neg { color: var(--red); }

  /* ── Signal ── */
  .signal-quality {
    font-size: 28px; font-weight: bold; text-align: center;
    padding: 8px 0 4px;
  }
  .signal-reasoning {
    font-size: 11px; color: var(--subtext); line-height: 1.6;
    max-height: 120px; overflow-y: auto;
    border-top: 1px solid var(--border); margin-top: 8px; padding-top: 8px;
    white-space: pre-wrap; word-break: break-word;
  }

  /* ── Stats ── */
  .stats-grid {
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px;
  }
  .stat-box {
    background: var(--bg); border-radius: 4px; padding: 8px;
    text-align: center; border: 1px solid var(--border);
  }
  .stat-box .num { font-size: 20px; font-weight: bold; }
  .stat-box .lbl { font-size: 10px; color: var(--subtext); margin-top: 2px; }

  /* ── Logs ── */
  #log-card {
    max-height: 200px;
  }
  #log-container {
    height: 140px; overflow-y: auto;
    font-size: 11px; line-height: 1.7;
    color: var(--subtext);
  }
  #log-container .log-line { padding: 1px 0; border-bottom: 1px solid rgba(37,42,53,.5); }
  #log-container .log-line.warn  { color: var(--yellow); }
  #log-container .log-line.error { color: var(--red); }
  #log-container .log-line.info  { color: var(--subtext); }
  #log-container .log-line.trade { color: var(--green); font-weight: bold; }

  .refresh-time { font-size: 10px; color: var(--muted); }

  /* ── Scrollbars ── */
  ::-webkit-scrollbar { width: 4px; height: 4px; }
  ::-webkit-scrollbar-track { background: var(--bg); }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
</style>
</head>
<body>

<!-- Header -->
<header>
  <div class="logo">&#9651; BTC/USD AGENT</div>

  <div class="header-stat">
    <span class="label">Price</span>
    <span class="value price-big" id="h-price">—</span>
  </div>

  <div class="header-stat">
    <span class="label">Session</span>
    <span class="value" id="h-session">—</span>
  </div>

  <div class="header-stat">
    <span class="label">Account</span>
    <span class="value" id="h-balance">—</span>
  </div>

  <div class="header-stat">
    <span class="label">Daily P&L</span>
    <span class="value" id="h-pnl">—</span>
  </div>

  <div class="header-stat">
    <span class="label">Bot Status</span>
    <span class="value" id="h-status"><span class="dot dot-yellow"></span>connecting...</span>
  </div>

  <span class="refresh-time" id="h-refreshed">—</span>
</header>

<!-- Main grid -->
<div class="main-grid">

  <!-- Chart -->
  <div class="card" id="chart-card">
    <div class="card-title">
      <span>BTC/USD — 1H Chart (5 days)</span>
      <span id="chart-status" style="color:var(--muted);font-size:10px;">loading...</span>
    </div>
    <div id="chart-container"></div>
  </div>

  <!-- Right panel -->
  <div class="right-panel">

    <!-- Current Trade -->
    <div class="card" id="trade-card">
      <div class="card-title">
        <span>Current Trade</span>
        <span id="trade-badge"></span>
      </div>
      <div id="trade-body">
        <div class="no-trade">No active trade</div>
      </div>
    </div>

    <!-- Last AI Signal -->
    <div class="card" id="signal-card">
      <div class="card-title">
        <span>Last AI Signal</span>
        <span id="signal-time" style="color:var(--muted);font-size:10px;">—</span>
      </div>
      <div id="signal-body">
        <div class="no-trade">No signal yet</div>
      </div>
    </div>

    <!-- Stats -->
    <div class="card">
      <div class="card-title">Session Stats</div>
      <div class="stats-grid">
        <div class="stat-box">
          <div class="num" id="stat-trades">0</div>
          <div class="lbl">Trades</div>
        </div>
        <div class="stat-box">
          <div class="num" id="stat-wr">—</div>
          <div class="lbl">Win Rate</div>
        </div>
        <div class="stat-box">
          <div class="num" id="stat-pnl">—</div>
          <div class="lbl">Total P&L</div>
        </div>
      </div>
    </div>

    <!-- Logs -->
    <div class="card" id="log-card">
      <div class="card-title">Live Logs</div>
      <div id="log-container"></div>
    </div>

  </div>
</div>

<script>
// ── Chart setup ────────────────────────────────────────────────────────────
const chartEl = document.getElementById('chart-container');
const chart = LightweightCharts.createChart(chartEl, {
  layout: { background: { color: '#151820' }, textColor: '#718096' },
  grid:   { vertLines: { color: '#252a35' }, horzLines: { color: '#252a35' } },
  crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  rightPriceScale: { borderColor: '#252a35' },
  timeScale: { borderColor: '#252a35', timeVisible: true },
  width:  chartEl.offsetWidth,
  height: chartEl.offsetHeight || 400,
});

const candleSeries = chart.addCandlestickSeries({
  upColor:   '#48bb78', downColor: '#f56565',
  borderUpColor: '#48bb78', borderDownColor: '#f56565',
  wickUpColor: '#48bb78', wickDownColor: '#f56565',
});

// Entry/SL/TP lines
let entryLine = null, slLine = null, tp1Line = null, tp2Line = null;

function clearTradelines() {
  [entryLine, slLine, tp1Line, tp2Line].forEach(l => { if (l) try { candleSeries.removePriceLine(l); } catch(e){} });
  entryLine = slLine = tp1Line = tp2Line = null;
}

function drawTradelines(trade) {
  clearTradelines();
  if (!trade) return;
  const opts = (color, label) => ({ price: 0, color, lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: label });

  if (trade.entry_price) {
    entryLine = candleSeries.createPriceLine({ ...opts('#63b3ed','Entry'), price: trade.entry_price });
  }
  if (trade.stop_loss) {
    slLine = candleSeries.createPriceLine({ ...opts('#f56565','SL'), price: trade.stop_loss });
  }
  if (trade.take_profit_1) {
    tp1Line = candleSeries.createPriceLine({ ...opts('#48bb78','TP1'), price: trade.take_profit_1 });
  }
  if (trade.take_profit_2) {
    tp2Line = candleSeries.createPriceLine({ ...opts('#b794f4','TP2'), price: trade.take_profit_2 });
  }
}

// Resize chart with window
new ResizeObserver(() => {
  chart.applyOptions({ width: chartEl.offsetWidth, height: chartEl.offsetHeight });
}).observe(chartEl);

// ── Helpers ────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);

function fmtPrice(p) {
  if (!p) return '—';
  return '$' + Number(p).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtPct(p) {
  if (p === null || p === undefined) return '—';
  const v = Number(p);
  return (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
}

function colorPct(el, v) {
  el.className = 'value ' + (v > 0 ? 'pnl-pos' : v < 0 ? 'pnl-neg' : '');
}

function qualityColor(q) {
  if (q === 'A+') return '#48bb78';
  if (q === 'A')  return '#63b3ed';
  if (q === 'B')  return '#ecc94b';
  return '#718096';
}

function badgeHTML(bias) {
  if (bias === 'BULLISH') return '<span class="badge badge-green">LONG</span>';
  if (bias === 'BEARISH') return '<span class="badge badge-red">SHORT</span>';
  return '<span class="badge badge-muted">' + (bias||'—') + '</span>';
}

function timeSince(isoStr) {
  if (!isoStr) return '—';
  const diff = Math.floor((Date.now() - new Date(isoStr)) / 1000);
  if (diff < 60)  return diff + 's ago';
  if (diff < 3600) return Math.floor(diff/60) + 'm ago';
  return Math.floor(diff/3600) + 'h ago';
}

// ── State updater ──────────────────────────────────────────────────────────
function updateState(s) {
  // Header
  const price = s.latest_price || 0;
  $('h-price').textContent = price ? fmtPrice(price) : '—';
  $('h-session').textContent = s.session || '—';
  $('h-balance').textContent = s.account ? fmtPrice(s.account.balance) : '—';

  const pnl = s.daily_pnl_pct;
  const pnlEl = $('h-pnl');
  pnlEl.textContent = pnl !== undefined ? fmtPct(pnl) : '—';
  pnlEl.style.color = pnl > 0 ? 'var(--green)' : pnl < 0 ? 'var(--red)' : '';

  // Bot status
  const statusEl = $('h-status');
  if (s.bot_status === 'running') {
    statusEl.innerHTML = '<span class="dot dot-green"></span>running';
    statusEl.style.color = 'var(--green)';
  } else if (s.bot_status === 'halted') {
    statusEl.innerHTML = '<span class="dot dot-red"></span>halted';
    statusEl.style.color = 'var(--red)';
  } else {
    statusEl.innerHTML = '<span class="dot dot-yellow"></span>' + (s.bot_status || 'offline');
    statusEl.style.color = 'var(--yellow)';
  }

  // Trade panel
  const trade = s.current_trade;
  const tradeBody = $('trade-body');
  const tradeBadge = $('trade-badge');

  if (trade && trade.entry_price) {
    tradeBadge.innerHTML = badgeHTML(trade.bias);
    const plPct  = trade.unrealized_pl_pct || 0;
    const plCls  = plPct >= 0 ? 'pnl-pos' : 'pnl-neg';
    const plDoll = trade.unrealized_pl || 0;
    const distToSL = trade.entry_price && trade.stop_loss
      ? Math.abs(((trade.stop_loss - trade.entry_price) / trade.entry_price) * 100).toFixed(1) + '%'
      : '—';

    tradeBody.innerHTML = `
      <div class="trade-row"><span class="trade-label">Entry</span><span class="trade-value">${fmtPrice(trade.entry_price)}</span></div>
      <div class="trade-row"><span class="trade-label">Stop Loss</span><span class="trade-value" style="color:var(--red)">${fmtPrice(trade.stop_loss)} <small>(${distToSL})</small></span></div>
      <div class="trade-row"><span class="trade-label">TP1</span><span class="trade-value" style="color:var(--green)">${fmtPrice(trade.take_profit_1)}</span></div>
      <div class="trade-row"><span class="trade-label">TP2</span><span class="trade-value" style="color:var(--purple)">${fmtPrice(trade.take_profit_2)}</span></div>
      <div class="trade-row"><span class="trade-label">Current Price</span><span class="trade-value">${fmtPrice(trade.current_price || price)}</span></div>
      <div class="trade-row"><span class="trade-label">Unrealized P&L</span>
        <span class="trade-value ${plCls}">${fmtPct(plPct)} (${plDoll >= 0 ? '+' : ''}$${Number(plDoll).toFixed(2)})</span></div>
      <div class="trade-row"><span class="trade-label">Notional</span><span class="trade-value">${fmtPrice(trade.notional)}</span></div>
      <div class="trade-row"><span class="trade-label">Opened</span><span class="trade-value">${timeSince(trade.open_time)}</span></div>
      <div class="trade-row"><span class="trade-label">Trade ID</span><span class="trade-value" style="color:var(--subtext)">${trade.trade_id || '—'}</span></div>
    `;
    drawTradelines(trade);
  } else {
    tradeBadge.innerHTML = '';
    tradeBody.innerHTML = '<div class="no-trade">No active trade</div>';
    clearTradelines();
  }

  // Signal panel
  const sig = s.last_signal;
  const sigBody = $('signal-body');
  const sigTime = $('signal-time');

  if (sig && sig.signal_quality) {
    sigTime.textContent = timeSince(sig.timestamp);
    const qColor = qualityColor(sig.signal_quality);
    sigBody.innerHTML = `
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px;">
        <div class="signal-quality" style="color:${qColor}">${sig.signal_quality}</div>
        <div>
          ${badgeHTML(sig.bias)}<br>
          <small style="color:var(--subtext)">${sig.signal_score || '—'} signals</small>
        </div>
      </div>
      <div class="trade-row"><span class="trade-label">Strategy</span><span class="trade-value" style="color:var(--blue)">${sig.strategy || '—'}</span></div>
      <div class="trade-row"><span class="trade-label">Entry</span><span class="trade-value">${fmtPrice(sig.entry_price)}</span></div>
      <div class="trade-row"><span class="trade-label">R:R (T1)</span><span class="trade-value">${sig.risk_reward_t1 || '—'}</span></div>
      <div class="trade-row"><span class="trade-label">Session</span><span class="trade-value">${sig.session || '—'}</span></div>
      <div class="trade-row"><span class="trade-label">VWAP Dist.</span><span class="trade-value">${sig.vwap_distance || '—'}</span></div>
      <div class="trade-row"><span class="trade-label">Max Hold</span><span class="trade-value">${sig.max_hold_time || '—'}</span></div>
      <div class="signal-reasoning">${(sig.entry_trigger || '') + (sig.invalidation ? '\n\nInvalidation: ' + sig.invalidation : '')}</div>
    `;
  } else {
    sigTime.textContent = '—';
    sigBody.innerHTML = '<div class="no-trade">No signal yet</div>';
  }

  // Stats
  const stats = s.stats || {};
  $('stat-trades').textContent = stats.total_trades || 0;
  const wr = stats.win_rate;
  const wrEl = $('stat-wr');
  wrEl.textContent = wr !== undefined ? (wr * 100).toFixed(0) + '%' : '—';
  wrEl.style.color = wr >= 0.5 ? 'var(--green)' : wr > 0 ? 'var(--red)' : '';
  const spnl = stats.total_pnl_pct;
  const spnlEl = $('stat-pnl');
  spnlEl.textContent = spnl !== undefined ? fmtPct(spnl) : '—';
  spnlEl.style.color = spnl > 0 ? 'var(--green)' : spnl < 0 ? 'var(--red)' : '';

  $('h-refreshed').textContent = 'updated ' + new Date().toLocaleTimeString();
}

// ── Log updater ────────────────────────────────────────────────────────────
let lastLogCount = 0;

function updateLogs(lines) {
  if (lines.length === lastLogCount) return;
  lastLogCount = lines.length;

  const container = $('log-container');
  const wasAtBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 30;

  container.innerHTML = lines.map(line => {
    let cls = 'info';
    const lo = line.toLowerCase();
    if (lo.includes('warning') || lo.includes('warn'))   cls = 'warn';
    if (lo.includes('error') || lo.includes('failed'))   cls = 'error';
    if (lo.includes('trade opened') || lo.includes('trade closed') || lo.includes('order placed')) cls = 'trade';
    const escaped = line.replace(/</g,'&lt;').replace(/>/g,'&gt;');
    return `<div class="log-line ${cls}">${escaped}</div>`;
  }).join('');

  if (wasAtBottom) container.scrollTop = container.scrollHeight;
}

// ── Chart loader ───────────────────────────────────────────────────────────
let chartLoaded = false;
function loadChart() {
  fetch('/api/chart')
    .then(r => r.json())
    .then(data => {
      if (data.error) { $('chart-status').textContent = 'error: ' + data.error; return; }
      if (!Array.isArray(data) || data.length === 0) { $('chart-status').textContent = 'no data'; return; }
      candleSeries.setData(data);
      chart.timeScale().fitContent();
      $('chart-status').textContent = data.length + ' candles (1H)';
      chartLoaded = true;
    })
    .catch(e => { $('chart-status').textContent = 'fetch error'; });
}

// ── Polling ────────────────────────────────────────────────────────────────
function poll() {
  fetch('/api/state')
    .then(r => r.json())
    .then(updateState)
    .catch(() => {});

  fetch('/api/logs?lines=60')
    .then(r => r.json())
    .then(d => updateLogs(d.lines || []))
    .catch(() => {});
}

// Refresh chart every 2 minutes
setInterval(loadChart, 120_000);
// Poll state + logs every 8 seconds
setInterval(poll, 8_000);

// Initial load
loadChart();
poll();
</script>
</body>
</html>"""


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 50)
    print("  BTC Trading Dashboard")
    print("  Open: http://localhost:8080")
    print("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=8080, reload=False)
