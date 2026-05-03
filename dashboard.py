"""
dashboard.py  —  BTC/USD Trading Agent Dashboard
Features:
  - TradingView live chart
  - Start / Stop the bot
  - Trade approval: see signal → click Execute or Skip
  - Current trade, AI signal, stats, live logs

Open: http://localhost:8080
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, DEMO_MODE, GROQ_API_KEY

load_dotenv()

app = FastAPI(title="BTC Trading Dashboard")

BASE      = Path(__file__).parent
LOGS_DIR  = BASE / "logs"
STATE_F   = LOGS_DIR / "state.json"
TRADES_F  = LOGS_DIR / "trades_log.json"
LOG_F     = LOGS_DIR / "agent.log"
PENDING_F = LOGS_DIR / "pending_signal.json"
PID_F     = LOGS_DIR / "bot_pid.txt"


def _missing_bot_credentials() -> list[str]:
    if DEMO_MODE:
        return []
    missing: list[str] = []
    if not ALPACA_API_KEY:
        missing.append("ALPACA_API_KEY")
    if not ALPACA_SECRET_KEY:
        missing.append("ALPACA_SECRET_KEY")
    if not GROQ_API_KEY:
        missing.append("GROQ_API_KEY")
    return missing


def _read_bot_pid() -> int | None:
    if not PID_F.exists():
        return None
    try:
        return int(PID_F.read_text().strip())
    except Exception:
        PID_F.unlink(missing_ok=True)
        return None


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _live_bot_pid() -> int | None:
    pid = _read_bot_pid()
    if pid is None:
        return None
    if _is_pid_alive(pid):
        return pid
    PID_F.unlink(missing_ok=True)
    return None


# ── Bot process control ───────────────────────────────────────────────────────

@app.post("/api/bot/start")
def bot_start():
    missing = _missing_bot_credentials()
    if missing:
        return JSONResponse(
            {
                "status": "config_error",
                "detail": "Missing required environment variables",
                "missing": missing,
            },
            status_code=400,
        )

    pid = _live_bot_pid()
    if pid is not None:
        return JSONResponse({"status": "already_running", "pid": pid})

    log_handle = open(LOG_F, "a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(BASE / "agent.py")],
        stdout=log_handle,
        stderr=log_handle,
        cwd=str(BASE),
    )
    PID_F.write_text(str(proc.pid))
    return JSONResponse({"status": "started", "pid": proc.pid})


@app.post("/api/bot/stop")
def bot_stop():
    pid = _live_bot_pid()
    if pid is None:
        return JSONResponse({"status": "not_running"})
    try:
        if sys.platform == "win32":
            subprocess.call(["taskkill", "/F", "/PID", str(pid)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.kill(pid, signal.SIGTERM)
        PID_F.unlink(missing_ok=True)
        return JSONResponse({"status": "stopped", "pid": pid})
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)})


# ── Signal approval ───────────────────────────────────────────────────────────

@app.post("/api/signal/approve")
def approve():
    if not PENDING_F.exists():
        return JSONResponse({"status": "no_pending"})
    data = json.loads(PENDING_F.read_text(encoding="utf-8"))
    data["status"] = "approved"
    PENDING_F.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return JSONResponse({"status": "approved"})


@app.post("/api/signal/skip")
def skip():
    if not PENDING_F.exists():
        return JSONResponse({"status": "no_pending"})
    data = json.loads(PENDING_F.read_text(encoding="utf-8"))
    data["status"] = "skipped"
    PENDING_F.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return JSONResponse({"status": "skipped"})


@app.post("/api/bot/analyze")
def bot_analyze():
    """Trigger immediate re-analysis without waiting for hourly interval."""
    pid = _live_bot_pid()
    if pid is None:
        return JSONResponse({"status": "not_running"}, status_code=409)
    trigger = LOGS_DIR / "analyze_now.json"
    trigger.parent.mkdir(parents=True, exist_ok=True)
    trigger.write_text('{"trigger": true}', encoding="utf-8")
    return JSONResponse({"status": "triggered", "pid": pid})


@app.get("/api/pending")
def get_pending():
    if not PENDING_F.exists():
        return JSONResponse({"status": "none"})
    try:
        return JSONResponse(json.loads(PENDING_F.read_text(encoding="utf-8")))
    except Exception:
        return JSONResponse({"status": "none"})


# ── Data endpoints ────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return HTMLResponse(HTML)


@app.get("/api/state")
def get_state():
    if STATE_F.exists():
        try:
            state = json.loads(STATE_F.read_text(encoding="utf-8"))
            if _live_bot_pid() is None:
                state["bot_status"] = "offline"
            return JSONResponse(state)
        except Exception:
            pass
    return JSONResponse({"bot_status": "offline", "latest_price": 0, "session": ""})


@app.get("/api/trades")
def get_trades():
    if TRADES_F.exists():
        try:
            return JSONResponse(json.loads(TRADES_F.read_text(encoding="utf-8")))
        except Exception:
            pass
    return JSONResponse([])


@app.get("/api/logs")
def get_logs(lines: int = Query(default=80, le=300)):
    if not LOG_F.exists():
        return JSONResponse({"lines": []})
    try:
        with open(LOG_F, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        return JSONResponse({"lines": [l.rstrip() for l in all_lines[-lines:]]})
    except Exception as e:
        return JSONResponse({"lines": [str(e)]})


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>BTC/USD Agent</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d0f14;--surf:#131722;--bdr:#2a2e39;
  --txt:#d1d4dc;--sub:#787b86;
  --G:#26a69a;--R:#ef5350;--Y:#f9a825;--B:#2196f3;--P:#9c27b0;
}
body{background:var(--bg);color:var(--txt);font:13px/1.4 -apple-system,monospace;
  height:100vh;overflow:hidden;display:flex;flex-direction:column}

/* header */
header{display:flex;align-items:center;gap:16px;padding:7px 14px;
  background:var(--surf);border-bottom:1px solid var(--bdr);flex-shrink:0;flex-wrap:wrap}
.logo{font-size:14px;font-weight:700;color:var(--B);letter-spacing:1px}
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

/* start/stop button */
.btn{padding:5px 14px;border-radius:4px;border:none;font:12px monospace;
  cursor:pointer;font-weight:700;letter-spacing:.5px}
.btn-start{background:rgba(38,166,154,.2);color:var(--G);border:1px solid var(--G)}
.btn-stop {background:rgba(239,83,80,.2); color:var(--R);border:1px solid var(--R)}
.btn:hover{opacity:.8}

/* layout */
.main{display:grid;grid-template-columns:1fr 310px;flex:1;min-height:0}

/* chart side */
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
#tv-container{flex:1;min-height:0}
#tv-container iframe{width:100%;height:100%;border:none}

/* right panel */
.rp{display:flex;flex-direction:column;overflow-y:auto}
.card{border-bottom:1px solid var(--bdr);padding:11px}
.ct{font-size:10px;font-weight:700;color:var(--sub);text-transform:uppercase;
  letter-spacing:1px;margin-bottom:9px;display:flex;justify-content:space-between;align-items:center}
.tr{display:flex;justify-content:space-between;align-items:center;
  padding:3px 0;border-bottom:1px solid rgba(42,46,57,.5)}
.tr:last-child{border:none}
.tl{color:var(--sub);font-size:12px} .tv{font-weight:600;font-size:12px;text-align:right}
.empty{text-align:center;color:var(--sub);padding:14px 0;font-size:12px}

/* approval banner */
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

::-webkit-scrollbar{width:3px}::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--bdr);border-radius:2px}
</style>
</head>
<body>

<header>
  <div class="logo">&#9651; BTC/USD AGENT</div>
  <div class="hs"><span class="l">Price</span><span class="v" id="h-price">—</span></div>
  <div class="hs"><span class="l">Session</span><span class="v" id="h-session">—</span></div>
  <div class="hs"><span class="l">Account</span><span class="v" id="h-bal">—</span></div>
  <div class="hs"><span class="l">Daily P&amp;L</span><span class="v" id="h-pnl">—</span></div>
  <div class="hs"><span class="l">Bot Status</span><span class="v" id="h-status"><span class="dot dy"></span>connecting...</span></div>
  <div style="margin-left:auto;display:flex;align-items:center;gap:10px">
    <span style="font-size:10px;color:var(--sub)" id="h-ts"></span>
    <button class="btn" id="analyze-btn" onclick="analyzeNow()" style="background:rgba(33,150,243,.2);color:#64b5f6;border:1px solid #2196f3">Analyze Now</button>
    <button class="btn btn-start" id="bot-btn" onclick="toggleBot()">Start Bot</button>
  </div>
</header>

<div class="main">
  <div class="cs">
    <div class="ctbar">
      <span>BTC/USD &nbsp;·&nbsp; TradingView &nbsp;·&nbsp; 1H</span>
      <span id="tv-s">loading chart...</span>
    </div>
    <div class="lvbar" id="lvbar"><span style="color:var(--sub);font-style:italic;font-size:11px">No active trade</span></div>
    <div id="tv-container">
      <iframe
        src="https://www.tradingview.com/widgetembed/?symbol=COINBASE%3ABTCUSD&interval=60&theme=dark&style=1&locale=en&timezone=Asia%2FKolkata&allow_symbol_change=0&hide_top_toolbar=0&hide_legend=0&save_image=0"
        allowtransparency="true" scrolling="no" allowfullscreen
        onload="document.getElementById('tv-s').textContent='live ✓';document.getElementById('tv-s').style.color='#26a69a'"
      ></iframe>
    </div>
  </div>

  <div class="rp">

    <!-- APPROVAL PANEL (shown when bot awaits decision) -->
    <div class="card" id="approval-card">
      <div class="ct">⚡ TRADE DECISION REQUIRED <span id="ap-badge"></span></div>
      <div id="ap-body"></div>
      <div class="apnl">
        <button class="btn-exec" onclick="approveSignal()">✓ Execute Trade</button>
        <button class="btn-skip" onclick="skipSignal()">✕ Skip</button>
      </div>
      <div id="countdown"></div>
    </div>

    <!-- Current Trade -->
    <div class="card">
      <div class="ct">Current Trade <span id="trade-badge"></span></div>
      <div id="trade-body"><div class="empty">No active trade</div></div>
    </div>

    <!-- Last AI Signal -->
    <div class="card">
      <div class="ct">Last AI Signal <span id="sig-ts" style="color:var(--sub);font-size:10px"></span></div>
      <div id="sig-body"><div class="empty">No signal yet</div></div>
    </div>

    <!-- Stats -->
    <div class="card">
      <div class="ct">Session Stats</div>
      <div class="sgrid">
        <div class="sbox"><div class="n" id="st-t">0</div><div class="lb">Trades</div></div>
        <div class="sbox"><div class="n" id="st-w">—</div><div class="lb">Win Rate</div></div>
        <div class="sbox"><div class="n" id="st-p">—</div><div class="lb">Total P&amp;L</div></div>
      </div>
    </div>

    <!-- Logs -->
    <div class="card" style="flex:1;min-height:0">
      <div class="ct">Live Logs</div>
      <div id="log-box"></div>
    </div>

  </div>
</div>

<script>
// ── utils ──
function p$(v){ return (v===null||v===undefined)?'—':'$'+Number(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}); }
function pp(v){ if(v===null||v===undefined)return'—'; var n=Number(v); return(n>=0?'+':'')+n.toFixed(2)+'%'; }
function bdg(b){ if(b==='BULLISH')return'<span class="badge bg">LONG</span>'; if(b==='BEARISH')return'<span class="badge br">SHORT</span>'; return'<span class="badge bm">'+(b||'—')+'</span>'; }
function ago(iso){ if(!iso)return'—'; var s=Math.floor((Date.now()-new Date(iso))/1000); if(s<60)return s+'s ago'; if(s<3600)return Math.floor(s/60)+'m ago'; return Math.floor(s/3600)+'h ago'; }
function qc(q){ return q==='A+'?'#26a69a':q==='A'?'#2196f3':q==='B'?'#f9a825':'#787b86'; }
function el(id){ return document.getElementById(id); }
function post(url){
  return fetch(url,{method:'POST'}).then(function(r){
    return r.json().then(function(data){
      if(!r.ok){ throw data; }
      return data;
    });
  });
}

var botRunning = false;

// ── Analyze now ──
function analyzeNow(){
  var btn = el('analyze-btn');
  btn.textContent = 'Requesting...';
  btn.disabled = true;
  post('/api/bot/analyze').then(function(d){
    btn.textContent = 'Sent!';
    setTimeout(function(){ btn.textContent='Analyze Now'; btn.disabled=false; }, 3000);
    poll();
  }).catch(function(err){
    btn.textContent = (err && err.status==='not_running') ? 'Bot Offline' : 'Error';
    setTimeout(function(){ btn.textContent='Analyze Now'; btn.disabled=false; }, 3000);
    poll();
  });
}

// ── Bot start/stop ──
function toggleBot(){
  if(botRunning){
    post('/api/bot/stop').then(function(d){
      console.log('stop:',d); poll();
    }).catch(function(d){
      console.log('stop error:',d); poll();
    });
  } else {
    post('/api/bot/start').then(function(d){
      console.log('start:',d); poll();
    }).catch(function(d){
      console.log('start error:',d); poll();
    });
  }
}

// ── Approval ──
function approveSignal(){
  post('/api/signal/approve').then(function(d){
    console.log('approved',d);
    el('approval-card').style.display='none';
    poll();
  });
}
function skipSignal(){
  post('/api/signal/skip').then(function(d){
    console.log('skipped',d);
    el('approval-card').style.display='none';
    poll();
  });
}

// ── Countdown timer ──
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

// ── State update ──
function updateState(s){
  // header
  el('h-price').textContent   = s.latest_price ? p$(s.latest_price) : '—';
  el('h-session').textContent = (s.session||'—').toUpperCase();
  el('h-bal').textContent     = s.account ? p$(s.account.balance) : '—';

  var pnl = s.daily_pnl_pct;
  el('h-pnl').textContent  = pp(pnl);
  el('h-pnl').className    = 'v '+(pnl>0?'ppos':pnl<0?'pneg':'');
  el('h-ts').textContent   = new Date().toLocaleTimeString();

  var st = el('h-status');
  var bs = s.bot_status||'offline';
  botRunning = (bs==='running'||bs==='awaiting_approval'||bs==='starting');
  if(bs==='running'){
    st.innerHTML='<span class="dot dg"></span>running';st.style.color='var(--G)';
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

  // level bar
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

  // trade panel
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

  // signal panel
  var sig = s.last_signal;
  if(sig && sig.signal_quality){
    el('sig-ts').textContent = ago(sig.timestamp);
    el('sig-body').innerHTML=
      '<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">'+
        '<div class="sig-q" style="color:'+qc(sig.signal_quality)+'">'+sig.signal_quality+'</div>'+
        '<div>'+bdg(sig.bias)+'<br><small style="color:var(--sub)">'+(sig.signal_score||'—')+' aligned</small></div>'+
        '<div style="margin-left:auto;text-align:right">'+
          '<div style="color:var(--B);font-size:11px">'+(sig.strategy||'—')+'</div>'+
          '<div style="color:var(--sub);font-size:10px">'+(sig.session||'—')+' · '+(sig.risk_reward_t1||'—')+' R:R</div>'+
        '</div>'+
      '</div>'+
      row('Entry',p$(sig.entry_price))+
      row('Stop Loss','<span class="pneg">'+p$(sig.stop_loss)+'</span>')+
      row('TP1 / TP2',p$(sig.take_profit_1)+' / '+p$(sig.take_profit_2))+
      row('VWAP Dist','<span style="font-size:11px">'+(sig.vwap_distance||'—')+'</span>')+
      row('Max Hold',sig.max_hold_time||'—')+
      '<div class="sig-reason">'+(sig.entry_trigger||'')+(sig.invalidation?'\n\nInvalidation: '+sig.invalidation:'')+'</div>';
  } else {
    el('sig-ts').textContent='';
    el('sig-body').innerHTML='<div class="empty">No signal yet</div>';
  }

  // stats
  var st2=s.stats||{};
  el('st-t').textContent=st2.total_trades||0;
  var wr=st2.win_rate;
  el('st-w').textContent=wr!==undefined?(wr*100).toFixed(0)+'%':'—';
  el('st-w').style.color=wr>=.5?'var(--G)':wr>0?'var(--R)':'';
  el('st-p').textContent=st2.total_pnl_pct!==undefined?pp(st2.total_pnl_pct):'—';
  el('st-p').style.color=(st2.total_pnl_pct||0)>0?'var(--G)':(st2.total_pnl_pct||0)<0?'var(--R)':'';
}

function row(l,v){ return '<div class="tr"><span class="tl">'+l+'</span><span class="tv">'+v+'</span></div>'; }

// ── Pending signal (approval) ──
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

  var pc=sig.stop_loss&&sig.entry_price?Math.abs(((sig.stop_loss-sig.entry_price)/sig.entry_price)*100).toFixed(1)+'%':'5%';
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
    row('Entry',p$(sig.entry_price))+
    row('Stop Loss','<span class="pneg">'+p$(sig.stop_loss)+' ('+pc+')</span>')+
    row('TP1','<span class="ppos">'+p$(sig.take_profit_1)+'</span>')+
    row('TP2','<span style="color:var(--P)">'+p$(sig.take_profit_2)+'</span>')+
    (sig.entry_trigger?'<div style="font-size:11px;color:var(--sub);margin-top:6px;padding:4px;background:rgba(255,255,255,.04);border-radius:3px">'+sig.entry_trigger+'</div>':'');

  if(p.status==='pending' && lastPendingStatus!=='pending'){
    startCountdown(p.expires_ts);
  }
  lastPendingStatus=p.status;
}

// ── Logs ──
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
    if(lo.indexOf('trade opened')>=0||lo.indexOf('order placed')>=0||lo.indexOf('trade closed')>=0)cls='t';
    return'<div class="ll '+cls+'">'+line.replace(/</g,'&lt;').replace(/>/g,'&gt;')+'</div>';
  }).join('');
  if(atBot)box.scrollTop=box.scrollHeight;
}

// ── Poll ──
function poll(){
  fetch('/api/state').then(function(r){return r.json();}).then(updateState).catch(function(e){console.log('state err',e);});
  fetch('/api/pending').then(function(r){return r.json();}).then(updatePending).catch(function(){});
  fetch('/api/logs?lines=80').then(function(r){return r.json();}).then(function(d){updateLogs(d.lines||[]);}).catch(function(){});
}

poll();
setInterval(poll, 6000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    print("=" * 50)
    print("  BTC Trading Dashboard - http://localhost:8080")
    print("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=8080, reload=False)
