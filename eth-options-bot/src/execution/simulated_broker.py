from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from src.data.models import (
    AccountState,
    IronCondor,
    Leg,
    OptionQuote,
    OptionType,
    Order,
    OrderSide,
    OrderStatus,
    PositionStatus,
)
from src.execution.broker_interface import BrokerInterface
from config.settings import BacktestConfig

logger = logging.getLogger(__name__)


class SimulatedBroker(BrokerInterface):
    """
    Paper trading / backtest broker.

    Fills orders at mid price (or bid/ask) with configurable slippage and fees.
    Maintains an in-memory account ledger.
    """

    def __init__(
        self,
        initial_capital: float,
        fee_per_contract: float = 0.0003,
        slippage_pct: float = 0.001,
        fill_model: str = "mid",
    ) -> None:
        self._capital = initial_capital
        self._equity = initial_capital
        self.fee_per_contract = fee_per_contract
        self.slippage_pct = slippage_pct
        self.fill_model = fill_model

        self._quotes: dict[str, OptionQuote] = {}
        self._underlying_price: float = 0.0
        self._open_condors: list[IronCondor] = []
        self._orders: list[Order] = []
        self._daily_pnl: float = 0.0

    # ------------------------------------------------------------------
    # Feed market data (called by backtest engine / paper trading loop)
    # ------------------------------------------------------------------

    def update_quotes(self, quotes: dict[str, OptionQuote]) -> None:
        self._quotes.update(quotes)

    def update_underlying(self, price: float) -> None:
        self._underlying_price = price

    # ------------------------------------------------------------------
    # BrokerInterface implementation
    # ------------------------------------------------------------------

    def get_account_state(self) -> AccountState:
        open_pnl = sum(
            c.unrealized_pnl(self._quotes)
            for c in self._open_condors
            if c.status == PositionStatus.OPEN
        )
        return AccountState(
            balance=self._capital,
            equity=self._capital + open_pnl,
            open_pnl=open_pnl,
            daily_pnl=self._daily_pnl,
        )

    def get_option_quotes(self, instrument_names: list[str]) -> dict[str, OptionQuote]:
        return {n: self._quotes[n] for n in instrument_names if n in self._quotes}

    def get_underlying_price(self) -> float:
        return self._underlying_price

    def open_condor(self, condor: IronCondor) -> list[Order]:
        orders = []
        for leg in condor.legs:
            order = self._simulate_fill(leg)
            orders.append(order)
            self._orders.append(order)

        # Deduct net premium received (credit is positive, so we add it to capital)
        credit = condor.credit_received
        active_legs = sum(1 for leg in condor.legs if leg.quantity > 0)
        fees = self.fee_per_contract * active_legs * condor.quantity
        self._capital += credit - fees
        self._daily_pnl += credit - fees

        condor.status = PositionStatus.OPEN
        self._open_condors.append(condor)

        logger.info(
            "[SimBroker] Opened condor %s | credit=%.4f | fees=%.4f | legs=%d",
            condor.id, credit, fees, active_legs,
        )
        return orders

    def close_condor(self, condor: IronCondor, reason: str = "", as_of: datetime | None = None) -> list[Order]:
        orders = []
        close_value = 0.0

        for leg in condor.legs:
            # Closing leg = opposite side
            closing_side = OrderSide.BUY if leg.side == OrderSide.SELL else OrderSide.SELL
            quote = self._quotes.get(leg.instrument_name)
            if quote is None:
                logger.warning("No quote for %s — using entry price", leg.instrument_name)
                close_price = leg.entry_price
            else:
                close_price = self._fill_price(quote, closing_side)

            leg.exit_price = close_price

            # PnL contribution: sold at entry_price, buy back at close_price (for short legs)
            if leg.side == OrderSide.SELL:
                close_value -= close_price * leg.quantity
            else:
                close_value += close_price * leg.quantity

            order = Order(
                id=str(uuid.uuid4())[:8],
                instrument_name=leg.instrument_name,
                side=closing_side,
                quantity=leg.quantity,
                price=close_price,
                status=OrderStatus.FILLED,
                filled_quantity=leg.quantity,
                avg_fill_price=close_price,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            orders.append(order)
            self._orders.append(order)

        # close_value is the cost to close (negative = we pay, positive = receive)
        realized_pnl = condor.credit_received + close_value
        active_legs = sum(1 for leg in condor.legs if leg.quantity > 0)
        fees = self.fee_per_contract * active_legs * condor.quantity

        self._capital += close_value - fees
        self._daily_pnl += realized_pnl - fees

        condor.status = PositionStatus.CLOSED
        condor.exit_time = as_of if as_of is not None else datetime.now(timezone.utc)
        condor.exit_reason = reason
        condor.realized_pnl = realized_pnl - fees

        self._open_condors = [c for c in self._open_condors if c.status == PositionStatus.OPEN]

        logger.info(
            "[SimBroker] Closed condor %s | reason=%s | pnl=%.4f",
            condor.id, reason, realized_pnl - fees,
        )
        return orders

    def cancel_order(self, order_id: str) -> bool:
        for order in self._orders:
            if order.id == order_id and order.status == OrderStatus.OPEN:
                order.status = OrderStatus.CANCELLED
                return True
        return False

    def get_open_orders(self) -> list[Order]:
        return [o for o in self._orders if o.status == OrderStatus.OPEN]

    def get_open_positions(self) -> list[IronCondor]:
        return [c for c in self._open_condors if c.status == PositionStatus.OPEN]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _fill_price(self, quote: OptionQuote, side: OrderSide) -> float:
        if self.fill_model == "bid_ask":
            base = quote.bid if side == OrderSide.SELL else quote.ask
        else:
            base = quote.mid

        # Apply slippage (always adverse)
        slip = base * self.slippage_pct
        return base - slip if side == OrderSide.SELL else base + slip

    def _simulate_fill(self, leg: Leg) -> Order:
        quote = self._quotes.get(leg.instrument_name)
        if quote is None:
            fill_price = leg.entry_price
        else:
            fill_price = self._fill_price(quote, leg.side)

        return Order(
            id=str(uuid.uuid4())[:8],
            instrument_name=leg.instrument_name,
            side=leg.side,
            quantity=leg.quantity,
            price=fill_price,
            status=OrderStatus.FILLED,
            filled_quantity=leg.quantity,
            avg_fill_price=fill_price,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

    def reset_daily_pnl(self) -> None:
        self._daily_pnl = 0.0
