from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from src.data.models import (
    AccountState,
    IronCondor,
    OptionQuote,
    Order,
    OrderSide,
    OrderStatus,
    PositionStatus,
)
from src.data.ingestion import parse_option_quote
from src.deribit.rest_client import DeribitRESTClient
from src.execution.broker_interface import BrokerInterface
from config.settings import ExecutionConfig

logger = logging.getLogger(__name__)


class DeribitBroker(BrokerInterface):
    """
    Live trading broker backed by the Deribit REST API.

    Implements retry logic, partial fill handling, and cancel/replace.
    """

    def __init__(
        self,
        client: DeribitRESTClient,
        config: ExecutionConfig,
        currency: str = "ETH",
    ) -> None:
        self.client = client
        self.config = config
        self.currency = currency

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def get_account_state(self) -> AccountState:
        summary = self.client.get_account_summary(self.currency)
        return AccountState(
            balance=summary.get("balance", 0.0),
            equity=summary.get("equity", 0.0),
            open_pnl=summary.get("session_upl", 0.0),
            daily_pnl=summary.get("session_rpl", 0.0),
            margin_used=summary.get("initial_margin", 0.0),
        )

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def get_option_quotes(self, instrument_names: list[str]) -> dict[str, OptionQuote]:
        quotes: dict[str, OptionQuote] = {}
        for name in instrument_names:
            try:
                raw = self.client.get_ticker(name)
                raw["instrument_name"] = name
                # Enrich with static instrument data
                q = parse_option_quote(raw)
                if q:
                    quotes[name] = q
            except Exception as exc:
                logger.warning("Failed to get quote for %s: %s", name, exc)
        return quotes

    def get_underlying_price(self) -> float:
        return self.client.get_index_price(f"{self.currency.lower()}_usd")

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    def open_condor(self, condor: IronCondor) -> list[Order]:
        orders: list[Order] = []
        # Refresh quotes so we use live bid/ask at fill time
        active_names = [leg.instrument_name for leg in condor.legs
                        if leg.quantity > 0 and leg.instrument_name != "STUB"]
        live_quotes = self.get_option_quotes(active_names)

        for leg in condor.legs:
            if leg.quantity == 0 or leg.instrument_name == "STUB":
                continue
            side = "sell" if leg.side == OrderSide.SELL else "buy"
            # Use bid for sells, ask for buys — guarantees fills on testnet
            q = live_quotes.get(leg.instrument_name)
            if q:
                price = q.bid if leg.side == OrderSide.SELL else q.ask
            else:
                price = leg.entry_price
            order = self._place_with_retry(
                instrument_name=leg.instrument_name,
                side=side,
                amount=leg.quantity,
                price=price,
                label=f"condor_{condor.id}",
            )
            orders.append(order)
        return orders

    def close_condor(self, condor: IronCondor, reason: str = "") -> list[Order]:
        orders: list[Order] = []
        active_names = [leg.instrument_name for leg in condor.legs
                        if leg.quantity > 0 and leg.instrument_name != "STUB"]
        quotes = self.get_option_quotes(active_names)

        close_value = 0.0
        for leg in condor.legs:
            if leg.quantity == 0 or leg.instrument_name == "STUB":
                continue  # skip placeholder legs
            closing_side = "buy" if leg.side == OrderSide.SELL else "sell"
            quote = quotes.get(leg.instrument_name)
            close_price = quote.mid if quote else leg.entry_price

            # Track close value: selling back our longs (+), buying back our shorts (-)
            if leg.side == OrderSide.SELL:
                close_value -= close_price * leg.quantity
            else:
                close_value += close_price * leg.quantity

            order = self._place_with_retry(
                instrument_name=leg.instrument_name,
                side=closing_side,
                amount=leg.quantity,
                price=close_price,
                label=f"close_{condor.id}_{reason[:10]}",
            )
            orders.append(order)

        # realized_pnl = credit received at open + close_value (negative = paid to close)
        condor.realized_pnl = condor.credit_received + close_value
        condor.status = PositionStatus.CLOSED
        condor.exit_time = datetime.now(timezone.utc)
        condor.exit_reason = reason
        return orders

    def cancel_order(self, order_id: str) -> bool:
        try:
            self.client.cancel_order(order_id)
            return True
        except Exception as exc:
            logger.error("Failed to cancel order %s: %s", order_id, exc)
            return False

    def get_open_orders(self) -> list[Order]:
        raw_orders = self.client.get_open_orders(self.currency)
        return [self._parse_order(o) for o in raw_orders]

    def get_open_positions(self) -> list[IronCondor]:
        """
        NOTE: Reconstructing iron condors from raw Deribit positions requires
        matching positions by label. This returns an empty list by default —
        in production, maintain an in-memory condor registry and cross-reference.
        """
        logger.warning("get_open_positions: condor reconstruction not implemented for live broker")
        return []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _place_with_retry(
        self,
        instrument_name: str,
        side: str,
        amount: float,
        price: float,
        label: str = "",
    ) -> Order:
        last_error: Optional[Exception] = None

        for attempt in range(self.config.order_retry_attempts):
            try:
                result = self.client.place_order(
                    instrument_name=instrument_name,
                    side=side,
                    amount=amount,
                    price=price,
                    label=label,
                )
                order_data = result.get("order", result)
                order = Order(
                    id=str(uuid.uuid4())[:8],
                    instrument_name=instrument_name,
                    side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                    quantity=amount,
                    price=price,
                    broker_order_id=order_data.get("order_id"),
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )

                # Poll for fill
                filled_order = self._wait_for_fill(order)
                return filled_order

            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Order attempt %d/%d failed for %s: %s",
                    attempt + 1,
                    self.config.order_retry_attempts,
                    instrument_name,
                    exc,
                )
                if attempt < self.config.order_retry_attempts - 1:
                    time.sleep(self.config.order_retry_delay_s)

        # Return a rejected order if all retries failed
        return Order(
            id=str(uuid.uuid4())[:8],
            instrument_name=instrument_name,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            quantity=amount,
            price=price,
            status=OrderStatus.REJECTED,
            error_message=str(last_error),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

    def _wait_for_fill(self, order: Order, poll_interval: float = 1.0) -> Order:
        if not order.broker_order_id:
            order.status = OrderStatus.REJECTED
            return order

        deadline = time.time() + self.config.order_timeout_s

        while time.time() < deadline:
            try:
                state = self.client.get_order_state(order.broker_order_id)
                status = state.get("order_state", "open")
                filled_qty = float(state.get("filled_amount", 0))
                avg_price = float(state.get("average_price", order.price))

                order.filled_quantity = filled_qty
                order.avg_fill_price = avg_price
                order.updated_at = datetime.now(timezone.utc)

                if status == "filled":
                    order.status = OrderStatus.FILLED
                    logger.info("Order %s filled at %.4f", order.broker_order_id, avg_price)
                    return order

                if status in ("cancelled", "rejected"):
                    order.status = OrderStatus.CANCELLED
                    return order

                # Partial fill: update and continue waiting
                if filled_qty > 0:
                    order.status = OrderStatus.PARTIALLY_FILLED

            except Exception as exc:
                logger.error("Error polling order state: %s", exc)

            time.sleep(poll_interval)

        # Timeout — cancel the order
        logger.warning("Order %s timed out — cancelling", order.broker_order_id)
        self.cancel_order(order.broker_order_id)
        order.status = OrderStatus.CANCELLED
        return order

    def _parse_order(self, raw: dict) -> Order:
        side_str = raw.get("direction", "buy")
        return Order(
            id=str(uuid.uuid4())[:8],
            instrument_name=raw.get("instrument_name", ""),
            side=OrderSide.BUY if side_str == "buy" else OrderSide.SELL,
            quantity=float(raw.get("amount", 0)),
            price=float(raw.get("price", 0)),
            status=OrderStatus.OPEN,
            filled_quantity=float(raw.get("filled_amount", 0)),
            avg_fill_price=float(raw.get("average_price", 0)),
            broker_order_id=raw.get("order_id"),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
