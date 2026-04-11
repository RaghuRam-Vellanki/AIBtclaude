# SKILL: BTC/USD Elite Institutional Trading Agent
**6-Framework Synthesis | Bot-Ready | Liquidity & Structure Driven | Alpaca Paper Trading**

---

## OVERVIEW

This skill combines six institutional trading perspectives into one unified framework for BTC/USD:

| Perspective | Source Firm Style | Primary Edge |
|---|---|---|
| 1. HFT / Market Microstructure | Citadel Securities, Jump, Virtu | Speed, liquidity mapping, order flow |
| 2. Institutional Bank Desk | JPMorgan, Goldman Sachs, HSBC | Client flow, inventory, quote skewing |
| 3. Quant Hedge Fund | Two Sigma, Jane Street, Tower | Statistical models, factor analysis, regime classification |
| 4. Prop Trading Firm | Optiver, IMC, Graviton | Intraday precision, fast setup execution |
| 5. Combined HFT + Institutional | Full desk view | Session structure + VWAP + liquidity sweep |
| 6. Elite Synthesis | All of the above | Multi-signal confluence, narrative-driven trades |

**Core rules that never change across all six:**
- No lagging indicators (RSI, MACD, Bollinger Bands) as primary signals
- Entry is always justified by liquidity, structure, or VWAP — never opinion
- Risk per trade: 0.5%–1% of account
- No trade if fewer than 3/6 signal categories align
- Funding rate gate is mandatory before every trade

---

## MANDATORY FIRST STEP: LIVE DATA SEARCH

Before every analysis, retrieve the following via web search:

```
Search 1: "BTC Bitcoin price today [current month year]"
Search 2: "Bitcoin ETF flows [current week/month year]"
Search 3: "BTC funding rate open interest [today]"
Search 4: "Bitcoin macro sentiment DXY yields [current month year]"
Search 5: "BTC liquidation levels Coinglass [today]"
```

Extract and state explicitly:
- Current BTC/USD price + 24h high/low
- 7-day trend direction and % change
- BTC spot ETF net flows (BlackRock IBIT, Fidelity FBTC — positive = accumulation)
- Funding rate on Binance/Bybit perpetuals (positive / negative / neutral)
- Open interest trend (rising / falling / flat)
- Major on-chain events, regulatory news, or macro catalysts
- VIX level and equity market direction

---

## SECTION 1: MACRO & CRYPTO CONTEXT

### 1A. Macro Regime Classification

| Condition | BTC Bias |
|---|---|
| Risk-on + weak DXY + falling real yields | Strongly Bullish |
| Risk-on + neutral DXY + stable yields | Bullish |
| Risk-off + strong DXY + rising yields | Bearish |
| Stagflation (high inflation + weak growth) | Ambiguous — wait |
| Geopolitical crisis escalation | Initial sell-off → potential recovery |
| FOMC / CPI day | Avoid trading 30 min before and after |

**Regime output required:**
- DXY direction: Rising / Falling / Flat (BTC inversely correlated)
- Real yields (10Y TIPS): Rising = BTC headwind / Falling = BTC tailwind
- VIX: <15 (risk-on) / 15–25 (neutral) / >25 (risk-off, reduce size 20%)
- Equity market: S&P 500 direction as risk sentiment proxy
- **Overall macro bias:** Bullish / Bearish / Neutral

### 1B. BTC-Specific Macro Signals (Quant Layer)

**ETF Flows (most powerful institutional signal):**
- Consecutive daily inflows → institutional accumulation → bullish continuation
- Outflows → distribution → reduce longs or bias short
- $3B+ outflow events historically precede major corrections

**On-chain & Market Structure:**
- BTC dominance rising → capital rotating to BTC (risk-off within crypto) → bullish BTC
- Stablecoin supply increasing → dry powder entering → bullish setup
- Realized price (~$54K) → long-term institutional support floor
- Below realized price → bear market framework, flip to short-bias entirely

**Factor Sensitivity (Quant Hedge Fund View):**

| Factor | Current Direction | BTC Impact |
|---|---|---|
| Real yields | ? | Inverse — rising yields = bearish |
| DXY | ? | Inverse — strong USD = bearish |
| Oil | ? | Mild positive — inflation proxy |
| Global M2 | ? | Positive with 3–6 month lag |
| Risk sentiment (VIX) | ? | Inverse — high VIX = bearish |

Dominant factor today: [identify which is moving most]

---

## SECTION 2: SESSION STRUCTURE

BTC trades 24/7. Use liquidity-weighted session windows (IST = UTC+5:30):

| Session | IST Window | Characteristics |
|---|---|---|
| Asian accumulation | 00:00–08:30 | Lowest volume, tight ranges, Binance/OKX dominant |
| European wake-up | 12:30–14:30 | Volume picks up, often sets intraday direction |
| US pre-market | 16:30–18:30 | ETF flow anticipation, options activity |
| US session peak | 18:30–22:30 | Highest liquidity, largest moves, institutional flow |
| Late US / rollover | 22:30–00:00 | Funding rate settlement windows, 30-min volatility spike |

**Funding rate settlement times (IST):** 01:30 / 09:30 / 17:30 → expect 15–30 min volatility

### The Core BTC Intraday Pattern (Bank Desk + HFT View)
1. Asian session builds a range (quiet, low volume)
2. European session creates a directional bias (often false / engineered)
3. US session (18:30–20:30 IST) sweeps the Asian range HIGH or LOW
4. True directional move begins AFTER the sweep
5. This is the highest-probability setup in BTC — treat as A+ candidate

**Session analysis output required:**
- Asian range: High $\_\_\_ / Low $\_\_\_
- London behavior: Expansion / False breakout / Sweep
- US session intent: Sweeping which side of Asian range?
- Current price: Upper / Middle / Lower third of 24h range
- Next funding settlement: [time IST]

---

## SECTION 3: MARKET STRUCTURE

### Timeframe Hierarchy

| Timeframe | Role |
|---|---|
| Weekly / Daily | Macro trend — bull or bear market structure |
| 4H | Swing high/low identification, order block origin zones |
| 1H | Session-level structure, FVG identification |
| 15m | Entry zone confirmation, order block validation |
| 5m | Entry candle, rejection confirmation |
| 1m | Scalp entry precision, bot execution trigger |

**Required structure outputs:**
- Daily: HH/HL (uptrend) / LH/LL (downtrend) / Ranging
- 4H last significant swing: price + direction
- 1H micro-trend for current session
- Key levels:
  - Previous Day High (PDH): $___
  - Previous Day Low (PDL): $___
  - Weekly open: $___
  - Round numbers within 5% of current price
  - Most recent 4H swing high and low

---

## SECTION 4: LIQUIDITY MAPPING

### Stop Cluster Identification (HFT + Prop Desk View)

**Buy stop clusters (above price) — fuel for shorts:**
- Equal highs on 1H/4H (2+ touches of same resistance = dense buy stops)
- Round numbers: $73K, $75K, $80K, $85K, $90K, $100K
- Previous week/month high
- Previous ATH swing points

**Sell stop clusters (below price) — fuel for longs:**
- Equal lows on 1H/4H
- Round numbers: $70K, $68K, $65K, $60K
- Previous week/month low
- Realized price (~$54K)

### BTC-Specific: Liquidation Cascades

Unlike gold, BTC has perpetual futures. A sweep of a major level can trigger hundreds of millions in forced liquidations — far larger than the initial stop run. These cascades are both the target AND the entry opportunity.

**Liquidation heatmap reading (Coinglass):**
- Price is magnetically drawn to large liquidation clusters
- After sweeping a cluster → expect sharp reversal
- Enter AFTER the cascade completes, not during

### Funding Rate as Liquidity Signal

| Funding Rate | Interpretation | Trading Rule |
|---|---|---|
| > +0.15% | Longs severely overheated | NEVER go long. Fade rips. |
| +0.05% to +0.15% | Longs overextended | Reduce long size, tight stops |
| -0.05% to +0.05% | Neutral | No funding pressure |
| -0.05% to -0.15% | Shorts overextended | Reduce short size, tight stops |
| < -0.15% | Shorts severely overheated | NEVER go short. Fade drops. |

**Required liquidity output:**
- Buy stop clusters: Top 3 zones with price ranges
- Sell stop clusters: Top 3 zones with price ranges
- Liquidation clusters (Coinglass): Major levels if available
- Funding rate: Current value + direction
- Liquidity already swept today: Which levels were taken
- Next untested liquidity target: $___

---

## SECTION 5: VWAP & FAIR VALUE

### VWAP (Anchored — not daily reset since BTC is 24/7)

Use **anchored VWAP**:
- **Session VWAP**: Anchored to US market open (18:30 IST) — primary intraday reference
- **Weekly VWAP**: Anchored to Sunday midnight UTC — swing context
- **Event VWAP**: Anchored to major news events when relevant

### VWAP Distance Rules

| Price vs Session VWAP | Bias | Bot Action |
|---|---|---|
| > $500 above | Overbought / Extended | Fade rips, wait for reversion |
| $100–$500 above | Bullish zone | Buy pullbacks to VWAP |
| Within $100 | Fair value | Wait for directional break |
| $100–$500 below | Bearish zone | Sell rips to VWAP |
| > $500 below | Oversold / Extended | Fade drops, wait for reversion |

*On high-ATR days (>4% daily move): double all thresholds*

### Fair Value Gaps (FVG) — Imbalance Zones

**Definition:** A 3-candle pattern where an aggressive move leaves a gap between candle 1's high/low and candle 3's low/high.

```
Bullish FVG:  gap between [candle 1 high] and [candle 3 low]  — price likely returns
Bearish FVG:  gap between [candle 1 low]  and [candle 3 high] — price likely returns
```

**Valid FVG sizes for BTC:**
- 5m FVG: $100–$400
- 15m FVG: $200–$800
- 1H FVG: $500–$2,000

**Entry rule:** Enter at 50% of the FVG (equilibrium point)
**Quality ranking:** US session FVGs (18:30–22:30 IST) > London FVGs > Asian FVGs

**Required VWAP/FVG output:**
- Session VWAP: $___
- Distance from VWAP: $___ (above/below)
- VWAP mode: Trending away / Mean-reverting toward
- Active FVGs: [Zone: $X–$Y | TF | Bull/Bear | Filled/Unfilled]

---

## SECTION 6: ORDER FLOW & MICROSTRUCTURE

### Order Flow Proxies (Available Without L2 Data)

**Price action speed (HFT microstructure):**
- Large, fast candles = aggressive market orders = real conviction
- Small, grinding candles = passive limit orders = absorption or indecision
- Wick without follow-through = rejection = potential reversal

**Cumulative Volume Delta (CVD) — if available:**
- CVD rising + price rising = healthy momentum (buyers driving)
- CVD falling + price rising = distribution (sellers absorbing) → reversal signal
- CVD rising + price falling = accumulation (buyers absorbing) → reversal signal
- CVD falling + price falling = capitulation (genuine sell pressure)

**Open Interest (OI):**
- OI rising + price rising = new longs (leveraged bull, squeeze risk builds)
- OI rising + price falling = new shorts (may continue or be squeezed)
- OI falling + price moving = deleveraging (less reliable moves)
- OI spike + price reversal = liquidation cascade occurred

**Required order flow output:**
- Price movement speed: Aggressive (expanding candles) / Absorptive (compressing)
- CVD trend: Rising / Falling / Diverging
- Open interest: Rising / Falling / Flat + direction vs prior period
- Key level behavior: Accepted (multiple closes) or Rejected (wicks)
- Dominant order type: Large whale moves / Small retail / Mixed

---

## SECTION 7: STRATEGY SELECTION

**Choose EXACTLY ONE.** If no strategy clearly fits → output `NO TRADE — conditions unclear.`

### Strategy 1: US Session Liquidity Sweep Reversal ⭐ (Highest Frequency)
- **Condition:** Asian range established. US session sweeps one side. Price reverses back inside.
- **Signal:** 5m candle closes back inside Asian range after sweep wick
- **Confluence:** CVD diverges from sweep direction + funding rate not extreme in sweep direction
- **Entry:** Limit order $50–$100 inside the Asian range edge
- **Stop:** $300–$500 beyond the sweep wick extremity
- **Target:** Opposite side of Asian range + any FVG in path
- **R:R minimum:** 2:1
- **Duration:** 1–4 hours

### Strategy 2: VWAP Mean Reversion
- **Condition:** Price >$500 extended from session VWAP with no major catalyst
- **Signal:** Slowing momentum (compressing candles) + CVD diverging from price
- **Confluence:** OI falling (deleveraging, not new positions)
- **Entry:** Limit order at VWAP or 50% retrace toward VWAP
- **Stop:** $400–$600 beyond the extension extreme
- **Target:** VWAP level
- **R:R minimum:** 1.5:1
- **Duration:** 30 min–2 hours

### Strategy 3: Momentum Pullback Continuation
- **Condition:** Clear 1H trend (HH/HL or LH/LL). Price pulls back to VWAP / FVG / order block.
- **Signal:** CVD rising on pullback (buyers absorbing dip for longs) + OI stable or rising
- **Confluence:** ETF flows positive (for longs) / negative (for shorts)
- **Entry:** Limit at FVG midpoint or VWAP
- **Stop:** $500–$800 beyond pullback low/high
- **Target:** Previous swing high/low or next liquidity pool
- **R:R minimum:** 2:1
- **Duration:** 2–8 hours

### Strategy 4: Funding Rate Squeeze (Most Explosive)
- **Condition:** Funding rate at extreme (>+0.15% or <-0.15%) + price near liquidation cluster
- **Signal:** Price reverses from cluster with strong 1m candle + OI drops (liquidations firing)
- **Action:** Fade the overheated side (short if funding extremely positive, long if extremely negative)
- **Entry:** Market order on confirmation candle
- **Stop:** Beyond next liquidation cluster (not current one)
- **Target:** VWAP or next equilibrium level
- **R:R minimum:** 1.5:1
- **Duration:** 30 min max (hard time-based exit)
- **CRITICAL:** 1m–5m scalp only. Bot must force-close after 30 minutes.

### Strategy 5: Clean Breakout (Rare — High Standards)
- **Condition:** Price breaks major weekly level with expanding volume AND OI rising
- **Signal:** 1H candle closes beyond level + CVD surge + funding rate neutral (not >+0.1%)
- **Avoid if:** Funding already positive at breakout (longs overextended = likely fake)
- **Entry:** Stop-limit order above/below the key level
- **Stop:** $600–$1,000 below the broken level
- **Target:** Next major liquidity pool / round number
- **R:R minimum:** 2:1
- **Duration:** 4–24 hours

---

## SECTION 8: SIGNAL QUALITY SCORING

Score 0–6 before every trade. Bot only trades A (5/6) and A+ (6/6) during demo phase.

| Signal Category | Score Condition |
|---|---|
| **Macro bias** matches trade direction (DXY + risk sentiment aligned) | +1 |
| **Session structure** confirms (US session sweep direction, London context) | +1 |
| **Liquidity taken** before entry (stop cluster or liquidation swept) | +1 |
| **VWAP / FVG / Order Block** confluence at entry zone | +1 |
| **Order flow** confirms (CVD + OI aligned with trade direction) | +1 |
| **Funding rate** not opposing trade (not extreme in wrong direction) | +1 |

```
6/6 = A+  → Execute at 1.0% risk
5/6 = A   → Execute at 0.75% risk
3–4/6 = B → Execute at 0.5% risk (only after 50+ demo trades logged)
<3/6      → NO TRADE
```

---

## SECTION 9: BOT-READY TRADE PLAN OUTPUT

Every analysis MUST end with this exact block. The bot parser reads this block directly.

```
=== BTC BOT SIGNAL ===
TIMESTAMP: [ISO 8601 — e.g., 2026-04-11T14:30:00+05:30]
ASSET: BTC/USD
BIAS: [BULLISH | BEARISH | NEUTRAL]
STRATEGY: [Strategy name from Section 7]
SIGNAL_QUALITY: [A+ | A | B | NO_TRADE]
SIGNAL_SCORE: [X/6]
ENTRY_TRIGGER: [Plain language description of exact trigger]
ENTRY_TYPE: [LIMIT | MARKET | STOP_LIMIT]
ENTRY_PRICE: $[X]
STOP_LOSS: $[X]
STOP_RATIONALE: [Beyond which liquidity/structure point]
TAKE_PROFIT_1: $[X] — 60% of position
TAKE_PROFIT_2: $[X] — 30% of position
TAKE_PROFIT_3: $[X] — 10% of position (trailing)
RISK_REWARD_T1: [X:1]
RISK_REWARD_T2: [X:1]
RISK_PCT: [0.5 | 0.75 | 1.0]
BEST_TIMEFRAME: [Xm]
MAX_HOLD_TIME: [X hours — cancel if not triggered]
INVALIDATION: [Specific price or condition that cancels this setup]
FUNDING_RATE_CHECK: [Current rate value | PASS or FAIL]
ETF_FLOW_CHECK: [Positive | Negative | Neutral | Confirms or Contradicts bias]
SESSION: [Asia | London | NewYork | Overlap]
VWAP_DISTANCE: [$X above/below session VWAP]
=== END SIGNAL ===
```

---

## SECTION 10: RISK MANAGEMENT

### ATR-Based Position Sizing Formula

```python
# Step 1: Calculate risk in dollar terms
risk_dollars = account_value * risk_pct          # e.g., $100,000 * 0.01 = $1,000

# Step 2: Apply ATR volatility adjustment
if daily_atr > 3500:
    risk_dollars *= 0.50     # 50% reduction — extremely volatile day
elif daily_atr > 2000:
    risk_dollars *= 0.75     # 25% reduction — high volatility day

# Apply VIX adjustment
if vix > 25:
    risk_dollars *= 0.80     # 20% additional reduction

# Step 3: Calculate stop distance
stop_distance = abs(entry_price - stop_loss)

# Step 4: Validate stop distance
if stop_distance < 300:
    raise ValueError("Stop too tight — signal rejected")

# Step 5: Calculate position size in BTC
position_size_btc = risk_dollars / stop_distance
```

### Stop Loss Rules

- **Minimum stop distance:** $300 (below this, spread + slippage destroys the setup)
- **Scalp stops:** $300–$600 (1m–5m setups)
- **Intraday stops:** $600–$1,500 (15m–1H setups)
- Stop goes BEYOND the sweep wick, BEYOND the FVG, BEYOND the order block
- NEVER place a stop inside a liquidity cluster — it will be hunted
- For liquidation cascade setups: stop must be beyond the NEXT cluster, not the current one

### Daily Loss Limits (Bot Circuit Breakers)

```
DAILY_MAX_LOSS:         2% of account — bot stops all trading for the day
CONSECUTIVE_LOSS_PAUSE: Stop after 3 consecutive losses — resume next session
SESSION_MAX_TRADES:     5 per session
DRAWDOWN_PAUSE:         Pause if down 1.5% in any 4-hour window
```

### Hard Funding Rate Gates

```
NEVER go long  if funding rate > +0.15%
NEVER go short if funding rate < -0.15%
Exception: Funding Rate Squeeze strategy (Strategy 4) — which fades the extreme
```

---

## SECTION 11: TAKE PROFIT LOGIC

### Priority Order for Targets

1. Next untested liquidity pool (equal highs/lows, round numbers, prior swing)
2. VWAP reversion target (for extension trades)
3. FVG fill target (visible imbalance above/below)
4. Fixed R:R minimum (1.5:1 for scalps, 2:1+ for intraday)

### Partial Profit Protocol (Bot Instructions)

```
TP1 → 60% of position at first liquidity target or 1.5:1 R:R
TP2 → 30% of position at second target or 2.5:1 R:R
TP3 → 10% of position with $300 trailing stop (maximum capture)
Move stop to breakeven: immediately after TP1 fills
```

### Early Exit Triggers (Bot overrides TP targets)

- Funding rate flips to extreme opposing your position
- OI spikes sharply against your direction (new positions building against you)
- CVD diverges from price for 3+ consecutive 5m candles
- Liquidation cascade triggers on your side
- Major news event (ETF outflow report, regulatory announcement) contradicts thesis

---

## SECTION 12: TRADE NARRATIVE

Answer all three questions before executing any trade:

**Q1: Who is trapped?**
Be specific about which leveraged crowd is wrong:
> "Longs leveraged at $72,500 who chased the breakout are trapped — funding rate at +0.12% signals they are overextended and the sweep of equal highs on the 1H just gave institutions the liquidity to sell."

**Q2: Where was liquidity taken?**
Name the specific level and event:
> "Sell stops below $70,800 (equal lows on 1H dating back 3 sessions) were swept in the Asian session, triggering $180M in liquidations per Coinglass data. The sweep wick was $300 deep and instantly recovered — a classic stop run."

**Q3: Why should price move in our direction?**
Build the causal chain:
> [Macro reason] → [Session context] → [Liquidity event occurred] → [Trapped positions must cover] → [Price moves to target]
>
> Example: "DXY weakening on the week → risk-on bid supporting BTC → sell stops below $70,800 swept (fuel collected, trapped shorts covering) → price targets $74,500 equal highs / buy stop cluster on 4H"

---

## SECTION 13: POST-TRADE REVIEW (Bot Log Format)

```
=== POST-TRADE REVIEW ===
TRADE_ID: [Auto-generated UUID]
ASSET: BTC/USD
DATE_TIME_OPEN:  [ISO 8601]
DATE_TIME_CLOSE: [ISO 8601]
STRATEGY: [Strategy name]
SIGNAL_QUALITY: [Score/6]
PLANNED_ENTRY:  $[X]
ACTUAL_ENTRY:   $[X]
SLIPPAGE:       $[X]
PLANNED_SL:     $[X]
ACTUAL_SL:      $[X]
PLANNED_TP1:    $[X]
ACTUAL_EXIT:    $[X]
RESULT: [WIN | LOSS | BREAKEVEN]
PNL_$: [+/-$X]
PNL_%: [+/-X%]
HOLD_TIME: [X minutes]
CHECKS:
  macro_aligned:      [Y/N]
  session_confirmed:  [Y/N]
  liquidity_swept:    [Y/N]
  vwap_confluence:    [Y/N]
  order_flow_aligned: [Y/N]
  funding_ok:         [Y/N]
IMPROVEMENT: [One specific thing to do better next trade]
=== END REVIEW ===
```

---

## SECTION 14: DEMO TESTING PROTOCOL

### Phase 1 — Signal Validation (Trades 1–30)
- Only trade A+ signals (6/6)
- Record every signal Claude generates even if the bot skips it
- Measure: "What would have happened?" vs "What did the bot do?"
- Target baseline: 50–60% win rate on A+ setups

### Phase 2 — Strategy Refinement (Trades 31–100)
- Allow A signals (5/6) in addition to A+
- Identify which of the 6 scoring categories has highest predictive value
- Identify which strategy type wins most consistently
- Adjust risk sizing based on observed drawdown patterns

### Phase 3 — Bot Calibration (Trades 101–200)
- Introduce B signals at 0.5% risk
- Optimize entry timing: limit vs market order performance by strategy
- Test TP scales: 1.5:1 vs 2:1 vs 3:1

### Phase 4 — Live Readiness Criteria
Only go live if ALL are true after 200+ demo trades:
- ✓ Win rate on A+ signals ≥ 52%
- ✓ Average realized R:R ≥ 1.3:1
- ✓ Maximum drawdown ≤ 8%
- ✓ No single-session loss > 2%
- ✓ 200 trades with logged post-trade reviews
- ✓ No revenge-trade patterns in the log

---

## SECTION 15: 3 CORE PLAYBOOK SETUPS

### Setup 1: US Session Liquidity Sweep Reversal (Most Reliable)

**When:** 18:30–20:30 IST  
**Pattern:**
1. Asian session builds a clear range
2. US session opens and sweeps one side (high or low) by $200–$600
3. Price immediately reclaims the range boundary (5m candle close inside)
4. Enter at the range boundary level

**Bot instruction:**
```
Detect Asian range: high = max(1m highs 00:00–08:30 IST), low = min
At 18:30 IST, set alert if price breaks above/below Asian range
If price sweeps > range by $200 AND closes back inside within 3 candles:
  → Signal: Sweep Reversal (fade the sweep)
  → Entry: Limit at Asian range boundary
  → Stop: $400 beyond sweep wick extreme
  → TP1: Midpoint of Asian range
  → TP2: Opposite side of Asian range
```

### Setup 2: VWAP Pullback in Trending Session

**When:** Any session with >$800 directional move  
**Pattern:**
1. Clear 1H trend established (HH/HL or LH/LL)
2. Price pulls back to session VWAP
3. CVD rising on the pullback (buyers absorbing for longs)
4. 5m rejection candle at VWAP

**Bot instruction:**
```
Confirm 1H trend via structure (3+ consecutive HH/HL for up)
Monitor VWAP level in real time
If price touches VWAP ±$50 AND 5m candle shows rejection wick:
  → Check CVD: must be flat or rising (for long)
  → Entry: Limit at VWAP
  → Stop: $400–$500 below VWAP pullback low
  → TP: Prior session high / equal highs above
Cancel: if price stays below VWAP for > 15 minutes without reclaiming
```

### Setup 3: Funding Rate Squeeze (Most Explosive, Fastest)

**When:** Funding rate > +0.15% or < -0.15%  
**Pattern:**
1. Crowd is leveraged to extreme in one direction
2. Price approaches major liquidation cluster (Coinglass)
3. Strong reversal candle appears (1m–5m)
4. OI drops confirming liquidations fired

**Bot instruction:**
```
Monitor funding rate every 15 min
If |funding_rate| > 0.15% AND price within $500 of major liquidation cluster:
  → Watch for reversal candle (body > 80% of candle, closes against crowd direction)
  → Entry: Market order on reversal candle close
  → Stop: Beyond the liquidation cluster just swept
  → TP: VWAP or equilibrium (1.5:1 minimum)
  → HARD TIME EXIT: Close position after 30 minutes regardless
```

---

## STRICT PROHIBITIONS

1. Do NOT use RSI, MACD, Stochastics, or any lagging indicator as primary signal
2. Do NOT enter with stop distance < $300 (spread/slippage will destroy the trade)
3. Do NOT chase price that has moved > $500 from the setup trigger level
4. Do NOT trade within 15 minutes of CPI, FOMC, NFP, or major regulatory news
5. Do NOT go long when funding rate > +0.15%
6. Do NOT go short when funding rate < -0.15%
7. Do NOT average down into a losing position
8. Do NOT override the consecutive-loss pause (3 losses = session stop)
9. Do NOT take B-grade signals until 50+ demo trades are logged
10. Do NOT manually override bot risk limits — ever

---

## BOT INTEGRATION NOTES (Alpaca Paper Trading)

**API Setup:**
- Use Alpaca Paper Trading API: `https://paper-api.alpaca.markets`
- Crypto endpoint: `https://data.alpaca.markets/v1beta3/crypto`
- Symbol format: `BTC/USD`
- WebSocket for 1m bars: `wss://stream.data.alpaca.markets/v1beta3/crypto/us`

**Claude API Call Frequency:**
- Session opens (00:00, 12:30, 18:30 IST): Full analysis
- Every 1 hour during active session: Update analysis
- Post-trade: Immediate post-trade review generation
- Do NOT call more than once per 15 minutes (cost + rate limit)

**Data inputs the bot must provide to Claude at each call:**
```json
{
  "current_price": 72000,
  "daily_high": 73200,
  "daily_low": 71100,
  "asian_range_high": 72400,
  "asian_range_low": 71800,
  "session_vwap": 72150,
  "daily_atr": 1850,
  "funding_rate": 0.0082,
  "open_interest_change_pct": 2.3,
  "current_session": "newyork",
  "vix": 18.5,
  "dxy_direction": "falling",
  "last_trade_result": "WIN",
  "consecutive_losses": 0,
  "account_value": 100000,
  "daily_pnl_pct": 0.4,
  "active_fvgs": [{"top": 72300, "bottom": 72100, "direction": "bullish", "filled": false}],
  "equal_highs": [72400, 72410],
  "equal_lows": [71800, 71790],
  "key_levels": {"pdh": 73500, "pdl": 71200, "weekly_open": 70800}
}
```

**Parsing the BOT SIGNAL block:**
```python
import re

def parse_signal(claude_response: str) -> dict:
    block = re.search(r"=== BTC BOT SIGNAL ===(.*?)=== END SIGNAL ===", 
                      claude_response, re.DOTALL)
    if not block:
        return {"SIGNAL_QUALITY": "NO_TRADE"}
    lines = block.group(1).strip().splitlines()
    result = {}
    for line in lines:
        if ":" in line:
            key, _, val = line.partition(":")
            result[key.strip()] = val.strip()
    return result
```
