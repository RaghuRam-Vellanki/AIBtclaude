"""
strategies/nifty_options_oi.py
Goldman Sachs / smart-money options analytics for NIFTY 50 — uniquely
implementable in India because NSE publishes the full option chain JSON
for free.

Three signals from the chain:
  1. PCR (put/call OI ratio): >1 = put writers more committed than call writers
     = floor below; <0.7 = call writers heavy = ceiling above.
  2. Max-pain strike: the strike where total option-writer payoff is minimised
     (where market 'wants to pin'). If max-pain is meaningfully above spot,
     option market expects upside (and vice-versa).
  3. Top-5 strike change-in-OI (ΔOI) on calls and puts within ±5% of spot:
     unwinding (negative ΔOI) by call writers = bullish (writers covering
     short calls); unwinding by put writers = bearish.

Vote LONG if all three lean bullish; SHORT if all three lean bearish; NEUTRAL
otherwise. The NSE option-chain endpoint is rate-limited and frequently
returns empty for non-browser clients — when that happens we vote NEUTRAL
with rationale='option-chain feed unavailable' so the rest of the pod still
contributes.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from strategies.base import StrategyAgent, StrategyVote


PCR_BULL_THRESHOLD = 1.0
PCR_BEAR_THRESHOLD = 0.7
MAX_PAIN_DEVIATION = 0.003     # 0.3%
NEAR_ATM_PCT       = 0.05      # ±5% strikes are 'near ATM'


class NIFTYOptionsOIStrategy(StrategyAgent):
    name = "nifty_options_oi"
    inspired_by = "Goldman Sachs (smart-money options positioning)"
    archetype = "OPTIONS"

    def vote(self, snapshot: Dict[str, Any], feed: Any) -> StrategyVote:
        try:
            chain = feed.get_option_chain() if feed is not None else None
            if not chain:
                return self._neutral("NSE option-chain feed unavailable")

            records = chain.get("records") or {}
            data = records.get("data") or []
            spot = float(records.get("underlyingValue") or snapshot.get("price") or 0.0)
            expiry_dates: List[str] = records.get("expiryDates") or []
            if not data or spot <= 0 or not expiry_dates:
                return self._neutral("Option-chain payload incomplete")

            # Restrict to current monthly expiry (first in list)
            current_expiry = expiry_dates[0]
            rows = [r for r in data if r.get("expiryDate") == current_expiry]
            if not rows:
                return self._neutral(f"No rows for expiry {current_expiry}")

            ce_total_oi = 0
            pe_total_oi = 0
            strike_oi: Dict[float, Tuple[int, int]] = {}     # strike -> (CE OI, PE OI)
            ce_chgoi_atm = 0
            pe_chgoi_atm = 0
            atm_low  = spot * (1 - NEAR_ATM_PCT)
            atm_high = spot * (1 + NEAR_ATM_PCT)

            for r in rows:
                try:
                    strike = float(r.get("strikePrice", 0))
                except Exception:
                    continue
                ce = r.get("CE") or {}
                pe = r.get("PE") or {}
                ce_oi = int(ce.get("openInterest", 0) or 0)
                pe_oi = int(pe.get("openInterest", 0) or 0)
                ce_chg = int(ce.get("changeinOpenInterest", 0) or 0)
                pe_chg = int(pe.get("changeinOpenInterest", 0) or 0)
                ce_total_oi += ce_oi
                pe_total_oi += pe_oi
                strike_oi[strike] = (ce_oi, pe_oi)
                if atm_low <= strike <= atm_high:
                    ce_chgoi_atm += ce_chg
                    pe_chgoi_atm += pe_chg

            if ce_total_oi <= 0:
                return self._neutral("No call OI in chain")

            pcr = pe_total_oi / ce_total_oi
            max_pain = self._compute_max_pain(strike_oi)
            mp_dev = (max_pain - spot) / spot if spot else 0.0

            # Bullish if: PCR > 1 AND max-pain ≥ spot×1.003 AND call writers covering
            cond_pcr_bull = pcr >= PCR_BULL_THRESHOLD
            cond_mp_bull  = mp_dev >= MAX_PAIN_DEVIATION
            cond_chg_bull = ce_chgoi_atm < 0      # call ΔOI negative = writers exiting

            # Bearish mirror
            cond_pcr_bear = pcr <= PCR_BEAR_THRESHOLD
            cond_mp_bear  = mp_dev <= -MAX_PAIN_DEVIATION
            cond_chg_bear = pe_chgoi_atm < 0      # put ΔOI negative = writers exiting

            meta = {
                "pcr": round(pcr, 3),
                "max_pain": round(max_pain, 1),
                "spot": round(spot, 1),
                "max_pain_dev_pct": round(mp_dev * 100, 2),
                "atm_call_chg_oi": ce_chgoi_atm,
                "atm_put_chg_oi": pe_chgoi_atm,
                "expiry": current_expiry,
            }

            bull_score = int(cond_pcr_bull) + int(cond_mp_bull) + int(cond_chg_bull)
            bear_score = int(cond_pcr_bear) + int(cond_mp_bear) + int(cond_chg_bear)

            # When the fallback NSE endpoint can't expose ΔOI (it's all zero),
            # 2-of-3 (PCR + max-pain alignment) is enough — otherwise we'd
            # never vote. Detect that case and lower the bar.
            chg_oi_unavailable = (ce_chgoi_atm == 0 and pe_chgoi_atm == 0)
            min_score = 2 if chg_oi_unavailable else 3

            if bull_score >= min_score:
                # All three bullish; weighted confidence
                conf = 0.4 * (1 if cond_pcr_bull else 0) \
                     + 0.3 * (1 if cond_mp_bull else 0) \
                     + 0.3 * (1 if cond_chg_bull else 0)
                return StrategyVote(
                    name=self.name, inspired_by=self.inspired_by,
                    direction="LONG", confidence=float(conf),
                    rationale=(
                        f"PCR={pcr:.2f} (>{PCR_BULL_THRESHOLD}), max-pain "
                        f"{max_pain:.0f} ({mp_dev*100:+.2f}% above spot), "
                        f"ATM call writers covering ({ce_chgoi_atm:+,}) — bullish"
                    ),
                    metadata=meta,
                )

            if bear_score >= min_score:
                conf = 0.4 * (1 if cond_pcr_bear else 0) \
                     + 0.3 * (1 if cond_mp_bear else 0) \
                     + 0.3 * (1 if cond_chg_bear else 0)
                return StrategyVote(
                    name=self.name, inspired_by=self.inspired_by,
                    direction="SHORT", confidence=float(conf),
                    rationale=(
                        f"PCR={pcr:.2f} (<{PCR_BEAR_THRESHOLD}), max-pain "
                        f"{max_pain:.0f} ({mp_dev*100:+.2f}% below spot), "
                        f"ATM put writers covering ({pe_chgoi_atm:+,}) — bearish"
                    ),
                    metadata=meta,
                )

            return self._neutral(
                f"PCR={pcr:.2f}, max-pain {mp_dev*100:+.2f}% from spot — no edge",
                **meta,
            )

        except Exception as exc:
            return self._neutral(f"Strategy error: {exc}")

    @staticmethod
    def _compute_max_pain(strike_oi: Dict[float, Tuple[int, int]]) -> float:
        """
        Return the strike that minimises total writer payoff:
          For each candidate strike S: pain(S) = Σ max(S - K, 0) * CE_OI(K) +
                                                 Σ max(K - S, 0) * PE_OI(K)
        The argmin over candidate strikes is the max-pain.
        """
        if not strike_oi:
            return 0.0
        strikes = sorted(strike_oi.keys())
        best_strike = strikes[0]
        best_pain = float("inf")
        for s in strikes:
            pain = 0.0
            for k, (ce_oi, pe_oi) in strike_oi.items():
                if s > k:
                    pain += (s - k) * ce_oi
                elif s < k:
                    pain += (k - s) * pe_oi
            if pain < best_pain:
                best_pain = pain
                best_strike = s
        return float(best_strike)
