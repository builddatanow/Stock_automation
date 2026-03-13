# region imports
from AlgorithmImports import *
from collections import deque
from datetime import timedelta
import math
# endregion

# ============================================================
# SPY Core + SPX LEAPS Overlay
# 90% SPY buy-and-hold core + 10% SPX LEAPS call sleeve
#
# Structure:
#   - 90% of portfolio always in SPY (rebalanced daily ±3% drift)
#   - Up to 10% of portfolio in SPX LEAPS calls
#   - Base LEAPS trade: two-stage exit (+100% sell half, +150% sell rest)
#   - Extra crash trade: enters when SPX -25% from peak, exits at +200%
#
# Compare vs:
#   - SPY buy-and-hold (benchmark)
#   - spx_leaps_qc_case1_baseline.py (pure LEAPS, 21.37% CAGR)
# Period: 2012-01-01 to 2026-02-28 | Start: $100,000
# ============================================================

class SPYCorePlusSPXLeapsOverlay(QCAlgorithm):

    # Core + overlay weights
    CORE_SPY_WEIGHT = 0.90
    LEAPS_SLEEVE_MAX = 0.10

    # Entry parameters
    VIX_THRESHOLD = 20.0
    CALL_DELTA_TGT = 0.40
    CALL_DTE = 300
    PUT_DTE = 90
    PUT_MODE = "none"   # "none" | "conditional" | "always"
    PUT_COST_FRAC = 0.10

    # Base trade exits
    FIRST_PROFIT_TARGET = 1.00   # +100%, sell half
    SECOND_PROFIT_TARGET = 1.50  # +150%, sell rest

    # Extra crash trade
    EXTRA_DRAWDOWN_TRIGGER = 0.25
    EXTRA_PROFIT_TARGET = 2.00

    # Crash exits
    CRASH_RULES = {
        7:  -0.03,
        10: -0.04,
        14: -0.06,
        30: -0.08,
    }

    RF_RATE = 0.045
    COOLDOWN_DAYS = 5

    def Initialize(self):
        self.SetStartDate(2012, 1, 1)
        self.SetEndDate(2026, 2, 28)
        self.SetCash(100000)
        self.SetBenchmark("SPY")

        self.spy = self.AddEquity("SPY", Resolution.Daily).Symbol
        self.spx = self.AddIndex("SPX", Resolution.Daily).Symbol

        opt = self.AddIndexOption("SPX", Resolution.Daily)
        opt.SetFilter(self._option_filter)
        self.option_symbol = opt.Symbol

        self.vix = self.AddData(CBOE, "VIX", Resolution.Daily).Symbol

        self.spx_window = deque(maxlen=35)
        self.last_spx_date = None
        self.spx_peak = None
        self.extra_trade_allowed = True

        self._reset_base_state()
        self._reset_extra_state()

        self.cooldown_until = self.StartDate
        self.core_allocated = False

        self.SetWarmUp(timedelta(days=400))

        self.Schedule.On(
            self.DateRules.EveryDay(self.spy),
            self.TimeRules.AfterMarketOpen(self.spy, 30),
            self._rebalance_core
        )

    def _option_filter(self, universe):
        return (
            universe
            .IncludeWeeklys()
            .Strikes(-150, 150)
            .Expiration(60, 400)
        )

    def OnData(self, data: Slice):
        self._update_histories(data)

        if self.IsWarmingUp:
            return

        self._rebalance_core()

        spx_price = self._get_price(self.spx)
        vix_level = self._get_price(self.vix)

        if spx_price <= 0 or vix_level <= 0:
            return

        if self.spx_peak is None or spx_price > self.spx_peak:
            self.spx_peak = spx_price
            self.extra_trade_allowed = True

        if self.base_in_trade:
            self._check_base_exits(spx_price, vix_level)

        if self.extra_in_trade:
            self._check_extra_exits(spx_price, vix_level)

        if (not self.base_in_trade) and self.Time >= self.cooldown_until and vix_level < self.VIX_THRESHOLD:
            self._try_enter_base(data, spx_price, vix_level)

        if (not self.extra_in_trade) and self.extra_trade_allowed and self._extra_entry_triggered(spx_price):
            self._try_enter_extra(data, spx_price, vix_level)

    def _rebalance_core(self):
        # Keep SPY near 90% of portfolio.
        # Does not force refill LEAPS losses; just maintains core target.
        if self.spy not in self.Securities:
            return

        target = self.CORE_SPY_WEIGHT
        current = self.Portfolio[self.spy].HoldingsValue / max(float(self.Portfolio.TotalPortfolioValue), 1.0)

        if (not self.core_allocated) or abs(current - target) > 0.03:
            self.SetHoldings(self.spy, target)
            self.core_allocated = True

    def _update_histories(self, data: Slice):
        if data.Bars.ContainsKey(self.spx):
            bar = data.Bars[self.spx]
            bar_date = bar.EndTime.date()

            if self.last_spx_date != bar_date:
                self.spx_window.append(float(bar.Close))
                self.last_spx_date = bar_date

    def _current_leaps_value(self):
        total = 0.0
        for sym in [self.base_call_symbol, self.base_put_symbol, self.extra_call_symbol]:
            if sym is not None and sym in self.Portfolio:
                total += abs(float(self.Portfolio[sym].HoldingsValue))
        return total

    def _available_leaps_budget(self):
        total_value = float(self.Portfolio.TotalPortfolioValue)
        max_leaps_value = total_value * self.LEAPS_SLEEVE_MAX
        current_leaps_value = self._current_leaps_value()
        return max(0.0, max_leaps_value - current_leaps_value)

    def _try_enter_base(self, data: Slice, spx_price: float, vix_level: float):
        chain = data.OptionChains.get(self.option_symbol)
        if chain is None:
            return

        best_call = self._select_call_contract(chain, spx_price, vix_level)
        if best_call is None:
            return

        call_mid = self._mid(best_call)
        if call_mid <= 0:
            return

        budget = self._available_leaps_budget()
        if budget <= 0:
            return

        n_contracts = int(budget / (call_mid * 100))
        if n_contracts < 1:
            return

        self.MarketOrder(best_call.Symbol, n_contracts)

        self.base_call_symbol = best_call.Symbol
        self.base_call_entry_px = call_mid
        self.base_num_contracts = n_contracts
        self.base_in_trade = True
        self.base_first_target_hit = False
        self.base_entry_time = self.Time

        dte = (best_call.Expiry.date() - self.Time.date()).days
        delta = self._get_delta(best_call, spx_price, vix_level)

        self.Log(
            f"BASE ENTRY | {self.Time.date()} | SPX={spx_price:.0f} VIX={vix_level:.1f} | "
            f"Strike={best_call.Strike:.0f} DTE={dte} Delta={delta:.2f} Mid=${call_mid:.2f} "
            f"Qty={n_contracts} | LEAPS sleeve used=${self._current_leaps_value():,.0f}"
        )

    def _try_enter_extra(self, data: Slice, spx_price: float, vix_level: float):
        chain = data.OptionChains.get(self.option_symbol)
        if chain is None:
            return

        best_call = self._select_call_contract(chain, spx_price, vix_level)
        if best_call is None:
            return

        call_mid = self._mid(best_call)
        if call_mid <= 0:
            return

        budget = self._available_leaps_budget()
        if budget <= 0:
            return

        n_contracts = int(budget / (call_mid * 100))
        if n_contracts < 1:
            return

        self.MarketOrder(best_call.Symbol, n_contracts)

        self.extra_call_symbol = best_call.Symbol
        self.extra_call_entry_px = call_mid
        self.extra_num_contracts = n_contracts
        self.extra_in_trade = True
        self.extra_entry_time = self.Time
        self.extra_trade_allowed = False

        dte = (best_call.Expiry.date() - self.Time.date()).days
        delta = self._get_delta(best_call, spx_price, vix_level)
        drawdown = (self.spx_peak - spx_price) / self.spx_peak if self.spx_peak else 0.0

        self.Log(
            f"EXTRA ENTRY | {self.Time.date()} | SPX={spx_price:.0f} Peak={self.spx_peak:.0f} "
            f"Drawdown={drawdown:.1%} | Strike={best_call.Strike:.0f} DTE={dte} Delta={delta:.2f} "
            f"Mid=${call_mid:.2f} Qty={n_contracts}"
        )

    def _check_base_exits(self, spx_price, vix_level):
        if self.base_call_symbol is None or self.base_call_symbol not in self.Securities:
            self._exit_base("missing_call_security")
            return

        call_sec = self.Securities[self.base_call_symbol]
        call_mid = self._mid_from_security(call_sec)
        if call_mid <= 0:
            call_mid = self._bs_call(
                spx_price,
                vix_level / 100.0,
                float(self.base_call_symbol.ID.StrikePrice),
                max((self.base_call_symbol.ID.Date.date() - self.Time.date()).days, 0)
            )

        # Stage 1: sell 50% at +100%
        if (
            self.base_call_entry_px > 0
            and not self.base_first_target_hit
            and call_mid >= self.base_call_entry_px * (1 + self.FIRST_PROFIT_TARGET)
        ):
            qty = self.Portfolio[self.base_call_symbol].Quantity
            sell_qty = int(qty * 0.5)
            if sell_qty > 0:
                self.MarketOrder(self.base_call_symbol, -sell_qty)
                self.base_first_target_hit = True
                self.Log(f"BASE PARTIAL EXIT [+100%] | {self.Time.date()} | Sold {sell_qty}")

        # Stage 2: sell remaining 50% at +150%
        if (
            self.base_call_entry_px > 0
            and self.base_first_target_hit
            and call_mid >= self.base_call_entry_px * (1 + self.SECOND_PROFIT_TARGET)
        ):
            self._exit_base("profit_target_150")
            return

        # Crash rules
        for lb, threshold in sorted(self.CRASH_RULES.items()):
            if len(self.spx_window) > lb:
                past_px = self.spx_window[-(lb + 1)]
                if past_px > 0:
                    pct_chg = (spx_price - past_px) / past_px
                    if pct_chg <= threshold:
                        self._exit_base(f"crash_{lb}d")
                        return

        # Expiry
        dte = (self.base_call_symbol.ID.Date.date() - self.Time.date()).days
        if dte <= 1:
            self._exit_base("expiry")

    def _check_extra_exits(self, spx_price, vix_level):
        if self.extra_call_symbol is None or self.extra_call_symbol not in self.Securities:
            self._exit_extra("missing_call_security")
            return

        call_sec = self.Securities[self.extra_call_symbol]
        call_mid = self._mid_from_security(call_sec)
        if call_mid <= 0:
            call_mid = self._bs_call(
                spx_price,
                vix_level / 100.0,
                float(self.extra_call_symbol.ID.StrikePrice),
                max((self.extra_call_symbol.ID.Date.date() - self.Time.date()).days, 0)
            )

        # Exit fully at +200%
        if (
            self.extra_call_entry_px > 0
            and call_mid >= self.extra_call_entry_px * (1 + self.EXTRA_PROFIT_TARGET)
        ):
            self._exit_extra("profit_target_200")
            return

        # Crash rules
        for lb, threshold in sorted(self.CRASH_RULES.items()):
            if len(self.spx_window) > lb:
                past_px = self.spx_window[-(lb + 1)]
                if past_px > 0:
                    pct_chg = (spx_price - past_px) / past_px
                    if pct_chg <= threshold:
                        self._exit_extra(f"crash_{lb}d")
                        return

        # Expiry
        dte = (self.extra_call_symbol.ID.Date.date() - self.Time.date()).days
        if dte <= 1:
            self._exit_extra("expiry")

    def _exit_base(self, reason):
        if self.base_call_symbol is not None and self.base_call_symbol in self.Portfolio:
            qty = self.Portfolio[self.base_call_symbol].Quantity
            if qty > 0:
                self.MarketOrder(self.base_call_symbol, -qty)

        if self.base_put_symbol is not None and self.base_put_symbol in self.Portfolio:
            qty = self.Portfolio[self.base_put_symbol].Quantity
            if qty > 0:
                self.MarketOrder(self.base_put_symbol, -qty)

        self.Log(f"BASE EXIT [{reason}] | {self.Time.date()}")
        self._reset_base_state()
        self.cooldown_until = self.Time + timedelta(days=self.COOLDOWN_DAYS)

    def _exit_extra(self, reason):
        if self.extra_call_symbol is not None and self.extra_call_symbol in self.Portfolio:
            qty = self.Portfolio[self.extra_call_symbol].Quantity
            if qty > 0:
                self.MarketOrder(self.extra_call_symbol, -qty)

        self.Log(f"EXTRA EXIT [{reason}] | {self.Time.date()}")
        self._reset_extra_state()

    def _extra_entry_triggered(self, spx_price: float) -> bool:
        if self.spx_peak is None or self.spx_peak <= 0:
            return False
        drawdown = (self.spx_peak - spx_price) / self.spx_peak
        return drawdown >= self.EXTRA_DRAWDOWN_TRIGGER

    def _select_call_contract(self, chain, spx_price, vix_level):
        target_exp = self.Time.date() + timedelta(days=self.CALL_DTE)

        calls = [
            c for c in chain
            if c.Right == OptionRight.Call and c.AskPrice > 0 and c.BidPrice > 0
        ]
        if not calls:
            return None

        near_calls = [c for c in calls if abs((c.Expiry.date() - target_exp).days) <= 30]
        pool = near_calls if near_calls else calls

        return min(pool, key=lambda c: abs(self._get_delta(c, spx_price, vix_level) - self.CALL_DELTA_TGT))

    def _reset_base_state(self):
        self.base_in_trade = False
        self.base_call_symbol = None
        self.base_put_symbol = None
        self.base_call_entry_px = 0.0
        self.base_put_entry_px = 0.0
        self.base_num_contracts = 0
        self.base_first_target_hit = False
        self.base_entry_time = None

    def _reset_extra_state(self):
        self.extra_in_trade = False
        self.extra_call_symbol = None
        self.extra_call_entry_px = 0.0
        self.extra_num_contracts = 0
        self.extra_entry_time = None

    def _get_delta(self, contract, spx_price, vix_level):
        if contract.Greeks is not None and contract.Greeks.Delta is not None and contract.Greeks.Delta > 0:
            return float(contract.Greeks.Delta)
        t_days = (contract.Expiry.date() - self.Time.date()).days
        return self._bs_delta(spx_price, vix_level / 100.0, float(contract.Strike), t_days)

    def _mid(self, contract):
        if contract.AskPrice > 0 and contract.BidPrice > 0:
            return float((contract.AskPrice + contract.BidPrice) / 2.0)
        return float(contract.LastPrice) if contract.LastPrice else 0.0

    def _mid_from_security(self, security):
        ask = float(security.AskPrice) if security.AskPrice else 0.0
        bid = float(security.BidPrice) if security.BidPrice else 0.0
        last = float(security.Price) if security.Price else 0.0
        if ask > 0 and bid > 0:
            return (ask + bid) / 2.0
        return last

    def _get_price(self, symbol):
        if symbol not in self.Securities:
            return 0.0
        sec = self.Securities[symbol]
        return float(sec.Price) if sec.Price and sec.Price > 0 else 0.0

    def _bs_delta(self, S, sigma, K, T_days):
        T = max(T_days / 365.0, 1e-6)
        sigma = max(sigma, 1e-6)
        d1 = (math.log(S / K) + (self.RF_RATE + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
        return 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))

    def _bs_call(self, S, sigma, K, T_days):
        T = max(T_days / 365.0, 1e-6)
        sigma = max(sigma, 1e-6)
        d1 = (math.log(S / K) + (self.RF_RATE + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        nd1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
        nd2 = 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))
        return S * nd1 - K * math.exp(-self.RF_RATE * T) * nd2

    def OnOrderEvent(self, orderEvent):
        if orderEvent.Status == OrderStatus.Filled:
            self.Log(
                f"Fill | {orderEvent.Symbol.Value} "
                f"qty={orderEvent.FillQuantity:+.0f} @ ${orderEvent.FillPrice:.2f}"
            )
