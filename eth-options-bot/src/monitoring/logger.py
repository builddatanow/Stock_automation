from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path


def setup_logging(log_level: str = "INFO", log_file: str = "logs/trading.log") -> None:
    """Configure root logger with console + rotating file handlers."""
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, log_level.upper(), logging.INFO)
    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    root = logging.getLogger()
    root.setLevel(level)

    # Console
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(logging.Formatter(fmt, datefmt))
    root.addHandler(console)

    # Rotating file
    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(fmt, datefmt))
    root.addHandler(file_handler)

    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


class TradeJournal:
    """
    Structured trade journal that writes CSV-formatted trade logs.
    """

    def __init__(self, journal_path: str = "logs/trade_journal.csv") -> None:
        self.path = Path(journal_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._logger = logging.getLogger("TradeJournal")
        self._init_file()

    def _init_file(self) -> None:
        if not self.path.exists():
            with open(self.path, "w") as f:
                f.write(
                    "timestamp,event,condor_id,short_call,long_call,short_put,long_put,"
                    "expiry,credit,max_loss,pnl,reason\n"
                )

    def log_entry(self, condor, iv_pct: float = 0.0) -> None:
        from src.data.models import IronCondor
        row = (
            f"{condor.entry_time.isoformat()},ENTRY,{condor.id},"
            f"{condor.short_call.strike},{condor.long_call.strike},"
            f"{condor.short_put.strike},{condor.long_put.strike},"
            f"{condor.short_call.expiry.date()},"
            f"{condor.credit_received:.6f},{condor.max_loss:.6f},,"
        )
        with open(self.path, "a") as f:
            f.write(row + "\n")
        self._logger.info("ENTRY logged for condor %s", condor.id)

    def log_exit(self, condor) -> None:
        pnl = condor.realized_pnl or ""
        row = (
            f"{(condor.exit_time or condor.entry_time).isoformat()},EXIT,{condor.id},"
            f"{condor.short_call.strike},{condor.long_call.strike},"
            f"{condor.short_put.strike},{condor.long_put.strike},"
            f"{condor.short_call.expiry.date()},"
            f"{condor.credit_received:.6f},{condor.max_loss:.6f},{pnl},{condor.exit_reason}"
        )
        with open(self.path, "a") as f:
            f.write(row + "\n")
        self._logger.info("EXIT logged for condor %s | pnl=%s", condor.id, pnl)

    def log_daily_pnl(self, date: str, pnl: float, equity: float) -> None:
        self._logger.info("DAILY PnL | date=%s | pnl=%.4f | equity=%.2f", date, pnl, equity)
