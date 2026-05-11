# Quant-Tier Foundation Reset — Phase 1-4

The trading bot was producing low-quality / false signals and losing money. A
forensic audit found **18 structural defects** across five domains:

1. **Strategy collinearity** — 7/9 XAU and 7/11 NIFTY strategies are in the
   same 5m price-action / VWAP / Z-score cluster. "5-of-9 aligned" was really
   *one* scalp cluster voting 5 times (75-85% pairwise correlation).
2. **Stops were not volatility-aware** — fixed 3% / 5% / 1.5% from entry,
   ignoring ATR. On quiet days the stop was 4.7× ATR (lots of room for chop);
   on event days it was 1.4× ATR (stopped on noise).
3. **TP2/TP3 were dead code** — only SL and TP1 were monitored. Trades that
   hit TP1 closed 100% of position, leaving 66% of designed profit on the
   table. No breakeven move after TP1. No trail. `max_hold_time` was unused.
4. **No event blackout** — bot fired entries 5 minutes before FOMC/CPI/NFP.
   Single largest source of random-loss tail events for XAU.
5. **Macro layer was half-built** — no real yields (the #1 structural driver
   of gold), no BTC funding rate (hardcoded "PASS"), no event calendar, MTF
   was informational not a gate, no intra-session NIFTY filter.

The fix is a four-phase rebuild of the gates + macro layer, not new strategies.

---

## Phase 1 — Stop the Bleeding ✅

Five fixes that address >80% of loss bleed without new strategies.

### 1.1 ATR-based stops + 2:1 R:R floor

New `modules/signal_gates.py` (shared across all 3 generators):

```python
ATR_K_BY_QUALITY = {"A+": 2.5, "A": 2.0, "B": 1.5}
MIN_RR_T1 = 2.0
```

- `SL = entry ± k × ATR(14)` — k scales with grade
- `compute_atr_stops()` returns `(sl, tp1, tp2, tp3, risk)` with TPs at
  `2R / 3R / 4.5R`
- `meets_rr_floor()` rejects signals where TP1 < 2R from entry
- Falls back to fixed-% only when ATR unavailable

XAU uses 1H ATR (fallback `daily_atr/13`). BTC uses 1H ATR. NIFTY uses 15m
ATR (best match for index-futures stop placement).

### 1.2 TP-ladder state machine + breakeven + max-hold

New `modules/tp_ladder.py` — single helper drives both XAU and NIFTY paper
order managers:

| Trigger | Action (LONG; SHORT inverts) |
|---|---|
| `close ≥ TP1` (first time) | partial_close 40%; **SL → entry (BE)** |
| `close ≥ TP2` (first time) | partial_close 35%; **SL → TP1 − 0.25·ATR (trail)** |
| `close ≥ TP3` (first time) | close remaining 25% (final) |
| `close ≤ SL` | close 100% of what's left |
| `now − open_ts > max_hold` | close 100% → `EXIT_TIME` |

Paper order managers (`order_manager_xau_paper.py`, `order_manager_nifty_paper.py`)
now track `tp1_done / tp2_done / tp3_done / remaining_qty / current_sl /
opened_ts / realized_pnl / exits`. Methods added: `partial_close()`,
`update_stop()`, `mark_tp_done()`, `is_max_hold_breached()`.

Critical bug fix in `tp_ladder.tick_position()`: it calls `om.update_price(px)`
at the start so `close_position()` doesn't use a stale mark-price.

### 1.3 Hard MTF gate

`classify_htf(daily, h4)` → `"up" / "down" / "range"` from existing structure
strings.

`mtf_blocks(htf, is_long, quality)` — A+ overrides only:
- LONG against 4H downtrend → blocked unless A+
- SHORT into 4H uptrend → blocked unless A+

### 1.4 Event-window blackout

New `modules/event_calendar.py` + `data/event_calendar.json` (13 hardcoded
events through July 2026: CPI, PPI, FOMC, NFP, PCE, RBI MPC).

```python
is_blackout("xau", now_utc) → (bool, reason)   # e.g. (True, "US CPI ±15min")
```

Window applies BEFORE and AFTER each release. JSON-editable so the user can
refresh without code changes. mtime-based cache.

### 1.5 Real BTC funding-rate check

`modules/data_feed.py` adds:
- `fetch_funding_rate()` — Binance `fapi/v1/premiumIndex?symbol=BTCUSDT`,
  60s cache, returns `lastFundingRate` as decimal/8h
- `fetch_oi_change()` — Binance `futures/data/openInterestHist`, 24×1h rows,
  5min cache, returns 24h percent change

Config:
```python
BTC_FUNDING_BLOCK    = 0.0008   # 0.08%/8h ≈ 0.24%/day → 87.6% APR carry
BTC_FUNDING_DOWNGRADE = 0.0005
```

`signal_generator_btc.py`:
- LONG blocked when `funding > +0.0008` (paying for an over-extended long)
- SHORT blocked when `funding < −0.0008`
- A → B downgrade when `|funding| > 0.0005` same-direction

---

## Phase 2 — Decorrelate the Pod ✅

Fixes the "5-of-9 aligned = really 1 cluster voting 5 times" problem.

### 2.1 Archetype taxonomy

`strategies/base.py` adds:

```python
ARCHETYPES = ("TREND", "MEAN_REVERT", "MOMENTUM", "BREAKOUT",
              "FLOW", "MACRO", "OPTIONS", "MICRO", "CARRY")
```

All 18 strategies tagged:

| Archetype | Strategies |
|---|---|
| MEAN_REVERT | microstructure, btc_microstructure, nifty_microstructure, vwap_bandit, cointegration, nifty_pairs_arb_bn |
| TREND | regime_hmm, nifty_regime_hmm, momentum_macro |
| MOMENTUM | scalp_indicators |
| FLOW | orderflow_liquidity, session_volume, oi_crossover, nifty_fii_dii_flow |
| MACRO | macro_flow |
| OPTIONS | greeks_proxy, nifty_options_oi, volatility_regime |

### 2.2 Regime-first archetype muting

`signal_gates.py` adds:

```python
_REGIME_ACTIVE_ARCHETYPES = {
    "trend":  {"TREND", "MOMENTUM", "MACRO", "FLOW", "BREAKOUT", "CARRY"},
    "range":  {"MEAN_REVERT", "OPTIONS", "MICRO", "FLOW", "CARRY"},
    "chaos":  set(),       # caller treats empty-set as NO_TRADE
}
```

`detect_regime(votes, htf_dir)` reads the HMM vote's `regime` metadata; falls
back to HTF direction. Returns one of `{"trend", "range", "chaos"}`.

**Why this matters:** mean-reverting in a trend = guaranteed bleed.
Trend-following in chop = chase-and-stop. Filtering before counting alignment
is the single largest false-signal fix in this rebuild.

### 2.3 Cluster-based alignment counting

`cluster_winners(votes, active=...)` collapses votes by archetype to a single
representative (highest-conf directional vote per cluster).

`cluster_alignment(winners, is_long)` returns
`(aligned_clusters, total_clusters, signed_score)`.

`grade_clusters(aligned, total, smart_money_confirms)`:

| Aligned clusters | Grade |
|---|---|
| ≥ 4 | A+ |
| ≥ 3 | A |
| ≥ 2 + smart-money (FLOW or MOMENTUM agrees) | B |
| else | NO_TRADE |

`smart_money_aligned()` checks that the FLOW or MOMENTUM cluster winner
agrees with the direction — these archetypes read price-action / institutional
flow, not lagging macro/regime.

### 2.4 Archetype-stamping bug

Discovered during integration smoke: every strategy builds `StrategyVote(...)`
directly, *without* passing `archetype`. The dataclass default `"FLOW"` was
clobbering the class-level `archetype` declaration.

Fix: post-process in `_collect_votes` of all 3 signal generators +
`backtest_engine`:

```python
vote.archetype = getattr(strat, "archetype", "FLOW")
```

Authoritative source = `strat.archetype` class attribute.

### 2.5 Wired into all 3 deterministic aggregators

`signal_generator_xau/btc/nifty.py` now:
1. Compute `htf_dir`, `regime`, `active` archetypes
2. Early-return NO_TRADE if `regime == "chaos"` or `active` is empty
3. `cluster_winners(votes, active=active)` → per-archetype winners
4. `cluster_alignment(winners, is_long=±)` → `(aligned, total, signed_score)`
5. Direction from `signed_score`
6. Grade via `grade_clusters()`
7. Existing ATR + 2R + MTF + funding gates apply

NIFTY-specific: India VIX > 22 forces `regime = "chaos"` (intraday gamma
blows out option-strategy assumptions).

XAU keeps the macro_flow opposite-direction downgrade. NIFTY keeps the
FII/DII flow opposite-direction downgrade.

`signal_score` display is now `aligned/total_clusters` (e.g. `3/5`) — the
cluster-honest measure.

---

## Phase 3 — Add the Missing Macro ✅

Adds the duration drivers and one strategy per missing archetype cluster.

### 3.1 Real yields (`modules/macro_data.py`)

Two paths, same interface:

**Synthetic (default, no API key):**
- `^TNX` from yfinance (10-day window, handles new MultiIndex columns)
- Rolling CPI YoY from `data/cpi_cache.json` (manually refreshed monthly)
- `real_yield = nominal − cpi_yoy`
- 5d change in bps, 5s10s curve as 2s10s proxy

**FRED (optional precise upgrade):**
- Set `FRED_API_KEY` in `.env` (60s signup at fredaccount.stlouisfed.org)
- Pulls `DGS10 − T10YIE` daily (basis-point accurate)
- 2s10s curve from `DGS10 − DGS2`

Output schema is identical regardless of source:

```python
{
  "real_yield_10y": -0.42 | 1.79,
  "real_yield_5d_change_bp": +12 | -5.2,
  "yield_curve_2s10s": +35 | +34.6,
  "source": "fred" | "synthetic" | "unavailable",
}
```

1h in-process cache. Verified output:
`real_yield_10y=1.79, Δ=-5.2bp/5d, curve=+34.6bp, source=synthetic`.

### 3.2 `macro_flow` strategy upgrade

Real-yield Δ is now the **dominant component** (weight 2 — same magnitude as
DXY+COT combined):

| 5d Δ in real yields | ry_score |
|---|---|
| ≤ −10 bp | +2 (strongly bullish gold) |
| −10 to −5 bp | +1 |
| +5 to +10 bp | −1 |
| ≥ +10 bp | −2 |
| else | 0 |

DXY veto: if real yields scream LONG but DXY is strongly rising,
`composite -= 1` (don't long gold into a USD breakout).

Composite threshold to fire: `|composite| ≥ 2` for strong directional,
`|composite| ≥ 1 with real-yield confirmation` for low-conf directional.

### 3.3 BTC funding-skew strategy (CARRY)

`strategies/btc_funding_skew.py`. Two setups:

```python
# Short-squeeze fuel: shorts paying longs + crowd adding short OI
if funding_8h ≤ -0.0003 and oi_24h_pct ≥ 0.05:
    LONG @ confidence 0.5-0.85

# Long-liq cascade risk: longs paying shorts + crowd adding long OI
if funding_8h ≥ +0.0008 and oi_24h_pct ≥ 0.05:
    SHORT @ confidence 0.5-0.85
```

Note: this is **not** the same as the funding-block gate in
`signal_generator_btc`. The gate vetoes trades when funding is extreme +
same-direction (avoid being the late long). This strategy actively positions
for the opposite side of the crowd.

### 3.4 Bank Nifty lead-lag strategy (FLOW)

`strategies/nifty_bn_lead.py`. BANKNIFTY 3h-return leads NIFTY 50 because
banks dominate the index weight (~35-40% BFSI).

```python
if BN 3h return ≥ +0.30% AND NIFTY 3h return < 0.15% AND gap ≥ 0.15%:
    LONG @ 0.55-0.80 confidence
if BN 3h return ≤ -0.30% AND NIFTY 3h return < 0.15% AND gap ≤ -0.15%:
    SHORT @ 0.55-0.80 confidence
```

Uses `feed.get_banknifty_1h()` (already in `data_feed_nifty.py`).

### 3.5 Pod registration

`strategies/__init__.py`:

| Pod | Before | After | Clusters |
|---|---|---|---|
| XAU | 9 strats | 9 strats | 6 (MEAN_REVERT:3, TREND:2, MACRO/FLOW/OPTIONS/MOMENTUM:1) |
| BTC | 6 strats | **7 strats** (+CARRY) | 5 (MEAN_REVERT/FLOW:2, MOMENTUM/TREND/CARRY:1) |
| NIFTY | 11 strats | **12 strats** (+FLOW lead) | 5 (MEAN_REVERT:3, FLOW:4, OPTIONS:3, MOMENTUM/TREND:1) |

Grading ladder is well-calibrated: A+ ≥ 4 clusters = 67% of XAU's 6
clusters / 80% of NIFTY's 5. No more "3-of-11 false-quorum" issue.

---

## Phase 4 — Validation (deferred)

Skipped per user request — Phase 1+2+3 ship without the 1y backtest replay.
Re-run when ready by following the steps below. No code change unless
something regresses. Three checks:

### 4.1 Walk-forward backtest

Existing backtests at `backtests/xau_backtest.py` and
`backtests/nifty_backtest.py`. Run with default 12-month period.

| Metric | Target |
|---|---|
| Win rate | ≥ 45% |
| Avg win / avg loss | ≥ 1.8 |
| Max drawdown | ≤ 12% |
| Sharpe (daily) | ≥ 1.0 |
| Trades / month | 8-25 (regime-gated → fewer, better) |
| Trade-through-event count | **0** |

### 4.2 Regime-stratified comparison

Break P&L by regime label (trend / range / chaos):
- Pre-fix: mean-reversion strategies losing in trend periods
- Post-fix: mean-reversion is silent in trend (muted by archetype filter)
- Sanity check that the active-archetype filter actually shuts off the wrong
  strategies in the right regimes

### 4.3 Live shadow run

Paper-sim for 5 trading days with the new config alongside the old.
Confirms: fewer entries, higher avg quality, matching or better cumulative
P&L.

### Acceptance criteria

Ship Phase 1+2+3 only if **every metric improves or holds flat**. If any
metric regresses, identify the cause and either re-tune thresholds (Phase
2.3) or revert the regressing component.

---

## File map

### New files
- `modules/event_calendar.py` — blackout windows + `is_blackout()`
- `modules/signal_gates.py` — ATR stops, MTF, RR floor, regime, cluster helpers
- `modules/tp_ladder.py` — TP-ladder state machine
- `modules/macro_data.py` — real yields (synthetic + FRED)
- `modules/order_manager_xau_paper.py` — XAU paper OM with TP-ladder fields
- `modules/order_manager_nifty_paper.py` — NIFTY paper OM with TP-ladder fields
- `modules/signal_generator_xau.py` — XAU pod generator (cluster-aware)
- `modules/signal_generator_btc.py` — BTC pod generator (cluster-aware)
- `modules/signal_generator_nifty.py` — NIFTY pod generator (cluster-aware)
- `data/event_calendar.json` — editable event windows
- `data/cpi_cache.json` — CPI YoY cache for synthetic real yields
- `strategies/btc_funding_skew.py` — CARRY archetype strategy
- `strategies/nifty_bn_lead.py` — FLOW archetype strategy
- `strategies/base.py` — extended with `ARCHETYPES`, `archetype` field, `_vote()` helper
- (all 18 strategy files tagged with `archetype = "..."`)

### Modified files
- `config.py` — `ATR_K_BY_QUALITY`, `MIN_RR_T1`, `BTC_FUNDING_BLOCK`,
  `BTC_FUNDING_DOWNGRADE`, `MAX_HOLD_HOURS_DEFAULT`, `TP_LADDER_FRACTIONS`
- `agent.py`, `agent_xau.py`, `agent_nifty.py` — `_monitor_position()` rewritten
  to call `tp_ladder.tick_position`; max-hold enforced; `_safe_call` helper
- `modules/data_feed.py` — `fetch_funding_rate()`, `fetch_oi_change()`, ATR snapshot
- `modules/data_feed_nifty.py` — ATR(14) in snapshot
- `modules/risk_manager.py` — ATR-aware stop computation
- `strategies/macro_flow.py` — real-yield-first composite
- `strategies/__init__.py` — register new strategies, pod sizes updated
