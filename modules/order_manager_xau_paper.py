"""
modules/order_manager_xau_paper.py
Local paper-trading simulator for XAU/USD. No broker required. Maintains
a single open position with mark-to-market P&L + TP-ladder state machine
(scale out at TP1/TP2/TP3, breakeven move at TP1, trailing SL after TP2),
persists state to disk so the dashboard can read it.

Surface mirrors `modules/order_manager.py::OrderManager`.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import TP_LADDER_FRACTIONS, XAU_PAPER_STARTING_BALANCE
from modules.signal_generator import TradeSignal

logger = logging.getLogger(__name__)


_STATE_PATH = Path("logs") / "xau_paper_state.json"


class XAUPaperOrderManager:
    """In-memory + on-disk paper position tracker. Fills at signal price."""

    def __init__(self, starting_balance: float = XAU_PAPER_STARTING_BALANCE):
        self._balance: float = float(starting_balance)
        self._position: Optional[dict] = None    # see _new_position()
        self._latest_price: float = 0.0
        self._last_close_summary: Optional[dict] = None
        self._load()

    @property
    def last_close_summary(self) -> Optional[dict]:
        """Snapshot of the most recently fully-flattened position. Used by the
        agent monitor to log the final trade after the TP-ladder runs."""
        return self._last_close_summary

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not _STATE_PATH.exists():
            return
        try:
            data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            self._balance = float(data.get("balance", self._balance))
            self._position = data.get("position")
        except Exception as exc:
            logger.warning("Failed to load paper state: %s", exc)

    def _save(self) -> None:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            _STATE_PATH.write_text(
                json.dumps({"balance": self._balance, "position": self._position},
                           indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Failed to save paper state: %s", exc)

    # ── Public API (mirrors Alpaca OrderManager) ──────────────────────────────

    def update_price(self, price: float) -> None:
        if price > 0:
            self._latest_price = float(price)

    def get_account_value(self) -> float:
        """Cash + unrealized P&L of any open position."""
        if not self._position:
            return float(self._balance)
        unreal = self._unrealized_pnl()
        return float(self._balance + unreal)

    def get_open_position(self) -> Optional[dict]:
        if not self._position:
            return None
        remaining = float(self._position.get("remaining_qty", self._position["qty"]))
        return {
            "qty":           self._position["qty"],
            "remaining_qty": remaining,
            "side":          self._position["side"],
            "avg_entry":     self._position["entry_price"],
            "current_sl":    self._position.get("current_sl", self._position["stop_loss"]),
            "tp1":           self._position.get("tp1"),
            "tp2":           self._position.get("tp2"),
            "tp3":           self._position.get("tp3"),
            "tp1_done":      bool(self._position.get("tp1_done", False)),
            "tp2_done":      bool(self._position.get("tp2_done", False)),
            "tp3_done":      bool(self._position.get("tp3_done", False)),
            "market_value":  remaining * self._latest_price if self._latest_price else 0.0,
            "unrealized_pl": self._unrealized_pnl(),
            "realized_pnl":  float(self._position.get("realized_pnl", 0.0)),
            "exits":         list(self._position.get("exits", [])),
        }

    def place_order(self, signal: TradeSignal, position_size: float) -> Optional[str]:
        if self._position is not None:
            logger.warning("Paper position already open; refusing new order")
            return None
        if position_size <= 0 or signal.entry_price <= 0:
            logger.error("Invalid paper order: size=%s entry=%s", position_size, signal.entry_price)
            return None

        side = "long" if signal.bias == "BULLISH" else "short"
        order_id = f"PAPER-{uuid.uuid4().hex[:8].upper()}"
        notional = position_size * signal.entry_price

        # Subtract margin (we use the full notional as locked capital — simpler than margin model).
        if side == "long" and notional > self._balance:
            logger.warning("Paper notional $%.2f > balance $%.2f — capping size", notional, self._balance)
            position_size = (self._balance * 0.95) / signal.entry_price
            notional = position_size * signal.entry_price
        if position_size <= 0:
            return None

        self._position = {
            "order_id":      order_id,
            "side":          side,
            "qty":           float(position_size),       # original qty (immutable)
            "remaining_qty": float(position_size),       # decrements on partial closes
            "entry_price":   float(signal.entry_price),
            "stop_loss":     float(signal.stop_loss),
            "current_sl":    float(signal.stop_loss),    # dynamic SL (moves to BE @ TP1, trails @ TP2)
            "tp1":           float(signal.take_profit_1),
            "tp2":           float(signal.take_profit_2),
            "tp3":           float(signal.take_profit_3),
            "tp1_done":      False,
            "tp2_done":      False,
            "tp3_done":      False,
            "notional":      float(notional),
            "opened_at":     datetime.now(timezone.utc).isoformat(),
            "opened_ts":     time.time(),                # epoch for max_hold checks
            "max_hold_time": signal.max_hold_time,
            "realized_pnl":  0.0,
            "exits":         [],                         # ordered list of partial-close events
        }
        self._save()
        logger.info(
            "[PAPER] %s %.4f oz XAU @ $%.2f (notional $%.2f) id=%s",
            side.upper(), position_size, signal.entry_price, notional, order_id,
        )
        return order_id

    def close_position(self, reason: str = "MANUAL") -> bool:
        if not self._position:
            logger.info("No paper position to close")
            return True
        exit_price = self._latest_price or self._position["entry_price"]
        remaining = float(self._position.get("remaining_qty", self._position["qty"]))
        pnl = self._pnl_at(exit_price, qty=remaining)
        self._balance += pnl
        self._position["realized_pnl"] = float(self._position.get("realized_pnl", 0.0)) + pnl
        self._position.setdefault("exits", []).append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "reason": reason, "qty": remaining, "exit_price": exit_price, "pnl": pnl,
        })
        logger.info(
            "[PAPER] Close-%s %s %.4f oz XAU @ $%.2f  P&L $%+.2f  total $%+.2f  → balance $%.2f",
            reason, self._position["side"].upper(), remaining, exit_price,
            pnl, self._position["realized_pnl"], self._balance,
        )
        self._last_close_summary = {
            "order_id":     self._position["order_id"],
            "side":         self._position["side"],
            "entry_price":  self._position["entry_price"],
            "exit_price":   exit_price,
            "qty":          self._position["qty"],
            "realized_pnl": float(self._position["realized_pnl"]),
            "exits":        list(self._position.get("exits", [])),
            "reason":       reason,
        }
        self._position = None
        self._save()
        return True

    # ── TP-ladder state machine ───────────────────────────────────────────────

    def partial_close(self, fraction_of_original: float, exit_price: float,
                      reason: str) -> dict:
        """Close `fraction_of_original` × original qty at `exit_price`. Updates
        balance + remaining_qty + exits log. Returns event dict.

        If remaining_qty would go non-positive, closes whole position.
        """
        if not self._position:
            return {"qty_closed": 0.0, "pnl": 0.0, "reason": reason, "remaining": 0.0}
        original = float(self._position["qty"])
        remaining = float(self._position.get("remaining_qty", original))
        qty_to_close = max(0.0, min(remaining, original * float(fraction_of_original)))
        if qty_to_close <= 1e-9:
            return {"qty_closed": 0.0, "pnl": 0.0, "reason": reason, "remaining": remaining}

        pnl = self._pnl_at(exit_price, qty=qty_to_close)
        self._balance += pnl
        self._position["remaining_qty"] = remaining - qty_to_close
        self._position["realized_pnl"] = float(self._position.get("realized_pnl", 0.0)) + pnl
        evt = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "reason": reason, "qty": qty_to_close,
            "exit_price": exit_price, "pnl": pnl,
        }
        self._position.setdefault("exits", []).append(evt)
        logger.info(
            "[PAPER] %s scale-out %.4f oz XAU @ $%.2f  P&L $%+.2f  remaining %.4f",
            reason, qty_to_close, exit_price, pnl, self._position["remaining_qty"],
        )
        if self._position["remaining_qty"] <= 1e-9:
            # Final tranche closed → flatten + cache summary
            self._last_close_summary = {
                "order_id":     self._position["order_id"],
                "side":         self._position["side"],
                "entry_price":  self._position["entry_price"],
                "exit_price":   exit_price,
                "qty":          self._position["qty"],
                "realized_pnl": float(self._position["realized_pnl"]),
                "exits":        list(self._position.get("exits", [])),
                "reason":       reason,
            }
            self._position = None
        self._save()
        return {**evt, "remaining": 0.0 if not self._position else self._position["remaining_qty"]}

    def update_stop(self, new_sl: float, reason: str = "") -> None:
        """Move the active stop-loss. Called on TP1 (move to BE) and TP2 (trail)."""
        if not self._position or new_sl <= 0:
            return
        prev = self._position.get("current_sl", self._position["stop_loss"])
        self._position["current_sl"] = float(new_sl)
        logger.info("[PAPER] SL moved %.2f → %.2f (%s)", prev, new_sl, reason)
        self._save()

    def mark_tp_done(self, level: str) -> None:
        """Idempotent: mark tp1_done/tp2_done/tp3_done so the monitor doesn't fire twice."""
        if not self._position:
            return
        key = f"{level.lower()}_done"
        if key in self._position:
            self._position[key] = True
            self._save()

    def is_max_hold_breached(self, default_hours: float = 8.0) -> bool:
        """Parse `max_hold_time` ('8 hours' / '4-12 hours' / '4 hours') and check
        whether the position has been open longer than that."""
        if not self._position:
            return False
        opened_ts = float(self._position.get("opened_ts", 0) or 0)
        if opened_ts <= 0:
            return False
        raw = str(self._position.get("max_hold_time", "") or "").lower().strip()
        # Pull first integer found (e.g. "4-12 hours" → 4; "8 hours" → 8)
        import re as _re
        m = _re.search(r"(\d+)", raw)
        hours = float(m.group(1)) if m else default_hours
        return (time.time() - opened_ts) > (hours * 3600.0)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _unrealized_pnl(self) -> float:
        if not self._position or self._latest_price <= 0:
            return 0.0
        return self._pnl_at(self._latest_price)

    def _pnl_at(self, exit_price: float, qty: Optional[float] = None) -> float:
        """P&L for `qty` units (defaults to remaining_qty) closed at exit_price."""
        pos = self._position
        if not pos:
            return 0.0
        q = float(qty) if qty is not None else float(pos.get("remaining_qty", pos["qty"]))
        if pos["side"] == "long":
            return (exit_price - pos["entry_price"]) * q
        return (pos["entry_price"] - exit_price) * q

    # No-op methods for compatibility with the BTC interface
    def cancel_stale_orders(self, max_age_hours: float = 4.0) -> int:
        return 0

    def cancel_all_orders(self) -> None:
        return None

    @property
    def balance(self) -> float:
        return self._balance
