"""
strategies/base.py
Common types for the institutional strategy pod. Each pod member returns one
StrategyVote; the signal generator aggregates them into a single trade signal.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict


# Archetype taxonomy: groups strategies that fire on the same underlying market
# structure so the signal generator can collapse them into one vote per cluster
# instead of counting collinear strategies as independent confirmations.
# See plan/Phase-2 for the categorisation table.
ARCHETYPES = (
    "TREND",        # follows established direction (regime-HMM, momentum-macro)
    "MEAN_REVERT",  # fades extension (microstructure, vwap-bandit, cointegration)
    "MOMENTUM",     # confluence-based directional momentum (scalp_indicators)
    "BREAKOUT",     # range-breakout / resistance-level
    "FLOW",         # institutional flow / volume / OI / FII-DII
    "MACRO",        # fundamental drivers (DXY, real yields, COT)
    "OPTIONS",      # gamma walls, IV/HV, put-call positioning
    "MICRO",        # pure microstructure tickers (currently unused; reserved)
    "CARRY",        # funding-rate skew, basis trading
)


@dataclass
class StrategyVote:
    name:        str                              # "microstructure", "regime_hmm", ...
    inspired_by: str                              # "Citadel Securities", "Renaissance Technologies", ...
    direction:   str                              # "LONG" | "SHORT" | "NEUTRAL"
    confidence:  float                            # 0.0 – 1.0
    rationale:   str                              # one-sentence human explanation
    archetype:   str = "FLOW"                     # one of ARCHETYPES; defaults to FLOW
    metadata:    Dict[str, Any] = field(default_factory=dict)

    @property
    def score(self) -> float:
        """Signed score for aggregation: +confidence if LONG, -confidence if SHORT, 0 if NEUTRAL."""
        if self.direction == "LONG":
            return float(self.confidence)
        if self.direction == "SHORT":
            return -float(self.confidence)
        return 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name":        self.name,
            "inspired_by": self.inspired_by,
            "direction":   self.direction,
            "confidence":  round(self.confidence, 3),
            "score":       round(self.score, 3),
            "rationale":   self.rationale,
            "archetype":   self.archetype,
            "metadata":    self.metadata,
        }


class StrategyAgent(ABC):
    """Abstract base for a pod member. Subclasses set `name`, `inspired_by`,
    and `archetype` (one of base.ARCHETYPES) and override `vote`."""

    name: str = "base"
    inspired_by: str = ""
    archetype: str = "FLOW"   # subclasses MUST override

    @abstractmethod
    def vote(self, snapshot: Dict[str, Any], feed: Any) -> StrategyVote:
        """
        Build a StrategyVote from the market snapshot and (optionally) the feed
        for fresh side-data. Implementations should never raise — return
        StrategyVote(direction="NEUTRAL", ...) on any failure.
        """

    def _neutral(self, reason: str, **meta: Any) -> StrategyVote:
        return StrategyVote(
            name=self.name,
            inspired_by=self.inspired_by,
            direction="NEUTRAL",
            confidence=0.0,
            rationale=reason,
            archetype=self.archetype,
            metadata=meta,
        )

    def _vote(self, direction: str, confidence: float, rationale: str,
              **meta: Any) -> StrategyVote:
        """Convenience for directional votes that auto-stamps archetype."""
        return StrategyVote(
            name=self.name,
            inspired_by=self.inspired_by,
            direction=direction,
            confidence=float(confidence),
            rationale=rationale,
            archetype=self.archetype,
            metadata=meta,
        )
