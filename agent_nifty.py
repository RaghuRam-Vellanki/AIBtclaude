"""
agent_nifty.py
NIFTY 50 Institutional Multi-Strategy Trading Agent — Main Orchestrator.

Mirror of agent_xau.py but:
  - Polls ^NSEI via yfinance + NSE JSON for FII/DII + option chain
  - 5-strategy NIFTY pod (microstructure / regime_hmm / fii_dii / pairs_arb / options_oi)
  - Local paper-sim order manager (no Zerodha/Angel/Dhan in v1)
  - Honours Indian market hours (9:15-15:30 IST, Mon-Fri, ex NSE holidays)
  - Separate state / pending / pid / log files

Usage:
    pip install -r requirements.txt
    python agent_nifty.py
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
    NIFTY_AGENT_LOG,
    NIFTY_ANALYSIS_INTERVAL,
    NIFTY_ANALYZE_TRIGGER,
    NIFTY_BOT_PID_FILE,
    NIFTY_PAPER_STARTING_BALANCE,
    NIFTY_PENDING_FILE,
    NIFTY_STATE_FILE,
    NIFTY_STOP_LOSS_PCT,
    NIFTY_TRADES_LOG,
    REQUIRE_APPROVAL,
)
from modules import market_calendar
from modules.data_feed_nifty import NIFTYDataFeed
from modules.order_manager_nifty_paper import NIFTYPaperOrderManager
from modules.risk_manager import RiskManager
from modules.session_manager import SessionManager
from modules.signal_generator import TradeSignal
from modules.signal_generator_nifty import NIFTYSignalGenerator
from modules.technical_analysis import (
    calculate_atr,
    calculate_vwap,
    classify_structure,
    get_key_levels,
    price_zscore,
)
from modules.trade_logger import TradeLogger
from strategies import default_nifty_pod

# ── Logging setup ─────────────────────────────────────────────────────────────
Path(NIFTY_AGENT_LOG).parent.mkdir(parents=True, exist_ok=True)
_handlers = [logging.FileHandler(str(NIFTY_AGENT_LOG), encoding="utf-8")]
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
logger = logging.getLogger("agent_nifty")


def _ist_session_label() -> str:
    """Map current IST time → session bucket used by the skill prompt."""
    from datetime import time as dtime
    ist = market_calendar._to_ist(None).time()
    if not market_calendar.is_market_open():
        return "Closed"
    if ist < dtime(9, 30):     return "Opening"
    if ist < dtime(11, 30):    return "Discovery"
    if ist < dtime(14, 0):     return "FIIPeak"
    if ist < dtime(15, 0):     return "CloseOut"
    return "Pre-close"


class NIFTYTradingAgent:
    """Main orchestrator for the NIFTY 50 institutional pod (paper-sim)."""

    def __init__(self):
        logger.info("=" * 60)
        logger.info("NIFTY 50 Institutional Trading Agent")
        logger.info("Mode: PAPER-SIM (local fills, no broker)")
        logger.info("Pod: 11 strategies (Citadel | Renaissance | FII/DII | Pairs-Arb | Options-OI | JPM Order-Flow | Aladdin VWAP-Bandit | RiskMetrics Vol | Scalp-Confluence | OI-Crossover | Greeks-Wall)")
        logger.info("=" * 60)

        NIFTY_BOT_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        NIFTY_BOT_PID_FILE.write_text(str(os.getpid()))

        # Modules
        self._data_feed       = NIFTYDataFeed(on_bar_callback=self._on_bar)
        self._session_manager = SessionManager(on_session_open=self._on_session_open)
        self._signal_gen      = NIFTYSignalGenerator(strategies=default_nifty_pod())
        self._order_manager   = NIFTYPaperOrderManager(starting_balance=NIFTY_PAPER_STARTING_BALANCE)
        self._trade_logger    = TradeLogger(log_file=NIFTY_TRADES_LOG, asset="NIFTY 50")

        account_value = self._order_manager.get_account_value()
        if account_value <= 0:
            account_value = NIFTY_PAPER_STARTING_BALANCE
        self._risk_manager = RiskManager(account_value=account_value, asset_label="lots")

        # State
        self._last_analysis_time: float = 0.0
        self._active_trade_id:    Optional[str]         = None
        self._active_order_id:    Optional[str]         = None
        self._active_signal:      Optional[TradeSignal] = None
        self._last_signal:        Optional[TradeSignal] = None
        self._running             = True
        self._analyzing_until     = 0.0
        self._state_file          = Path(NIFTY_STATE_FILE)
        self._analyze_trigger     = Path(NIFTY_ANALYZE_TRIGGER)

        logger.info("Paper account balance: ₹%.2f", account_value)
        self._write_state("starting")

        # Close any leftover position from previous run
        existing = self._order_manager.get_open_position()
        if existing:
            logger.warning(
                "Leftover paper position: %.4f units @ ₹%.2f (unrealized ₹%.2f) — closing",
                existing["qty"], existing["avg_entry"], existing["unrealized_pl"],
            )
            self._order_manager.close_position()
            refreshed = self._order_manager.get_account_value()
            if refreshed > 0:
                self._risk_manager.account_value = refreshed
                logger.info("Balance after close: ₹%.2f", refreshed)

    # ── Startup ───────────────────────────────────────────────────────────────

    def run(self) -> None:
        os_signal.signal(os_signal.SIGINT,  self._shutdown)
        os_signal.signal(os_signal.SIGTERM, self._shutdown)

        if not market_calendar.is_market_open():
            delta = market_calendar.time_until_open()
            logger.info("NIFTY market closed — next open in %s. Pre-loading history anyway.", delta)
        else:
            logger.info("NIFTY market is open. Pre-loading history.")

        self._data_feed.preload_history()
        logger.info("Historical data loaded. Latest price: ₹%.2f", self._data_feed.latest_price)

        poll_thread = threading.Thread(
            target=self._data_feed.start_polling,
            name="nifty-poll",
            daemon=True,
        )
        poll_thread.start()
        logger.info("yfinance polling thread started.")

        # Transition state from "starting" → "running" immediately so the
        # dashboard doesn't sit on "starting..." while the first 30-min
        # analysis cycle (yfinance + NSE + HMM) runs.
        self._write_state("running")

        self._analysis_loop()

    # ── Polling callback ──────────────────────────────────────────────────────

    def _on_bar(self, bar) -> None:
        try:
            current_price = float(bar.get("close", 0))
        except Exception:
            current_price = 0.0

        self._session_manager.tick()
        self._order_manager.update_price(current_price)

        if int(time.time()) % 600 < 2:
            av = self._order_manager.get_account_value()
            if av > 0:
                self._risk_manager.account_value = av

        if self._active_trade_id and current_price > 0:
            self._monitor_position(current_price)

        self._write_state()

    def _on_session_open(self, session: str) -> None:
        # Indian session open is handled by market_calendar; ignore generic global sessions
        return

    # ── Analysis loop ─────────────────────────────────────────────────────────

    def _analysis_loop(self) -> None:
        TICK = 2
        first_success = False
        elapsed_since_state_write = 0.0
        while self._running:
            now = time.time()
            elapsed = now - self._last_analysis_time
            interval = NIFTY_ANALYSIS_INTERVAL if first_success else 120

            # Always read trigger files (dashboard "Analyze Now"). Polled every 2s so
            # the user sees the analysis kick in within seconds.
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
                self._run_analysis(trigger="cycle" if first_success else "startup")
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
        logger.info("--- Running NIFTY analysis [trigger: %s] ---", trigger)
        self._last_analysis_time = time.time()

        if self._active_trade_id:
            logger.info("Active trade %s in progress — skipping new signal", self._active_trade_id)
            return

        snapshot = self._build_snapshot()
        if snapshot["current_price"] == 0:
            logger.warning("No price data available yet — skipping analysis")
            return

        signal = self._signal_gen.generate(snapshot, self._data_feed)
        signal.asset = "NIFTY 50"
        self._last_signal = signal

        # Override SL to 1.5% regardless of model output
        if signal.entry_price > 0 and signal.bias in ("BULLISH", "BEARISH"):
            if signal.bias == "BULLISH":
                signal.stop_loss = round(signal.entry_price * (1 - NIFTY_STOP_LOSS_PCT), 2)
            else:
                signal.stop_loss = round(signal.entry_price * (1 + NIFTY_STOP_LOSS_PCT), 2)

            # Fill missing TP1/TP2/TP3 from entry+SL — chart trade-zone overlay
            # needs all 3 even when Groq omits them.
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
            "Signal: %s | Quality: %s | Score: %s | Entry: ₹%.2f | SL: ₹%.2f",
            signal.bias, signal.signal_quality, signal.signal_score,
            signal.entry_price, signal.stop_loss,
        )

        if signal.signal_quality == "NO_TRADE" or signal.bias not in ("BULLISH", "BEARISH"):
            logger.info("No tradeable signal this cycle (quality=%s).", signal.signal_quality)
            self._write_state()
            return

        # Block new entries when market is closed (or after 15:00 IST)
        if not market_calendar.is_market_open():
            logger.info("NIFTY market is closed — emitting signal but not opening a paper trade")
            self._write_state()
            return

        from datetime import time as dtime
        ist = market_calendar._to_ist(None).time()
        if ist >= dtime(15, 0):
            logger.info("After 15:00 IST — no new entries (close-out window)")
            self._write_state()
            return

        if signal.entry_price <= 0 or signal.stop_loss <= 0:
            logger.warning("Signal missing entry/stop levels — skipping")
            return

        buying_power = self._order_manager.get_account_value()
        pos_size = self._risk_manager.calculate_position_size(
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            signal_quality=signal.signal_quality,
            daily_atr=snapshot.get("daily_atr", 0.0),
            vix=snapshot.get("india_vix", 0.0),
            buying_power=buying_power,
        )
        if pos_size <= 0:
            logger.error("Position size returned 0 — aborting")
            return

        logger.info(
            "Placing paper order: %s %.4f units NIFTY @ ₹%.2f (SL ₹%.2f, TP1 ₹%.2f)",
            signal.bias, pos_size, signal.entry_price, signal.stop_loss, signal.take_profit_1,
        )

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
        """Drive the TP-ladder. Partial closes happen inside the order manager;
        we only book the trade-logger close on full flatten."""
        if not self._active_signal:
            return
        from modules.tp_ladder import tick_position
        try:
            bars_15m = self._data_feed.get_bars("15Min")
            atr = calculate_atr(bars_15m) if not bars_15m.empty else 0.0
        except Exception:
            atr = 0.0
        reason = tick_position(self._order_manager, current_price, atr=float(atr))
        if reason in ("SL", "TP3", "EXIT_TIME"):
            summary = self._order_manager.last_close_summary or {}
            exit_px = float(summary.get("exit_price", current_price))
            pnl = float(summary.get("realized_pnl", 0.0))
            logger.info("[NIFTY] Position fully flat (%s)  P&L ₹%+.2f", reason, pnl)
            self._book_trade_close(exit_px, pnl=pnl, reason=reason,
                                   exits=summary.get("exits", []))

    def _close_trade(self, exit_price: float, reason: str = "") -> None:
        """Manual full-flatten path (dashboard 'close now', shutdown). Ladder
        path uses `_book_trade_close`."""
        if not self._active_trade_id or not self._active_signal:
            return
        sig     = self._active_signal
        is_long = sig.bias == "BULLISH"
        entry   = sig.entry_price
        pnl_per_unit = (exit_price - entry) if is_long else (entry - exit_price)

        log_entries = [t for t in self._trade_logger._trades
                       if t["trade_id"] == self._active_trade_id]
        pos_size = log_entries[0]["position_size"] if log_entries else 0.0
        pnl_inr = pnl_per_unit * pos_size

        if not self._order_manager.close_position(reason=reason or "MANUAL"):
            logger.error("Failed to close paper position")
            return

        # Prefer realized_pnl from OM if it tracked tranches
        summary = self._order_manager.last_close_summary or {}
        if summary.get("realized_pnl") is not None:
            pnl_inr = float(summary["realized_pnl"])

        self._trade_logger.log_trade_close(
            trade_id=self._active_trade_id,
            actual_exit=exit_price,
            actual_sl=sig.stop_loss,
            pnl_dollars=pnl_inr,
            improvement=f"Exit reason: {reason}",
        )
        self._risk_manager.on_trade_result(pnl_inr)

        new_balance = self._order_manager.get_account_value()
        if new_balance > 0:
            self._risk_manager.account_value = new_balance

        stats = self._trade_logger.get_session_stats()
        logger.info(
            "NIFTY stats — Trades: %d | Win rate: %.1f%% | Avg R:R: %.2f | Total P&L: %.2f%%",
            stats["total"], stats["win_rate"] * 100, stats["avg_rr"], stats["total_pnl_pct"],
        )

        self._active_trade_id = None
        self._active_order_id = None
        self._active_signal   = None
        self._write_state()

    def _book_trade_close(self, exit_price: float, pnl: float, reason: str,
                          exits: list) -> None:
        """Trade-logger close path for the TP-ladder state machine."""
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
            "NIFTY stats — Trades: %d | Win rate: %.1f%% | Avg R:R: %.2f | Total P&L: %.2f%%",
            stats["total"], stats["win_rate"] * 100, stats["avg_rr"], stats["total_pnl_pct"],
        )
        self._active_trade_id = None
        self._active_order_id = None
        self._active_signal   = None
        self._write_state()

    # ── Snapshot builder ──────────────────────────────────────────────────────

    def _build_snapshot(self) -> dict:
        bars_1m    = self._data_feed.get_bars("1Min")
        bars_15m   = self._data_feed.get_bars("15Min")
        bars_1h    = self._data_feed.get_bars("1Hour")
        bars_daily = self._data_feed.get_bars("1Day")

        current_price = self._data_feed.latest_price
        session_vwap  = calculate_vwap(bars_1m) if not bars_1m.empty else 0.0
        daily_atr     = calculate_atr(bars_daily) if not bars_daily.empty else 0.0
        atr_h1        = calculate_atr(bars_1h)    if not bars_1h.empty    else 0.0
        atr_15m       = calculate_atr(bars_15m)   if not bars_15m.empty   else 0.0
        zscore        = price_zscore(bars_1h) if not bars_1h.empty else 0.0

        key_levels = get_key_levels(bars_1h, bars_daily, current_price) \
                     if (not bars_1h.empty and not bars_daily.empty) else None

        daily_struct = classify_structure(bars_daily) if not bars_daily.empty else "ranging"
        h1_struct    = classify_structure(bars_1h)    if not bars_1h.empty  else "ranging"
        # NIFTY only has ~6h sessions; use 4-bar 1H resample as h4 proxy.
        h4_struct = h1_struct
        if not bars_1h.empty and len(bars_1h) >= 16:
            try:
                bars_4h_resampled = bars_1h.resample("4h").agg(
                    {"open": "first", "high": "max", "low": "min",
                     "close": "last", "volume": "sum"}).dropna()
                if not bars_4h_resampled.empty:
                    h4_struct = classify_structure(bars_4h_resampled)
            except Exception:
                pass

        # Indian-specific macro
        try:
            usdinr_df = self._data_feed.get_usdinr_1d()
            usdinr = float(usdinr_df["close"].iloc[-1]) if not usdinr_df.empty else 0.0
        except Exception:
            usdinr = 0.0
        try:
            vix_df = self._data_feed.get_vix_1d()
            india_vix = float(vix_df["close"].iloc[-1]) if not vix_df.empty else 0.0
        except Exception:
            india_vix = 0.0
        try:
            fii_dii = self._data_feed.get_fii_dii_summary()
        except Exception:
            fii_dii = {}

        return {
            "timestamp_ist":       market_calendar._to_ist(None).isoformat(),
            "current_price":       current_price,
            "price":               current_price,
            "daily_high":          float(bars_daily["high"].max()) if not bars_daily.empty else 0,
            "daily_low":           float(bars_daily["low"].min())  if not bars_daily.empty else 0,
            "session_vwap":        session_vwap,
            "vwap_distance":       round(current_price - session_vwap, 2) if session_vwap else 0,
            "daily_atr":           daily_atr,
            "atr_h1":              atr_h1,
            "atr_15m":             atr_15m,
            "zscore":              zscore,
            "current_session":     _ist_session_label(),
            "daily_structure":     daily_struct,
            "h4_structure":        h4_struct,
            "h1_structure":        h1_struct,
            "pdh":                 key_levels.pdh         if key_levels else 0,
            "pdl":                 key_levels.pdl         if key_levels else 0,
            "weekly_open":         key_levels.weekly_open if key_levels else 0,
            "usdinr":              usdinr,
            "india_vix":           india_vix,
            "fii_dii":             fii_dii,
            "consecutive_losses":  self._risk_manager.consecutive_losses,
            "daily_pnl_pct":       round(self._risk_manager.daily_pnl_pct * 100, 3),
            "last_trade_result":   self._trade_logger.last_trade_result(),
            "account_value":       round(self._risk_manager.account_value, 2),
            "market_open":         market_calendar.is_market_open(),
        }

    # ── Approval helpers ──────────────────────────────────────────────────────

    def _write_pending_signal(self, signal: TradeSignal, pos_size: float) -> None:
        expires = datetime.now(timezone.utc).timestamp() + APPROVAL_TIMEOUT_SEC
        data = {
            "asset":        "NIFTY 50",
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
        NIFTY_PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(NIFTY_PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _wait_for_approval(self) -> bool:
        deadline = time.time() + APPROVAL_TIMEOUT_SEC
        while time.time() < deadline:
            try:
                data = json.loads(NIFTY_PENDING_FILE.read_text(encoding="utf-8"))
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
            if NIFTY_PENDING_FILE.exists():
                NIFTY_PENDING_FILE.unlink()
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
                    unreal_per_unit = (current_price - sig.entry_price) if current_price else 0
                else:
                    unreal_per_unit = (sig.entry_price - current_price) if current_price else 0

                log_entries = [t for t in self._trade_logger._trades
                               if t["trade_id"] == self._active_trade_id]
                pos_size = log_entries[0]["position_size"] if log_entries else 0
                notional = pos_size * sig.entry_price if pos_size else 0
                unreal_pl_pct = (unreal_per_unit / sig.entry_price * 100) if sig.entry_price else 0

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
                    "unrealized_pl":     round(unreal_per_unit * pos_size, 2),
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
                "asset":         "NIFTY 50",
                "last_updated":  datetime.now(timezone.utc).isoformat(),
                "bot_status":    bot_status,
                "latest_price":  self._data_feed.latest_price,
                "session":       _ist_session_label(),
                "market_status": market_calendar.market_status_dict(),
                "account": {
                    "balance":   round(self._order_manager.get_account_value(), 2),
                    "currency":  "INR",
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
            logger.debug("Failed to write nifty_state.json: %s", exc)

    # ── Shutdown ──────────────────────────────────────────────────────────────

    def _shutdown(self, *_) -> None:
        logger.info("Shutdown signal received. Stopping NIFTY agent...")
        self._running = False
        self._data_feed.stop()
        if self._active_trade_id:
            current_price = self._data_feed.latest_price
            if current_price > 0:
                logger.warning("Open trade %s during shutdown — closing at ₹%.2f",
                               self._active_trade_id, current_price)
                self._close_trade(current_price, reason="agent_shutdown")
        sys.exit(0)


if __name__ == "__main__":
    agent = NIFTYTradingAgent()
    agent.run()
