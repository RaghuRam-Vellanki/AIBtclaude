"""
modules/signal_generator_xau.py
Multi-strategy signal generator for XAU/USD. Fans out to 5 pod members,
collects votes, builds the Pod Report, asks Groq for the final decision.
Falls back to a deterministic vote-aggregator when no GROQ_API_KEY is set
(useful for backtests and offline runs).

Reuses the BTC `TradeSignal` dataclass; only the SIGNAL block label and the
parser sentinel differ.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from groq import Groq

from config import (
    GROQ_API_KEY,
    GROQ_MODEL,
    XAU_POD_REPORT_FILE,
    XAU_SKILL_FILE,
    XAU_STOP_LOSS_PCT,
)
from modules.signal_generator import TradeSignal
from modules.signal_gates import (
    active_archetypes, check_blackout, classify_htf, cluster_alignment,
    cluster_winners, compute_atr_stops, detect_regime, grade_clusters,
    k_for_quality, meets_rr_floor, mtf_blocks, smart_money_aligned,
)
from strategies import default_pod
from strategies.base import StrategyAgent, StrategyVote

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You are the Decision Agent of an institutional XAU/USD pod (5 strategy members).
You receive a Pod Report (one vote per member) plus the market snapshot. Aggregate them per the rules in your skill file.

Rules:
- Output ONE structured signal block; nothing after === END SIGNAL ===.
- Both LONG and SHORT are allowed (gold is paper-simulated locally).
- Stop loss = entry × (1 ∓ 0.03). The agent re-overrides anyway, just emit the 3% level.
- Quality: 5 aligned = A+, 4 aligned = A, 3 = B, else NO_TRADE.
- If regime_hmm says chaos OR pod sum |Σ| < 1.5 → NO_TRADE.
- BIAS must be BULLISH or BEARISH or NEUTRAL.
- Use the institutional macro framework (DXY → yields → COT → microstructure → trend) when writing the rationale."""


class XAUSignalGenerator:
    """Pod-fanout + Groq decision call + parser."""

    def __init__(self, strategies: Optional[List[StrategyAgent]] = None,
                 use_llm: bool = True):
        self._strategies = strategies if strategies is not None else default_pod()
        self._use_llm = use_llm and bool(GROQ_API_KEY)
        self._client = Groq(api_key=GROQ_API_KEY) if self._use_llm else None
        self._skill = self._load_skill()

    @staticmethod
    def _load_skill() -> str:
        path = Path(XAU_SKILL_FILE)
        if not path.exists():
            logger.error("XAU skill file missing at %s", path)
            return ""
        return path.read_text(encoding="utf-8")

    # ── Public entry point ────────────────────────────────────────────────────

    def generate(self, market_snapshot: Dict[str, Any], feed: Any) -> TradeSignal:
        votes = self._collect_votes(market_snapshot, feed)
        self._persist_pod_report(votes, market_snapshot)

        if not self._use_llm or self._client is None:
            return self._deterministic_signal(votes, market_snapshot)

        try:
            user_message = self._build_user_message(market_snapshot, votes)
            logger.info("Calling Groq (%s) for XAU decision (5-pod report attached)...", GROQ_MODEL)
            response = self._client.chat.completions.create(
                model=GROQ_MODEL,
                max_tokens=2048,
                temperature=0.2,
                messages=[
                    {"role": "system", "content": self._skill or _SYSTEM_PROMPT},
                    {"role": "user",   "content": user_message},
                ],
            )
            raw = response.choices[0].message.content or ""
            signal = self._parse_signal(raw)
            signal.raw_response = raw
            signal.asset = "XAU/USD"

            # If Groq is being conservative (NO_TRADE / NEUTRAL) but the
            # deterministic aggregator finds a directional edge, surface the
            # deterministic call. The user wants to see what the pod thinks,
            # not a flat "NO_TRADE" every cycle.
            if signal.signal_quality == "NO_TRADE" or signal.bias == "NEUTRAL":
                det = self._deterministic_signal(votes, market_snapshot,
                                                  fallback_reason="Groq returned NO_TRADE")
                if det.signal_quality != "NO_TRADE" and det.bias in ("BULLISH", "BEARISH"):
                    logger.info(
                        "Groq said NO_TRADE; deterministic aggregator overrides → %s %s",
                        det.bias, det.signal_quality,
                    )
                    det.raw_response = (
                        "[Groq returned NO_TRADE; surfacing deterministic pod call]\n\n"
                        + raw + "\n---\n" + (det.raw_response or "")
                    )
                    return det

            return signal
        except Exception as exc:
            logger.warning("Groq XAU call failed (%s) — falling back to deterministic aggregator", exc)
            return self._deterministic_signal(votes, market_snapshot, fallback_reason=str(exc))

    # ── Pod fan-out ───────────────────────────────────────────────────────────

    def _collect_votes(self, snap: Dict[str, Any], feed: Any) -> List[StrategyVote]:
        out: List[StrategyVote] = []
        for strat in self._strategies:
            try:
                vote = strat.vote(snap, feed)
            except Exception as exc:
                logger.warning("Strategy %s raised: %s", strat.name, exc)
                vote = StrategyVote(
                    name=strat.name, inspired_by=strat.inspired_by,
                    direction="NEUTRAL", confidence=0.0,
                    rationale=f"Strategy error: {exc}",
                )
            # Stamp archetype from the strategy class — most strategies build
            # StrategyVote() directly (not via _vote helper) and don't pass
            # archetype, so the dataclass default "FLOW" would clobber the
            # class-level declaration. Authoritative source = strat.archetype.
            vote.archetype = getattr(strat, "archetype", "FLOW")
            out.append(vote)
        return out

    @staticmethod
    def _persist_pod_report(votes: List[StrategyVote], snap: Dict[str, Any]) -> None:
        try:
            path = Path(XAU_POD_REPORT_FILE)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "current_price": snap.get("current_price", 0),
                "session": snap.get("current_session", ""),
                "pod_sum": round(sum(v.score for v in votes), 3),
                "votes": [v.to_dict() for v in votes],
            }
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, default=str)
        except Exception as exc:
            logger.debug("Failed to write pod report: %s", exc)

    # ── User message ──────────────────────────────────────────────────────────

    @staticmethod
    def _build_user_message(snap: Dict[str, Any], votes: List[StrategyVote]) -> str:
        lines = [
            "## Current Market Snapshot — XAU/USD (Gold)",
            f"Timestamp (IST): {snap.get('timestamp_ist', datetime.now(timezone.utc).isoformat())}",
            "",
            "### Price & Range",
            f"- Current price: ${snap.get('current_price', 0):,.2f}",
            f"- 24h high: ${snap.get('daily_high', 0):,.2f}  |  Low: ${snap.get('daily_low', 0):,.2f}",
            f"- Asian range: high ${snap.get('asian_range_high', 0):,.2f}  low ${snap.get('asian_range_low', 0):,.2f}",
            "",
            "### VWAP & Volatility",
            f"- Session VWAP: ${snap.get('session_vwap', 0):,.2f}",
            f"- Distance from VWAP: ${snap.get('vwap_distance', 0):+.2f}",
            f"- Daily ATR (14): ${snap.get('daily_atr', 0):,.2f}",
            f"- Price Z-score (20-period): {snap.get('zscore', 0):+.2f}",
            "",
            "### Macro Context",
            f"- Current session (IST): {snap.get('current_session', 'unknown')}",
            f"- Daily structure: {snap.get('daily_structure', 'ranging')}",
            f"- 4H structure:    {snap.get('h4_structure', 'ranging')}",
            f"- 1H structure:    {snap.get('h1_structure', 'ranging')}",
            "",
            "### Key Levels",
            f"- PDH: ${snap.get('pdh', 0):,.2f}  |  PDL: ${snap.get('pdl', 0):,.2f}",
            f"- Weekly open: ${snap.get('weekly_open', 0):,.2f}",
            f"- Round numbers nearby: {snap.get('round_numbers', [])}",
            "",
            "### POD REPORT (5 institutional strategies)",
        ]
        for v in votes:
            lines.append(
                f"- [{v.name}] (inspired by {v.inspired_by}): "
                f"{v.direction} @ confidence {v.confidence:.2f}  "
                f"(score {v.score:+.2f})  →  {v.rationale}"
            )
        pod_sum = sum(v.score for v in votes)
        aligned_long  = sum(1 for v in votes if v.direction == "LONG")
        aligned_short = sum(1 for v in votes if v.direction == "SHORT")
        lines += [
            "",
            f"Pod sum (signed): {pod_sum:+.2f}    "
            f"LONG votes: {aligned_long}    SHORT votes: {aligned_short}    "
            f"NEUTRAL: {5 - aligned_long - aligned_short}",
            "",
            "---",
            "Apply the decision rules in your skill. End with the exact === XAU BOT SIGNAL === block.",
        ]
        return "\n".join(lines)

    # ── Deterministic fallback (also used in backtests) ───────────────────────

    @staticmethod
    def _deterministic_signal(votes: List[StrategyVote], snap: Dict[str, Any],
                              fallback_reason: str = "") -> TradeSignal:
        current_price = float(snap.get("current_price", 0) or 0)
        if current_price <= 0:
            return TradeSignal(
                asset="XAU/USD", signal_quality="NO_TRADE",
                raw_response="No price data",
            )

        # ── Phase-1 gate: high-impact event blackout (FOMC/CPI/NFP/PCE) ──
        blackout, reason = check_blackout("xau")
        if blackout:
            return TradeSignal(
                timestamp=datetime.now(timezone.utc).isoformat(),
                asset="XAU/USD", bias="NEUTRAL",
                strategy=f"Event blackout: {reason}",
                signal_quality="NO_TRADE",
                signal_score="0/9",
                invalidation=f"Blackout window for {reason} clears",
                funding_check=f"Event blackout | {reason}",
                raw_response=_format_pod_summary(votes, fallback_reason or reason),
            )

        # Phase-2: regime-first archetype filter, then cluster (1 vote per archetype)
        htf_dir = classify_htf(snap.get("daily_structure", ""), snap.get("h4_structure", ""))
        regime  = detect_regime(votes, htf_dir)        # 'trend' | 'range' | 'chaos'
        active  = active_archetypes(regime)            # archetypes allowed to vote

        # chaos: don't hard-block — let cluster path run with all archetypes
        # active, then demote final quality by one rung. Lets A+/A confluence
        # still fire (at B), and gates the noise without going perma-NEUTRAL.
        chaos_mode = (regime == "chaos")
        if chaos_mode:
            active = None  # no regime filter — let all archetypes vote, then demote grade
        elif not active:
            return TradeSignal(
                timestamp=datetime.now(timezone.utc).isoformat(),
                asset="XAU/USD", bias="NEUTRAL",
                strategy=f"No active archetypes in regime={regime}",
                signal_quality="NO_TRADE",
                signal_score="0/0",
                invalidation="Regime shifts to allow active archetypes",
                funding_check=f"Regime={regime} | NO ARCHETYPES",
                raw_response=_format_pod_summary(votes, fallback_reason),
            )

        winners_long  = cluster_winners(votes, active=active)
        # cluster_alignment expects a direction; try both to find winning side.
        long_aligned, total_clusters, signed_score = cluster_alignment(winners_long, is_long=True)
        short_aligned, _,            _            = cluster_alignment(winners_long, is_long=False)

        if total_clusters == 0:
            return TradeSignal(
                timestamp=datetime.now(timezone.utc).isoformat(),
                asset="XAU/USD", bias="NEUTRAL",
                strategy=f"No active archetypes in regime={regime}",
                signal_quality="NO_TRADE",
                signal_score=f"0/{total_clusters}",
                invalidation="Regime shifts to allow active archetypes",
                funding_check=f"Regime={regime} | NO CLUSTERS",
                raw_response=_format_pod_summary(votes, fallback_reason),
            )

        is_long = signed_score >= 0
        bias    = "BULLISH" if is_long else "BEARISH"
        aligned = long_aligned if is_long else short_aligned

        # Phase-2 cluster-honest grading. Ladder:
        #   A+ : ≥ 4 aligned clusters
        #   A  : ≥ 3 aligned clusters
        #   B  : ≥ 2 aligned clusters AND smart-money (FLOW/MOMENTUM) confirms
        #   else: NO_TRADE
        smart_agrees = smart_money_aligned(winners_long, is_long)
        quality = grade_clusters(aligned, total_clusters,
                                 smart_money_confirms=smart_agrees)

        # Chaos demotion: one rung down.
        if chaos_mode:
            quality = {"A+": "A", "A": "B", "B": "C", "C": "NO_TRADE"}.get(quality, quality)

        # Macro override: if macro_flow votes opposite with ≥0.66 confidence, downgrade
        macro_vote = next((v for v in votes if v.name == "macro_flow"), None)
        if macro_vote and macro_vote.confidence >= 0.66:
            macro_long = (macro_vote.direction == "LONG")
            if macro_long != is_long and macro_vote.direction != "NEUTRAL":
                quality = {"A+": "A", "A": "B", "B": "NO_TRADE"}.get(quality, quality)

        # ── Phase-1 gate: hard MTF reject (don't long against 4H downtrend) ──
        if quality != "NO_TRADE" and mtf_blocks(htf_dir, is_long, quality):
            return TradeSignal(
                timestamp=datetime.now(timezone.utc).isoformat(),
                asset="XAU/USD", bias="NEUTRAL",
                strategy=f"MTF gate: {bias.lower()} against 4H {htf_dir}trend (need A+ to override)",
                signal_quality="NO_TRADE",
                signal_score=f"{aligned}/{total_clusters}",
                invalidation=f"4H structure flips out of {htf_dir}trend",
                funding_check=f"MTF {htf_dir} | BLOCK {bias}",
                raw_response=_format_pod_summary(votes, fallback_reason),
            )

        if quality == "NO_TRADE":
            return TradeSignal(
                timestamp=datetime.now(timezone.utc).isoformat(),
                asset="XAU/USD", bias="NEUTRAL",
                strategy=f"Insufficient cluster alignment ({aligned}/{total_clusters}) in regime={regime}",
                signal_quality="NO_TRADE",
                signal_score=f"{aligned}/{total_clusters}",
                funding_check=f"Regime={regime} | INSUFFICIENT CLUSTERS",
                raw_response=_format_pod_summary(votes, fallback_reason),
            )

        # ── Phase-1: ATR-based stops (fallback to fixed % when ATR unavailable) ──
        # Prefer 1H ATR for intraday; fall back to daily/13 (~1H proxy) then fixed %.
        atr_h1   = float(snap.get("atr_h1", 0) or 0)
        atr_daily = float(snap.get("daily_atr", 0) or 0)
        atr_for_stop = atr_h1 if atr_h1 > 0 else (atr_daily / 13.0 if atr_daily > 0 else 0.0)
        stop_loss, tp1, tp2, tp3, risk = compute_atr_stops(
            entry=current_price, is_long=is_long, atr=atr_for_stop,
            quality=quality, fallback_pct=XAU_STOP_LOSS_PCT,
            tp_r=(2.0, 3.0, 4.5), round_dp=2,
        )

        # ── Phase-1: 2:1 R:R floor (mathematically required for sustainable expectancy) ──
        if not meets_rr_floor(current_price, stop_loss, tp1):
            return TradeSignal(
                timestamp=datetime.now(timezone.utc).isoformat(),
                asset="XAU/USD", bias="NEUTRAL",
                strategy=f"R:R floor not met (TP1 < 2R from entry @ ATR={atr_for_stop:.2f})",
                signal_quality="NO_TRADE",
                signal_score=f"{aligned}/{total_clusters}",
                invalidation="Stop placement re-evaluates with new volatility",
                raw_response=_format_pod_summary(votes, fallback_reason),
            )

        risk_pct = {"A+": 1.0, "A": 0.75, "B": 0.5, "C": 0.25}.get(quality, 0.5)

        # Strategy text: name the leading cluster (not just the highest vote),
        # so the user sees which archetype actually drove the call.
        primary = winners_long.get("MOMENTUM") or winners_long.get("FLOW") or winners_long.get("TREND")
        if primary is None or primary.direction != ("LONG" if is_long else "SHORT"):
            # Fall back to highest-conf aligned cluster winner
            aligned_winners = [v for v in winners_long.values()
                               if v.direction == ("LONG" if is_long else "SHORT")]
            primary = max(aligned_winners, key=lambda v: v.confidence) if aligned_winners else None
        strategy = (f"{regime}-regime {bias.lower()} ({aligned}/{total_clusters} clusters) — "
                    f"{primary.archetype}/{primary.name}: {primary.rationale}"
                    if primary else f"{regime}-regime {bias.lower()} ({aligned}/{total_clusters} clusters)")

        cot_dir = "neutral"
        if macro_vote and macro_vote.metadata:
            cot = macro_vote.metadata.get("cot", {}) if isinstance(macro_vote.metadata, dict) else {}
            cot_dir = str(cot.get("change", "neutral"))

        return TradeSignal(
            timestamp=datetime.now(timezone.utc).isoformat(),
            asset="XAU/USD",
            bias=bias,
            strategy=strategy[:140],
            signal_quality=quality,
            signal_score=f"{aligned}/{total_clusters}",
            entry_trigger="Enter at market on next bar close confirming pod direction",
            entry_type="MARKET",
            entry_price=round(current_price, 2),
            stop_loss=stop_loss,
            stop_rationale=f"{k_for_quality(quality):.1f}×ATR(1H={atr_for_stop:.2f}) = ${risk:.2f} from entry",
            take_profit_1=tp1,
            take_profit_2=tp2,
            take_profit_3=tp3,
            risk_reward_t1="2.0:1",
            risk_reward_t2="3.0:1",
            risk_pct=risk_pct,
            best_timeframe="1h",
            max_hold_time="8 hours",
            invalidation=("Lose VWAP and close below 1H structure low" if is_long
                          else "Reclaim VWAP and close above 1H structure high"),
            funding_check=f"MTF {htf_dir} | OK",
            etf_flow_check=f"COT noncomm {cot_dir} | "
                           f"{'Confirms' if cot_dir in ('rising' if is_long else 'falling') else 'Neutral'}",
            session=snap.get("current_session", ""),
            vwap_distance=f"${snap.get('vwap_distance', 0):+.2f} from VWAP",
            raw_response=_format_pod_summary(votes, fallback_reason),
        )

    # ── Parser ────────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_signal(raw: str) -> TradeSignal:
        match = re.search(
            r"===\s*XAU BOT SIGNAL\s*===(.*?)===\s*END SIGNAL\s*===",
            raw, re.DOTALL | re.IGNORECASE,
        )
        if not match:
            logger.warning("No XAU BOT SIGNAL block found. First 400 chars:\n%s", raw[:400])
            return TradeSignal(asset="XAU/USD", signal_quality="NO_TRADE")

        block = match.group(1).strip()
        fields: Dict[str, str] = {}
        for line in block.splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                fields[key.strip().upper()] = val.strip()

        def _price(key: str) -> float:
            v = fields.get(key, "0")
            clean = re.sub(r"[,$]", "", v.split()[0].split("—")[0].split("-")[0])
            try:
                return float(clean)
            except ValueError:
                return 0.0

        def _pct(key: str) -> float:
            v = fields.get(key, "0.01")
            clean = re.sub(r"[%]", "", v.split()[0])
            try:
                return float(clean)
            except ValueError:
                return 0.01

        return TradeSignal(
            timestamp      = fields.get("TIMESTAMP", ""),
            asset          = "XAU/USD",
            bias           = fields.get("BIAS", "NEUTRAL"),
            strategy       = fields.get("STRATEGY", ""),
            signal_quality = fields.get("SIGNAL_QUALITY", "NO_TRADE"),
            signal_score   = fields.get("SIGNAL_SCORE", "0/5"),
            entry_trigger  = fields.get("ENTRY_TRIGGER", ""),
            entry_type     = fields.get("ENTRY_TYPE", "MARKET"),
            entry_price    = _price("ENTRY_PRICE"),
            stop_loss      = _price("STOP_LOSS"),
            stop_rationale = fields.get("STOP_RATIONALE", ""),
            take_profit_1  = _price("TAKE_PROFIT_1"),
            take_profit_2  = _price("TAKE_PROFIT_2"),
            take_profit_3  = _price("TAKE_PROFIT_3"),
            risk_reward_t1 = fields.get("RISK_REWARD_T1", ""),
            risk_reward_t2 = fields.get("RISK_REWARD_T2", ""),
            risk_pct       = _pct("RISK_PCT"),
            best_timeframe = fields.get("BEST_TIMEFRAME", "1h"),
            max_hold_time  = fields.get("MAX_HOLD_TIME", "8 hours"),
            invalidation   = fields.get("INVALIDATION", ""),
            funding_check  = fields.get("FUNDING_RATE_CHECK", "Pod alignment OK | PASS"),
            etf_flow_check = fields.get("ETF_FLOW_CHECK", ""),
            session        = fields.get("SESSION", ""),
            vwap_distance  = fields.get("VWAP_DISTANCE", ""),
        )


def _format_pod_summary(votes: List[StrategyVote], fallback_reason: str = "") -> str:
    lines = [f"- [{v.name}] {v.direction} {v.confidence:.2f}: {v.rationale}" for v in votes]
    if fallback_reason:
        lines.append(f"- LLM fallback reason: {fallback_reason}")
    return "\n".join(lines)
