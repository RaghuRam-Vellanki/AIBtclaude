"""
dashboard.py
Web dashboard for the BTC/USD trading agent.
Shows TradingView live chart, current trade, AI signal, and logs.

Run alongside the bot:
  python dashboard.py
Then open: http://localhost:8080
"""
from __future__ import annotations

import json
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

load_dotenv()

app = FastAPI(title="BTC Trading Dashboard")

LOGS_DIR    = Path("logs")
STATE_FILE  = LOGS_DIR / "state.json"
TRADES_FILE = LOGS_DIR / "trades_log.json"
AGENT_LOG   = LOGS_DIR / "agent.log"


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


# ── HTML dashboard ─────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BTC/USD Trading Agent</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:      #0d0f14;
    --surface: #131722;
    --border:  #2a2e39;
    --text:    #d1d4dc;
    --subtext: #787b86;
    --green:   #26a69a;
    --red:     #ef5350;
    --yellow:  #f9a825;
    --blue:    #2196f3;
    --purple:  #9c27b0;
  }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', monospace;
    font-size: 13px;
    height: 100vh;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }

  /* ── Header ── */
  header {
    display: flex;
    align-items: center;
    gap: 20px;
    padding: 8px 16px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
    flex-wrap: wrap;
  }
  .logo {
    font-size: 14px;
    font-weight: 700;
    color: var(--blue);
    letter-spacing: 1px;
    white-space: nowrap;
  }
  .hstat { display: flex; flex-direction: column; gap: 1px; }
  .hstat .lbl { font-size: 9px; color: var(--subtext); text-transform: uppercase; letter-spacing: .5px; }
  .hstat .val { font-size: 13px; font-weight: 600; }
  .price-val  { font-size: 20px !important; }

  .dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; margin-right: 4px; }
  .dot-green  { background: var(--green);  animation: blink 2s infinite; }
  .dot-yellow { background: var(--yellow); animation: blink 2s infinite; }
  .dot-red    { background: var(--red); }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:.3} }

  .badge {
    padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 700;
  }
  .bg { background:rgba(38,166,154,.15); color:var(--green); border:1px solid var(--green); }
  .br { background:rgba(239,83,80,.15);  color:var(--red);   border:1px solid var(--red);  }
  .by { background:rgba(249,168,37,.15); color:var(--yellow);border:1px solid var(--yellow);}
  .bb { background:rgba(33,150,243,.15); color:var(--blue);  border:1px solid var(--blue); }
  .bm { background:rgba(120,123,134,.15);color:var(--subtext);border:1px solid var(--subtext);}

  .ppos { color: var(--green); }
  .pneg { color: var(--red); }

  /* ── Main layout ── */
  .main {
    display: grid;
    grid-template-columns: 1fr 320px;
    flex: 1;
    min-height: 0;
    gap: 0;
  }

  /* ── Chart side ── */
  .chart-side {
    display: flex;
    flex-direction: column;
    min-height: 0;
    border-right: 1px solid var(--border);
  }

  .chart-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 6px 12px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  .chart-toolbar span { font-size: 11px; color: var(--subtext); }

  /* Trade level overlay badges */
  .level-bar {
    display: flex;
    gap: 10px;
    padding: 4px 12px;
    background: var(--bg);
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
    flex-wrap: wrap;
  }
  .level-pill {
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 3px;
    font-weight: 600;
  }
  .lp-entry { background:rgba(33,150,243,.2);  color:#64b5f6; }
  .lp-sl    { background:rgba(239,83,80,.2);   color:#ef9a9a; }
  .lp-tp1   { background:rgba(38,166,154,.2);  color:#80cbc4; }
  .lp-tp2   { background:rgba(156,39,176,.2);  color:#ce93d8; }
  .lp-none  { color: var(--subtext); font-style: italic; }

  /* TradingView iframe */
  #tv-container {
    flex: 1;
    min-height: 0;
  }
  #tv-container iframe,
  #tv-widget {
    width: 100%;
    height: 100%;
    border: none;
  }

  /* ── Right panel ── */
  .right {
    display: flex;
    flex-direction: column;
    overflow-y: auto;
    background: var(--bg);
  }

  .card {
    border-bottom: 1px solid var(--border);
    padding: 12px;
  }
  .card-title {
    font-size: 10px;
    font-weight: 700;
    color: var(--subtext);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 10px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  /* Trade rows */
  .trow {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 4px 0;
    border-bottom: 1px solid rgba(42,46,57,.6);
  }
  .trow:last-child { border-bottom: none; }
  .trow .tl { color: var(--subtext); font-size: 12px; }
  .trow .tv { font-weight: 600; font-size: 12px; text-align: right; }

  .empty { text-align:center; color:var(--subtext); padding:16px 0; font-size:12px; }

  /* Signal quality */
  .sig-top {
    display: flex; align-items: center; gap: 12px; margin-bottom: 8px;
  }
  .sig-q { font-size: 30px; font-weight: 800; }
  .sig-reasoning {
    font-size: 11px; color: var(--subtext); line-height: 1.55;
    max-height: 90px; overflow-y: auto;
    border-top: 1px solid var(--border);
    margin-top: 8px; padding-top: 8px;
    white-space: pre-wrap; word-break: break-word;
  }

  /* Stats */
  .stats-row {
    display: grid; grid-template-columns: repeat(3,1fr); gap: 6px;
  }
  .stat {
    background: var(--surface); border-radius: 4px; padding: 8px 4px;
    text-align: center; border: 1px solid var(--border);
  }
  .stat .n { font-size: 18px; font-weight: 700; }
  .stat .l { font-size: 10px; color: var(--subtext); margin-top: 2px; }

  /* Logs */
  #log-box {
    height: 130px; overflow-y: auto;
    font-size: 11px; line-height: 1.65; color: var(--subtext);
  }
  .ll { padding: 1px 0; border-bottom: 1px solid rgba(42,46,57,.4); }
  .ll.w { color: var(--yellow); }
  .ll.e { color: var(--red); }
  .ll.t { color: var(--green); font-weight: 600; }

  ::-webkit-scrollbar { width: 3px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
</style>
</head>
<body>

<!-- Header -->
<header>
  <div class="logo">&#9651; BTC/USD AGENT</div>

  <div class="hstat">
    <span class="lbl">Price</span>
    <span class="val price-val" id="h-price">—</span>
  </div>
  <div class="hstat">
    <span class="lbl">Session</span>
    <span class="val" id="h-session">—</span>
  </div>
  <div class="hstat">
    <span class="lbl">Account</span>
    <span class="val" id="h-balance">—</span>
  </div>
  <div class="hstat">
    <span class="lbl">Daily P&amp;L</span>
    <span class="val" id="h-pnl">—</span>
  </div>
  <div class="hstat">
    <span class="lbl">Bot Status</span>
    <span class="val" id="h-status"><span class="dot dot-yellow"></span>connecting...</span>
  </div>
  <div style="margin-left:auto;font-size:10px;color:var(--subtext)" id="h-ts">—</div>
</header>

<!-- Main -->
<div class="main">

  <!-- Left: Chart -->
  <div class="chart-side">
    <div class="chart-toolbar">
      <span>BTC/USD &nbsp;·&nbsp; TradingView Live Chart &nbsp;·&nbsp; 1H</span>
      <span id="tv-status">loading...</span>
    </div>

    <!-- Trade level pills shown above the chart -->
    <div class="level-bar" id="level-bar">
      <span class="lp-none">No active trade</span>
    </div>

    <!-- TradingView widget -->
    <div id="tv-container">
      <div id="tradingview_widget"></div>
    </div>
  </div>

  <!-- Right: Panels -->
  <div class="right">

    <!-- Current Trade -->
    <div class="card">
      <div class="card-title">
        Current Trade
        <span id="trade-badge"></span>
      </div>
      <div id="trade-body"><div class="empty">No active trade</div></div>
    </div>

    <!-- Last AI Signal -->
    <div class="card">
      <div class="card-title">
        Last AI Signal
        <span id="sig-time" style="color:var(--subtext);font-size:10px">—</span>
      </div>
      <div id="sig-body"><div class="empty">No signal yet</div></div>
    </div>

    <!-- Stats -->
    <div class="card">
      <div class="card-title">Session Stats</div>
      <div class="stats-row">
        <div class="stat"><div class="n" id="st-trades">0</div><div class="l">Trades</div></div>
        <div class="stat"><div class="n" id="st-wr">—</div><div class="l">Win Rate</div></div>
        <div class="stat"><div class="n" id="st-pnl">—</div><div class="l">Total P&amp;L</div></div>
      </div>
    </div>

    <!-- Logs -->
    <div class="card" style="flex:1">
      <div class="card-title">Live Logs</div>
      <div id="log-box"></div>
    </div>

  </div>
</div>

<!-- TradingView widget script (loads asynchronously — safe) -->
<script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
<script>
// ── Helpers ────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);

function fmt$(p) {
  if (!p && p !== 0) return '—';
  return '$' + Number(p).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
}
function fmtPct(p) {
  if (p === null || p === undefined) return '—';
  const v = Number(p);
  return (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
}
function badge(bias) {
  if (bias === 'BULLISH') return '<span class="badge bg">LONG</span>';
  if (bias === 'BEARISH') return '<span class="badge br">SHORT</span>';
  return '<span class="badge bm">' + (bias||'—') + '</span>';
}
function timeSince(iso) {
  if (!iso) return '—';
  const s = Math.floor((Date.now() - new Date(iso)) / 1000);
  if (s < 60)   return s + 's ago';
  if (s < 3600) return Math.floor(s/60) + 'm ago';
  return Math.floor(s/3600) + 'h ago';
}
function qColor(q) {
  return q==='A+'?'#26a69a': q==='A'?'#2196f3': q==='B'?'#f9a825':'#787b86';
}

// ── TradingView widget ─────────────────────────────────────────────────────
function initChart() {
  try {
    new TradingView.widget({
      autosize:             true,
      symbol:               "COINBASE:BTCUSD",
      interval:             "60",
      timezone:             "Asia/Kolkata",
      theme:                "dark",
      style:                "1",
      locale:               "en",
      toolbar_bg:           "#131722",
      enable_publishing:    false,
      allow_symbol_change:  false,
      hide_top_toolbar:     false,
      hide_legend:          false,
      save_image:           false,
      container_id:         "tradingview_widget",
    });
    $('tv-status').textContent = 'live';
    $('tv-status').style.color = '#26a69a';
  } catch(e) {
    $('tv-status').textContent = 'chart error: ' + e.message;
  }
}

// Init chart after TradingView script loads
if (typeof TradingView !== 'undefined') {
  initChart();
} else {
  // Script is async — wait for it
  document.querySelector('script[src*="tradingview"]').addEventListener('load', initChart);
}

// ── State updater ──────────────────────────────────────────────────────────
function updateState(s) {
  // Header
  const price = s.latest_price || 0;
  $('h-price').textContent = price ? fmt$(price) : '—';
  $('h-session').textContent = (s.session || '—').toUpperCase();
  $('h-balance').textContent = s.account ? fmt$(s.account.balance) : '—';

  const pnl = s.daily_pnl_pct;
  const pe = $('h-pnl');
  pe.textContent = pnl !== undefined ? fmtPct(pnl) : '—';
  pe.className   = 'val ' + (pnl > 0 ? 'ppos' : pnl < 0 ? 'pneg' : '');

  const se = $('h-status');
  if (s.bot_status === 'running') {
    se.innerHTML = '<span class="dot dot-green"></span>running';
    se.style.color = 'var(--green)';
  } else if (s.bot_status === 'halted') {
    se.innerHTML = '<span class="dot dot-red"></span>halted';
    se.style.color = 'var(--red)';
  } else {
    se.innerHTML = '<span class="dot dot-yellow"></span>' + (s.bot_status||'offline');
    se.style.color = 'var(--yellow)';
  }
  $('h-ts').textContent = 'updated ' + new Date().toLocaleTimeString();

  // Level pills (show entry/SL/TP above chart)
  const t = s.current_trade;
  const lb = $('level-bar');
  if (t && t.entry_price) {
    const plPct = t.unrealized_pl_pct || 0;
    const plCls = plPct >= 0 ? 'ppos' : 'pneg';
    lb.innerHTML =
      `<span class="level-pill lp-entry">Entry ${fmt$(t.entry_price)}</span>` +
      `<span class="level-pill lp-sl">SL ${fmt$(t.stop_loss)}</span>` +
      `<span class="level-pill lp-tp1">TP1 ${fmt$(t.take_profit_1)}</span>` +
      `<span class="level-pill lp-tp2">TP2 ${fmt$(t.take_profit_2)}</span>` +
      `<span class="level-pill" style="background:rgba(255,255,255,.05)">` +
        `P&amp;L <span class="${plCls}">${fmtPct(plPct)}</span></span>`;
  } else {
    lb.innerHTML = '<span class="lp-none">No active trade</span>';
  }

  // Trade panel
  const tb = $('trade-body');
  const tbadge = $('trade-badge');
  if (t && t.entry_price) {
    tbadge.innerHTML = badge(t.bias);
    const plPct  = t.unrealized_pl_pct || 0;
    const plDoll = t.unrealized_pl || 0;
    const plCls  = plPct >= 0 ? 'ppos' : 'pneg';
    const slDist = t.entry_price && t.stop_loss
      ? Math.abs(((t.stop_loss - t.entry_price)/t.entry_price)*100).toFixed(1)+'%' : '—';
    tb.innerHTML = `
      <div class="trow"><span class="tl">Entry</span><span class="tv">${fmt$(t.entry_price)}</span></div>
      <div class="trow"><span class="tl">Stop Loss</span><span class="tv pneg">${fmt$(t.stop_loss)} <small style="opacity:.6">(${slDist})</small></span></div>
      <div class="trow"><span class="tl">TP1</span><span class="tv ppos">${fmt$(t.take_profit_1)}</span></div>
      <div class="trow"><span class="tl">TP2</span><span class="tv" style="color:var(--purple)">${fmt$(t.take_profit_2)}</span></div>
      <div class="trow"><span class="tl">Current Price</span><span class="tv">${fmt$(t.current_price||price)}</span></div>
      <div class="trow"><span class="tl">Unrealized P&amp;L</span>
        <span class="tv ${plCls}">${fmtPct(plPct)} (${plDoll>=0?'+':''}$${Number(plDoll).toFixed(2)})</span></div>
      <div class="trow"><span class="tl">Notional</span><span class="tv">${fmt$(t.notional)}</span></div>
      <div class="trow"><span class="tl">Open</span><span class="tv" style="color:var(--subtext)">${timeSince(t.open_time)}</span></div>
      <div class="trow"><span class="tl">Trade ID</span><span class="tv" style="color:var(--subtext);font-size:11px">${t.trade_id||'—'}</span></div>
      <div class="trow"><span class="tl">Strategy</span><span class="tv" style="color:var(--blue);font-size:11px">${t.strategy||'—'}</span></div>
    `;
  } else {
    tbadge.innerHTML = '';
    tb.innerHTML = '<div class="empty">No active trade</div>';
  }

  // Signal panel
  const sig = s.last_signal;
  const sb  = $('sig-body');
  $('sig-time').textContent = timeSince(sig && sig.timestamp);
  if (sig && sig.signal_quality) {
    const qc = qColor(sig.signal_quality);
    sb.innerHTML = `
      <div class="sig-top">
        <div class="sig-q" style="color:${qc}">${sig.signal_quality}</div>
        <div>${badge(sig.bias)}<br><small style="color:var(--subtext)">${sig.signal_score||'—'} aligned</small></div>
        <div style="margin-left:auto;text-align:right">
          <div style="color:var(--blue);font-size:11px">${sig.strategy||'—'}</div>
          <div style="color:var(--subtext);font-size:10px">${sig.session||'—'} · ${sig.risk_reward_t1||'—'} R:R</div>
        </div>
      </div>
      <div class="trow"><span class="tl">Entry</span><span class="tv">${fmt$(sig.entry_price)}</span></div>
      <div class="trow"><span class="tl">Stop Loss</span><span class="tv pneg">${fmt$(sig.stop_loss)}</span></div>
      <div class="trow"><span class="tl">TP1 / TP2</span><span class="tv">${fmt$(sig.take_profit_1)} / ${fmt$(sig.take_profit_2)}</span></div>
      <div class="trow"><span class="tl">VWAP Dist.</span><span class="tv" style="font-size:11px">${sig.vwap_distance||'—'}</span></div>
      <div class="trow"><span class="tl">Max Hold</span><span class="tv">${sig.max_hold_time||'—'}</span></div>
      <div class="sig-reasoning">${(sig.entry_trigger||'')}${sig.invalidation?'\n\nInvalidation: '+sig.invalidation:''}</div>
    `;
  } else {
    sb.innerHTML = '<div class="empty">No signal yet</div>';
  }

  // Stats
  const st = s.stats || {};
  $('st-trades').textContent = st.total_trades || 0;
  const wr = st.win_rate;
  const we = $('st-wr');
  we.textContent = wr !== undefined ? (wr*100).toFixed(0)+'%' : '—';
  we.style.color = wr >= .5 ? 'var(--green)' : wr > 0 ? 'var(--red)' : '';
  const pe2 = $('st-pnl');
  pe2.textContent = st.total_pnl_pct !== undefined ? fmtPct(st.total_pnl_pct) : '—';
  pe2.style.color = (st.total_pnl_pct||0) > 0 ? 'var(--green)' : (st.total_pnl_pct||0) < 0 ? 'var(--red)' : '';
}

// ── Log updater ────────────────────────────────────────────────────────────
let lastLogCount = 0;
function updateLogs(lines) {
  if (lines.length === lastLogCount) return;
  lastLogCount = lines.length;
  const box = $('log-box');
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 30;
  box.innerHTML = lines.map(line => {
    const lo = line.toLowerCase();
    let cls = '';
    if (lo.includes('warning') || lo.includes('warn')) cls = 'w';
    if (lo.includes('error')   || lo.includes('fail')) cls = 'e';
    if (lo.includes('trade opened') || lo.includes('trade closed') || lo.includes('order placed')) cls = 't';
    return `<div class="ll ${cls}">${line.replace(/</g,'&lt;').replace(/>/g,'&gt;')}</div>`;
  }).join('');
  if (atBottom) box.scrollTop = box.scrollHeight;
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

poll();
setInterval(poll, 8000);
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
