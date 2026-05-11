"""
trade_logger.py
Persists trade records and post-trade reviews to logs/trades_log.json.
Provides session statistics (win rate, avg R:R, drawdown).
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import LOG_FILE

logger = logging.getLogger(__name__)


class TradeLogger:
    """
    Writes trade open/close records and post-trade reviews to JSON log.
    """

    def __init__(self, log_file: Optional[Path] = None, asset: str = "BTC/USD"):
        self._log_path = Path(log_file) if log_file is not None else Path(LOG_FILE)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._asset = asset
        self._trades: List[Dict[str, Any]] = self._load()
        self._open_trade: Optional[Dict[str, Any]] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def log_trade_open(
        self,
        signal,
        actual_entry:    float,
        position_size:   float,
        alpaca_order_id: str,
    ) -> str:
        """Record trade entry. Returns trade_id."""
        trade_id = str(uuid.uuid4())[:8].upper()
        record = {
            "trade_id":       trade_id,
            "alpaca_order_id": alpaca_order_id,
            "asset":          self._asset,
            "date_time_open": datetime.now(timezone.utc).isoformat(),
            "date_time_close": None,
            "strategy":       signal.strategy,
            "signal_quality": signal.signal_quality,
            "signal_score":   signal.signal_score,
            "planned_entry":  signal.entry_price,
            "actual_entry":   actual_entry,
            "slippage":       round(actual_entry - signal.entry_price, 2),
            "planned_sl":     signal.stop_loss,
            "planned_tp1":    signal.take_profit_1,
            "planned_tp2":    signal.take_profit_2,
            "position_size":  position_size,
            "risk_pct":       signal.risk_pct,
            "bias":           signal.bias,
            "session":        signal.session,
            "actual_sl":      None,
            "actual_exit":    None,
            "result":         None,
            "pnl_dollars":    None,
            "pnl_pct":        None,
            "hold_time_min":  None,
            "checks": {
                "macro_aligned":      None,
                "session_confirmed":  None,
                "liquidity_swept":    None,
                "vwap_confluence":    None,
                "order_flow_aligned": None,
                "funding_ok":         "PASS" in signal.funding_check.upper(),
            },
            "improvement":    None,
            "raw_signal":     signal.raw_response[:500] if signal.raw_response else "",
        }
        self._open_trade = record
        self._trades.append(record)
        self._save()
        logger.info("Trade opened: %s | Entry: $%.2f | SL: $%.2f",
                    trade_id, actual_entry, signal.stop_loss)
        return trade_id

    def log_trade_close(
        self,
        trade_id:    str,
        actual_exit: float,
        actual_sl:   float,
        pnl_dollars: float,
        improvement: str = "",
    ) -> None:
        """Record trade exit and write post-trade review."""
        record = self._find_trade(trade_id)
        if not record:
            logger.error("Trade %s not found in log", trade_id)
            return

        open_dt  = datetime.fromisoformat(record["date_time_open"])
        close_dt = datetime.now(timezone.utc)
        hold_min = int((close_dt - open_dt).total_seconds() / 60)
        entry    = record["actual_entry"] or record["planned_entry"]
        pnl_pct  = pnl_dollars / (entry * record["position_size"]) if entry > 0 else 0

        record.update({
            "date_time_close": close_dt.isoformat(),
            "actual_exit":     actual_exit,
            "actual_sl":       actual_sl,
            "result":          "WIN" if pnl_dollars > 0 else ("LOSS" if pnl_dollars < 0 else "BREAKEVEN"),
            "pnl_dollars":     round(pnl_dollars, 2),
            "pnl_pct":         round(pnl_pct * 100, 3),
            "hold_time_min":   hold_min,
            "improvement":     improvement,
        })
        self._save()
        self._print_post_trade_review(record)
        self._open_trade = None
        logger.info("Trade closed: %s | Result: %s | P&L: $%.2f",
                    trade_id, record["result"], pnl_dollars)

    # ── Statistics ────────────────────────────────────────────────────────────

    def get_session_stats(self) -> Dict[str, Any]:
        """Win rate, avg R:R, drawdown, trade count from all closed trades."""
        closed = [t for t in self._trades if t.get("result") is not None]
        if not closed:
            return {"total": 0, "win_rate": 0.0, "avg_rr": 0.0, "total_pnl_pct": 0.0}

        wins      = [t for t in closed if t["result"] == "WIN"]
        losses    = [t for t in closed if t["result"] == "LOSS"]
        win_rate  = len(wins) / len(closed) if closed else 0.0
        total_pnl = sum(t.get("pnl_pct", 0) or 0 for t in closed)

        rrs = []
        for t in closed:
            entry = t.get("actual_entry") or t.get("planned_entry", 0)
            sl    = t.get("planned_sl", 0)
            tp1   = t.get("planned_tp1", 0)
            if entry and sl and tp1:
                stop_dist   = abs(entry - sl)
                target_dist = abs(tp1 - entry)
                if stop_dist > 0:
                    rrs.append(target_dist / stop_dist)

        return {
            "total":        len(closed),
            "wins":         len(wins),
            "losses":       len(losses),
            "win_rate":     round(win_rate, 3),
            "avg_rr":       round(sum(rrs) / len(rrs), 2) if rrs else 0.0,
            "total_pnl_pct": round(total_pnl, 3),
            "a_plus_count": len([t for t in closed if t.get("signal_quality") == "A+"]),
        }

    def demo_trades_count(self) -> int:
        return len([t for t in self._trades if t.get("result") is not None])

    def consecutive_losses(self) -> int:
        closed = [t for t in self._trades if t.get("result") is not None]
        count = 0
        for t in reversed(closed):
            if t["result"] == "LOSS":
                count += 1
            else:
                break
        return count

    def last_trade_result(self) -> str:
        closed = [t for t in self._trades if t.get("result") is not None]
        return closed[-1]["result"] if closed else "N/A"

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _find_trade(self, trade_id: str) -> Optional[Dict[str, Any]]:
        for t in self._trades:
            if t["trade_id"] == trade_id:
                return t
        return None

    def _load(self) -> List[Dict[str, Any]]:
        if not self._log_path.exists() or self._log_path.stat().st_size == 0:
            return []
        try:
            with open(self._log_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.warning("trades_log.json is malformed — starting fresh")
            return []

    def _save(self) -> None:
        with open(self._log_path, "w", encoding="utf-8") as f:
            json.dump(self._trades, f, indent=2, default=str)

    @staticmethod
    def _print_post_trade_review(t: Dict[str, Any]) -> None:
        checks = t.get("checks", {})
        review = f"""
=== POST-TRADE REVIEW ===
TRADE_ID:        {t['trade_id']}
ASSET:           BTC/USD
DATE_TIME_OPEN:  {t['date_time_open']}
DATE_TIME_CLOSE: {t['date_time_close']}
STRATEGY:        {t['strategy']}
SIGNAL_QUALITY:  {t['signal_quality']} ({t['signal_score']})
PLANNED_ENTRY:   ${t['planned_entry']:,.2f}
ACTUAL_ENTRY:    ${t['actual_entry']:,.2f}
SLIPPAGE:        ${t['slippage']:+.2f}
PLANNED_SL:      ${t['planned_sl']:,.2f}
ACTUAL_SL:       ${t.get('actual_sl') or t['planned_sl']:,.2f}
PLANNED_TP1:     ${t['planned_tp1']:,.2f}
ACTUAL_EXIT:     ${t['actual_exit']:,.2f}
RESULT:          {t['result']}
PNL_$:           ${t['pnl_dollars']:+.2f}
PNL_%:           {t['pnl_pct']:+.3f}%
HOLD_TIME:       {t['hold_time_min']} minutes
CHECKS:
  macro_aligned:      {_yn(checks.get('macro_aligned'))}
  session_confirmed:  {_yn(checks.get('session_confirmed'))}
  liquidity_swept:    {_yn(checks.get('liquidity_swept'))}
  vwap_confluence:    {_yn(checks.get('vwap_confluence'))}
  order_flow_aligned: {_yn(checks.get('order_flow_aligned'))}
  funding_ok:         {_yn(checks.get('funding_ok'))}
IMPROVEMENT:     {t.get('improvement') or 'N/A'}
=== END REVIEW ===
"""
        logger.info(review)
        print(review)


def _yn(val) -> str:
    if val is None:
        return "?"
    return "Y" if val else "N"
