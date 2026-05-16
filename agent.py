"""
agent.py
BTC/USD Institutional Trading Agent — Main Orchestrator

Event-driven loop:
  - WebSocket 1m bar stream (real-time price + VWAP updates)
  - Hourly + session-open full analysis via Claude API
  - Order placement and circuit breakers via Alpaca paper account

Usage:
  cp .env.example .env  # fill in your API keys
  pip install -r requirements.txt
  python agent.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal as os_signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import (
    ANALYSIS_INTERVAL_SECONDS, DEMO_MODE, PAPER_MODE, SYMBOL,
    REQUIRE_APPROVAL, APPROVAL_TIMEOUT_SEC, PENDING_FILE, BOT_PID_FILE,
    MAX_HOLD_HOURS_DEFAULT,
)
from modules.data_feed import DataFeed
from modules.order_manager import OrderManager
from modules.risk_manager import RiskManager
from modules.session_manager import SessionManager
from modules.signal_generator import SignalGenerator, TradeSignal
from modules.signal_generator_btc import BTCSignalGenerator
from modules.technical_analysis import (
    calculate_atr,
    calculate_vwap,
    classify_structure,
    detect_fvg,
    find_equal_highs_lows,
    get_asian_range,
    get_key_levels,
    price_zscore,
)
from modules.trade_logger import TradeLogger

# ── Logging setup ─────────────────────────────────────────────────────────────
import io
_handlers = [logging.FileHandler("logs/agent.log", encoding="utf-8")]
try:
    if hasattr(sys.stdout, "buffer"):
        _handlers.insert(0, logging.StreamHandler(io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")))
    else:
        _handlers.insert(0, logging.StreamHandler(sys.stdout))
except (ValueError, AttributeError):
    _handlers.insert(0, logging.StreamHandler(sys.stdout))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=_handlers,
)
logger = logging.getLogger("agent")


def _safe_call(fn, key: str, default):
    """Call fn(), pull `key` from returned dict, fall back to default on error."""
    try:
        result = fn()
        if isinstance(result, dict):
            return result.get(key, default)
        return default
    except Exception:
        return default


class BTCTradingAgent:
    """
    Main orchestrator. Ties together data, analysis, signals, orders, and logging.
    """

    def __init__(self):
        logger.info("=" * 60)
        logger.info("BTC/USD Institutional Trading Agent")
        logger.info("Mode: %s", "PAPER" if PAPER_MODE else "LIVE")
        if DEMO_MODE:
            logger.info("Demo mode: ON (public market data, signal-only, no broker execution)")
        logger.info("Pod: 6 strategies (BTC-Microstructure | JPM Order-Flow | Aladdin VWAP-Bandit | Scalp-Confluence | Session-Vol | Trending-Regime)")
        logger.info("=" * 60)
        # Write PID so dashboard can stop/manage this process
        import os as _os
        BOT_PID_FILE.write_text(str(_os.getpid()))

        # Modules
        self._data_feed       = DataFeed(on_bar_callback=self._on_bar)
        self._session_manager = SessionManager(on_session_open=self._on_session_open)
        # BTC now uses the 6-strategy institutional pod (Phase 4 BlackRock-tier
        # expansion). The legacy `SignalGenerator` is kept for reference but
        # no longer wired into the live agent.
        self._signal_gen      = BTCSignalGenerator()
        self._order_manager   = None if DEMO_MODE else OrderManager()
        self._trade_logger    = TradeLogger()

        # Get starting account value
        account_value = 100_000.0 if DEMO_MODE else self._order_manager.get_account_value()
        if account_value == 0.0:
            logger.warning("Could not fetch account value — using $100,000 default")
            account_value = 100_000.0
        self._risk_manager = RiskManager(account_value=account_value, asset_label="BTC")

        # State
        self._last_analysis_time: float = 0.0
        self._active_trade_id:    Optional[str] = None
        self._active_order_id:    Optional[str] = None
        self._active_signal:      Optional[TradeSignal] = None
        self._active_signal_opened_ts: float = 0.0
        self._last_signal:        Optional[TradeSignal] = None   # persists after skip/close
        self._running             = True
        self._analyzing_until     = 0.0   # epoch deadline; while > now, _write_state reports "analyzing"
        self._state_file          = Path("logs/state.json")
        self._analyze_trigger     = Path("logs/analyze_now.json")

        logger.info("Account value: $%.2f", account_value)
        self._write_state("starting")

        # Close any leftover open BTC position from a previous run
        existing = None if DEMO_MODE else self._order_manager.get_open_position()
        if existing:
            logger.warning(
                "Leftover position detected: %.6f BTC (entry $%.2f, P&L $%.4f) — closing before fresh start",
                existing["qty"], existing["avg_entry"], existing["unrealized_pl"],
            )
            self._order_manager.close_position()
            logger.info("Leftover position closed. Waiting 8s for Alpaca to settle funds...")
            time.sleep(8)
            # Re-fetch account value after settlement
            refreshed = self._order_manager.get_account_value()
            if refreshed > 0:
                self._risk_manager.account_value = refreshed
                logger.info("Account balance after settlement: $%.2f", refreshed)

    # ── Startup ───────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Main entry point. Loads history, starts stream, runs analysis loop."""
        # Graceful shutdown on Ctrl+C
        os_signal.signal(os_signal.SIGINT,  self._shutdown)
        os_signal.signal(os_signal.SIGTERM, self._shutdown)

        logger.info("Preloading historical bars...")
        self._data_feed.preload_history()
        logger.info("Historical data loaded.")

        # Start WebSocket stream in a background thread
        stream_thread = threading.Thread(
            target=self._data_feed.start_stream,
            name="alpaca-stream",
            daemon=True,
        )
        stream_thread.start()
        logger.info("WebSocket stream thread started.")

        # Run the main analysis loop in the foreground
        self._analysis_loop()

    # ── WebSocket callback ────────────────────────────────────────────────────

    def _on_bar(self, bar) -> None:
        """Called on every new 1m bar from the WebSocket stream."""
        current_price = float(bar.get("close", 0))

        # Update session manager (checks for session open transitions)
        self._session_manager.tick()

        # Update account value from risk manager every 100 bars
        # (avoid hammering Alpaca REST)
        if int(time.time()) % 600 < 2:  # roughly every 10 minutes
            av = self._risk_manager.account_value if DEMO_MODE else self._order_manager.get_account_value()
            if av > 0:
                self._risk_manager.account_value = av

        # Monitor open position for SL/TP breach (belt-and-suspenders)
        if self._active_trade_id and current_price > 0:
            self._monitor_position(current_price)

        # Keep the dashboard price/state fresh between full analyses.
        self._write_state()

    def _on_session_open(self, session: str) -> None:
        """Fires when a major session opens (Asia / London / NY)."""
        logger.info("Session open detected: %s — triggering analysis", session.upper())
        self._run_analysis(trigger=f"session_open:{session}")

    # ── Analysis loop ─────────────────────────────────────────────────────────

    def _analysis_loop(self) -> None:
        """Blocking loop that triggers full analysis every ANALYSIS_INTERVAL_SECONDS.
        On startup, retries every 2 minutes until the first successful analysis runs.
        Polls the dashboard trigger file every 2s so the Analyze Now button responds
        immediately instead of waiting up to 30s."""
        TICK = 2  # seconds between trigger-file checks
        first_success = False
        elapsed_since_state_write = 0.0
        while self._running:
            now = time.time()
            elapsed = now - self._last_analysis_time
            interval = ANALYSIS_INTERVAL_SECONDS if first_success else 120

            # Dashboard "Analyze Now" trigger — checked every TICK seconds
            if self._analyze_trigger.exists():
                try:
                    self._analyze_trigger.unlink()
                except Exception:
                    pass
                logger.info("Analyze Now triggered from dashboard")
                self._analyzing_until = time.time() + 120  # max 2-min Groq window
                self._write_state("analyzing")
                self._run_analysis(trigger="dashboard_request")
                if self._data_feed.latest_price > 0:
                    first_success = True
                self._analyzing_until = 0.0
                self._write_state()
                elapsed_since_state_write = 0.0

            elif elapsed >= interval:
                self._analyzing_until = time.time() + 120
                self._write_state("analyzing")
                self._run_analysis(trigger="hourly_poll" if first_success else "startup")
                if self._data_feed.latest_price > 0:
                    first_success = True
                self._analyzing_until = 0.0
                self._write_state()
                elapsed_since_state_write = 0.0

            # Refresh state ~every 10s so dashboard sees fresh price
            elapsed_since_state_write += TICK
            if elapsed_since_state_write >= 10:
                self._write_state()
                elapsed_since_state_write = 0.0

            time.sleep(TICK)

    def _run_analysis(self, trigger: str = "manual") -> None:
        """Build market snapshot, call Claude, validate signal, place order."""
        logger.info("--- Running analysis [trigger: %s] ---", trigger)
        self._last_analysis_time = time.time()

        # Skip if risk manager is halted
        if self._risk_manager.is_halted:
            logger.warning("Risk manager halted — skipping analysis")
            return

        # Skip if already in a trade
        if self._active_trade_id:
            logger.info("Active trade %s in progress — skipping new signal", self._active_trade_id)
            return

        # Cancel stale unfilled orders
        if not DEMO_MODE:
            self._order_manager.cancel_stale_orders(max_age_hours=4.0)

        # Build snapshot
        snapshot = self._build_snapshot()
        if snapshot["current_price"] == 0:
            logger.warning("No price data available yet — skipping analysis")
            return

        # Call pod for signal — BTC now uses BTCSignalGenerator(pod fan-out)
        signal = self._signal_gen.generate(snapshot, self._data_feed)
        self._last_signal = signal   # persist for dashboard even if skipped

        # Override stop loss to exactly 5% from entry (regardless of model suggestion)
        if signal.entry_price > 0 and signal.bias in ("BULLISH", "BEARISH"):
            from config import STOP_LOSS_PCT
            if signal.bias == "BULLISH":
                signal.stop_loss = round(signal.entry_price * (1 - STOP_LOSS_PCT), 2)
            else:
                signal.stop_loss = round(signal.entry_price * (1 + STOP_LOSS_PCT), 2)

            # If Groq didn't emit TP1/TP2/TP3, derive them as 1.5R/2.5R/3.5R off
            # the (now-overridden) entry/SL — the trade-zone overlay needs them.
            risk = abs(signal.entry_price - signal.stop_loss)
            if risk > 0:
                # R-multiples 2/3/4.5 — matches XAU/NIFTY spec and ensures
                # TP1 meets the 2:1 R:R floor for sustainable expectancy.
                if signal.bias == "BULLISH":
                    if not signal.take_profit_1: signal.take_profit_1 = round(signal.entry_price + 2.0 * risk, 2)
                    if not signal.take_profit_2: signal.take_profit_2 = round(signal.entry_price + 3.0 * risk, 2)
                    if not getattr(signal, "take_profit_3", 0):
                        signal.take_profit_3 = round(signal.entry_price + 4.5 * risk, 2)
                else:
                    if not signal.take_profit_1: signal.take_profit_1 = round(signal.entry_price - 2.0 * risk, 2)
                    if not signal.take_profit_2: signal.take_profit_2 = round(signal.entry_price - 3.0 * risk, 2)
                    if not getattr(signal, "take_profit_3", 0):
                        signal.take_profit_3 = round(signal.entry_price - 4.5 * risk, 2)

            logger.info(
                "Signal: %s | Quality: %s | Entry: $%.2f | SL: $%.2f (5%% override) | TP1/2/3: %.2f / %.2f / %.2f",
                signal.bias, signal.signal_quality, signal.entry_price, signal.stop_loss,
                signal.take_profit_1, signal.take_profit_2, getattr(signal, "take_profit_3", 0) or 0,
            )
        else:
            logger.info(
                "Signal: %s | Quality: %s | Entry: $%.2f | SL: $%.2f",
                signal.bias, signal.signal_quality, signal.entry_price, signal.stop_loss,
            )

        # Validate
        ok, reason = self._risk_manager.validate_signal(
            signal, demo_trades_count=self._trade_logger.demo_trades_count()
        )
        if not ok:
            logger.info("Signal rejected: %s", reason)
            return

        # Calculate position size (pass live buying power for small-account cap)
        daily_atr    = snapshot.get("daily_atr", 0.0)
        vix          = snapshot.get("vix", 0.0)
        buying_power = self._risk_manager.account_value if DEMO_MODE else self._order_manager.get_account_value()
        pos_size     = self._risk_manager.calculate_position_size(
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            signal_quality=signal.signal_quality,
            daily_atr=daily_atr,
            vix=vix,
            buying_power=buying_power,
        )

        if pos_size <= 0:
            logger.error("Position size calculation returned 0 — aborting")
            return

        if DEMO_MODE:
            logger.info(
                "Demo signal: %s | Quality: %s | Entry $%.2f | SL $%.2f | TP1 $%.2f | Strategy: %s",
                signal.bias,
                signal.signal_quality,
                signal.entry_price,
                signal.stop_loss,
                signal.take_profit_1,
                signal.strategy,
            )
            self._write_state("running")
            return

        logger.info(
            "Placing order: %s %.6f BTC @ $%.2f (SL $%.2f, TP1 $%.2f)",
            signal.bias, pos_size, signal.entry_price, signal.stop_loss, signal.take_profit_1,
        )

        # ── Approval gate ─────────────────────────────────────────────────────
        if REQUIRE_APPROVAL:
            self._write_pending_signal(signal, pos_size)
            self._write_state("awaiting_approval")
            logger.info("Waiting for dashboard approval (timeout %ds)...", APPROVAL_TIMEOUT_SEC)
            approved = self._wait_for_approval()
            if not approved:
                logger.info("Signal not approved — skipping trade")
                self._clear_pending_signal()
                self._write_state()
                return
            logger.info("Trade approved by user — executing")
            self._clear_pending_signal()

        # Place order
        order_id = self._order_manager.place_order(signal, pos_size)
        if not order_id:
            logger.error("Order placement failed")
            return

        # Log trade open
        trade_id = self._trade_logger.log_trade_open(
            signal=signal,
            actual_entry=signal.entry_price,  # updated on fill
            position_size=pos_size,
            alpaca_order_id=order_id,
        )
        self._active_trade_id = trade_id
        self._active_order_id = order_id
        self._active_signal   = signal
        self._active_signal_opened_ts = time.time()

        logger.info("Trade opened: %s | Order: %s", trade_id, order_id)
        self._write_state()

    # ── Position monitoring ───────────────────────────────────────────────────

    def _monitor_position(self, current_price: float) -> None:
        """Check SL, TP1, and max_hold_time. Phase 1.2 spec: BTC trades on a
        live broker so partial scale-out at TP2/TP3 is a Phase-5 enhancement;
        for now TP1-hit closes 100% but we DO enforce max_hold_time so a
        Friday-3pm trade can't sit open through the weekend."""
        if not self._active_signal:
            return

        sig   = self._active_signal
        is_long = sig.bias == "BULLISH"

        # Stop loss hit
        sl_hit = (is_long  and current_price <= sig.stop_loss) or \
                 (not is_long and current_price >= sig.stop_loss)
        tp1_hit = (is_long  and current_price >= sig.take_profit_1 and sig.take_profit_1 > 0) or \
                  (not is_long and current_price <= sig.take_profit_1 and sig.take_profit_1 > 0)

        if sl_hit:
            logger.warning("STOP LOSS HIT at $%.2f", current_price)
            self._close_trade(current_price, reason="stop_loss")
            return

        if tp1_hit:
            logger.info("TAKE PROFIT 1 HIT at $%.2f", current_price)
            self._close_trade(current_price, reason="tp1")
            return

        # Max-hold enforcement (Phase 1.2 spec). Parse the first integer out
        # of strings like "8 hours" / "4-12 hours" — falls back to config default.
        if self._active_signal_opened_ts > 0:
            max_hold_str = getattr(sig, "max_hold_time", "") or ""
            hours = MAX_HOLD_HOURS_DEFAULT
            import re as _re
            m = _re.search(r"(\d+)", max_hold_str)
            if m:
                try:
                    hours = float(m.group(1))
                except ValueError:
                    pass
            if (time.time() - self._active_signal_opened_ts) > hours * 3600.0:
                logger.warning("MAX HOLD %s reached at $%.2f — closing at market",
                               max_hold_str or f"{hours:.0f}h", current_price)
                self._close_trade(current_price, reason="EXIT_TIME")

    def _close_trade(self, exit_price: float, reason: str = "") -> None:
        """Close position and record the result."""
        if not self._active_trade_id or not self._active_signal:
            return

        sig      = self._active_signal
        is_long  = sig.bias == "BULLISH"
        entry    = sig.entry_price
        pnl_per_btc = (exit_price - entry) if is_long else (entry - exit_price)

        # Get actual position size from log
        log_entries = [t for t in self._trade_logger._trades
                       if t["trade_id"] == self._active_trade_id]
        pos_size = log_entries[0]["position_size"] if log_entries else 0.0
        pnl_dollars = pnl_per_btc * pos_size

        success = self._order_manager.close_position()
        if not success:
            logger.error("Failed to close position — manual intervention required")
            return

        self._trade_logger.log_trade_close(
            trade_id=self._active_trade_id,
            actual_exit=exit_price,
            actual_sl=sig.stop_loss,
            pnl_dollars=pnl_dollars,
            improvement=f"Exit reason: {reason}",
        )
        self._risk_manager.on_trade_result(pnl_dollars)

        # Print session stats
        stats = self._trade_logger.get_session_stats()
        logger.info(
            "Session stats — Trades: %d | Win rate: %.1f%% | Avg R:R: %.2f | Total P&L: %.2f%%",
            stats["total"], stats["win_rate"] * 100, stats["avg_rr"], stats["total_pnl_pct"],
        )

        self._active_trade_id = None
        self._active_order_id = None
        self._active_signal   = None
        self._active_signal_opened_ts = 0.0
        self._write_state()

    # ── Snapshot builder ──────────────────────────────────────────────────────

    def _build_snapshot(self) -> dict:
        """Assemble the full market snapshot dict for Claude."""
        bars_1m    = self._data_feed.get_bars("1Min")
        bars_5m    = self._data_feed.get_bars("5Min")
        bars_15m   = self._data_feed.get_bars("15Min")
        bars_1h    = self._data_feed.get_bars("1Hour")
        bars_4h    = self._data_feed.get_bars("4Hour")
        bars_daily = self._data_feed.get_bars("1Day")

        current_price = self._data_feed.latest_price
        session_vwap  = calculate_vwap(bars_1m) if not bars_1m.empty else 0.0
        daily_atr     = calculate_atr(bars_daily) if not bars_daily.empty else 0.0
        atr_h1        = calculate_atr(bars_1h)    if not bars_1h.empty    else 0.0
        atr_h4        = calculate_atr(bars_4h)    if not bars_4h.empty    else 0.0
        zscore        = price_zscore(bars_1h) if not bars_1h.empty else 0.0

        asian_high, asian_low = get_asian_range(bars_1m)

        fvgs_15m = detect_fvg(bars_15m, "15Min") if not bars_15m.empty else []
        fvgs_1h  = detect_fvg(bars_1h,  "1Hour") if not bars_1h.empty else []
        all_fvgs = [
            {"top": f.top, "bottom": f.bottom, "direction": f.direction,
             "timeframe": f.timeframe, "midpoint": f.midpoint, "size": f.size}
            for f in (fvgs_15m + fvgs_1h)[:5]
        ]

        clusters = find_equal_highs_lows(bars_1h) if not bars_1h.empty else []
        equal_highs = [c.price for c in clusters if c.direction == "buy_stops"][:3]
        equal_lows  = [c.price for c in clusters if c.direction == "sell_stops"][:3]

        key_levels  = get_key_levels(bars_1h, bars_daily, current_price) \
                      if (not bars_1h.empty and not bars_daily.empty) \
                      else None

        daily_struct = classify_structure(bars_daily) if not bars_daily.empty else "ranging"
        h4_struct    = classify_structure(bars_4h)    if not bars_4h.empty   else "ranging"
        h1_struct    = classify_structure(bars_1h)    if not bars_1h.empty   else "ranging"

        stats = self._trade_logger.get_session_stats()

        return {
            "timestamp_ist":        SessionManager.ist_now().isoformat(),
            "current_price":        current_price,
            "daily_high":           float(bars_daily["high"].max())  if not bars_daily.empty else 0,
            "daily_low":            float(bars_daily["low"].min())   if not bars_daily.empty else 0,
            "asian_range_high":     asian_high,
            "asian_range_low":      asian_low,
            "session_vwap":         session_vwap,
            "vwap_distance":        round(current_price - session_vwap, 2) if session_vwap else 0,
            "daily_atr":            daily_atr,
            "atr_h1":               atr_h1,
            "atr_h4":               atr_h4,
            "zscore":               zscore,
            # Real funding + OI from Binance fapi (no auth)
            "funding_rate":         _safe_call(self._data_feed.fetch_funding_rate, "lastFundingRate", 0.0),
            "open_interest_change_pct": _safe_call(self._data_feed.fetch_oi_change, "oi_24h_pct", 0.0),
            "current_session":      self._session_manager.current_session(),
            "vix":                  0.0,
            "dxy_direction":        "unknown",
            "macro_bias":           "neutral",
            "daily_structure":      daily_struct,
            "h4_structure":         h4_struct,
            "h1_structure":         h1_struct,
            "pdh":                  key_levels.pdh          if key_levels else 0,
            "pdl":                  key_levels.pdl          if key_levels else 0,
            "weekly_open":          key_levels.weekly_open  if key_levels else 0,
            "swing_highs":          key_levels.swing_highs  if key_levels else [],
            "swing_lows":           key_levels.swing_lows   if key_levels else [],
            "round_numbers":        key_levels.round_numbers if key_levels else [],
            "equal_highs":          equal_highs,
            "equal_lows":           equal_lows,
            "active_fvgs":          all_fvgs,
            "consecutive_losses":   self._risk_manager.consecutive_losses,
            "daily_pnl_pct":        round(self._risk_manager.daily_pnl_pct * 100, 3),
            "last_trade_result":    self._trade_logger.last_trade_result(),
            "account_value":        round(self._risk_manager.account_value, 2),
        }

    # ── Approval helpers ──────────────────────────────────────────────────────

    def _write_pending_signal(self, signal, pos_size: float) -> None:
        """Write pending signal to file so dashboard can show approve/skip UI."""
        expires = datetime.now(timezone.utc).timestamp() + APPROVAL_TIMEOUT_SEC
        data = {
            "status":       "pending",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "expires_at":   datetime.fromtimestamp(expires, tz=timezone.utc).isoformat(),
            "expires_ts":   expires,
            "signal": {
                "bias":            signal.bias,
                "strategy":        signal.strategy,
                "signal_quality":  signal.signal_quality,
                "signal_score":    signal.signal_score,
                "entry_price":     signal.entry_price,
                "stop_loss":       signal.stop_loss,
                "take_profit_1":   signal.take_profit_1,
                "take_profit_2":   signal.take_profit_2,
                "risk_reward_t1":  signal.risk_reward_t1,
                "entry_trigger":   signal.entry_trigger,
                "invalidation":    signal.invalidation,
                "session":         signal.session,
                "vwap_distance":   signal.vwap_distance,
                "max_hold_time":   signal.max_hold_time,
                "stop_rationale":  signal.stop_rationale,
            },
            "position_size": pos_size,
            "notional":      round(pos_size * signal.entry_price, 2),
        }
        PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _wait_for_approval(self) -> bool:
        """Poll pending_signal.json until approved/skipped or timeout. Returns True if approved."""
        deadline = time.time() + APPROVAL_TIMEOUT_SEC
        while time.time() < deadline:
            try:
                data = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
                status = data.get("status", "pending")
                if status == "approved":
                    return True
                if status == "skipped":
                    return False
            except Exception:
                pass
            time.sleep(3)
        return False  # timeout

    def _clear_pending_signal(self) -> None:
        try:
            if PENDING_FILE.exists():
                PENDING_FILE.unlink()
        except Exception:
            pass

    # ── State writer (for dashboard) ──────────────────────────────────────────

    def _write_state(self, bot_status: str = "running") -> None:
        """Write current agent state to logs/state.json for the dashboard.
        If an analyze_now is in progress, force bot_status='analyzing' so it
        survives concurrent _on_bar writes from the data-feed thread."""
        if bot_status == "running" and time.time() < self._analyzing_until:
            bot_status = "analyzing"
        try:
            sig   = self._active_signal
            trade = None
            if sig and self._active_trade_id:
                current_price = self._data_feed.latest_price
                if sig.bias == "BULLISH":
                    unreal_pl = (current_price - sig.entry_price) if current_price else 0
                else:
                    unreal_pl = (sig.entry_price - current_price) if current_price else 0

                log_entries = [t for t in self._trade_logger._trades
                               if t["trade_id"] == self._active_trade_id]
                notional = (log_entries[0]["position_size"] * sig.entry_price) if log_entries else 0
                unreal_pl_pct = (unreal_pl / sig.entry_price * 100) if sig.entry_price else 0

                trade = {
                    "trade_id":        self._active_trade_id,
                    "bias":            sig.bias,
                    "entry_price":     sig.entry_price,
                    "stop_loss":       sig.stop_loss,
                    "take_profit_1":   sig.take_profit_1,
                    "take_profit_2":   sig.take_profit_2,
                    "take_profit_3":   getattr(sig, "take_profit_3", 0) or 0,
                    "risk_per_unit":   round(abs(sig.entry_price - sig.stop_loss), 2),
                    "current_price":   current_price,
                    "unrealized_pl":   round(unreal_pl * (notional / sig.entry_price if sig.entry_price else 0), 2),
                    "unrealized_pl_pct": round(unreal_pl_pct, 3),
                    "notional":        round(notional, 2),
                    "open_time":       log_entries[0]["date_time_open"] if log_entries else None,
                    "strategy":        sig.strategy,
                }

            # Use last_signal (persists after skip) for display; active_signal for trade
            display_sig = self._last_signal
            last_signal = None
            if display_sig:
                last_signal = {
                    "timestamp":      display_sig.timestamp,
                    "bias":           display_sig.bias,
                    "signal_quality": display_sig.signal_quality,
                    "signal_score":   display_sig.signal_score,
                    "strategy":       display_sig.strategy,
                    "entry_price":    display_sig.entry_price,
                    "stop_loss":      display_sig.stop_loss,
                    "take_profit_1":  display_sig.take_profit_1,
                    "take_profit_2":  display_sig.take_profit_2,
                    "take_profit_3":  getattr(display_sig, "take_profit_3", 0) or 0,
                    "risk_per_unit":  round(abs(display_sig.entry_price - display_sig.stop_loss), 2)
                                       if display_sig.entry_price and display_sig.stop_loss else 0,
                    "risk_reward_t1": display_sig.risk_reward_t1,
                    "session":        display_sig.session,
                    "vwap_distance":  display_sig.vwap_distance,
                    "entry_trigger":  display_sig.entry_trigger,
                    "invalidation":   display_sig.invalidation,
                    "max_hold_time":  display_sig.max_hold_time,
                }

            stats_raw = self._trade_logger.get_session_stats()
            state = {
                "last_updated":  datetime.now(timezone.utc).isoformat(),
                "bot_status":    bot_status,
                "latest_price":  self._data_feed.latest_price,
                "session":       self._session_manager.current_session(),
                "account": {
                    "balance": round(self._risk_manager.account_value, 2),
                },
                "daily_pnl_pct": round(min(max(self._risk_manager.daily_pnl_pct * 100, -100), 100), 3),
                "current_trade": trade,
                "last_signal":   last_signal,
                "last_analysis_time": datetime.fromtimestamp(
                    self._last_analysis_time, tz=timezone.utc
                ).isoformat() if self._last_analysis_time else None,
                "stats": {
                    "total_trades": stats_raw.get("total", 0),
                    "wins":         stats_raw.get("wins", 0),
                    "losses":       stats_raw.get("losses", 0),
                    "win_rate":     stats_raw.get("win_rate", 0),
                    "total_pnl_pct": stats_raw.get("total_pnl_pct", 0),
                },
            }
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, default=str)
        except Exception as exc:
            logger.debug("Failed to write state.json: %s", exc)

    # ── Shutdown ──────────────────────────────────────────────────────────────

    def _shutdown(self, *_) -> None:
        logger.info("Shutdown signal received. Stopping agent...")
        self._running = False
        # Close any open position on shutdown
        if self._active_trade_id:
            logger.warning("Open trade %s detected during shutdown — closing at market",
                           self._active_trade_id)
            current_price = self._data_feed.latest_price
            if current_price > 0:
                self._close_trade(current_price, reason="agent_shutdown")
        sys.exit(0)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    agent = BTCTradingAgent()
    agent.run()
