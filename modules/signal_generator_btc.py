"""
modules/signal_generator_btc.py
Pod-aware signal generator for BTC/USD (Phase 4).

Mirrors XAUSignalGenerator's shape:
  - Fans out to default_btc_pod() (6 strategies)
  - Persists pod report to BTC_POD_REPORT_FILE
  - Calls Groq if available, else deterministic vote-aggregator
  - Strong-vote override (≥0.85 confidence wins even at low pod sum)

Demo-mode friendly: works without Alpaca, without Groq, signal-only.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import (
    BTC_FUNDING_BLOCK,
    BTC_FUNDING_DOWNGRADE,
    BTC_POD_REPORT_FILE,
    GROQ_API_KEY,
    GROQ_MODEL,
    STOP_LOSS_PCT,
    SKILL_FILE,
)
from modules.signal_generator import TradeSignal
from modules.signal_gates import (
    active_archetypes, check_blackout, classify_htf, cluster_alignment,
    cluster_winners, compute_atr_stops, detect_regime, grade_clusters,
    k_for_quality, meets_rr_floor, mtf_blocks, smart_money_aligned,
)
from strategies import default_btc_pod
from strategies.base import StrategyAgent, StrategyVote

try:
    from groq import Groq
except Exception:                            # pragma: no cover
    Groq = None  # type: ignore

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You are the Decision Agent of an institutional BTC/USD pod (6 strategy members).
Aggregate the Pod Report votes into a single trade signal.

Rules:
- Output exactly one structured `=== BOT SIGNAL ===` block; nothing after `=== END SIGNAL ===`.
- LONG and SHORT both allowed (BTC trades 24×7).
- Stop loss = 5% from entry (the agent overrides anyway, just emit the level).
- Quality grade: 5+ aligned of 6 = A+, 4 = A, 3 = B, else NO_TRADE.
- BIAS must be BULLISH | BEARISH | NEUTRAL.
- Refer to microstructure, order-flow, VWAP, scalping confluence, session
  volume, and HMM regime when writing rationale."""


class BTCSignalGenerator:
    """Pod fan-out + Groq decision (optional) + deterministic fallback."""

    def __init__(self, strategies: Optional[List[StrategyAgent]] = None,
                 use_llm: bool = True):
        self._strategies = strategies if strategies is not None else default_btc_pod()
        self._use_llm = use_llm and bool(GROQ_API_KEY) and Groq is not None
        self._client = Groq(api_key=GROQ_API_KEY) if self._use_llm else None
        self._skill = self._load_skill()

    @staticmethod
    def _load_skill() -> str:
        p = Path(SKILL_FILE)
        if not p.exists():
            return ""
        try:
            return p.read_text(encoding="utf-8")
        except Exception:
            return ""

    def generate(self, market_snapshot: Dict[str, Any], feed: Any) -> TradeSignal:
        votes = self._collect_votes(market_snapshot, feed)
        self._persist_pod_report(votes, market_snapshot)

        if not self._use_llm or self._client is None:
            return self._deterministic_signal(votes, market_snapshot)

        try:
            user_message = self._build_user_message(market_snapshot, votes)
            logger.info("Calling Groq (%s) for BTC decision (6-pod report)...", GROQ_MODEL)
            resp = self._client.chat.completions.create(
                model=GROQ_MODEL,
                max_tokens=1500,
                temperature=0.2,
                messages=[
                    {"role": "system", "content": self._skill or _SYSTEM_PROMPT},
                    {"role": "user",   "content": user_message},
                ],
            )
            raw = resp.choices[0].message.content or ""
            sig = self._parse_groq_block(raw, market_snapshot, votes)
            sig.raw_response = raw
            sig.asset = "BTC/USD"
            # Override conservative NO_TRADE with deterministic when pod fires
            if sig.signal_quality == "NO_TRADE" or sig.bias == "NEUTRAL":
                det = self._deterministic_signal(votes, market_snapshot,
                                                  fallback_reason="Groq returned NO_TRADE")
                if det.signal_quality != "NO_TRADE" and det.bias in ("BULLISH", "BEARISH"):
                    det.raw_response = (
                        "[Groq returned NO_TRADE; surfacing deterministic pod call]\n\n"
                        + raw + "\n---\n" + (det.raw_response or "")
                    )
                    return det
            return sig
        except Exception as exc:
            logger.warning("Groq BTC call failed (%s) — deterministic fallback", exc)
            return self._deterministic_signal(votes, market_snapshot, fallback_reason=str(exc))

    # ── Pod fan-out ──────────────────────────────────────────────────────────

    def _collect_votes(self, snap: Dict[str, Any], feed: Any) -> List[StrategyVote]:
        out: List[StrategyVote] = []
        for strat in self._strategies:
            try:
                v = strat.vote(snap, feed)
            except Exception as exc:
                logger.warning("Strategy %s raised: %s", strat.name, exc)
                v = StrategyVote(name=strat.name, inspired_by=strat.inspired_by,
                                 direction="NEUTRAL", confidence=0.0,
                                 rationale=f"Strategy error: {exc}")
            v.archetype = getattr(strat, "archetype", "FLOW")
            out.append(v)
        return out

    @staticmethod
    def _persist_pod_report(votes: List[StrategyVote], snap: Dict[str, Any]) -> None:
        try:
            path = Path(BTC_POD_REPORT_FILE)
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
            logger.debug("Failed to write BTC pod report: %s", exc)

    @staticmethod
    def _build_user_message(snap: Dict[str, Any], votes: List[StrategyVote]) -> str:
        lines = [
            "## BTC/USD Snapshot",
            f"- Current price: ${snap.get('current_price', 0):,.2f}",
            f"- Session VWAP:  ${snap.get('session_vwap', 0):,.2f}",
            f"- Daily ATR:     ${snap.get('daily_atr', 0):,.2f}",
            f"- 4H struct:     {snap.get('h4_structure', 'ranging')}",
            f"- 1H struct:     {snap.get('h1_structure', 'ranging')}",
            "",
            "### POD (6 strategies)",
        ]
        for v in votes:
            lines.append(
                f"- [{v.name}] (inspired by {v.inspired_by}): "
                f"{v.direction} @ conf {v.confidence:.2f} (score {v.score:+.2f}) → {v.rationale}"
            )
        ps = sum(v.score for v in votes)
        nl = sum(1 for v in votes if v.direction == "LONG")
        ns = sum(1 for v in votes if v.direction == "SHORT")
        lines += [
            "",
            f"Pod sum: {ps:+.2f}    LONG votes: {nl}    SHORT votes: {ns}",
            "Apply your skill and emit the BOT SIGNAL block.",
        ]
        return "\n".join(lines)

    # ── Deterministic fallback ───────────────────────────────────────────────

    @staticmethod
    def _deterministic_signal(votes: List[StrategyVote], snap: Dict[str, Any],
                              fallback_reason: str = "") -> TradeSignal:
        cur = float(snap.get("current_price", 0) or 0)
        if cur <= 0:
            return TradeSignal(asset="BTC/USD", signal_quality="NO_TRADE",
                               raw_response="No price data")

        # ── Phase-1 gate: high-impact event blackout ──
        blackout, reason = check_blackout("btc")
        if blackout:
            return TradeSignal(
                timestamp=datetime.now(timezone.utc).isoformat(),
                asset="BTC/USD", bias="NEUTRAL",
                strategy=f"Event blackout: {reason}",
                signal_quality="NO_TRADE", signal_score="0/6",
                invalidation=f"Blackout window for {reason} clears",
                funding_check=f"Event blackout | {reason}",
                raw_response=_format_pod_summary(votes, fallback_reason or reason),
            )

        # Phase-2: regime-first archetype filter, then cluster-based counting
        htf_dir = classify_htf(snap.get("daily_structure", ""), snap.get("h4_structure", ""))
        regime  = detect_regime(votes, htf_dir)
        active  = active_archetypes(regime)

        if regime == "chaos" or not active:
            return TradeSignal(
                timestamp=datetime.now(timezone.utc).isoformat(),
                asset="BTC/USD", bias="NEUTRAL",
                strategy=f"Regime={regime} — stand down",
                signal_quality="NO_TRADE", signal_score="0/0",
                invalidation="Regime exits chaos",
                funding_check=f"Regime={regime} | BLOCK",
                raw_response=_format_pod_summary(votes, fallback_reason),
            )

        winners = cluster_winners(votes, active=active)
        long_aligned, total_clusters, signed_score = cluster_alignment(winners, is_long=True)
        short_aligned, _, _                        = cluster_alignment(winners, is_long=False)

        if total_clusters == 0:
            return TradeSignal(
                timestamp=datetime.now(timezone.utc).isoformat(),
                asset="BTC/USD", bias="NEUTRAL",
                strategy=f"No active archetypes in regime={regime}",
                signal_quality="NO_TRADE", signal_score=f"0/{total_clusters}",
                funding_check=f"Regime={regime} | NO CLUSTERS",
                raw_response=_format_pod_summary(votes, fallback_reason),
            )

        is_long = signed_score >= 0
        bias = "BULLISH" if is_long else "BEARISH"
        aligned = long_aligned if is_long else short_aligned

        smart_agrees = smart_money_aligned(winners, is_long)
        quality = grade_clusters(aligned, total_clusters,
                                 smart_money_confirms=smart_agrees)

        # ── Phase-1: BTC funding gate ──
        # Block direction when funding is extreme + same direction (you'd be
        # the late long paying carry, or the late short paying carry).
        funding_8h = _safe_funding(snap)
        funding_check = f"Funding {funding_8h:+.4f}/8h"
        if is_long and funding_8h > BTC_FUNDING_BLOCK:
            return TradeSignal(
                timestamp=datetime.now(timezone.utc).isoformat(),
                asset="BTC/USD", bias="NEUTRAL",
                strategy=f"Funding {funding_8h:+.4f}/8h above +{BTC_FUNDING_BLOCK} — blocked LONG (liq cascade risk)",
                signal_quality="NO_TRADE", signal_score=f"{aligned}/{total_clusters}",
                funding_check=f"{funding_check} | BLOCK LONG",
                raw_response=_format_pod_summary(votes, fallback_reason),
            )
        if (not is_long) and funding_8h < -BTC_FUNDING_BLOCK:
            return TradeSignal(
                timestamp=datetime.now(timezone.utc).isoformat(),
                asset="BTC/USD", bias="NEUTRAL",
                strategy=f"Funding {funding_8h:+.4f}/8h below -{BTC_FUNDING_BLOCK} — blocked SHORT (squeeze risk)",
                signal_quality="NO_TRADE", signal_score=f"{aligned}/{total_clusters}",
                funding_check=f"{funding_check} | BLOCK SHORT",
                raw_response=_format_pod_summary(votes, fallback_reason),
            )
        # Same-direction over-extension → downgrade A→B
        if is_long and funding_8h > BTC_FUNDING_DOWNGRADE and quality == "A":
            quality = "B"
        if (not is_long) and funding_8h < -BTC_FUNDING_DOWNGRADE and quality == "A":
            quality = "B"

        # ── Phase-1 gate: hard MTF reject ──
        if quality != "NO_TRADE" and mtf_blocks(htf_dir, is_long, quality):
            return TradeSignal(
                timestamp=datetime.now(timezone.utc).isoformat(),
                asset="BTC/USD", bias="NEUTRAL",
                strategy=f"MTF gate: {bias.lower()} against 4H {htf_dir}trend (need A+ to override)",
                signal_quality="NO_TRADE", signal_score=f"{aligned}/{total_clusters}",
                invalidation=f"4H structure flips out of {htf_dir}trend",
                funding_check=f"{funding_check} | MTF {htf_dir} BLOCK",
                raw_response=_format_pod_summary(votes, fallback_reason),
            )

        if quality == "NO_TRADE":
            return TradeSignal(
                timestamp=datetime.now(timezone.utc).isoformat(),
                asset="BTC/USD", bias="NEUTRAL", strategy="Quality gate failed",
                signal_quality="NO_TRADE", signal_score=f"{aligned}/{total_clusters}",
                raw_response=_format_pod_summary(votes, fallback_reason),
            )

        # ── Phase-1: ATR-based stops (1H proxy = daily/13) ──
        atr_h1   = float(snap.get("atr_h1", 0) or 0)
        atr_daily = float(snap.get("daily_atr", 0) or 0)
        atr_for_stop = atr_h1 if atr_h1 > 0 else (atr_daily / 13.0 if atr_daily > 0 else 0.0)
        sl, tp1, tp2, tp3, risk = compute_atr_stops(
            entry=cur, is_long=is_long, atr=atr_for_stop, quality=quality,
            fallback_pct=STOP_LOSS_PCT, tp_r=(2.0, 3.0, 4.5), round_dp=2,
        )

        # ── Phase-1: 2:1 R:R floor ──
        if not meets_rr_floor(cur, sl, tp1):
            return TradeSignal(
                timestamp=datetime.now(timezone.utc).isoformat(),
                asset="BTC/USD", bias="NEUTRAL",
                strategy=f"R:R floor not met (TP1 < 2R from entry @ ATR={atr_for_stop:.2f})",
                signal_quality="NO_TRADE", signal_score=f"{aligned}/{total_clusters}",
                raw_response=_format_pod_summary(votes, fallback_reason),
            )

        primary = winners.get("MOMENTUM") or winners.get("FLOW") or winners.get("TREND")
        if primary is None or primary.direction != ("LONG" if is_long else "SHORT"):
            aligned_winners = [v for v in winners.values()
                               if v.direction == ("LONG" if is_long else "SHORT")]
            primary = max(aligned_winners, key=lambda v: v.confidence) if aligned_winners else None
        strategy = (f"{regime}-regime {bias.lower()} ({aligned}/{total_clusters} clusters) — "
                    f"{primary.archetype}/{primary.name}: {primary.rationale}"
                    if primary else f"{regime}-regime {bias.lower()} ({aligned}/{total_clusters} clusters)")

        return TradeSignal(
            timestamp=datetime.now(timezone.utc).isoformat(),
            asset="BTC/USD",
            bias=bias,
            strategy=strategy[:140],
            signal_quality=quality,
            signal_score=f"{aligned}/{total_clusters}",
            entry_trigger="Enter at market on next bar close confirming pod direction",
            entry_type="MARKET",
            entry_price=round(cur, 2),
            stop_loss=sl,
            stop_rationale=f"{k_for_quality(quality):.1f}×ATR(1H={atr_for_stop:.2f}) = ${risk:.2f} from entry",
            take_profit_1=tp1,
            take_profit_2=tp2,
            take_profit_3=tp3,
            risk_reward_t1="2.0",
            risk_reward_t2="3.0",
            session=snap.get("current_session", ""),
            vwap_distance=f"${snap.get('vwap_distance', 0):+.2f} from VWAP",
            invalidation="Pod realigns or regime exits direction",
            funding_check=f"{funding_check} | OK",
            etf_flow_check=f"MTF {htf_dir}",
            best_timeframe="1H",
            max_hold_time="8 hours",
            raw_response=_format_pod_summary(votes, fallback_reason),
        )

    @staticmethod
    def _parse_groq_block(raw: str, snap: Dict[str, Any], votes: List[StrategyVote]) -> TradeSignal:
        """Conservative parser — if model output isn't structured, fall back to deterministic."""
        # Look for `BIAS:` and `ENTRY:` lines; very loose.
        out = TradeSignal(asset="BTC/USD")
        if not raw:
            return BTCSignalGenerator._deterministic_signal(votes, snap)
        lines = [l.strip() for l in raw.splitlines() if ":" in l]
        for ln in lines:
            key, _, val = ln.partition(":")
            key = key.strip().upper()
            val = val.strip()
            try:
                if key == "BIAS":               out.bias = val.upper()
                elif key in ("ENTRY", "ENTRY_PRICE"):  out.entry_price = float(val.replace("$", "").replace(",", ""))
                elif key in ("SL", "STOP_LOSS"):       out.stop_loss = float(val.replace("$", "").replace(",", ""))
                elif key in ("TP1",): out.take_profit_1 = float(val.replace("$", "").replace(",", ""))
                elif key in ("TP2",): out.take_profit_2 = float(val.replace("$", "").replace(",", ""))
                elif key in ("TP3",): out.take_profit_3 = float(val.replace("$", "").replace(",", ""))
                elif key in ("QUALITY", "SIGNAL_QUALITY"): out.signal_quality = val
                elif key in ("STRATEGY", "RATIONALE"):     out.strategy = val[:140]
                elif key in ("SCORE", "SIGNAL_SCORE"):     out.signal_score = val
            except Exception:
                pass
        if out.bias and out.entry_price > 0 and out.stop_loss > 0:
            out.timestamp = datetime.now(timezone.utc).isoformat()
            return out
        # Fallback
        return BTCSignalGenerator._deterministic_signal(votes, snap)


def _safe_funding(snap: Dict[str, Any]) -> float:
    """Read funding-rate from snapshot. Returns 0.0 if unset.
    Snapshot may carry it under 'funding_rate' (decimal/8h, e.g. 0.0001)."""
    fr = snap.get("funding_rate", 0.0)
    try:
        return float(fr or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _format_pod_summary(votes: List[StrategyVote], reason: str = "") -> str:
    head = f"BTC pod: {len(votes)} strategies. {reason}".rstrip(". ")
    body = "\n".join(
        f"- {v.name}: {v.direction} (conf {v.confidence:.2f}) — {v.rationale}"
        for v in votes
    )
    return f"{head}\n{body}"
