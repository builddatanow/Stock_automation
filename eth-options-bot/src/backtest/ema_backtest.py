"""
Backtest engine for EMA-Based Directional Spread strategy.
Uses the same SimulatedBroker and storage layer as the iron condor engine.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd

from src.backtest.engine import compute_metrics
from src.data.models import IronCondor, OptionQuote, OptionType, PositionStatus, TradeRecord
from src.data.storage import ParquetStorage
from src.execution.simulated_broker import SimulatedBroker
from src.risk.risk_manager import RiskManager, RiskViolation
from src.strategy.ema_spread import (
    EMASpreadConfig,
    build_spread,
    check_exit_conditions,
    generate_trade_signal,
    get_ema_signal,
)
from src.strategy.weekly_iron_condor import (
    select_strikes as ic_select_strikes,
    build_condor as ic_build_condor,
    check_exit_conditions as ic_check_exit_conditions,
)
from config.settings import StrategyConfig as ICStrategyConfig

logger = logging.getLogger(__name__)


class EMASpreadBacktest:
    def __init__(
        self,
        config: EMASpreadConfig,
        parquet_storage: ParquetStorage,
        start_date: str,
        end_date: str,
        initial_capital: float = 2200.0,
        fee_per_contract: float = 0.0003,
        slippage_pct: float = 0.001,
    ) -> None:
        self.config = config
        self.storage = parquet_storage
        self.start_date = start_date
        self.end_date   = end_date

        self.broker = SimulatedBroker(
            initial_capital=initial_capital,
            fee_per_contract=fee_per_contract,
            slippage_pct=slippage_pct,
            fill_model="mid",
        )
        from config.settings import RiskConfig
        risk_cfg = RiskConfig(
            account_size=initial_capital,
            max_risk_per_trade_pct=config.max_risk_per_trade_pct,
            max_open_positions=1,
            daily_loss_limit_pct=0.10,
        )
        self.risk = RiskManager(risk_cfg)
        self._trade_records: list[TradeRecord] = []
        self._equity_series: list[tuple[datetime, float]] = []
        self._signal_log: list[dict] = []

    # ------------------------------------------------------------------
    def run(self) -> dict:
        logger.info("EMA Spread backtest: %s -> %s", self.start_date, self.end_date)

        df_all = self.storage.load_quotes(
            start_date=self.start_date, end_date=self.end_date
        )
        if df_all.empty:
            logger.error("No historical data found.")
            return {}

        df_all["date"] = df_all["timestamp"].dt.date
        dates = sorted(df_all["date"].unique())

        # IC fallback config (used when EMA signal is neutral or IV too low)
        ic_strat_cfg = ICStrategyConfig(
            target_dte_min=self.config.target_dte_min,
            target_dte_max=self.config.target_dte_max,
            short_delta_min=self.config.ic_short_delta_min,
            short_delta_max=self.config.ic_short_delta_max,
            wing_delta_min=self.config.ic_wing_delta_min,
            wing_delta_max=self.config.ic_wing_delta_max,
            take_profit_pct=self.config.take_profit_pct,
            stop_loss_multiplier=self.config.stop_loss_multiplier,
            close_dte=self.config.close_dte,
            iv_percentile_min=0.0,
            max_daily_move_pct=100.0,
        )

        open_spread: Optional[IronCondor] = None
        price_history: list[float] = []
        iv_window: list[float] = []

        for date in dates:
            df_day = df_all[df_all["date"] == date]
            quotes = self._df_to_quotes(df_day)
            if not quotes:
                continue

            quote_map = {q.instrument_name: q for q in quotes}
            spot = quotes[0].underlying_price
            price_history.append(spot)

            avg_iv = float(np.mean([q.implied_volatility for q in quotes if q.implied_volatility > 0]))
            iv_window.append(avg_iv)
            iv_pct = self._iv_percentile(iv_window)

            self.broker.update_quotes(quote_map)
            self.broker.update_underlying(spot)

            dt = datetime.combine(date, datetime.min.time()).replace(tzinfo=timezone.utc)
            current_signal = get_ema_signal(price_history, self.config.fast_ema, self.config.slow_ema)

            # --- Exit check ---
            if open_spread and open_spread.status == PositionStatus.OPEN:
                spread_type = open_spread.__dict__.get("spread_type", "bull_put")
                if spread_type == "iron_condor":
                    exit_reason = ic_check_exit_conditions(
                        open_spread, quote_map, ic_strat_cfg, as_of=dt,
                    )
                else:
                    exit_reason = check_exit_conditions(
                        open_spread, quote_map, self.config,
                        as_of=dt, current_signal=current_signal,
                    )
                if exit_reason:
                    self.broker.close_condor(open_spread, reason=exit_reason, as_of=dt)
                    self.risk.record_pnl(open_spread.realized_pnl or 0.0)
                    self._trade_records.append(self._to_record(open_spread, iv_pct, current_signal))
                    logger.info("Closed spread %s | %s | pnl=%.5f",
                                open_spread.id, exit_reason, open_spread.realized_pnl)
                    open_spread = None

            # --- Entry: Mondays only (or every weekday if entry_every_day=True) ---
            is_entry_day = (dt.weekday() == 0) or (self.config.entry_every_day and dt.weekday() < 5)
            if is_entry_day and open_spread is None:
                signal_result = generate_trade_signal(
                    chain=quotes,
                    config=self.config,
                    price_history=price_history,
                    iv_percentile=iv_pct,
                    has_open_position=False,
                    current_signal=current_signal,
                )
                self._signal_log.append({
                    "date": str(date),
                    "spot": spot,
                    "signal": signal_result["signal"],
                    "action": signal_result["action"],
                    "reason": signal_result["reason"],
                })

                if signal_result["action"] == "enter":
                    spread = build_spread(
                        strikes=signal_result["strikes"],
                        quantity=1.0,
                        fill_model="mid",
                    )
                    try:
                        account = self.broker.get_account_state()
                        self.risk.check_new_trade(spread, 0, account)
                        self.broker.open_condor(spread)
                        open_spread = spread
                        logger.info(
                            "Opened %s | credit=%.5f | max_loss=%.2f | signal=%s",
                            spread.__dict__.get("spread_type", "spread"),
                            spread.credit_received, spread.max_loss,
                            signal_result["signal"],
                        )
                    except RiskViolation as e:
                        logger.warning("Risk check prevented entry: %s", e)
                elif self.config.condor_on_low_iv:
                    # Fallback: open iron condor when EMA signal is skipped
                    ic_strikes = ic_select_strikes(quotes, ic_strat_cfg, as_of=dt)
                    if ic_strikes:
                        condor = ic_build_condor(ic_strikes, quantity=1.0, fill_model="mid")
                        condor.__dict__["spread_type"] = "iron_condor"
                        try:
                            account = self.broker.get_account_state()
                            self.risk.check_new_trade(condor, 0, account)
                            self.broker.open_condor(condor)
                            open_spread = condor
                            logger.info(
                                "Opened iron_condor (IC fallback) | credit=%.5f | max_loss=%.2f | reason=%s",
                                condor.credit_received, condor.max_loss,
                                signal_result["reason"],
                            )
                        except RiskViolation as e:
                            logger.warning("IC risk check prevented entry: %s", e)
                    else:
                        logger.info("Skip %s (IC fallback): no suitable IC strikes", date)
                else:
                    logger.info("Skip %s: %s", date, signal_result["reason"])

            # Same-day close: needed for 0 DTE (dte<=close_dte at moment of entry)
            if open_spread and open_spread.status == PositionStatus.OPEN:
                stype = open_spread.__dict__.get("spread_type", "")
                if stype == "iron_condor":
                    same_day_exit = ic_check_exit_conditions(open_spread, quote_map, ic_strat_cfg, as_of=dt)
                else:
                    same_day_exit = check_exit_conditions(open_spread, quote_map, self.config, as_of=dt, current_signal=current_signal)
                if same_day_exit:
                    self.broker.close_condor(open_spread, reason=same_day_exit, as_of=dt)
                    self.risk.record_pnl(open_spread.realized_pnl or 0.0)
                    self._trade_records.append(self._to_record(open_spread, iv_pct, current_signal))
                    logger.info("Same-day close %s | %s | pnl=%.5f", open_spread.id, same_day_exit, open_spread.realized_pnl)
                    open_spread = None

            account = self.broker.get_account_state()
            self._equity_series.append((dt, account.equity))
            self.broker.reset_daily_pnl()

        # Close any open position at end
        if open_spread and open_spread.status == PositionStatus.OPEN:
            self.broker.close_condor(open_spread, reason="backtest_end", as_of=dt)
            self._trade_records.append(self._to_record(open_spread, 50.0, "end"))

        return self._compile()

    # ------------------------------------------------------------------
    def _compile(self) -> dict:
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
            "signal_log": self._signal_log,
        }

    def print_summary(self, results: dict) -> None:
        m = results.get("metrics", {})
        print("\n" + "=" * 60)
        print("  EMA SPREAD STRATEGY  --  BACKTEST RESULTS")
        print("=" * 60)
        print(f"  Total trades        : {m.get('total_trades', 0)}")
        print(f"  Win rate            : {m.get('win_rate_pct', 0):.1f}%")
        print(f"  Total PnL (ETH)     : {m.get('total_pnl', 0):+.5f}")
        print(f"  Avg trade PnL (ETH) : {m.get('avg_trade_pnl', 0):+.5f}")
        print(f"  Total return        : {m.get('total_return_pct', 0):+.2f}%")
        print(f"  CAGR                : {m.get('cagr_pct', 0):+.2f}%")
        print(f"  Max drawdown        : {m.get('max_drawdown_pct', 0):.2f}%")
        print(f"  Sharpe ratio        : {m.get('sharpe_ratio', 0):.2f}")
        print(f"  Tail loss (5th %)   : {m.get('tail_loss_5pct', 0):.5f} ETH")
        print("=" * 60)

    # ------------------------------------------------------------------
    def _df_to_quotes(self, df: pd.DataFrame) -> list[OptionQuote]:
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
                    gamma=float(row.get("gamma", 0)),
                    theta=float(row.get("theta", 0)),
                    vega=float(row.get("vega", 0)),
                    underlying_price=float(row["underlying_price"]),
                )
                quotes.append(q)
            except Exception:
                pass
        return quotes

    def _iv_percentile(self, iv_window: list[float]) -> float:
        if len(iv_window) < 2:
            return 50.0
        current = iv_window[-1]
        return float(sum(1 for v in iv_window[:-1] if v < current) / len(iv_window[:-1]) * 100)

    def _to_record(self, spread: IronCondor, iv_pct: float, signal: str) -> TradeRecord:
        spread_type = spread.__dict__.get("spread_type", "")
        if spread_type == "bull_put":
            sc_strike = 0.0
            lc_strike = 0.0
            sp_strike = spread.short_put.strike
            lp_strike = spread.long_put.strike
            expiry    = spread.short_put.expiry
        elif spread_type == "iron_condor":
            sc_strike = spread.short_call.strike
            lc_strike = spread.long_call.strike
            sp_strike = spread.short_put.strike
            lp_strike = spread.long_put.strike
            expiry    = spread.short_call.expiry
        else:  # bear_call
            sc_strike = spread.short_call.strike
            lc_strike = spread.long_call.strike
            sp_strike = 0.0
            lp_strike = 0.0
            expiry    = spread.short_call.expiry

        return TradeRecord(
            id=spread.id,
            entry_time=spread.entry_time,
            exit_time=spread.exit_time,
            underlying_at_entry=spread.underlying_price_at_entry,
            underlying_at_exit=None,
            short_call_strike=sc_strike,
            long_call_strike=lc_strike,
            short_put_strike=sp_strike,
            long_put_strike=lp_strike,
            expiry=expiry,
            credit_received=spread.credit_received,
            max_loss=spread.max_loss,
            realized_pnl=spread.realized_pnl,
            exit_reason=spread.exit_reason,
            iv_percentile_at_entry=iv_pct,
        )
