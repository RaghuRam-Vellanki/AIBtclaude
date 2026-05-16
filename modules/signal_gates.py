"""
modules/signal_gates.py
Phase-1/2 quant-tier gates: ATR-based stops, hard MTF gate, 2:1 R:R floor,
event-blackout check, regime-aware archetype filtering, and cluster-based
alignment counting. Shared across BTC / XAU / NIFTY signal generators so the
math and policy are identical (and the tests are too).

Why this lives in its own module: the three asset generators were diverging
on stop logic (3% / 5% / 1.5% fixed) and ignoring HTF context. Centralising
removes drift and lets us tune one knob.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Set, Tuple

from config import ATR_K_BY_QUALITY, MIN_RR_T1
from modules.event_calendar import is_blackout as _calendar_blackout


# ── HTF classification ─────────────────────────────────────────────────────────

def classify_htf(daily_struct: str = "", h4_struct: str = "") -> str:
    """Map structure strings ("uptrend"/"downtrend"/"ranging"/etc.) to
    one of {"up", "down", "range"}. Prefers H4 over daily."""
    h4 = (h4_struct or "").lower()
    daily = (daily_struct or "").lower()
    primary = h4 if h4 else daily
    if any(tok in primary for tok in ("uptrend", "bullish", "up")):
        return "up"
    if any(tok in primary for tok in ("downtrend", "bearish", "down")):
        return "down"
    return "range"


def mtf_blocks(htf_dir: str, is_long: bool, quality: str) -> bool:
    """Hard MTF reject: don't long against a 4H downtrend, don't short into
    a 4H uptrend. A+ overrides (treat as fade-the-extension)."""
    if quality == "A+":
        return False
    if htf_dir == "down" and is_long:
        return True
    if htf_dir == "up" and not is_long:
        return True
    return False


# ── Stop placement ─────────────────────────────────────────────────────────────

def compute_atr_stops(
    entry: float,
    is_long: bool,
    atr: float,
    quality: str,
    fallback_pct: float = 0.02,
    tp_r: Tuple[float, float, float] = (2.0, 3.0, 4.5),
    round_dp: int = 2,
) -> Tuple[float, float, float, float, float]:
    """Return (sl, tp1, tp2, tp3, risk_per_unit).

    SL = entry ± k·ATR(14) where k scales by grade (A+=2.5, A=2.0, B=1.5).
    Falls back to entry·fallback_pct if ATR unavailable. TPs at 2R/3R/4.5R
    by default — TP1 ≥ 2R is required to satisfy MIN_RR_T1.
    """
    k = ATR_K_BY_QUALITY.get(quality, 1.5)
    if atr and atr > 0:
        risk = k * float(atr)
    else:
        risk = float(entry) * float(fallback_pct)
    if is_long:
        sl  = round(entry - risk, round_dp)
        tp1 = round(entry + tp_r[0] * risk, round_dp)
        tp2 = round(entry + tp_r[1] * risk, round_dp)
        tp3 = round(entry + tp_r[2] * risk, round_dp)
    else:
        sl  = round(entry + risk, round_dp)
        tp1 = round(entry - tp_r[0] * risk, round_dp)
        tp2 = round(entry - tp_r[1] * risk, round_dp)
        tp3 = round(entry - tp_r[2] * risk, round_dp)
    return sl, tp1, tp2, tp3, round(risk, round_dp)


def meets_rr_floor(entry: float, sl: float, tp1: float,
                   floor: float = MIN_RR_T1) -> bool:
    """Reject if TP1 reward / SL risk < floor. Symmetric for LONG/SHORT.

    The ratio is rounded to 2dp before comparison because compute_atr_stops
    rounds SL/TP1 to 2dp, so an exact 2.0R configuration measured at the
    rounded levels can come out as 1.9999… and falsely fail the floor."""
    risk = abs(entry - sl)
    if risk <= 0:
        return False
    reward = abs(tp1 - entry)
    return round(reward / risk, 2) >= floor


# ── Event blackout (thin wrapper for symmetric API) ────────────────────────────

def check_blackout(asset: str, now: datetime | None = None) -> Tuple[bool, str]:
    """Return (True, reason) if asset is in any high-impact event window."""
    if now is None:
        now = datetime.now(timezone.utc)
    return _calendar_blackout(asset, now)


# ── Convenience: quality string → ATR_K (for display in pod_report) ────────────

def k_for_quality(quality: str) -> float:
    return ATR_K_BY_QUALITY.get(quality, 1.5)


# ── Phase-2: regime detection & archetype-aware cluster counting ───────────────

# Which archetypes are allowed to vote per regime. Mean-revert in a trend leaks
# money (fading the move); trend-followers in chop chase noise. Filtering before
# we count alignment is the single largest false-signal fix in Phase 2.
_REGIME_ACTIVE_ARCHETYPES: Dict[str, Set[str]] = {
    "trend":  {"TREND", "MOMENTUM", "MACRO", "FLOW", "BREAKOUT", "CARRY"},
    "range":  {"MEAN_REVERT", "OPTIONS", "MICRO", "FLOW", "CARRY"},
    "chaos":  set(),       # caller treats empty-set as NO_TRADE
}
_ALL_ARCHETYPES: Set[str] = {
    "TREND", "MEAN_REVERT", "MOMENTUM", "BREAKOUT",
    "FLOW", "MACRO", "OPTIONS", "MICRO", "CARRY",
}


def detect_regime(votes: Iterable[Any], htf_dir: str = "range") -> str:
    """Return one of {'trend', 'range', 'chaos'}.

    Preference order: explicit `regime_hmm` / `nifty_regime_hmm` metadata
    label → HTF direction fallback. We never trust a single tag blindly —
    if HMM is silent we use H4 structure (already classified as up/down/range).
    """
    for v in votes:
        if getattr(v, "name", "") not in ("regime_hmm", "nifty_regime_hmm"):
            continue
        meta = getattr(v, "metadata", None) or {}
        r = str(meta.get("regime", "")).lower().strip()
        if not r:
            continue
        if r in ("chaos", "high_vol", "high-vol", "shock"):
            return "chaos"
        if "trend" in r or r in ("up", "down", "bull", "bear"):
            return "trend"
        if "range" in r or "chop" in r or r in ("flat", "consolidation"):
            return "range"
    # Fallback to HTF direction (already normalised to up/down/range)
    if htf_dir in ("up", "down"):
        return "trend"
    return "range"


def active_archetypes(regime: str) -> Set[str]:
    """Set of archetypes whose votes should be counted in the given regime.
    Unknown regime → all archetypes active (fail-open)."""
    return _REGIME_ACTIVE_ARCHETYPES.get(regime.lower(), _ALL_ARCHETYPES)


def cluster_winners(votes: Iterable[Any],
                    active: Set[str] | None = None) -> Dict[str, Any]:
    """Collapse votes by `archetype` → one winner per cluster.

    Selection rule per cluster:
      - if any directional (LONG/SHORT) vote exists → take the highest-conf one
      - else fall through to the highest-conf NEUTRAL (so the cluster is still
        represented in the cluster count, just not aligned with anything)

    Filtering: if `active` is provided, votes whose archetype is not in `active`
    are dropped *before* clustering — that's the regime mute.
    """
    by_arch: Dict[str, list] = {}
    for v in votes:
        arch = (getattr(v, "archetype", None) or "FLOW").upper()
        if active is not None and arch not in active:
            continue
        by_arch.setdefault(arch, []).append(v)

    out: Dict[str, Any] = {}
    for arch, vs in by_arch.items():
        directional = [v for v in vs if getattr(v, "direction", "") in ("LONG", "SHORT")]
        if directional:
            out[arch] = max(directional, key=lambda x: float(getattr(x, "confidence", 0.0)))
        else:
            out[arch] = max(vs, key=lambda x: float(getattr(x, "confidence", 0.0)))
    return out


def cluster_alignment(cluster_winners_: Dict[str, Any],
                      is_long: bool) -> Tuple[int, int, float]:
    """Return (aligned_clusters, total_clusters, signed_cluster_score).

    `signed_cluster_score` sums each cluster winner's signed score (+conf for
    LONG, −conf for SHORT, 0 for NEUTRAL). This is the cluster-honest version
    of `pod_sum` — use it instead of `sum(v.score for v in votes)` so collinear
    strategies don't 4×-inflate the magnitude.
    """
    want = "LONG" if is_long else "SHORT"
    aligned = 0
    signed = 0.0
    for v in cluster_winners_.values():
        d = getattr(v, "direction", "")
        c = float(getattr(v, "confidence", 0.0))
        if d == want:
            aligned += 1
        if d == "LONG":
            signed += c
        elif d == "SHORT":
            signed -= c
    return aligned, len(cluster_winners_), round(signed, 3)


def grade_clusters(aligned: int, total: int,
                   smart_money_confirms: bool = True) -> str:
    """Map (aligned_clusters, total_clusters) → quality grade.

    Cluster-honest ladder (smart-money is a bonus, not a hard B gate):
      A+  : ≥ 4 aligned clusters  (deep multi-archetype confluence)
      A   : ≥ 3 aligned clusters
      B   : ≥ 2 aligned clusters
      C   : 1 aligned cluster AND smart-money (FLOW or MOMENTUM) confirms
            — lower-conviction, smaller-size signal so the dashboard
            surfaces something instead of perma-NEUTRAL on quiet days.
      else: NO_TRADE
    """
    if total == 0:
        return "NO_TRADE"
    if aligned >= 4:
        return "A+"
    if aligned >= 3:
        return "A"
    if aligned >= 2:
        return "B"
    if aligned >= 1 and smart_money_confirms:
        return "C"
    return "NO_TRADE"


def smart_money_aligned(cluster_winners_: Dict[str, Any], is_long: bool) -> bool:
    """True if the FLOW or MOMENTUM cluster winner agrees with direction.
    These archetypes read price-action / institutional flow, not lagging macro,
    so they're the closest thing to a "this isn't just noise" confirmation."""
    want = "LONG" if is_long else "SHORT"
    for arch in ("FLOW", "MOMENTUM"):
        v = cluster_winners_.get(arch)
        if v is not None and getattr(v, "direction", "") == want:
            return True
    return False
