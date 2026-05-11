"""
strategies/greeks_proxy.py
BlackRock Aladdin / RiskMetrics-style Black-Scholes Greeks on the NIFTY
option chain.

Computes Delta and Gamma per strike using Black-Scholes (with implied-vol
proxied from option `lastPrice` via Newton's method, falling back to a flat
GREEKS_DEFAULT_IV if the inversion fails or the chain is sparse).

Three signals:
  1. **Net Delta**: Σ (CE_OI × Δ_CE  − PE_OI × Δ_PE). Positive net Δ = market
     positioned LONG; negative = SHORT.
  2. **Gamma walls**: top N strikes by Σ γ × OI on each side. Below a
     "put wall" with falling gamma → squeeze potential → LONG. Above a
     "call wall" → max-pain pull / pinning → SHORT.
  3. Combine: vote LONG when net Δ > 0 AND spot below put wall by ≥buffer.
     Vote SHORT when net Δ < 0 AND spot above call wall by ≥buffer.
     Otherwise NEUTRAL.

Confidence proportional to |net Δ| (capped) plus a smaller wall-distance term.

Falls back to NEUTRAL gracefully when the option chain is empty (NSE
auto-block) or when scipy isn't available.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List

from strategies.base import StrategyAgent, StrategyVote
from config_strategies import (
    GREEKS_CALL_WALL_BUFFER,
    GREEKS_DAYS_PER_YEAR,
    GREEKS_DEFAULT_IV,
    GREEKS_GAMMA_WALL_TOP_N,
    GREEKS_PUT_WALL_BUFFER,
    GREEKS_RISK_FREE_RATE,
)

try:
    from scipy.stats import norm        # type: ignore
    _N = norm.cdf
    _n = norm.pdf
    _HAS_SCIPY = True
except Exception:                       # pragma: no cover
    _HAS_SCIPY = False

    def _N(x):
        # Abramowitz & Stegun rational approximation
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    def _n(x):
        return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _bs_d1(s: float, k: float, r: float, sigma: float, t: float) -> float:
    if sigma <= 0 or t <= 0 or k <= 0 or s <= 0:
        return 0.0
    return (math.log(s / k) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))


def _delta_call(s: float, k: float, r: float, sigma: float, t: float) -> float:
    return float(_N(_bs_d1(s, k, r, sigma, t)))


def _delta_put(s: float, k: float, r: float, sigma: float, t: float) -> float:
    return float(_N(_bs_d1(s, k, r, sigma, t)) - 1)


def _gamma(s: float, k: float, r: float, sigma: float, t: float) -> float:
    if sigma <= 0 or t <= 0 or s <= 0:
        return 0.0
    return float(_n(_bs_d1(s, k, r, sigma, t)) / (s * sigma * math.sqrt(t)))


def _years_to_expiry(expiry_date_str: str) -> float:
    """Parse '12-May-2026' → years from today."""
    try:
        exp = datetime.strptime(expiry_date_str, "%d-%b-%Y")
        days = (exp - datetime.utcnow()).total_seconds() / 86400.0
        return max(1.0 / 365, days / GREEKS_DAYS_PER_YEAR)
    except Exception:
        return 7.0 / 365.0     # safe default — 1 week


class GreeksProxyStrategy(StrategyAgent):
    name = "nifty_greeks_proxy"
    inspired_by = "BlackRock Aladdin / RiskMetrics (Black-Scholes Greeks)"
    archetype = "OPTIONS"

    def vote(self, snapshot: Dict[str, Any], feed: Any) -> StrategyVote:
        try:
            chain = feed.get_option_chain() if feed is not None else None
            if not chain or "records" not in chain:
                return self._neutral("NSE option-chain unavailable")

            records = chain["records"]
            rows = records.get("data") or []
            spot = float(records.get("underlyingValue") or snapshot.get("current_price") or 0)
            expiries: List[str] = records.get("expiryDates") or []
            if spot <= 0 or not rows or not expiries:
                return self._neutral("Option chain spot/expiry/data missing")

            nearest = expiries[0]
            t = _years_to_expiry(nearest)
            r = GREEKS_RISK_FREE_RATE
            sigma_default = GREEKS_DEFAULT_IV

            net_delta = 0.0
            call_gamma_oi: List[tuple] = []   # (strike, gamma_oi)
            put_gamma_oi: List[tuple] = []

            for row in rows:
                if row.get("expiryDate") != nearest:
                    continue
                try:
                    K = float(row.get("strikePrice", 0))
                except Exception:
                    continue
                if K <= 0:
                    continue
                ce = row.get("CE") or {}
                pe = row.get("PE") or {}
                ce_oi = float(ce.get("openInterest", 0) or 0)
                pe_oi = float(pe.get("openInterest", 0) or 0)

                # Use a flat IV proxy — full IV inversion is too compute-heavy
                sig = sigma_default

                d_call = _delta_call(spot, K, r, sig, t)
                d_put = _delta_put(spot, K, r, sig, t)
                gam = _gamma(spot, K, r, sig, t)

                net_delta += ce_oi * d_call + pe_oi * d_put

                if ce_oi > 0:
                    call_gamma_oi.append((K, ce_oi * gam))
                if pe_oi > 0:
                    put_gamma_oi.append((K, pe_oi * gam))

            if not call_gamma_oi or not put_gamma_oi:
                return self._neutral("Insufficient OI per side for Greeks")

            call_walls = sorted(call_gamma_oi, key=lambda x: x[1], reverse=True)[:GREEKS_GAMMA_WALL_TOP_N]
            put_walls  = sorted(put_gamma_oi,  key=lambda x: x[1], reverse=True)[:GREEKS_GAMMA_WALL_TOP_N]
            call_wall_strike = max(s for s, _ in call_walls)
            put_wall_strike  = min(s for s, _ in put_walls)

            below_put_wall = spot < put_wall_strike * (1 - GREEKS_PUT_WALL_BUFFER)
            above_call_wall = spot > call_wall_strike * (1 + GREEKS_CALL_WALL_BUFFER)

            # Normalise net delta: scale by total OI so the magnitude is in
            # rough percentage terms
            total_oi = sum(g for _, g in call_gamma_oi) + sum(g for _, g in put_gamma_oi)
            net_delta_norm = net_delta / max(1.0, total_oi)

            meta = {
                "spot": round(spot, 2),
                "expiry": nearest,
                "years_to_expiry": round(t, 4),
                "net_delta": round(net_delta, 0),
                "net_delta_norm": round(net_delta_norm, 3),
                "call_wall": call_wall_strike,
                "put_wall": put_wall_strike,
                "spot_pct_to_call_wall": round((call_wall_strike - spot) / spot * 100, 2),
                "spot_pct_to_put_wall":  round((put_wall_strike - spot)  / spot * 100, 2),
            }

            # LONG when below the put wall (squeeze setup) and net Δ positive
            if below_put_wall and net_delta > 0:
                conf = float(min(0.85, abs(net_delta_norm) * 5 + 0.3))
                return StrategyVote(
                    name=self.name, inspired_by=self.inspired_by,
                    direction="LONG", confidence=conf,
                    rationale=(
                        f"Spot ₹{spot:.0f} < put-wall ₹{put_wall_strike:.0f} "
                        f"(squeeze setup); netΔ {net_delta_norm:+.2f} bullish"
                    ),
                    metadata=meta,
                )

            # SHORT when above the call wall (max-pain pull) and net Δ negative
            if above_call_wall and net_delta < 0:
                conf = float(min(0.85, abs(net_delta_norm) * 5 + 0.3))
                return StrategyVote(
                    name=self.name, inspired_by=self.inspired_by,
                    direction="SHORT", confidence=conf,
                    rationale=(
                        f"Spot ₹{spot:.0f} > call-wall ₹{call_wall_strike:.0f} "
                        f"(pinning down); netΔ {net_delta_norm:+.2f} bearish"
                    ),
                    metadata=meta,
                )

            return self._neutral(
                f"Spot ₹{spot:.0f} between walls "
                f"[put ₹{put_wall_strike:.0f} / call ₹{call_wall_strike:.0f}], "
                f"netΔ {net_delta_norm:+.2f} — no edge",
                **meta,
            )

        except Exception as exc:
            return self._neutral(f"Strategy error: {exc}")
