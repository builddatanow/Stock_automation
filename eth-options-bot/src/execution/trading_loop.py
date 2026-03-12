from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from src.data.ingestion import DataIngestionService
from src.data.models import IronCondor, PositionStatus
from src.data.storage import SQLiteStorage
from src.execution.broker_interface import BrokerInterface
from src.monitoring.logger import TradeJournal
from src.monitoring.notifier import AlertManager
from src.risk.risk_manager import RiskManager, RiskViolation
from src.strategy.weekly_iron_condor import (
    build_condor,
    calculate_risk,
    check_exit_conditions,
    generate_trade_signal,
)
from config.settings import AppConfig

logger = logging.getLogger(__name__)


class TradingLoop:
    """
    Unified trading loop for paper trading and live trading.
    Both modes use the same logic — only the broker differs.
    """

    POLL_INTERVAL_S = 60  # check every 60 seconds

    def __init__(
        self,
        config: AppConfig,
        broker: BrokerInterface,
        ingestion: DataIngestionService,
        db: SQLiteStorage,
        alerts: AlertManager,
        journal: TradeJournal,
        mode: str = "paper",
    ) -> None:
        self.config = config
        self.broker = broker
        self.ingestion = ingestion
        self.db = db
        self.alerts = alerts
        self.journal = journal
        self.mode = mode
        self.risk = RiskManager(config.risk)
        self._open_condor: Optional[IronCondor] = None
        self._consecutive_errors = 0

    def run(self) -> None:
        logger.info("Trading loop started in %s mode", self.mode.upper())
        self.alerts.alert(f"Bot started in *{self.mode.upper()}* mode", level="INFO")

        while True:
            try:
                self._tick()
                self._consecutive_errors = 0
            except KeyboardInterrupt:
                logger.info("Shutdown requested")
                self.alerts.alert("Bot shutting down", level="WARN")
                break
            except Exception as exc:
                self._consecutive_errors += 1
                logger.exception("Error in trading loop: %s", exc)
                self.risk.check_api_health(self._consecutive_errors)
                if self.risk.is_halted:
                    self.alerts.risk_alert(f"Kill switch activated after {self._consecutive_errors} errors")
                    break

            time.sleep(self.POLL_INTERVAL_S)

    def _tick(self) -> None:
        now = datetime.now(timezone.utc)

        if self.risk.is_halted:
            logger.warning("Kill switch active — skipping tick")
            return

        # Fetch current chain
        chain = self.ingestion.fetch_snapshot()
        if not chain:
            logger.warning("Empty option chain snapshot")
            return

        quote_map = {q.instrument_name: q for q in chain}
        underlying_price = self.ingestion.fetch_underlying_price()

        # Update simulated broker quotes (no-op for live broker)
        if hasattr(self.broker, "update_quotes"):
            self.broker.update_quotes(quote_map)
            self.broker.update_underlying(underlying_price)

        account = self.broker.get_account_state()

        # --- Exit check ---
        if self._open_condor and self._open_condor.status == PositionStatus.OPEN:
            exit_reason = check_exit_conditions(
                self._open_condor, quote_map, self.config.strategy, as_of=now
            )
            if exit_reason:
                self.broker.close_condor(self._open_condor, reason=exit_reason)
                pnl = self._open_condor.realized_pnl or 0.0
                self.risk.record_pnl(pnl)
                self.journal.log_exit(self._open_condor)
                self.alerts.trade_closed(self._open_condor)
                from src.data.models import TradeRecord
                self.db.save_trade(TradeRecord(
                    id=self._open_condor.id,
                    entry_time=self._open_condor.entry_time,
                    exit_time=self._open_condor.exit_time,
                    underlying_at_entry=self._open_condor.underlying_price_at_entry,
                    underlying_at_exit=underlying_price,
                    short_call_strike=self._open_condor.short_call.strike,
                    long_call_strike=self._open_condor.long_call.strike,
                    short_put_strike=self._open_condor.short_put.strike,
                    long_put_strike=self._open_condor.long_put.strike,
                    expiry=self._open_condor.short_call.expiry,
                    credit_received=self._open_condor.credit_received,
                    max_loss=self._open_condor.max_loss,
                    realized_pnl=pnl,
                    exit_reason=exit_reason,
                    iv_percentile_at_entry=0.0,
                ))
                self._open_condor = None

        # --- Entry check (Mondays only) ---
        is_monday = now.weekday() == 0
        if not is_monday or self._open_condor is not None:
            return

        try:
            iv_hist = self.ingestion.fetch_iv_history()
            current_iv = float((iv_hist.iloc[-1] if not iv_hist.empty else 50.0))
            iv_pct = self.ingestion.compute_iv_percentile(current_iv)
        except Exception as exc:
            logger.warning("Could not compute IV percentile: %s", exc)
            iv_pct = 50.0

        daily_move_pct = 0.0  # would compute from price history in production

        signal = generate_trade_signal(
            chain=chain,
            config=self.config.strategy,
            iv_percentile=iv_pct,
            daily_move_pct=daily_move_pct,
            has_open_position=(self._open_condor is not None),
        )

        if signal["action"] != "enter":
            logger.info("No entry: %s", signal["reason"])
            return

        condor = build_condor(
            strikes=signal["strikes"],
            quantity=1.0,
            fill_model="mid",
        )

        try:
            self.risk.check_new_trade(condor, len(self.broker.get_open_positions()), account)
        except RiskViolation as e:
            logger.warning("Risk check prevented entry: %s", e)
            self.alerts.risk_alert(str(e))
            return

        orders = self.broker.open_condor(condor)
        self._open_condor = condor
        self.journal.log_entry(condor, iv_pct)
        self.alerts.trade_opened(condor)
        logger.info("Trade opened: %s", condor.id)
