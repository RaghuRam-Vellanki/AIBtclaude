# AI Multi-Asset Trading Agent — BTC / XAU / NIFTY

Cluster-aware institutional pod trading three assets with regime-first,
archetype-decorrelated signal aggregation. BTC runs against Alpaca (live
demo-safe), XAU and NIFTY run on local paper-sim with the same strategy pods.

The full architecture rationale is in **[docs/PHASES_QUANT_RESET.md](docs/PHASES_QUANT_RESET.md)** —
this README is just clone-and-run.

---

## Quick start on a fresh machine

```bash
# 1. clone
git clone https://github.com/RaghuRam-Vellanki/AIBtclaude.git
cd AIBtclaude

# 2. (recommended) virtual env
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

# 3. install deps (Python 3.10+ required)
pip install -r requirements.txt

# 4. configure (optional — paper-sim pods run without any keys)
cp .env.example .env      # Windows:  copy .env.example .env
# then edit .env

# 5. run
python run.py             # dashboard at http://localhost:8080
```

---

## What runs where

| Asset | Mode | Pod size | Clusters | Needs API key? |
|---|---|---|---|---|
| BTC/USD  | Alpaca demo / live | 7 | 5 (incl. CARRY) | Alpaca for live; demo works without |
| XAU/USD  | local paper-sim    | 9 | 6 | none (yfinance only) |
| NIFTY 50 | local paper-sim    | 12 | 5 (incl. FLOW BANKNIFTY lead-lag) | none (yfinance + NSE public) |

All three share the same Phase-1/2/3 gates: ATR-based stops, 2R R:R floor,
TP-ladder (40/35/25 with BE@TP1 + trail@TP2), MTF gate, event blackout,
regime-first archetype muting, cluster-honest grading.

---

## Running individual pieces

```bash
python run.py                       # dashboard + all agents (default)
python run.py --agent dashboard     # dashboard only (still boots agents)
python run.py --agent xau           # XAU agent alone, no UI
python run.py --agent btc           # BTC agent alone
python run.py --agent nifty         # NIFTY agent alone
python run.py --port 9090           # use a different dashboard port
python run.py --check               # environment self-check, no run
```

Or the original entry points still work:

```bash
python dashboard.py
python agent.py        # BTC
python agent_xau.py
python agent_nifty.py
```

---

## Backtest

```bash
python backtests/xau_backtest.py    --period 1y --capital 10000
python backtests/nifty_backtest.py  --period 6mo
```

HTML report lands in `logs/backtest_report.html`. The backtest engine
reuses the live `_deterministic_signal()`, so it picks up all the cluster /
ATR / blackout logic automatically — no separate backtest config to maintain.

---

## Environment variables (`.env`)

| Variable | Required? | Purpose |
|---|---|---|
| `DEMO_MODE=true` | yes | starts BTC in demo (no real orders) |
| `ALPACA_API_KEY` | live BTC only | Alpaca paper or live key |
| `ALPACA_SECRET_KEY` | live BTC only | Alpaca secret |
| `ALPACA_BASE_URL` | live BTC only | `https://paper-api.alpaca.markets` |
| `OPENAI_API_KEY` | optional | LLM signal generator for BTC |
| `GROQ_API_KEY` | optional | LLM signal generator for XAU + NIFTY (deterministic fallback if absent) |
| `FRED_API_KEY` | optional | precise real yields for `macro_flow` — synthetic fallback used if absent |

Paper-sim pods (XAU + NIFTY) run with **none** of these set — yfinance and
NSE public endpoints cover everything they need. Keys only upgrade signal
quality or unlock live execution.

---

## Updating CPI / event calendar

Both refresh manually (weekly / monthly) — they're plain JSON:

- `data/cpi_cache.json` — update `latest_yoy_pct` after each US CPI release
  (or just set `FRED_API_KEY` in `.env` and skip this entirely)
- `data/event_calendar.json` — add upcoming FOMC / CPI / NFP / PCE / RBI
  windows; entries past their `ts` are auto-ignored

---

## Project layout

```
.
├── run.py                 # clone-and-run entry point
├── dashboard.py           # FastAPI UI + agent orchestrator
├── agent.py               # BTC agent
├── agent_xau.py           # XAU paper-sim agent
├── agent_nifty.py         # NIFTY paper-sim agent
├── config.py              # tunables (ATR_K, RR floor, funding thresholds)
├── modules/
│   ├── signal_gates.py    # ATR stops, MTF, RR, regime, clusters
│   ├── signal_generator_{btc,xau,nifty}.py
│   ├── tp_ladder.py       # TP1/2/3 state machine
│   ├── event_calendar.py  # FOMC / CPI / NFP blackout
│   ├── macro_data.py      # real yields (synthetic + FRED)
│   ├── data_feed*.py      # yfinance / Binance / NSE adapters
│   └── order_manager_*_paper.py
├── strategies/
│   ├── base.py            # archetype taxonomy + StrategyVote
│   └── *.py               # 18 strategies, each tagged with archetype
├── data/
│   ├── event_calendar.json
│   └── cpi_cache.json
├── backtests/
│   ├── xau_backtest.py
│   └── nifty_backtest.py
└── docs/
    └── PHASES_QUANT_RESET.md   # full Phase 1-4 design doc
```

---

## Troubleshooting on a new machine

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError` on first run | `pip install -r requirements.txt` |
| Dashboard fails on port 8080 | `python run.py --port 9090` |
| `yfinance` returning empty bars | the live yahoo-finance endpoint sometimes rate-limits — retry; or set a different `--symbol` for the backtest |
| `tzdata` errors on Windows | already in `requirements.txt`; if missing: `pip install tzdata` |
| NIFTY pod reports "NSE option chain unavailable" | NSE blocks non-browser UAs sometimes — strategy degrades to NEUTRAL gracefully, no crash |
| `FRED API key invalid` | leave `FRED_API_KEY` unset — synthetic real yields will be used (sufficient for direction) |
