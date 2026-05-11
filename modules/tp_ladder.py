"""
modules/tp_ladder.py
Drives the TP-ladder state machine that paper order managers expose.
Called once per monitor tick by the asset agents.

Behaviour (LONG; SHORT inverts):
  bar.close ≥ TP1 (and !tp1_done)  → partial_close 40%, SL → entry (BE)
  bar.close ≥ TP2 (and !tp2_done)  → partial_close 35%, SL → TP1 − 0.25·ATR (trail)
  bar.close ≥ TP3 (and !tp3_done)  → close remaining 25% (final)
  bar.close ≤ current_sl            → close all remaining (SL hit)
  time-since-open > max_hold        → close all remaining (EXIT_TIME)

Why a single helper: the ladder logic is identical for XAU + NIFTY paper
order managers. Centralising kills drift between the two agent monitors.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from config import TP_LADDER_FRACTIONS

logger = logging.getLogger(__name__)


def tick_position(om: Any, current_price: float, atr: float = 0.0) -> Optional[str]:
    """Drive the state machine one tick.

    `om` must implement:
      - get_open_position() -> dict or None
      - partial_close(fraction_of_original, exit_price, reason) -> dict
      - update_stop(new_sl, reason) -> None
      - mark_tp_done(level) -> None
      - close_position(reason) -> bool
      - is_max_hold_breached() -> bool

    Returns:
      None  → position still open (or no position)
      str   → reason position was fully flattened ("SL", "TP3", "EXIT_TIME")
    """
    pos = om.get_open_position()
    if not pos:
        return None

    px = float(current_price)
    if px <= 0:
        return None
    # Sync the OM's mark-price so close_position() uses the live value, not
    # whatever stale price was last set externally.
    try:
        om.update_price(px)
    except Exception:
        pass

    side = pos["side"]                    # "long" | "short"
    is_long = side == "long"
    entry = float(pos["avg_entry"])
    sl = float(pos["current_sl"])
    tp1 = float(pos["tp1"]) if pos.get("tp1") else 0.0
    tp2 = float(pos["tp2"]) if pos.get("tp2") else 0.0
    tp3 = float(pos["tp3"]) if pos.get("tp3") else 0.0

    # 1) max-hold breach: close at market regardless of P&L
    if om.is_max_hold_breached():
        om.close_position(reason="EXIT_TIME")
        return "EXIT_TIME"

    # 2) SL hit: close all remaining at current price
    sl_hit = (is_long and px <= sl) or ((not is_long) and px >= sl)
    if sl_hit:
        om.close_position(reason="SL")
        return "SL"

    # 3) TP3 hit (final): close remaining 25%
    tp3_hit = tp3 > 0 and ((is_long and px >= tp3) or ((not is_long) and px <= tp3))
    if tp3_hit and not pos.get("tp3_done"):
        om.partial_close(TP_LADDER_FRACTIONS["tp3"], px, reason="TP3")
        om.mark_tp_done("tp3")
        # If a sliver remains due to rounding, sweep it
        rem = om.get_open_position()
        if rem and rem.get("remaining_qty", 0) > 0:
            om.close_position(reason="TP3_SWEEP")
        return "TP3"

    # 4) TP2 hit: scale out 35%, trail SL to (TP1 − 0.25·ATR)
    tp2_hit = tp2 > 0 and ((is_long and px >= tp2) or ((not is_long) and px <= tp2))
    if tp2_hit and not pos.get("tp2_done"):
        om.partial_close(TP_LADDER_FRACTIONS["tp2"], px, reason="TP2")
        om.mark_tp_done("tp2")
        # Trail SL: protect 1R+ profit
        if tp1 > 0:
            buffer = 0.25 * atr if atr > 0 else 0.0
            new_sl = (tp1 - buffer) if is_long else (tp1 + buffer)
            om.update_stop(new_sl, reason="TP2 trail")
        return None

    # 5) TP1 hit: scale out 40%, move SL to entry (breakeven)
    tp1_hit = tp1 > 0 and ((is_long and px >= tp1) or ((not is_long) and px <= tp1))
    if tp1_hit and not pos.get("tp1_done"):
        om.partial_close(TP_LADDER_FRACTIONS["tp1"], px, reason="TP1")
        om.mark_tp_done("tp1")
        om.update_stop(entry, reason="BE @ TP1")
        return None

    return None
