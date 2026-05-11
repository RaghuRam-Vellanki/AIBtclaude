"""
agent_xau.py
XAU/USD Institutional Multi-Strategy Trading Agent — Main Orchestrator.

Mirror of agent.py (BTCTradingAgent) but:
  - Polling feed (yfinance), not WebSocket
  - 5-strategy "institutional pod" + Decision LLM (deterministic fallback)
  - Local paper-sim order manager (no broker)
  - Separate state / pending / pid / log files

Usage:
    pip install -r requirements.txt
    python agent_xau.py
"""
from __future__ import annotations

import io
import json
import logging
import os
import signal as os_signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import (
    APPROVAL_TIMEOUT_SEC,
    REQUIRE_APPROVAL,
    XAU_AGENT_LOG,
    XAU_ANALYSIS_INTERVAL,
    XAU_ANALYZE_TRIGGER,
    XAU_BOT_PID_FILE,
    XAU_PAPER_STARTING_BALANCE,
    XAU_PENDING_FILE,
    XAU_STATE_FILE,
    XAU_STOP_LOSS_PCT,
    XAU_TRADES_LOG,
)
from modules.data_feed_xau import XAUDataFeed
from modules.order_manager_xau_paper import XAUPaperOrderManager
from modules.risk_manager import RiskManager
from modules.session_manager import SessionManager
from modules.signal_generator import TradeSignal
from modules.signal_generator_xau import XAUSignalGenerator
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
from strategies import default_pod

# ── Logging setup ─────────────────────────────────────────────────────────────
Path(XAU_AGENT_LOG).parent.mkdir(parents=True, exist_ok=True)
_handlers = [logging.FileHandler(str(XAU_AGENT_LOG), encoding="utf-8")]
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
logger = logging.getLogger("agent_xau")


class XAUTradingAgent:
    """Main orchestrator for the XAU/USD institutional pod (paper-sim)."""

    def __init__(self):
        logger.info("=" * 60)
        logger.info("XAU/USD Institutional Trading Agent")
        logger.info("Mode: PAPER-SIM (local fills, no broker)")
        logger.info("Pod: 9 strategies (Citadel | Renaissance | JPM | D.E. Shaw | Goldman | Aladdin VWAP-Bandit | RiskMetrics Vol | Investopedia Scalp | Session-Vol)")
        logger.info("=" * 60)
        # Write PID so dashboard can stop/manage this process
        XAU_BOT_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        XAU_BOT_PID_FILE.write_text(str(os.getpid()))

        # Modules
        self._data_feed       = XAUDataFeed(on_bar_callback=self._on_bar)
        self._session_manager = SessionManager(on_session_open=self._on_session_open)
        self._signal_gen      = XAUSignalGenerator(strategies=default_pod())
        self._order_manager   = XAUPaperOrderManager(starting_balance=XAU_PAPER_STARTING_BALANCE)
        self._trade_logger    = TradeLogger(log_file=XAU_TRADES_LOG, asset="XAU/USD")

        account_value = self._order_manager.get_account_value()
        if account_value <= 0:
            account_value = XAU_PAPER_STARTING_BALANCE
        self._risk_manager = RiskManager(account_value=account_value, asset_label="oz")

        # State
        self._last_analysis_time: float = 0.0
        self._active_trade_id:    Optional[str]         = None
        self._active_order_id:    Optional[str]         = None
        self._active_signal:      Optional[TradeSignal] = None
        self._last_signal:        Optional[TradeSignal] = None
        self._running             = True
        self._analyzing_until     = 0.0
        self._state_file          = Path(XAU_STATE_FILE)
        self._analyze_trigger     = Path(XAU_ANALYZE_TRIGGER)

        logger.info("Paper account balance: $%.2f", account_value)
        self._write_state("starting")

        # Close any leftover open paper position from a previous run
        existing = self._order_manager.get_open_position()
        if existing:
            logger.warning(
                "Leftover paper position detected: %.4f oz @ $%.2f (unrealized $%.2f) — closing",
                existing["qty"], existing["avg_entry"], existing["unrealized_pl"],
            )
            self._order_manager.close_position()
            refreshed = self._order_manager.get_account_value()
            if refreshed > 0:
                self._risk_manager.account_value = refreshed
                logger.info("Balance after close: $%.2f", refreshed)

    # ── Startup ───────────────────────────────────────────────────────────────

    def run(self) -> None:
        os_signal.signal(os_signal.SIGINT,  self._shutdown)
        os_signal.signal(os_signal.SIGTERM, self._shutdown)

        logger.info("Preloading historical bars from yfinance...")
        self._data_feed.preload_history()
        logger.info("Historical data loaded. Latest price: $%.2f", self._data_feed.latest_price)

        # Polling loop in background thread
        poll_thread = threading.Thread(
            target=self._data_feed.start_polling,
            name="xau-poll",
            daemon=True,
        )
        poll_thread.start()
        logger.info("yfinance polling thread started.")

        self._analysis_loop()

    # ── Polling callback ──────────────────────────────────────────────────────

    def _on_bar(self, bar) -> None:
        """Called on every fresh 1m poll from XAUDataFeed."""
        try:
            current_price = float(bar.get("close", 0))
        except Exception:
            current_price = 0.0

        self._session_manager.tick()
        self._order_manager.update_price(current_price)

        # Refresh account value occasionally (paper sim is cheap, but no need every tick)
        if int(time.time()) % 600 < 2:
            av = self._order_manager.get_account_value()
            if av > 0:
                self._risk_manager.account_value = av

        if self._active_trade_id and current_price > 0:
            self._monitor_position(current_price)

        self._write_state()

    def _on_session_open(self, session: str) -> None:
        logger.info("Session open detected: %s — triggering analysis", session.upper())
        self._run_analysis(trigger=f"session_open:{session}")

    # ── Analysis loop ─────────────────────────────────────────────────────────

    def _analysis_loop(self) -> None:
        """Trigger full pod analysis every XAU_ANALYSIS_INTERVAL seconds, plus on dashboard request.
        Polls the trigger file every 2s so Analyze Now responds within seconds."""
        TICK = 2
        first_success = False
        elapsed_since_state_write = 0.0
        while self._running:
            now = time.time()
            elapsed = now - self._last_analysis_time
            interval = XAU_ANALYSIS_INTERVAL if first_success else 120

            if self._analyze_trigger.exists():
                try:
                    self._analyze_trigger.unlink()
                except Exception:
                    pass
                logger.info("Analyze Now triggered from dashboard")
                self._analyzing_until = time.time() + 120
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

            elapsed_since_state_write += TICK
            if elapsed_since_state_write >= 10:
                self._write_state()
                elapsed_since_state_write = 0.0

            time.sleep(TICK)

    def _run_analysis(self, trigger: str = "manual") -> None:
        logger.info("--- Running XAU analysis [trigger: %s] ---", trigger)
        self._last_analysis_time = time.time()

        if self._active_trade_id:
            logger.info("Active trade %s in progress — skipping new signal", self._active_trade_id)
            return

        snapshot = self._build_snapshot()
        if snapshot["current_price"] == 0:
            logger.warning("No price data available yet — skipping analysis")
            return

        # Pod fan-out + Decision LLM (or deterministic fallback)
        signal = self._signal_gen.generate(snapshot, self._data_feed)
        signal.asset = "XAU/USD"
        self._last_signal = signal

        # Override stop loss to exactly XAU_STOP_LOSS_PCT (3%) regardless of model output
        if signal.entry_price > 0 and signal.bias in ("BULLISH", "BEARISH"):
            if signal.bias == "BULLISH":
                signal.stop_loss = round(signal.entry_price * (1 - XAU_STOP_LOSS_PCT), 2)
            else:
                signal.stop_loss = round(signal.entry_price * (1 + XAU_STOP_LOSS_PCT), 2)

            # Fill missing TP1/TP2/TP3 from the (possibly Groq-emitted, possibly
            # deterministic) signal so the chart trade-zone overlay always has
            # all 3 levels.
            risk = abs(signal.entry_price - signal.stop_loss)
            if risk > 0:
                if signal.bias == "BULLISH":
                    if not signal.take_profit_1: signal.take_profit_1 = round(signal.entry_price + 1.5 * risk, 2)
                    if not signal.take_profit_2: signal.take_profit_2 = round(signal.entry_price + 2.5 * risk, 2)
                    if not getattr(signal, "take_profit_3", 0):
                        signal.take_profit_3 = round(signal.entry_price + 3.5 * risk, 2)
                else:
                    if not signal.take_profit_1: signal.take_profit_1 = round(signal.entry_price - 1.5 * risk, 2)
                    if not signal.take_profit_2: signal.take_profit_2 = round(signal.entry_price - 2.5 * risk, 2)
                    if not getattr(signal, "take_profit_3", 0):
                        signal.take_profit_3 = round(signal.entry_price - 3.5 * risk, 2)

        logger.info(
            "Signal: %s | Quality: %s | Score: %s | Entry: $%.2f | SL: $%.2f",
            signal.bias, signal.signal_quality, signal.signal_score,
            signal.entry_price, signal.stop_loss,
        )

        if signal.signal_quality == "NO_TRADE" or signal.bias not in ("BULLISH", "BEARISH"):
            logger.info("No tradeable signal this cycle (quality=%s).", signal.signal_quality)
            self._write_state()
            return

        if signal.entry_price <= 0 or signal.stop_loss <= 0:
            logger.warning("Signal missing entry/stop levels — skipping")
            return

        # Position sizing — reuse RiskManager, no daily-ATR/VIX inputs needed for gold v1
        buying_power = self._order_manager.get_account_value()
        pos_size = self._risk_manager.calculate_position_size(
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            signal_quality=signal.signal_quality,
            daily_atr=snapshot.get("daily_atr", 0.0),
            vix=0.0,
            buying_power=buying_power,
        )
        if pos_size <= 0:
            logger.error("Position size calculation returned 0 — aborting")
            return

        logger.info(
            "Placing paper order: %s %.4f oz XAU @ $%.2f (SL $%.2f, TP1 $%.2f)",
            signal.bias, pos_size, signal.entry_price, signal.stop_loss, signal.take_profit_1,
        )

        # Approval gate
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
            logger.info("Trade approved — executing paper fill")
            self._clear_pending_signal()

        order_id = self._order_manager.place_order(signal, pos_size)
        if not order_id:
            logger.error("Paper order placement failed")
            return

        trade_id = self._trade_logger.log_trade_open(
            signal=signal,
            actual_entry=signal.entry_price,
            position_size=pos_size,
            alpaca_order_id=order_id,
        )
        self._active_trade_id = trade_id
        self._active_order_id = order_id
        self._active_signal   = signal

        logger.info("Paper trade opened: %s | OrderID: %s", trade_id, order_id)
        self._write_state()

    # ── Position monitoring ───────────────────────────────────────────────────

    def _monitor_position(self, current_price: float) -> None:
        """Drive the TP-ladder state machine. Partial closes happen inside the
        order manager; we only book the trade-logger close when the position is
        fully flat (SL / TP3 / EXIT_TIME)."""
        if not self._active_signal:
            return
        from modules.tp_ladder import tick_position
        # ATR for trailing-stop buffer (post-TP2). Falls back to 0 → no buffer.
        try:
            bars_1h = self._data_feed.get_bars("1Hour")
            atr = calculate_atr(bars_1h) if not bars_1h.empty else 0.0
        except Exception:
            atr = 0.0
        reason = tick_position(self._order_manager, current_price, atr=float(atr))
        if reason in ("SL", "TP3", "EXIT_TIME"):
            summary = self._order_manager.last_close_summary or {}
            exit_px = float(summary.get("exit_price", current_price))
            pnl = float(summary.get("realized_pnl", 0.0))
            logger.info("[XAU] Position fully flat (%s)  P&L $%+.2f", reason, pnl)
            self._book_trade_close(exit_px, pnl=pnl, reason=reason,
                                   exits=summary.get("exits", []))

    def _close_trade(self, exit_price: float, reason: str = "") -> None:
        """Manual full-flatten path (e.g. dashboard 'close now'). The TP-ladder
        path goes through `_book_trade_close` instead."""
        if not self._active_trade_id or not self._active_signal:
            return

        sig     = self._active_signal
        is_long = sig.bias == "BULLISH"
        entry   = sig.entry_price
        pnl_per_oz = (exit_price - entry) if is_long else (entry - exit_price)

        log_entries = [t for t in self._trade_logger._trades
                       if t["trade_id"] == self._active_trade_id]
        pos_size = log_entries[0]["position_size"] if log_entries else 0.0
        pnl_dollars = pnl_per_oz * pos_size

        if not self._order_manager.close_position(reason=reason or "MANUAL"):
            logger.error("Failed to close paper position")
            return

        # Prefer realized_pnl from OM if it tracked tranches
        summary = self._order_manager.last_close_summary or {}
        if summary.get("realized_pnl") is not None:
            pnl_dollars = float(summary["realized_pnl"])

        self._trade_logger.log_trade_close(
            trade_id=self._active_trade_id,
            actual_exit=exit_price,
            actual_sl=sig.stop_loss,
            pnl_dollars=pnl_dollars,
            improvement=f"Exit reason: {reason}",
        )
        self._risk_manager.on_trade_result(pnl_dollars)

        # Update RiskManager balance from paper account
        new_balance = self._order_manager.get_account_value()
        if new_balance > 0:
            self._risk_manager.account_value = new_balance

        stats = self._trade_logger.get_session_stats()
        logger.info(
            "XAU stats — Trades: %d | Win rate: %.1f%% | Avg R:R: %.2f | Total P&L: %.2f%%",
            stats["total"], stats["win_rate"] * 100, stats["avg_rr"], stats["total_pnl_pct"],
        )

        self._active_trade_id = None
        self._active_order_id = None
        self._active_signal   = None
        self._write_state()

    def _book_trade_close(self, exit_price: float, pnl: float, reason: str,
                          exits: list) -> None:
        """Trade-logger close path for the TP-ladder state machine.
        Records the FINAL aggregate P&L across all tranches."""
        if not self._active_trade_id or not self._active_signal:
            return
        sig = self._active_signal
        improvement = (
            f"Exit reason: {reason}; tranches: " +
            ", ".join(f"{e.get('reason','?')}@{e.get('exit_price',0):.2f}" for e in exits)
        ) if exits else f"Exit reason: {reason}"
        self._trade_logger.log_trade_close(
            trade_id=self._active_trade_id,
            actual_exit=exit_price,
            actual_sl=sig.stop_loss,
            pnl_dollars=float(pnl),
            improvement=improvement,
        )
        self._risk_manager.on_trade_result(float(pnl))
        new_balance = self._order_manager.get_account_value()
        if new_balance > 0:
            self._risk_manager.account_value = new_balance
        stats = self._trade_logger.get_session_stats()
        logger.info(
            "XAU stats — Trades: %d | Win rate: %.1f%% | Avg R:R: %.2f | Total P&L: %.2f%%",
            stats["total"], stats["win_rate"] * 100, stats["avg_rr"], stats["total_pnl_pct"],
        )
        self._active_trade_id = None
        self._active_order_id = None
        self._active_signal   = None
        self._write_state()

    # ── Snapshot builder ──────────────────────────────────────────────────────

    def _build_snapshot(self) -> dict:
        """Assemble market snapshot for the pod and the Decision LLM."""
        bars_1m    = self._data_feed.get_bars("1Min")
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

        key_levels = get_key_levels(bars_1h, bars_daily, current_price) \
                     if (not bars_1h.empty and not bars_daily.empty) else None

        daily_struct = classify_structure(bars_daily) if not bars_daily.empty else "ranging"
        h4_struct    = classify_structure(bars_4h)    if not bars_4h.empty  else "ranging"
        h1_struct    = classify_structure(bars_1h)    if not bars_1h.empty  else "ranging"

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
            "current_session":      self._session_manager.current_session(),
            "daily_structure":      daily_struct,
            "h4_structure":         h4_struct,
            "h1_structure":         h1_struct,
            "pdh":                  key_levels.pdh         if key_levels else 0,
            "pdl":                  key_levels.pdl         if key_levels else 0,
            "weekly_open":          key_levels.weekly_open if key_levels else 0,
            "swing_highs":          key_levels.swing_highs if key_levels else [],
            "swing_lows":           key_levels.swing_lows  if key_levels else [],
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

    def _write_pending_signal(self, signal: TradeSignal, pos_size: float) -> None:
        expires = datetime.now(timezone.utc).timestamp() + APPROVAL_TIMEOUT_SEC
        data = {
            "asset":        "XAU/USD",
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
        XAU_PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(XAU_PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _wait_for_approval(self) -> bool:
        deadline = time.time() + APPROVAL_TIMEOUT_SEC
        while time.time() < deadline:
            try:
                data = json.loads(XAU_PENDING_FILE.read_text(encoding="utf-8"))
                status = data.get("status", "pending")
                if status == "approved":
                    return True
                if status == "skipped":
                    return False
            except Exception:
                pass
            time.sleep(3)
        return False

    def _clear_pending_signal(self) -> None:
        try:
            if XAU_PENDING_FILE.exists():
                XAU_PENDING_FILE.unlink()
        except Exception:
            pass

    # ── State writer (for dashboard) ──────────────────────────────────────────

    def _write_state(self, bot_status: str = "running") -> None:
        if bot_status == "running" and time.time() < self._analyzing_until:
            bot_status = "analyzing"
        try:
            sig   = self._active_signal
            trade = None
            if sig and self._active_trade_id:
                current_price = self._data_feed.latest_price
                if sig.bias == "BULLISH":
                    unreal_per_oz = (current_price - sig.entry_price) if current_price else 0
                else:
                    unreal_per_oz = (sig.entry_price - current_price) if current_price else 0

                log_entries = [t for t in self._trade_logger._trades
                               if t["trade_id"] == self._active_trade_id]
                pos_size = log_entries[0]["position_size"] if log_entries else 0
                notional = pos_size * sig.entry_price if pos_size else 0
                unreal_pl_pct = (unreal_per_oz / sig.entry_price * 100) if sig.entry_price else 0

                trade = {
                    "trade_id":          self._active_trade_id,
                    "bias":              sig.bias,
                    "entry_price":       sig.entry_price,
                    "stop_loss":         sig.stop_loss,
                    "take_profit_1":     sig.take_profit_1,
                    "take_profit_2":     sig.take_profit_2,
                    "take_profit_3":     getattr(sig, "take_profit_3", 0) or 0,
                    "risk_per_unit":     round(abs(sig.entry_price - sig.stop_loss), 2),
                    "current_price":     current_price,
                    "unrealized_pl":     round(unreal_per_oz * pos_size, 2),
                    "unrealized_pl_pct": round(unreal_pl_pct, 3),
                    "notional":          round(notional, 2),
                    "open_time":         log_entries[0]["date_time_open"] if log_entries else None,
                    "strategy":          sig.strategy,
                }

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
                "asset":         "XAU/USD",
                "last_updated":  datetime.now(timezone.utc).isoformat(),
                "bot_status":    bot_status,
                "latest_price":  self._data_feed.latest_price,
                "session":       self._session_manager.current_session(),
                "account": {
                    "balance":   round(self._order_manager.get_account_value(), 2),
                },
                "daily_pnl_pct": round(min(max(self._risk_manager.daily_pnl_pct * 100, -100), 100), 3),
                "current_trade": trade,
                "last_signal":   last_signal,
                "last_analysis_time": datetime.fromtimestamp(
                    self._last_analysis_time, tz=timezone.utc
                ).isoformat() if self._last_analysis_time else None,
                "stats": {
                    "total_trades":  stats_raw.get("total", 0),
                    "wins":          stats_raw.get("wins", 0),
                    "losses":        stats_raw.get("losses", 0),
                    "win_rate":      stats_raw.get("win_rate", 0),
                    "total_pnl_pct": stats_raw.get("total_pnl_pct", 0),
                },
            }
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, default=str)
        except Exception as exc:
            logger.debug("Failed to write xau_state.json: %s", exc)

    # ── Shutdown ──────────────────────────────────────────────────────────────

    def _shutdown(self, *_) -> None:
        logger.info("Shutdown signal received. Stopping XAU agent...")
        self._running = False
        self._data_feed.stop()
        if self._active_trade_id:
            current_price = self._data_feed.latest_price
            if current_price > 0:
                logger.warning("Open trade %s detected during shutdown — closing at $%.2f",
                               self._active_trade_id, current_price)
                self._close_trade(current_price, reason="agent_shutdown")
        sys.exit(0)


if __name__ == "__main__":
    agent = XAUTradingAgent()
    agent.run()
