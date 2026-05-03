"""
signal_generator.py
Calls OpenAI API (gpt-4o-mini) with BTC_INSTITUTIONAL_SKILL.md as system prompt
and a structured market snapshot as the user message.
Parses the === BTC BOT SIGNAL === block from the response.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from groq import Groq

from config import DEMO_MODE, GROQ_API_KEY, GROQ_MODEL, SKILL_FILE, STOP_LOSS_PCT

logger = logging.getLogger(__name__)


# ── Signal dataclass ──────────────────────────────────────────────────────────

@dataclass
class TradeSignal:
    timestamp:       str   = ""
    asset:           str   = "BTC/USD"
    bias:            str   = "NEUTRAL"
    strategy:        str   = ""
    signal_quality:  str   = "NO_TRADE"
    signal_score:    str   = "0/6"
    entry_trigger:   str   = ""
    entry_type:      str   = "LIMIT"
    entry_price:     float = 0.0
    stop_loss:       float = 0.0
    stop_rationale:  str   = ""
    take_profit_1:   float = 0.0
    take_profit_2:   float = 0.0
    take_profit_3:   float = 0.0
    risk_reward_t1:  str   = ""
    risk_reward_t2:  str   = ""
    risk_pct:        float = 0.01
    best_timeframe:  str   = "15m"
    max_hold_time:   str   = "4 hours"
    invalidation:    str   = ""
    funding_check:   str   = ""
    etf_flow_check:  str   = ""
    session:         str   = ""
    vwap_distance:   str   = ""
    raw_response:    str   = ""

    @property
    def is_tradeable(self) -> bool:
        return self.signal_quality in ("A+", "A") and self.entry_price > 0 and self.stop_loss > 0

    @property
    def stop_distance(self) -> float:
        return abs(self.entry_price - self.stop_loss)


# ── Signal Generator ──────────────────────────────────────────────────────────

class SignalGenerator:
    """Wraps Groq API call and signal parsing."""

    def __init__(self):
        self._client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY and not DEMO_MODE else None
        self._skill_content = self._load_skill()

    def _load_skill(self) -> str:
        path = Path(SKILL_FILE)
        if not path.exists():
            logger.error("SKILL file not found at %s", path)
            return ""
        return path.read_text(encoding="utf-8")

    # Compact system prompt — keeps token count low for Groq free tier
    _SYSTEM_PROMPT = """You are an institutional BTC/USD trading agent. Long-only (no shorting).
Analyze the market snapshot and output ONE structured trade signal.
Rules: No RSI/MACD. Use liquidity, VWAP, structure only.
BIAS must be BULLISH only. We cannot short — if bearish, set SIGNAL_QUALITY: NO_TRADE.
Stop loss: 5% below entry (entry * 0.95).
Risk per trade: 0.5-1%.
Only trade A/A+ quality (5-6/6 signals aligned).
Always end with the exact === BTC BOT SIGNAL === block."""

    def generate(self, market_snapshot: Dict[str, Any]) -> TradeSignal:
        """Send market snapshot to Groq and return a parsed TradeSignal."""
        if self._client is None:
            return self._generate_demo_signal(market_snapshot)

        user_message = self._build_user_message(market_snapshot)
        logger.info("Calling Groq API (%s) for signal generation...", GROQ_MODEL)

        try:
            response = self._client.chat.completions.create(
                model=GROQ_MODEL,
                max_tokens=2048,
                messages=[
                    {"role": "system", "content": self._SYSTEM_PROMPT},
                    {"role": "user",   "content": user_message},
                ],
                temperature=0.2,
            )
            raw = response.choices[0].message.content or ""
            logger.debug("Groq response length: %d chars", len(raw))
            signal = self._parse_signal(raw)
            signal.raw_response = raw
            return signal

        except Exception as exc:
            logger.error("Groq API error: %s", exc)
            return self._generate_demo_signal(market_snapshot, fallback_reason=str(exc))

    def _generate_demo_signal(
        self,
        snap: Dict[str, Any],
        fallback_reason: str = "",
    ) -> TradeSignal:
        current_price = float(snap.get("current_price", 0) or 0)
        if current_price <= 0:
            return TradeSignal(
                signal_quality="NO_TRADE",
                raw_response="No public market price available yet.",
            )

        daily_structure = snap.get("daily_structure", "ranging")
        h1_structure = snap.get("h1_structure", "ranging")
        session_vwap = float(snap.get("session_vwap", 0) or 0)
        vwap_distance = float(snap.get("vwap_distance", 0) or 0)
        zscore = float(snap.get("zscore", 0) or 0)
        daily_atr = float(snap.get("daily_atr", 0) or 0)
        pdh = float(snap.get("pdh", 0) or 0)
        pdl = float(snap.get("pdl", 0) or 0)
        equal_highs = snap.get("equal_highs", []) or []
        equal_lows = snap.get("equal_lows", []) or []
        fvgs = snap.get("active_fvgs", []) or []

        bullish_points = 0
        bearish_points = 0
        reasons: list[str] = []

        if daily_structure == "uptrend":
            bullish_points += 2
            reasons.append("Daily structure is uptrend")
        elif daily_structure == "downtrend":
            bearish_points += 2
            reasons.append("Daily structure is downtrend")

        if h1_structure == "uptrend":
            bullish_points += 2
            reasons.append("1H structure is uptrend")
        elif h1_structure == "downtrend":
            bearish_points += 2
            reasons.append("1H structure is downtrend")

        if session_vwap:
            if current_price >= session_vwap:
                bullish_points += 1
                reasons.append("Price is above session VWAP")
            else:
                bearish_points += 1
                reasons.append("Price is below session VWAP")

        if zscore <= -0.75:
            bullish_points += 1
            reasons.append("Price is below its 20-period mean and may revert upward")
        elif zscore >= 0.75:
            bearish_points += 1
            reasons.append("Price is above its 20-period mean and may revert lower")

        bullish_fvgs = [f for f in fvgs if f.get("direction") == "bullish"]
        bearish_fvgs = [f for f in fvgs if f.get("direction") == "bearish"]
        if bullish_fvgs:
            bullish_points += 1
            reasons.append("Unfilled bullish fair value gap is nearby")
        if bearish_fvgs:
            bearish_points += 1
            reasons.append("Unfilled bearish fair value gap is nearby")

        if equal_lows:
            bullish_points += 1
            reasons.append("Sell-side liquidity pool exists below price")
        if equal_highs:
            bearish_points += 1
            reasons.append("Buy-side liquidity pool exists above price")

        if pdh and current_price > pdh:
            bullish_points += 1
            reasons.append("Price is trading above previous day high")
        elif pdl and current_price < pdl:
            bearish_points += 1
            reasons.append("Price is trading below previous day low")

        bias = "NEUTRAL"
        strategy = "Wait for clearer alignment"
        score = max(bullish_points, bearish_points)
        if bullish_points >= bearish_points + 2 and bullish_points >= 4:
            bias = "BULLISH"
            strategy = "Trend continuation with VWAP support"
        elif bearish_points >= bullish_points + 2 and bearish_points >= 4:
            bias = "BEARISH"
            strategy = "Mean-reversion short from overhead liquidity"
        elif bullish_points > bearish_points and bullish_points >= 3:
            bias = "BULLISH"
            strategy = "Early bullish continuation setup"
        elif bearish_points > bullish_points and bearish_points >= 3:
            bias = "BEARISH"
            strategy = "Early bearish continuation setup"
        elif score >= 3 and session_vwap:
            bias = "BULLISH" if current_price >= session_vwap else "BEARISH"
            strategy = "VWAP-led directional demo setup"

        quality_map = {
            6: "A+",
            5: "A",
            4: "B",
        }
        signal_quality = quality_map.get(score, "NO_TRADE")
        if bias == "NEUTRAL":
            signal_quality = "NO_TRADE"

        if bias == "BULLISH":
            stop_loss = round(current_price * (1 - STOP_LOSS_PCT), 2)
            risk = current_price - stop_loss
            take_profit_1 = round(current_price + (risk * 1.5), 2)
            take_profit_2 = round(current_price + (risk * 2.5), 2)
            take_profit_3 = round(current_price + (risk * 3.5), 2)
            invalidation = "Lose session VWAP and close below the current 1H structure low"
            funding = "Public demo mode | PASS"
        elif bias == "BEARISH":
            stop_loss = round(current_price * (1 + STOP_LOSS_PCT), 2)
            risk = stop_loss - current_price
            take_profit_1 = round(current_price - (risk * 1.5), 2)
            take_profit_2 = round(current_price - (risk * 2.5), 2)
            take_profit_3 = round(current_price - (risk * 3.5), 2)
            invalidation = "Reclaim session VWAP and close above the current 1H structure high"
            funding = "Public demo mode | PASS"
        else:
            stop_loss = take_profit_1 = take_profit_2 = take_profit_3 = 0.0
            invalidation = "No edge: structure and VWAP are not aligned"
            funding = "Public demo mode | PASS"

        raw_reason = "\n".join(f"- {reason}" for reason in reasons[:6]) or "- No clear institutional edge"
        if fallback_reason:
            raw_reason += f"\n- LLM fallback reason: {fallback_reason}"

        return TradeSignal(
            timestamp=datetime.now(timezone.utc).isoformat(),
            bias=bias,
            strategy=strategy,
            signal_quality=signal_quality,
            signal_score=f"{score}/6",
            entry_trigger="Enter near current market price after the next 1m candle confirms the direction",
            entry_type="MARKET",
            entry_price=round(current_price, 2) if bias != "NEUTRAL" else 0.0,
            stop_loss=stop_loss,
            stop_rationale=f"Fixed {int(STOP_LOSS_PCT * 100)}% demo stop from entry",
            take_profit_1=take_profit_1,
            take_profit_2=take_profit_2,
            take_profit_3=take_profit_3,
            risk_reward_t1="1.5:1" if bias != "NEUTRAL" else "",
            risk_reward_t2="2.5:1" if bias != "NEUTRAL" else "",
            risk_pct=0.5 if signal_quality == "B" else 0.75 if signal_quality == "A" else 1.0,
            best_timeframe="1H",
            max_hold_time="8 hours",
            invalidation=invalidation,
            funding_check=funding,
            etf_flow_check="Public demo mode | Neutral",
            session=snap.get("current_session", ""),
            vwap_distance=f"${abs(vwap_distance):,.0f} {'above' if vwap_distance >= 0 else 'below'} session VWAP",
            raw_response=raw_reason,
        )

    # ── User message builder ──────────────────────────────────────────────────

    @staticmethod
    def _build_user_message(snap: Dict[str, Any]) -> str:
        lines = [
            "## Current Market Snapshot — BTC/USD",
            f"Timestamp (IST): {snap.get('timestamp_ist', datetime.now(timezone.utc).isoformat())}",
            "",
            "### Price & Range",
            f"- Current price: ${snap.get('current_price', 0):,.0f}",
            f"- 24h high: ${snap.get('daily_high', 0):,.0f}",
            f"- 24h low:  ${snap.get('daily_low',  0):,.0f}",
            f"- Asian session high: ${snap.get('asian_range_high', 0):,.0f}",
            f"- Asian session low:  ${snap.get('asian_range_low',  0):,.0f}",
            "",
            "### VWAP & Volatility",
            f"- Session VWAP: ${snap.get('session_vwap', 0):,.0f}",
            f"- Distance from VWAP: ${snap.get('vwap_distance', 0):+,.0f}",
            f"- Daily ATR (14): ${snap.get('daily_atr', 0):,.0f}",
            f"- Price Z-score (20-period): {snap.get('zscore', 0):.2f}",
            "",
            "### Funding & Open Interest",
            f"- Funding rate: {snap.get('funding_rate', 0):.4f}",
            f"- OI change (last period): {snap.get('open_interest_change_pct', 0):+.1f}%",
            "",
            "### Macro Context",
            f"- Current session: {snap.get('current_session', 'unknown')}",
            f"- VIX: {snap.get('vix', 0):.1f}",
            f"- DXY direction: {snap.get('dxy_direction', 'unknown')}",
            f"- Macro bias: {snap.get('macro_bias', 'neutral')}",
            "",
            "### Market Structure",
            f"- Daily structure: {snap.get('daily_structure', 'ranging')}",
            f"- 1H structure:    {snap.get('h1_structure', 'ranging')}",
            f"- Key levels:",
            f"  PDH: ${snap.get('pdh', 0):,.0f}  |  PDL: ${snap.get('pdl', 0):,.0f}",
            f"  Weekly open: ${snap.get('weekly_open', 0):,.0f}",
            f"  Swing highs: {snap.get('swing_highs', [])}",
            f"  Swing lows:  {snap.get('swing_lows', [])}",
            f"  Round numbers nearby: {snap.get('round_numbers', [])}",
            "",
            "### Liquidity Clusters",
            f"- Equal highs (buy stop clusters): {snap.get('equal_highs', [])}",
            f"- Equal lows (sell stop clusters): {snap.get('equal_lows', [])}",
            "",
            "### Fair Value Gaps (unfilled)",
        ]
        for fvg in snap.get("active_fvgs", []):
            lines.append(
                f"  - {fvg.get('direction','?').upper()} FVG "
                f"${fvg.get('bottom', 0):,.0f}–${fvg.get('top', 0):,.0f} "
                f"[{fvg.get('timeframe','?')}] mid=${fvg.get('midpoint', 0):,.0f}"
            )
        lines += [
            "",
            "### Bot State",
            f"- Consecutive losses: {snap.get('consecutive_losses', 0)}",
            f"- Daily P&L: {snap.get('daily_pnl_pct', 0):+.2f}%",
            f"- Last trade result: {snap.get('last_trade_result', 'N/A')}",
            f"- Account value: ${snap.get('account_value', 25):,.2f}",
            "",
            "---",
            "Analyze using: macro context, session structure, liquidity clusters, VWAP distance,",
            "fair value gaps, order flow speed, and funding rate.",
            "Score 0-6 signals. Only output A/A+ trades.",
            "",
            "End your response with EXACTLY this block (fill in values):",
            "=== BTC BOT SIGNAL ===",
            "TIMESTAMP: <ISO8601>",
            "ASSET: BTC/USD",
            "BIAS: <BULLISH|BEARISH|NEUTRAL>",
            "STRATEGY: <strategy name>",
            "SIGNAL_QUALITY: <A+|A|B|NO_TRADE>",
            "SIGNAL_SCORE: <X/6>",
            "ENTRY_TRIGGER: <description>",
            "ENTRY_TYPE: <LIMIT|MARKET|STOP_LIMIT>",
            "ENTRY_PRICE: $<price>",
            "STOP_LOSS: $<price>",
            "STOP_RATIONALE: <why>",
            "TAKE_PROFIT_1: $<price> -- 60% of position",
            "TAKE_PROFIT_2: $<price> -- 30% of position",
            "TAKE_PROFIT_3: $<price> -- 10% of position",
            "RISK_REWARD_T1: <X:1>",
            "RISK_REWARD_T2: <X:1>",
            "RISK_PCT: <0.5|0.75|1.0>",
            "BEST_TIMEFRAME: <Xm>",
            "MAX_HOLD_TIME: <X hours>",
            "INVALIDATION: <condition>",
            "FUNDING_RATE_CHECK: <value> | <PASS|FAIL>",
            "ETF_FLOW_CHECK: <Positive|Negative|Neutral> | <Confirms|Contradicts>",
            "SESSION: <Asia|London|NewYork|Overlap>",
            "VWAP_DISTANCE: $<X> <above|below> session VWAP",
            "=== END SIGNAL ===",
        ]
        return "\n".join(lines)

    # ── Parser ────────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_signal(raw: str) -> TradeSignal:
        """Extract the === BTC BOT SIGNAL === block and populate a TradeSignal."""
        match = re.search(
            r"===\s*BTC BOT SIGNAL\s*===(.*?)===\s*END SIGNAL\s*===",
            raw, re.DOTALL | re.IGNORECASE,
        )
        if not match:
            logger.warning("No BOT SIGNAL block found in response. Raw (first 400 chars):\n%s", raw[:400])
            return TradeSignal(signal_quality="NO_TRADE")

        block = match.group(1).strip()
        fields: Dict[str, str] = {}
        for line in block.splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                fields[key.strip().upper()] = val.strip()

        def _price(key: str) -> float:
            raw_val = fields.get(key, "0")
            clean = re.sub(r"[,$]", "", raw_val.split()[0].split("—")[0].split("-")[0])
            try:
                return float(clean)
            except ValueError:
                return 0.0

        def _pct(key: str) -> float:
            raw_val = fields.get(key, "0.01")
            clean = re.sub(r"[%]", "", raw_val.split()[0])
            try:
                return float(clean)
            except ValueError:
                return 0.01

        return TradeSignal(
            timestamp      = fields.get("TIMESTAMP", ""),
            bias           = fields.get("BIAS", "NEUTRAL"),
            strategy       = fields.get("STRATEGY", ""),
            signal_quality = fields.get("SIGNAL_QUALITY", "NO_TRADE"),
            signal_score   = fields.get("SIGNAL_SCORE", "0/6"),
            entry_trigger  = fields.get("ENTRY_TRIGGER", ""),
            entry_type     = fields.get("ENTRY_TYPE", "LIMIT"),
            entry_price    = _price("ENTRY_PRICE"),
            stop_loss      = _price("STOP_LOSS"),
            stop_rationale = fields.get("STOP_RATIONALE", ""),
            take_profit_1  = _price("TAKE_PROFIT_1"),
            take_profit_2  = _price("TAKE_PROFIT_2"),
            take_profit_3  = _price("TAKE_PROFIT_3"),
            risk_reward_t1 = fields.get("RISK_REWARD_T1", ""),
            risk_reward_t2 = fields.get("RISK_REWARD_T2", ""),
            risk_pct       = _pct("RISK_PCT"),
            best_timeframe = fields.get("BEST_TIMEFRAME", "15m"),
            max_hold_time  = fields.get("MAX_HOLD_TIME", "4 hours"),
            invalidation   = fields.get("INVALIDATION", ""),
            funding_check  = fields.get("FUNDING_RATE_CHECK", ""),
            etf_flow_check = fields.get("ETF_FLOW_CHECK", ""),
            session        = fields.get("SESSION", ""),
            vwap_distance  = fields.get("VWAP_DISTANCE", ""),
        )
