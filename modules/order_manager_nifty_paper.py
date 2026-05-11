"""
modules/order_manager_nifty_paper.py
Local paper-trading simulator for NIFTY 50. No broker required (no Zerodha /
Angel / Dhan integration in v1). Maintains a single open position with
mark-to-market P&L, persists state to disk so the dashboard can read it.

Lot-size aware: position sizing rounds DOWN to whole NIFTY index futures lots
(75 units / lot, FY26 spec). For spot-only paper-sim we still report fractional
units in `qty` so P&L tracks the same.

Same surface area as `modules/order_manager.py::OrderManager` so the
agent loop can call get_account_value / get_open_position / place_order /
close_position interchangeably.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import (
    NIFTY_LOT_SIZE, NIFTY_PAPER_STARTING_BALANCE, NIFTY_STATE_FILE,
    TP_LADDER_FRACTIONS,
)
from modules.signal_generator import TradeSignal

logger = logging.getLogger(__name__)


class NIFTYPaperOrderManager:
    """In-memory + on-disk paper position tracker for NIFTY 50."""

    def __init__(self, starting_balance: float = NIFTY_PAPER_STARTING_BALANCE):
        self._balance: float = float(starting_balance)
        self._position: Optional[dict] = None
        self._latest_price: float = 0.0
        self._last_close_summary: Optional[dict] = None
        self._state_path = Path(NIFTY_STATE_FILE)
        self._load()

    @property
    def last_close_summary(self) -> Optional[dict]:
        return self._last_close_summary

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            self._balance = float(data.get("balance", self._balance))
            self._position = data.get("position")
        except Exception as exc:
            logger.warning("Failed to load NIFTY paper state: %s", exc)

    def _save(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._state_path.write_text(
                json.dumps({"balance": self._balance, "position": self._position},
                           indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Failed to save NIFTY paper state: %s", exc)

    # ── Public API (mirrors Alpaca OrderManager) ──────────────────────────────

    def update_price(self, price: float) -> None:
        if price > 0:
            self._latest_price = float(price)

    def get_account_value(self) -> float:
        if not self._position:
            return float(self._balance)
        return float(self._balance + self._unrealized_pnl())

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
            "lots":          self._position.get("lots", 0),
        }

    def place_order(self, signal: TradeSignal, position_size: float) -> Optional[str]:
        if self._position is not None:
            logger.warning("NIFTY paper position already open; refusing new order")
            return None
        if position_size <= 0 or signal.entry_price <= 0:
            logger.error("Invalid NIFTY paper order: size=%s entry=%s",
                         position_size, signal.entry_price)
            return None

        # Lot-size awareness: round down to whole lots if we have at least 1 lot.
        # If sizing produced fractional sub-lot quantity, fall back to spot-units mode.
        lots = int(position_size // NIFTY_LOT_SIZE)
        if lots >= 1:
            qty = float(lots * NIFTY_LOT_SIZE)
        else:
            qty = float(position_size)
            lots = 0

        side = "long" if signal.bias == "BULLISH" else "short"
        order_id = f"PAPER-{uuid.uuid4().hex[:8].upper()}"
        notional = qty * signal.entry_price

        # Cap by balance (long side margin proxy)
        if side == "long" and notional > self._balance:
            logger.warning("NIFTY notional ₹%.2f > balance ₹%.2f — capping size",
                           notional, self._balance)
            qty = (self._balance * 0.95) / signal.entry_price
            if int(qty // NIFTY_LOT_SIZE) >= 1:
                lots = int(qty // NIFTY_LOT_SIZE)
                qty = float(lots * NIFTY_LOT_SIZE)
            notional = qty * signal.entry_price
        if qty <= 0:
            return None

        self._position = {
            "order_id":      order_id,
            "side":          side,
            "qty":           float(qty),
            "remaining_qty": float(qty),
            "lots":          lots,
            "entry_price":   float(signal.entry_price),
            "stop_loss":     float(signal.stop_loss),
            "current_sl":    float(signal.stop_loss),
            "tp1":           float(signal.take_profit_1),
            "tp2":           float(signal.take_profit_2),
            "tp3":           float(signal.take_profit_3),
            "tp1_done":      False,
            "tp2_done":      False,
            "tp3_done":      False,
            "notional":      float(notional),
            "opened_at":     datetime.now(timezone.utc).isoformat(),
            "opened_ts":     time.time(),
            "max_hold_time": signal.max_hold_time,
            "realized_pnl":  0.0,
            "exits":         [],
        }
        self._save()
        logger.info(
            "[PAPER] %s %.4f units (%d lot) NIFTY @ %.2f (notional ₹%.2f) id=%s",
            side.upper(), qty, lots, signal.entry_price, notional, order_id,
        )
        return order_id

    def close_position(self, reason: str = "MANUAL") -> bool:
        if not self._position:
            logger.info("No NIFTY paper position to close")
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
            "[PAPER] Close-%s %s %.4f units NIFTY @ %.2f  P&L ₹%+.2f  total ₹%+.2f  → balance ₹%.2f",
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
            "[PAPER] %s scale-out %.4f units NIFTY @ %.2f  P&L ₹%+.2f  remaining %.4f",
            reason, qty_to_close, exit_price, pnl, self._position["remaining_qty"],
        )
        if self._position["remaining_qty"] <= 1e-9:
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
        if not self._position or new_sl <= 0:
            return
        prev = self._position.get("current_sl", self._position["stop_loss"])
        self._position["current_sl"] = float(new_sl)
        logger.info("[PAPER] NIFTY SL moved %.2f → %.2f (%s)", prev, new_sl, reason)
        self._save()

    def mark_tp_done(self, level: str) -> None:
        if not self._position:
            return
        key = f"{level.lower()}_done"
        if key in self._position:
            self._position[key] = True
            self._save()

    def is_max_hold_breached(self, default_hours: float = 4.0) -> bool:
        if not self._position:
            return False
        opened_ts = float(self._position.get("opened_ts", 0) or 0)
        if opened_ts <= 0:
            return False
        raw = str(self._position.get("max_hold_time", "") or "").lower().strip()
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
        pos = self._position
        if not pos:
            return 0.0
        q = float(qty) if qty is not None else float(pos.get("remaining_qty", pos["qty"]))
        if pos["side"] == "long":
            return (exit_price - pos["entry_price"]) * q
        return (pos["entry_price"] - exit_price) * q

    # Compatibility no-ops
    def cancel_stale_orders(self, max_age_hours: float = 4.0) -> int:
        return 0

    def cancel_all_orders(self) -> None:
        return None

    @property
    def balance(self) -> float:
        return self._balance
