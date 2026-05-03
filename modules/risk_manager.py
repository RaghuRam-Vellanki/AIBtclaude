"""
risk_manager.py
ATR-based position sizing, daily loss circuit breakers,
consecutive loss pause, and signal validation.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from config import (
    ALLOW_BEARISH_SIGNALS,
    ATR_REDUCE_25_THRESHOLD,
    ATR_REDUCE_50_THRESHOLD,
    DAILY_MAX_LOSS_PCT,
    MAX_CONSECUTIVE_LOSSES,
    MIN_SIGNAL_QUALITY,
    MIN_STOP_DISTANCE_USD,
    RISK_PCT,
)

logger = logging.getLogger(__name__)

# Signal quality → allowed risk percentage
_QUALITY_RISK: dict = {
    "A+": 0.010,
    "A":  0.0075,
    "B":  0.005,
}


class RiskManager:
    """
    Validates signals and calculates position sizes.
    Tracks daily P&L and consecutive losses for circuit breakers.
    """

    def __init__(self, account_value: float):
        self._account_value      = account_value
        self._start_of_day_value = account_value
        self._consecutive_losses = 0
        self._daily_trades       = 0
        self._session_halted     = False

    # ── Public API ────────────────────────────────────────────────────────────

    def validate_signal(self, signal, demo_trades_count: int = 0) -> tuple[bool, str]:
        """
        Return (ok, reason). ok=True means safe to trade.
        """
        if self._session_halted:
            return False, "Session halted (circuit breaker active)"

        if signal.signal_quality == "NO_TRADE":
            return False, "Signal quality is NO_TRADE"

        if signal.bias not in ("BULLISH", "BEARISH"):
            return False, f"Bias is {signal.bias} — only BULLISH or BEARISH allowed"

        # Live Alpaca crypto is long-only in this app; demo mode allows both directions.
        if signal.bias == "BEARISH" and not ALLOW_BEARISH_SIGNALS:
            return False, "BEARISH signals skipped — Alpaca paper crypto is long-only (no shorting)"

        if signal.signal_quality == "B" and demo_trades_count < 50:
            return False, f"B-grade signals require 50+ demo trades (have {demo_trades_count})"

        if signal.signal_quality not in _QUALITY_RISK:
            return False, f"Unknown signal quality: {signal.signal_quality}"

        if signal.entry_price <= 0:
            return False, "Entry price is 0 or missing"

        if signal.stop_loss <= 0:
            return False, "Stop loss is 0 or missing"

        stop_dist = signal.stop_distance
        if stop_dist < MIN_STOP_DISTANCE_USD:
            return False, f"Stop distance ${stop_dist:.0f} < minimum ${MIN_STOP_DISTANCE_USD}"

        if self._consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            return False, f"Consecutive loss limit reached ({self._consecutive_losses})"

        if self._daily_trades >= 5:
            return False, "Daily trade limit reached (5 per session)"

        daily_loss = (self._account_value - self._start_of_day_value) / self._start_of_day_value
        if daily_loss <= -DAILY_MAX_LOSS_PCT:
            self._session_halted = True
            return False, f"Daily loss limit hit ({daily_loss:.1%})"

        # Funding rate hard gate (check string from signal)
        funding_check = signal.funding_check.upper()
        if "FAIL" in funding_check:
            return False, f"Funding rate gate failed: {signal.funding_check}"

        return True, "OK"

    def calculate_position_size(
        self,
        entry_price:    float,
        stop_loss:      float,
        signal_quality: str,
        daily_atr:      float = 0.0,
        vix:            float = 0.0,
        buying_power:   float = 0.0,
    ) -> float:
        """
        Returns position size in BTC.
        risk_dollars = account_value × risk_pct
        pos_size     = risk_dollars / stop_distance

        For small accounts: caps position notional to 95% of buying_power
        so we never try to buy more BTC than we can afford.
        """
        base_risk_pct = _QUALITY_RISK.get(signal_quality, RISK_PCT)
        risk_dollars  = self._account_value * base_risk_pct

        # ATR volatility adjustment
        if daily_atr > ATR_REDUCE_50_THRESHOLD:
            risk_dollars *= 0.50
            logger.info("ATR %.0f > %.0f: 50%% size reduction", daily_atr, ATR_REDUCE_50_THRESHOLD)
        elif daily_atr > ATR_REDUCE_25_THRESHOLD:
            risk_dollars *= 0.75
            logger.info("ATR %.0f > %.0f: 25%% size reduction", daily_atr, ATR_REDUCE_25_THRESHOLD)

        # VIX adjustment
        if vix > 25:
            risk_dollars *= 0.80
            logger.info("VIX %.1f > 25: 20%% size reduction", vix)

        stop_distance = abs(entry_price - stop_loss)
        if stop_distance <= 0:
            logger.error("Stop distance is 0 — cannot size position")
            return 0.0

        position_btc = risk_dollars / stop_distance

        # ── Small-account cap: never exceed available buying power ───────────
        available = buying_power or self._account_value
        if entry_price > 0:
            max_affordable_btc = available * 0.95 / entry_price
            if position_btc > max_affordable_btc:
                logger.info(
                    "Position capped by buying power: %.6f -> %.6f BTC ($%.2f available)",
                    position_btc, max_affordable_btc, available,
                )
                position_btc = max_affordable_btc

        # ── Alpaca minimum notional is $10 ────────────────────────────────────
        # For small accounts: if risk-based size is below $10, use 95% of
        # available balance so the 5% stop loss itself controls the dollar risk.
        if entry_price > 0:
            notional = position_btc * entry_price
            alpaca_min = 10.0
            if notional < alpaca_min:
                fallback_btc = available * 0.95 / entry_price
                fallback_notional = fallback_btc * entry_price
                if fallback_notional >= alpaca_min:
                    logger.info(
                        "Notional $%.2f < Alpaca min $%.0f — using 95%% of balance: $%.2f",
                        notional, alpaca_min, fallback_notional,
                    )
                    position_btc = fallback_btc
                else:
                    logger.warning("Insufficient balance for Alpaca $10 minimum — returning 0")
                    return 0.0

        logger.info(
            "Position size: %.6f BTC ($%.2f notional) | Risk: $%.2f | Stop dist: $%.0f",
            position_btc, position_btc * entry_price, risk_dollars, stop_distance,
        )
        return round(position_btc, 6)

    # ── Circuit breaker updates ───────────────────────────────────────────────

    def on_trade_result(self, pnl_dollars: float) -> None:
        """Update account state after a trade closes."""
        self._account_value += pnl_dollars
        self._daily_trades  += 1

        if pnl_dollars < 0:
            self._consecutive_losses += 1
            logger.warning("Loss recorded. Consecutive losses: %d", self._consecutive_losses)
        else:
            self._consecutive_losses = 0
            logger.info("Win recorded. Consecutive losses reset.")

        # Check 4-hour drawdown circuit breaker (simplified: check daily)
        daily_loss = (self._account_value - self._start_of_day_value) / self._start_of_day_value
        if daily_loss <= -0.015:  # -1.5% in any 4H window
            logger.warning("4H drawdown circuit breaker: %.2f%%", daily_loss * 100)

    def reset_daily(self) -> None:
        """Call at start of each new trading day."""
        self._start_of_day_value = self._account_value
        self._daily_trades       = 0
        self._session_halted     = False
        logger.info("Daily risk state reset. Account: $%.2f", self._account_value)

    def reset_session_halt(self) -> None:
        """Manually lift the session halt (use carefully)."""
        self._session_halted     = False
        self._consecutive_losses = 0

    # ── State properties ──────────────────────────────────────────────────────

    @property
    def account_value(self) -> float:
        return self._account_value

    @account_value.setter
    def account_value(self, value: float) -> None:
        self._account_value = value

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    @property
    def daily_pnl_pct(self) -> float:
        if self._start_of_day_value == 0:
            return 0.0
        return (self._account_value - self._start_of_day_value) / self._start_of_day_value

    @property
    def is_halted(self) -> bool:
        return self._session_halted
