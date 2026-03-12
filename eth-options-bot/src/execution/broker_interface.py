from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from src.data.models import AccountState, IronCondor, Order, OptionQuote


class BrokerInterface(ABC):
    """
    Abstract broker interface.

    All broker implementations (simulated and live) must implement this interface,
    ensuring the strategy code never depends on a concrete broker.
    """

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    @abstractmethod
    def get_account_state(self) -> AccountState:
        """Return current account balance, equity, and PnL."""

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    @abstractmethod
    def get_option_quotes(self, instrument_names: list[str]) -> dict[str, OptionQuote]:
        """Return current quotes for the given instruments."""

    @abstractmethod
    def get_underlying_price(self) -> float:
        """Return current ETH spot price."""

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    @abstractmethod
    def open_condor(self, condor: IronCondor) -> list[Order]:
        """
        Submit all four legs of the iron condor.
        Returns list of Order objects (one per leg).
        """

    @abstractmethod
    def close_condor(self, condor: IronCondor, reason: str = "") -> list[Order]:
        """
        Close all open legs by submitting offsetting orders.
        Returns list of closing Order objects.
        """

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Cancel a single open order. Returns True if successful."""

    @abstractmethod
    def get_open_orders(self) -> list[Order]:
        """Return all currently open orders."""

    @abstractmethod
    def get_open_positions(self) -> list[IronCondor]:
        """Return reconstructed open iron condor positions."""
