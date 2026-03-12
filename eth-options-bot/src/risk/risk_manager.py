from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from src.data.models import IronCondor, AccountState
from config.settings import RiskConfig

logger = logging.getLogger(__name__)


class RiskViolation(Exception):
    """Raised when a risk limit is breached."""


class RiskManager:
    """
    Centralized risk enforcement layer.

    All entry/exit decisions MUST be passed through this manager before execution.
    """

    def __init__(self, config: RiskConfig) -> None:
        self.config = config
        self._kill_switch_active = False
        self._daily_pnl: float = 0.0
        self._last_pnl_reset: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Kill switch
    # ------------------------------------------------------------------

    def activate_kill_switch(self, reason: str = "") -> None:
        self._kill_switch_active = True
        logger.critical("KILL SWITCH ACTIVATED: %s", reason)

    def deactivate_kill_switch(self) -> None:
        self._kill_switch_active = False
        logger.info("Kill switch deactivated")

    @property
    def is_halted(self) -> bool:
        return self._kill_switch_active

    # ------------------------------------------------------------------
    # Daily PnL tracking
    # ------------------------------------------------------------------

    def record_pnl(self, pnl: float, as_of: Optional[datetime] = None) -> None:
        if as_of is None:
            as_of = datetime.now(timezone.utc)

        if self._last_pnl_reset is None or as_of.date() != self._last_pnl_reset.date():
            self._daily_pnl = 0.0
            self._last_pnl_reset = as_of

        self._daily_pnl += pnl

        if self._daily_pnl <= -self.config.daily_loss_limit:
            self.activate_kill_switch(
                f"Daily loss limit breached: {self._daily_pnl:.4f} <= -{self.config.daily_loss_limit:.4f}"
            )

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    # ------------------------------------------------------------------
    # Pre-trade checks
    # ------------------------------------------------------------------

    def check_new_trade(
        self,
        condor: IronCondor,
        open_positions: int,
        account: AccountState,
    ) -> None:
        """
        Validate a proposed iron condor trade.
        Raises RiskViolation if any limit is breached.
        """
        if self._kill_switch_active:
            raise RiskViolation("Kill switch is active — trading halted")

        if open_positions >= self.config.max_open_positions:
            raise RiskViolation(
                f"Max open positions reached: {open_positions} >= {self.config.max_open_positions}"
            )

        if condor.max_loss > self.config.max_risk_per_trade:
            raise RiskViolation(
                f"Trade max loss {condor.max_loss:.4f} exceeds limit {self.config.max_risk_per_trade:.4f}"
            )

        # Ensure we have enough equity to cover the loss
        # Use configured account_size as floor — testnet accounts have minimal real equity
        effective_equity = max(account.equity, self.config.account_size)
        if condor.max_loss > effective_equity * 0.5:
            raise RiskViolation(
                f"Trade max loss {condor.max_loss:.4f} > 50% of equity {effective_equity:.2f}"
            )

        logger.info(
            "Risk check passed | max_loss=%.4f | daily_pnl=%.4f | open_pos=%d",
            condor.max_loss,
            self._daily_pnl,
            open_positions,
        )

    def check_api_health(self, consecutive_errors: int, error_threshold: int = 5) -> None:
        """Activate kill switch if too many consecutive API errors."""
        if consecutive_errors >= error_threshold:
            self.activate_kill_switch(
                f"API error threshold reached: {consecutive_errors} consecutive errors"
            )

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------

    def size_position(self, condor_max_loss_per_unit: float, account_equity: float) -> float:
        """
        Return how many contracts to trade given account equity and per-unit max loss.
        Rounds down to nearest integer, minimum 1.
        """
        if condor_max_loss_per_unit <= 0:
            return 1.0
        max_risk = min(
            self.config.max_risk_per_trade,
            account_equity * self.config.max_risk_per_trade_pct,
        )
        contracts = max_risk / condor_max_loss_per_unit
        return max(1.0, float(int(contracts)))

    # ------------------------------------------------------------------
    # Summary report
    # ------------------------------------------------------------------

    def status_report(self) -> dict:
        return {
            "kill_switch_active": self._kill_switch_active,
            "daily_pnl": self._daily_pnl,
            "daily_loss_limit": -self.config.daily_loss_limit,
            "max_risk_per_trade": self.config.max_risk_per_trade,
            "max_open_positions": self.config.max_open_positions,
        }
