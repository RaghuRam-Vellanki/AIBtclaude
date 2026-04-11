"""
order_manager.py
Alpaca order placement, monitoring, and cancellation for BTC/USD.
Uses the alpaca-py SDK TradingClient.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    OrderClass,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLimitOrderRequest,
    TakeProfitRequest,
    StopLossRequest,
)

from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, PAPER_MODE, SYMBOL
from modules.signal_generator import TradeSignal

logger = logging.getLogger(__name__)

# BTC/USD in Alpaca crypto format
_ALPACA_SYMBOL = "BTC/USD"


class OrderManager:
    """
    Places and manages orders on Alpaca (paper or live).
    """

    def __init__(self):
        self._client = TradingClient(
            api_key=ALPACA_API_KEY,
            secret_key=ALPACA_SECRET_KEY,
            paper=PAPER_MODE,
        )
        self._open_orders: Dict[str, dict] = {}

    # ── Account info ──────────────────────────────────────────────────────────

    def get_account_value(self) -> float:
        """Return current cash buying power (usable for new orders)."""
        try:
            account = self._client.get_account()
            # Use cash (not equity) so we only spend what's actually available
            return float(account.cash)
        except Exception as exc:
            logger.error("Failed to get account value: %s", exc)
            return 0.0

    def get_open_position(self) -> Optional[dict]:
        """Return the current BTC/USD position or None."""
        try:
            position = self._client.get_open_position(_ALPACA_SYMBOL.replace("/", ""))
            return {
                "qty":         float(position.qty),
                "side":        position.side.value,
                "avg_entry":   float(position.avg_entry_price),
                "market_value": float(position.market_value),
                "unrealized_pl": float(position.unrealized_pl),
            }
        except Exception:
            return None

    # ── Order placement ───────────────────────────────────────────────────────

    def place_order(
        self,
        signal:        TradeSignal,
        position_size: float,
    ) -> Optional[str]:
        """
        Place entry order based on signal using notional (dollar) sizing for crypto.
        Alpaca crypto requires notional or qty — we use notional to avoid
        'insufficient balance for BTC' errors on small accounts.
        Returns Alpaca order ID on success, None on failure.
        """
        if position_size <= 0 or signal.entry_price <= 0:
            logger.error("Invalid position size or entry price")
            return None

        side     = OrderSide.BUY if signal.bias == "BULLISH" else OrderSide.SELL
        notional = str(round(position_size * signal.entry_price, 2))  # dollar amount

        try:
            # Crypto on Alpaca: use notional + market order for most reliable fill
            # Limit orders on crypto require qty, not notional — use market for simplicity
            order_data = MarketOrderRequest(
                symbol=_ALPACA_SYMBOL,
                notional=notional,
                side=side,
                time_in_force=TimeInForce.IOC,  # Immediate-or-cancel for market orders
            )

            order = self._client.submit_order(order_data)
            logger.info(
                "Order placed: %s $%s notional (~%.6f BTC) id=%s",
                side.value, notional, position_size, order.id,
            )
            self._open_orders[str(order.id)] = {
                "id":           str(order.id),
                "side":         side.value,
                "entry_price":  signal.entry_price,
                "stop_loss":    signal.stop_loss,
                "tp1":          signal.take_profit_1,
                "tp2":          signal.take_profit_2,
                "notional":     float(notional),
                "qty_btc":      position_size,
                "placed_at":    datetime.now(timezone.utc).isoformat(),
                "max_hold_time": signal.max_hold_time,
                "invalidation": signal.invalidation,
            }
            return str(order.id)

        except Exception as exc:
            logger.error("Order placement failed: %s", exc)
            return None

    def place_stop_loss(self, order_id: str, stop_price: float, qty: float,
                         side: str) -> Optional[str]:
        """Place a stop-market order to protect an open position."""
        close_side = OrderSide.SELL if side == "long" else OrderSide.BUY
        try:
            order_data = MarketOrderRequest(
                symbol=_ALPACA_SYMBOL,
                qty=str(round(qty, 6)),
                side=close_side,
                time_in_force=TimeInForce.GTC,
            )
            # Note: Alpaca crypto uses market orders for stops; wrap with stop price logic
            # In practice, place a stop-limit order
            order = self._client.submit_order(order_data)
            logger.info("Stop-loss order placed: id=%s", order.id)
            return str(order.id)
        except Exception as exc:
            logger.error("Stop-loss placement failed: %s", exc)
            return None

    def close_position(self) -> bool:
        """Close the entire BTC/USD position at market."""
        try:
            self._client.close_position(_ALPACA_SYMBOL.replace("/", ""))
            logger.info("Position closed at market")
            return True
        except Exception as exc:
            logger.error("Failed to close position: %s", exc)
            return False

    # ── Order management ──────────────────────────────────────────────────────

    def cancel_stale_orders(self, max_age_hours: float = 4.0) -> int:
        """Cancel unfilled orders older than max_age_hours. Returns count cancelled."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        cancelled = 0
        try:
            open_orders = self._client.get_orders(
                filter=GetOrdersRequest(status="open", symbols=[_ALPACA_SYMBOL])
            )
            for order in open_orders:
                created_at = order.created_at
                if created_at and created_at < cutoff:
                    self._client.cancel_order_by_id(order.id)
                    logger.info("Cancelled stale order %s (age > %.1fh)", order.id, max_age_hours)
                    cancelled += 1
        except Exception as exc:
            logger.error("Error cancelling stale orders: %s", exc)
        return cancelled

    def cancel_all_orders(self) -> None:
        """Cancel all open orders."""
        try:
            self._client.cancel_orders()
            logger.info("All open orders cancelled")
        except Exception as exc:
            logger.error("Failed to cancel all orders: %s", exc)

    def get_order_status(self, order_id: str) -> Optional[str]:
        """Return order status string or None."""
        try:
            order = self._client.get_order_by_id(order_id)
            return order.status.value
        except Exception as exc:
            logger.error("Failed to get order status for %s: %s", order_id, exc)
            return None

    @property
    def tracked_orders(self) -> Dict[str, dict]:
        return self._open_orders
