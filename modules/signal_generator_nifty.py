"""
modules/signal_generator_nifty.py
Multi-strategy signal generator for NIFTY 50. Fans out to 5 pod members,
collects votes, builds the Pod Report, asks Groq for the final decision.
Falls back to a deterministic vote-aggregator when no GROQ_API_KEY is set
(used by backtests, offline runs, and any env without a Groq key).

Reuses the BTC `TradeSignal` dataclass; only the SIGNAL block label and the
parser sentinel differ. Prices are NIFTY index points (INR).
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
    NIFTY_POD_REPORT_FILE,
    NIFTY_SKILL_FILE,
    NIFTY_STOP_LOSS_PCT,
)
from modules.signal_generator import TradeSignal
from modules.signal_gates import (
    active_archetypes, check_blackout, classify_htf, cluster_alignment,
    cluster_winners, compute_atr_stops, detect_regime, grade_clusters,
    k_for_quality, meets_rr_floor, mtf_blocks, smart_money_aligned,
)
from strategies import default_nifty_pod
from strategies.base import StrategyAgent, StrategyVote

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You are the Decision Agent of an institutional NIFTY 50 pod (5 strategy members) for an Indian retail solotrader.
You receive a Pod Report (one vote per member) plus the market snapshot. Aggregate them per the rules in your skill file.

Rules:
- Output ONE structured signal block; nothing after === END SIGNAL ===.
- Both LONG and SHORT are allowed (NIFTY is paper-simulated locally).
- Stop loss = entry × (1 ∓ 0.015). The agent re-overrides anyway, just emit the 1.5% level.
- Quality: count members aligned with bias, EXCLUDING members whose data was 'unavailable'.
  5/5 = A+, 4/5 = A, 3/5 = B, else NO_TRADE.
- If nifty_regime_hmm says chaos OR pod sum |Σ| < 1.5 → NO_TRADE.
- BIAS must be BULLISH or BEARISH or NEUTRAL.
- Use the institutional flow framework (FII/DII → INR → VIX → BANKNIFTY pairs → option chain) when writing the rationale."""


class NIFTYSignalGenerator:
    """Pod-fanout + Groq decision call + parser for NIFTY 50."""

    def __init__(self, strategies: Optional[List[StrategyAgent]] = None,
                 use_llm: bool = True):
        self._strategies = strategies if strategies is not None else default_nifty_pod()
        self._use_llm = use_llm and bool(GROQ_API_KEY)
        self._client = Groq(api_key=GROQ_API_KEY) if self._use_llm else None
        self._skill = self._load_skill()

    @staticmethod
    def _load_skill() -> str:
        path = Path(NIFTY_SKILL_FILE)
        if not path.exists():
            logger.error("NIFTY skill file missing at %s", path)
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
            logger.info("Calling Groq (%s) for NIFTY decision (5-pod report attached)...", GROQ_MODEL)
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
            signal.asset = "NIFTY 50"

            # Override conservative Groq NO_TRADE with deterministic call when
            # the pod has a directional edge (mirrors XAU behaviour).
            if signal.signal_quality == "NO_TRADE" or signal.bias == "NEUTRAL":
                det = self._deterministic_signal(votes, market_snapshot,
                                                  fallback_reason="Groq returned NO_TRADE")
                if det.signal_quality != "NO_TRADE" and det.bias in ("BULLISH", "BEARISH"):
                    logger.info(
                        "Groq said NO_TRADE; deterministic NIFTY pod overrides → %s %s",
                        det.bias, det.signal_quality,
                    )
                    det.raw_response = (
                        "[Groq returned NO_TRADE; surfacing deterministic pod call]\n\n"
                        + raw + "\n---\n" + (det.raw_response or "")
                    )
                    return det

            return signal
        except Exception as exc:
            logger.warning("Groq NIFTY call failed (%s) — falling back to deterministic aggregator", exc)
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
            vote.archetype = getattr(strat, "archetype", "FLOW")
            out.append(vote)
        return out

    @staticmethod
    def _persist_pod_report(votes: List[StrategyVote], snap: Dict[str, Any]) -> None:
        try:
            path = Path(NIFTY_POD_REPORT_FILE)
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
            logger.debug("Failed to write NIFTY pod report: %s", exc)

    # ── User message ──────────────────────────────────────────────────────────

    @staticmethod
    def _build_user_message(snap: Dict[str, Any], votes: List[StrategyVote]) -> str:
        lines = [
            "## Current Market Snapshot — NIFTY 50",
            f"Timestamp (IST): {snap.get('timestamp_ist', datetime.now(timezone.utc).isoformat())}",
            "",
            "### Price & Range (index points)",
            f"- Current price: ₹{snap.get('current_price', 0):,.2f}",
            f"- Daily high: ₹{snap.get('daily_high', 0):,.2f}  |  Low: ₹{snap.get('daily_low', 0):,.2f}",
            "",
            "### VWAP & Volatility",
            f"- Session VWAP: ₹{snap.get('session_vwap', 0):,.2f}",
            f"- Distance from VWAP: ₹{snap.get('vwap_distance', 0):+.2f}",
            f"- Daily ATR (14): ₹{snap.get('daily_atr', 0):,.2f}",
            f"- India VIX: {snap.get('india_vix', 0):.2f}",
            "",
            "### Macro Context",
            f"- Current session: {snap.get('current_session', 'unknown')}",
            f"- Daily structure: {snap.get('daily_structure', 'ranging')}",
            f"- 1H structure:    {snap.get('h1_structure', 'ranging')}",
            f"- USDINR: {snap.get('usdinr', 0):.3f}",
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
            f"NEUTRAL: {len(votes) - aligned_long - aligned_short}",
            "",
            "---",
            "Apply the decision rules in your skill. End with the exact === NIFTY BOT SIGNAL === block.",
        ]
        return "\n".join(lines)

    # ── Deterministic fallback (also used in backtests) ───────────────────────

    @staticmethod
    def _deterministic_signal(votes: List[StrategyVote], snap: Dict[str, Any],
                              fallback_reason: str = "") -> TradeSignal:
        current_price = float(snap.get("current_price", 0) or 0)
        if current_price <= 0:
            return TradeSignal(
                asset="NIFTY 50", signal_quality="NO_TRADE",
                raw_response="No price data",
            )

        # ── Phase-1 gate: high-impact event blackout (RBI/India CPI/FOMC/etc) ──
        blackout, reason = check_blackout("nifty")
        if blackout:
            return TradeSignal(
                timestamp=datetime.now(timezone.utc).isoformat(),
                asset="NIFTY 50", bias="NEUTRAL",
                strategy=f"Event blackout: {reason}",
                signal_quality="NO_TRADE", signal_score="0/11",
                invalidation=f"Blackout window for {reason} clears",
                funding_check=f"Event blackout | {reason}",
                raw_response=_format_pod_summary(votes, fallback_reason or reason),
            )

        # ── Phase-1 gate: intra-session noise filter (first 15min auction +
        # last 5min cash-settle squeeze) ──
        ist_blocked, ist_reason = _intra_session_block(snap)
        if ist_blocked:
            return TradeSignal(
                timestamp=datetime.now(timezone.utc).isoformat(),
                asset="NIFTY 50", bias="NEUTRAL",
                strategy=f"Intra-session noise filter: {ist_reason}",
                signal_quality="NO_TRADE", signal_score="0/11",
                invalidation="Window passes (mid-session)",
                funding_check=f"Session filter | {ist_reason}",
                raw_response=_format_pod_summary(votes, fallback_reason or ist_reason),
            )

        # Phase-2: regime-first archetype filter, then cluster-based counting
        htf_dir = classify_htf(snap.get("daily_structure", ""), snap.get("h4_structure", ""))
        regime  = detect_regime(votes, htf_dir)
        active  = active_archetypes(regime)

        # India VIX → chaos override (NIFTY-specific). High VIX = re-classify as
        # chaos regardless of HMM, since intraday gamma blows out option-driven
        # strategies' assumptions.
        india_vix = float(snap.get("india_vix", 0) or 0)
        if india_vix > 22.0:
            regime = "chaos"
            active = set()

        if regime == "chaos" or not active:
            return TradeSignal(
                timestamp=datetime.now(timezone.utc).isoformat(),
                asset="NIFTY 50", bias="NEUTRAL",
                strategy=(f"India VIX {india_vix:.1f} >22 — stand down" if india_vix > 22.0
                          else f"Regime={regime} — stand down"),
                signal_quality="NO_TRADE", signal_score="0/0",
                entry_price=0.0, stop_loss=0.0,
                take_profit_1=0.0, take_profit_2=0.0, take_profit_3=0.0,
                invalidation="Regime exits chaos / VIX cools",
                funding_check=f"Regime={regime} | BLOCK",
                etf_flow_check="FII/DII proxy | Neutral",
                session=snap.get("current_session", ""),
                vwap_distance=f"₹{snap.get('vwap_distance', 0):+.2f} from VWAP",
                raw_response=_format_pod_summary(votes, fallback_reason),
            )

        winners = cluster_winners(votes, active=active)
        long_aligned, total_clusters, signed_score = cluster_alignment(winners, is_long=True)
        short_aligned, _, _                        = cluster_alignment(winners, is_long=False)

        if total_clusters == 0:
            return TradeSignal(
                timestamp=datetime.now(timezone.utc).isoformat(),
                asset="NIFTY 50", bias="NEUTRAL",
                strategy=f"No active archetypes in regime={regime}",
                signal_quality="NO_TRADE", signal_score=f"0/{total_clusters}",
                funding_check=f"Regime={regime} | NO CLUSTERS",
                raw_response=_format_pod_summary(votes, fallback_reason),
            )

        is_long = signed_score >= 0
        bias    = "BULLISH" if is_long else "BEARISH"
        aligned = long_aligned if is_long else short_aligned

        smart_agrees = smart_money_aligned(winners, is_long)
        quality = grade_clusters(aligned, total_clusters,
                                 smart_money_confirms=smart_agrees)

        # FII/DII override: if fii_dii_flow votes opposite with confidence ≥ 0.66, downgrade.
        # We keep this as a dedicated check because FLOW cluster may have been
        # represented by a different strategy (orderflow / session_volume) — FII
        # is the macro-flow gate for an Indian retail solotrader.
        flow_vote = next((v for v in votes if v.name == "nifty_fii_dii_flow"), None)
        if flow_vote and flow_vote.confidence >= 0.66:
            flow_long = (flow_vote.direction == "LONG")
            if flow_long != is_long and flow_vote.direction != "NEUTRAL":
                quality = {"A+": "A", "A": "B", "B": "NO_TRADE"}.get(quality, quality)

        # ── Phase-1 gate: hard MTF reject ──
        if quality != "NO_TRADE" and mtf_blocks(htf_dir, is_long, quality):
            return TradeSignal(
                timestamp=datetime.now(timezone.utc).isoformat(),
                asset="NIFTY 50", bias="NEUTRAL",
                strategy=f"MTF gate: {bias.lower()} against 4H {htf_dir}trend (need A+ to override)",
                signal_quality="NO_TRADE", signal_score=f"{aligned}/{total_clusters}",
                invalidation=f"4H structure flips out of {htf_dir}trend",
                funding_check=f"MTF {htf_dir} | BLOCK {bias}",
                raw_response=_format_pod_summary(votes, fallback_reason),
            )

        if quality == "NO_TRADE":
            return TradeSignal(
                timestamp=datetime.now(timezone.utc).isoformat(),
                asset="NIFTY 50", bias="NEUTRAL",
                strategy=f"Insufficient cluster alignment ({aligned}/{total_clusters}) in regime={regime}",
                signal_quality="NO_TRADE",
                signal_score=f"{aligned}/{total_clusters}",
                funding_check=f"Regime={regime} | INSUFFICIENT CLUSTERS",
                raw_response=_format_pod_summary(votes, fallback_reason),
            )

        # ── Phase-1: ATR-based stops (15m ATR for index scalp; 1H proxy fallback) ──
        atr_15m  = float(snap.get("atr_15m", 0) or 0)
        atr_h1   = float(snap.get("atr_h1", 0) or 0)
        atr_daily = float(snap.get("daily_atr", 0) or 0)
        # NIFTY moves ~0.5-1% intraday; 15m ATR best matches index futures stop placement
        atr_for_stop = atr_15m if atr_15m > 0 else (atr_h1 if atr_h1 > 0 else
                                                     (atr_daily / 13.0 if atr_daily > 0 else 0.0))
        stop_loss, tp1, tp2, tp3, risk = compute_atr_stops(
            entry=current_price, is_long=is_long, atr=atr_for_stop, quality=quality,
            fallback_pct=NIFTY_STOP_LOSS_PCT, tp_r=(2.0, 3.0, 4.5), round_dp=2,
        )

        # ── Phase-1: 2:1 R:R floor ──
        if not meets_rr_floor(current_price, stop_loss, tp1):
            return TradeSignal(
                timestamp=datetime.now(timezone.utc).isoformat(),
                asset="NIFTY 50", bias="NEUTRAL",
                strategy=f"R:R floor not met (TP1 < 2R from entry @ ATR={atr_for_stop:.2f})",
                signal_quality="NO_TRADE", signal_score=f"{aligned}/{total_clusters}",
                raw_response=_format_pod_summary(votes, fallback_reason),
            )

        risk_pct = {"A+": 1.0, "A": 0.75, "B": 0.5}.get(quality, 0.5)

        primary = winners.get("MOMENTUM") or winners.get("FLOW") or winners.get("OPTIONS")
        if primary is None or primary.direction != ("LONG" if is_long else "SHORT"):
            aligned_winners = [v for v in winners.values()
                               if v.direction == ("LONG" if is_long else "SHORT")]
            primary = max(aligned_winners, key=lambda v: v.confidence) if aligned_winners else None
        strategy = (f"{regime}-regime {bias.lower()} ({aligned}/{total_clusters} clusters) — "
                    f"{primary.archetype}/{primary.name}: {primary.rationale}"
                    if primary else f"{regime}-regime {bias.lower()} ({aligned}/{total_clusters} clusters)")

        # FII flow direction text
        fii_dir = "neutral"
        if flow_vote and flow_vote.metadata:
            fii = flow_vote.metadata.get("fii", {}) if isinstance(flow_vote.metadata, dict) else {}
            fii_dir = str(fii.get("flow", "neutral"))

        return TradeSignal(
            timestamp=datetime.now(timezone.utc).isoformat(),
            asset="NIFTY 50",
            bias=bias,
            strategy=strategy[:140],
            signal_quality=quality,
            signal_score=f"{aligned}/{total_clusters}",
            entry_trigger="Enter at market on next bar close confirming pod direction",
            entry_type="MARKET",
            entry_price=round(current_price, 2),
            stop_loss=stop_loss,
            stop_rationale=f"{k_for_quality(quality):.1f}×ATR(15m={atr_for_stop:.2f}) = ₹{risk:.2f} from entry",
            take_profit_1=tp1,
            take_profit_2=tp2,
            take_profit_3=tp3,
            risk_reward_t1="2.0:1",
            risk_reward_t2="3.0:1",
            risk_pct=risk_pct,
            best_timeframe="15m",
            max_hold_time="4 hours",
            invalidation=("Lose VWAP and close below 1H structure low" if is_long
                          else "Reclaim VWAP and close above 1H structure high"),
            funding_check=f"MTF {htf_dir} | OK",
            etf_flow_check=f"FII {fii_dir} | "
                           f"{'Confirms' if (fii_dir == 'buying' and is_long) or (fii_dir == 'selling' and not is_long) else 'Neutral'}",
            session=snap.get("current_session", ""),
            vwap_distance=f"₹{snap.get('vwap_distance', 0):+.2f} from VWAP",
            raw_response=_format_pod_summary(votes, fallback_reason),
        )

    # ── Parser ────────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_signal(raw: str) -> TradeSignal:
        match = re.search(
            r"===\s*NIFTY BOT SIGNAL\s*===(.*?)===\s*END SIGNAL\s*===",
            raw, re.DOTALL | re.IGNORECASE,
        )
        if not match:
            logger.warning("No NIFTY BOT SIGNAL block found. First 400 chars:\n%s", raw[:400])
            return TradeSignal(asset="NIFTY 50", signal_quality="NO_TRADE")

        block = match.group(1).strip()
        fields: Dict[str, str] = {}
        for line in block.splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                fields[key.strip().upper()] = val.strip()

        def _price(key: str) -> float:
            v = fields.get(key, "0")
            clean = re.sub(r"[,$₹]", "", v.split()[0].split("—")[0].split("-")[0])
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
            asset          = "NIFTY 50",
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
            max_hold_time  = fields.get("MAX_HOLD_TIME", "4 hours"),
            invalidation   = fields.get("INVALIDATION", ""),
            funding_check  = fields.get("FUNDING_RATE_CHECK", "Pod alignment OK | PASS"),
            etf_flow_check = fields.get("FII_DII_CHECK", ""),
            session        = fields.get("SESSION", ""),
            vwap_distance  = fields.get("VWAP_DISTANCE", ""),
        )


def _format_pod_summary(votes: List[StrategyVote], fallback_reason: str = "") -> str:
    lines = [f"- [{v.name}] {v.direction} {v.confidence:.2f}: {v.rationale}" for v in votes]
    if fallback_reason:
        lines.append(f"- LLM fallback reason: {fallback_reason}")
    return "\n".join(lines)


def _intra_session_block(snap: Dict[str, Any]) -> tuple:
    """NIFTY 50 cash session is 09:15–15:30 IST. Skip:
      - first 15 minutes (09:15-09:30): pre-open auction noise, wide spreads
      - last 5 minutes  (15:25-15:30): cash-settle squeeze, freak trades
    Returns (blocked: bool, reason: str)."""
    from datetime import datetime, timezone, timedelta
    ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    if ist.weekday() >= 5:        # Sat/Sun — market closed; no point firing signal
        return False, ""
    h, m = ist.hour, ist.minute
    if h == 9 and m < 30:
        return True, f"first-15min auction noise (IST {h:02d}:{m:02d})"
    if h == 15 and m >= 25:
        return True, f"last-5min cash-settle squeeze (IST {h:02d}:{m:02d})"
    return False, ""
