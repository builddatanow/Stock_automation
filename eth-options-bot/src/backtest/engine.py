from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd

from src.data.models import (
    IronCondor,
    OptionQuote,
    OptionType,
    PositionStatus,
    TradeRecord,
)
from src.data.storage import ParquetStorage, SQLiteStorage
from src.execution.simulated_broker import SimulatedBroker
from src.risk.risk_manager import RiskManager, RiskViolation
from src.strategy.weekly_iron_condor import (
    build_condor,
    calculate_risk,
    check_exit_conditions,
    generate_trade_signal,
    select_strikes,
)
from config.settings import AppConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Performance metrics
# ---------------------------------------------------------------------------

def compute_metrics(equity_curve: pd.Series, trade_records: list[TradeRecord]) -> dict:
    if equity_curve.empty or len(equity_curve) < 2:
        return {}

    returns = equity_curve.pct_change().dropna()
    total_return = (equity_curve.iloc[-1] / equity_curve.iloc[0]) - 1

    # CAGR
    n_years = len(equity_curve) / 252
    cagr = (1 + total_return) ** (1 / max(n_years, 0.01)) - 1

    # Max drawdown
    rolling_max = equity_curve.cummax()
    drawdown = (equity_curve - rolling_max) / rolling_max
    max_dd = float(drawdown.min())

    # Sharpe (annualised, risk-free ~ 0)
    sharpe = float(returns.mean() / returns.std() * np.sqrt(252)) if returns.std() > 0 else 0.0

    pnls = [t.realized_pnl for t in trade_records if t.realized_pnl is not None]
    win_rate = sum(1 for p in pnls if p > 0) / len(pnls) if pnls else 0.0
    avg_trade = float(np.mean(pnls)) if pnls else 0.0
    tail_loss = float(np.percentile(pnls, 5)) if pnls else 0.0

    return {
        "total_return_pct": total_return * 100,
        "cagr_pct": cagr * 100,
        "max_drawdown_pct": max_dd * 100,
        "sharpe_ratio": sharpe,
        "win_rate_pct": win_rate * 100,
        "avg_trade_pnl": avg_trade,
        "tail_loss_5pct": tail_loss,
        "total_trades": len(pnls),
        "total_pnl": sum(pnls),
    }


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------

class BacktestEngine:
    """
    Replays historical option chain snapshots and simulates
    weekly iron condor entries and exits.
    """

    def __init__(self, config: AppConfig, parquet_storage: ParquetStorage) -> None:
        self.config = config
        self.storage = parquet_storage
        self.broker = SimulatedBroker(
            initial_capital=config.backtest.initial_capital,
            fee_per_contract=config.backtest.fee_per_contract,
            slippage_pct=config.backtest.slippage_pct,
            fill_model=config.backtest.fill_model,
        )
        self.risk = RiskManager(config.risk)
        self._trade_records: list[TradeRecord] = []
        self._equity_series: list[tuple[datetime, float]] = []

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> dict:
        logger.info(
            "Starting backtest from %s to %s",
            self.config.backtest.start_date,
            self.config.backtest.end_date,
        )

        df_all = self.storage.load_quotes(
            start_date=self.config.backtest.start_date,
            end_date=self.config.backtest.end_date,
        )

        if df_all.empty:
            logger.error("No historical data found in storage.")
            return {}

        # Group by date (using UTC date)
        df_all["date"] = df_all["timestamp"].dt.date
        dates = sorted(df_all["date"].unique())

        open_condor: Optional[IronCondor] = None
        iv_window: list[float] = []

        for date in dates:
            df_day = df_all[df_all["date"] == date]
            quotes = self._df_to_quotes(df_day)
            quote_map = {q.instrument_name: q for q in quotes}

            if not quotes:
                continue

            underlying_price = quotes[0].underlying_price
            self.broker.update_quotes(quote_map)
            self.broker.update_underlying(underlying_price)

            # Track IV for percentile
            avg_iv = float(np.mean([q.implied_volatility for q in quotes if q.implied_volatility > 0]))
            iv_window.append(avg_iv)
            iv_pct = self._iv_percentile(iv_window)

            # Check exit
            if open_condor and open_condor.status == PositionStatus.OPEN:
                exit_reason = check_exit_conditions(
                    open_condor,
                    quote_map,
                    self.config.strategy,
                    as_of=datetime.combine(date, datetime.min.time()).replace(tzinfo=timezone.utc),
                )
                if exit_reason:
                    self.broker.close_condor(open_condor, reason=exit_reason)
                    self.risk.record_pnl(open_condor.realized_pnl or 0.0)
                    self._trade_records.append(self._to_trade_record(open_condor, iv_pct))
                    logger.info("Closed condor %s | %s | pnl=%.4f", open_condor.id, exit_reason, open_condor.realized_pnl)
                    open_condor = None

            # Entry on Mondays only
            dt = datetime.combine(date, datetime.min.time()).replace(tzinfo=timezone.utc)
            is_monday = dt.weekday() == 0

            if is_monday and open_condor is None:
                daily_move_pct = self._daily_move(df_all, date)
                signal = generate_trade_signal(
                    chain=quotes,
                    config=self.config.strategy,
                    iv_percentile=iv_pct,
                    daily_move_pct=daily_move_pct,
                    has_open_position=(open_condor is not None),
                )

                if signal["action"] == "enter":
                    condor = build_condor(
                        strikes=signal["strikes"],
                        quantity=1.0,
                        fill_model=self.config.backtest.fill_model,
                    )
                    risk_info = calculate_risk(condor, self.config.risk.account_size)

                    try:
                        account = self.broker.get_account_state()
                        self.risk.check_new_trade(condor, len(self.broker.get_open_positions()), account)
                        self.broker.open_condor(condor)
                        open_condor = condor
                        logger.info(
                            "Opened condor %s | SC=%.0f LC=%.0f SP=%.0f LP=%.0f | credit=%.4f | max_loss=%.4f",
                            condor.id,
                            condor.short_call.strike, condor.long_call.strike,
                            condor.short_put.strike, condor.long_put.strike,
                            condor.credit_received, condor.max_loss,
                        )
                    except RiskViolation as e:
                        logger.warning("Risk check failed: %s", e)
                else:
                    logger.info("Skipping entry on %s: %s", date, signal["reason"])

            # Record equity
            account = self.broker.get_account_state()
            self._equity_series.append((dt, account.equity))
            self.broker.reset_daily_pnl()

        # Close any remaining open position at end
        if open_condor and open_condor.status == PositionStatus.OPEN:
            self.broker.close_condor(open_condor, reason="backtest_end")
            self._trade_records.append(self._to_trade_record(open_condor, iv_pct))

        return self._compile_results()

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    def _compile_results(self) -> dict:
        equity_curve = pd.Series(
            [v for _, v in self._equity_series],
            index=pd.DatetimeIndex([d for d, _ in self._equity_series], tz="UTC"),
            name="equity",
        )
        metrics = compute_metrics(equity_curve, self._trade_records)

        return {
            "metrics": metrics,
            "equity_curve": equity_curve,
            "trades": self._trade_records,
        }

    def print_summary(self, results: dict) -> None:
        m = results.get("metrics", {})
        print("\n" + "=" * 55)
        print("  BACKTEST RESULTS — Weekly ETH Iron Condor")
        print("=" * 55)
        print(f"  Total trades      : {m.get('total_trades', 0)}")
        print(f"  Win rate          : {m.get('win_rate_pct', 0):.1f}%")
        print(f"  Total PnL (ETH)   : {m.get('total_pnl', 0):.4f}")
        print(f"  Avg trade PnL     : {m.get('avg_trade_pnl', 0):.4f}")
        print(f"  Total return      : {m.get('total_return_pct', 0):.1f}%")
        print(f"  CAGR              : {m.get('cagr_pct', 0):.1f}%")
        print(f"  Max drawdown      : {m.get('max_drawdown_pct', 0):.1f}%")
        print(f"  Sharpe ratio      : {m.get('sharpe_ratio', 0):.2f}")
        print(f"  Tail loss (5th %) : {m.get('tail_loss_5pct', 0):.4f}")
        print("=" * 55 + "\n")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _df_to_quotes(self, df: pd.DataFrame) -> list[OptionQuote]:
        from src.data.ingestion import parse_option_quote
        quotes = []
        for _, row in df.iterrows():
            try:
                q = OptionQuote(
                    timestamp=row["timestamp"].to_pydatetime(),
                    instrument_name=row["instrument_name"],
                    strike=float(row["strike"]),
                    expiry=row["expiry"].to_pydatetime(),
                    option_type=OptionType(row["option_type"]),
                    bid=float(row["bid"]),
                    ask=float(row["ask"]),
                    mark_price=float(row["mark_price"]),
                    implied_volatility=float(row["implied_volatility"]),
                    delta=float(row["delta"]),
                    gamma=float(row["gamma"]),
                    theta=float(row["theta"]),
                    vega=float(row["vega"]),
                    underlying_price=float(row["underlying_price"]),
                    open_interest=float(row.get("open_interest", 0)),
                    volume=float(row.get("volume", 0)),
                )
                quotes.append(q)
            except Exception as exc:
                pass
        return quotes

    def _iv_percentile(self, iv_window: list[float]) -> float:
        if len(iv_window) < 2:
            return 50.0
        current = iv_window[-1]
        return float(sum(1 for v in iv_window[:-1] if v < current) / len(iv_window[:-1]) * 100)

    def _daily_move(self, df_all: pd.DataFrame, date) -> float:
        dates = sorted(df_all["date"].unique())
        idx = dates.index(date)
        if idx == 0:
            return 0.0
        prev_date = dates[idx - 1]
        prev_px = df_all[df_all["date"] == prev_date]["underlying_price"].mean()
        curr_px = df_all[df_all["date"] == date]["underlying_price"].mean()
        if prev_px == 0:
            return 0.0
        return (curr_px - prev_px) / prev_px * 100

    def _to_trade_record(self, condor: IronCondor, iv_pct: float) -> TradeRecord:
        return TradeRecord(
            id=condor.id,
            entry_time=condor.entry_time,
            exit_time=condor.exit_time,
            underlying_at_entry=condor.underlying_price_at_entry,
            underlying_at_exit=None,
            short_call_strike=condor.short_call.strike,
            long_call_strike=condor.long_call.strike,
            short_put_strike=condor.short_put.strike,
            long_put_strike=condor.long_put.strike,
            expiry=condor.short_call.expiry,
            credit_received=condor.credit_received,
            max_loss=condor.max_loss,
            realized_pnl=condor.realized_pnl,
            exit_reason=condor.exit_reason,
            iv_percentile_at_entry=iv_pct,
        )
