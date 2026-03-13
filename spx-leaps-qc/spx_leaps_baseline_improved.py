# region imports
from AlgorithmImports import *
from collections import deque
from datetime import timedelta
import numpy as np
from scipy.stats import norm
# endregion

# ============================================================
# BASELINE IMPROVED: Two-Stage Exit + Crash-Recovery Extra Trade
# Base: Case 1 (no put hedge) — compare vs spx_leaps_qc_case1_baseline.py
# Period: 2012-01-01 to 2026-02-28 | Start: $100,000
#
# Changes vs original baseline (21.37% CAGR, 56.5% DD):
#   1. Two-stage profit exit: sell 50% at +100%, sell 50% at +150%
#   2. Extra crash-recovery trade: buy 15% of cash when SPX drops 25% from peak
#      - Extra trade exits fully at +200%, subject to same crash rules
# ============================================================

class SPXLeapsStrategy(QCAlgorithm):
    """
    SPX LEAPS Call Strategy — enhanced version

    Base trade:
    - Buy ~300 DTE SPX call near 0.40 delta when VIX < 20
    - Exit 50% at +100%
    - Exit remaining 50% at +150%
    - Crash exits active (no 10-day ignore rule)

    Extra crash-recovery trade:
    - If SPX falls more than 25% from the previous high,
      buy an extra ~300 DTE call
    - This extra trade has profit target = +200%
    - No partial exit for extra trade
    - Only one extra trade at a time
    """

    PUT_MODE = "none"   # "none" | "conditional" | "always"

    VIX_THRESHOLD = 20.0
    CALL_DELTA_TGT = 0.40
    CALL_DTE = 300
    PUT_DTE = 90
    RISK_PER_TRADE = 0.30
    PUT_COST_FRAC = 0.10
    COOLDOWN_DAYS = 5
    RF_RATE = 0.045

    # Base profit exits
    FIRST_PROFIT_TARGET = 1.00   # +100% => sell 50%
    SECOND_PROFIT_TARGET = 1.50  # +150% => sell rest

    # Extra trade settings
    EXTRA_DRAWDOWN_TRIGGER = 0.25   # 25% below prior high
    EXTRA_PROFIT_TARGET = 2.00      # +200%
    EXTRA_RISK_PER_TRADE = 0.15     # 15% of cash for extra position

    CRASH_RULES = {
        7:  -0.03,
        10: -0.04,
        14: -0.06,
        30: -0.08,
    }

    def Initialize(self):
        self.SetStartDate(2012, 1, 1)
        self.SetEndDate(2026, 2, 28)
        self.SetCash(100_000)
        self.SetBenchmark("SPY")

        self.spx = self.AddIndex("SPX", Resolution.Daily).Symbol

        opt = self.AddIndexOption("SPX", Resolution.Daily)
        opt.SetFilter(self._option_filter)
        self.option_symbol = opt.Symbol

        self.vix = self.AddData(CBOE, "VIX", Resolution.Daily).Symbol
        self.spy = self.AddEquity("SPY", Resolution.Daily).Symbol

        self.spx_window = deque(maxlen=35)
        self.pe_window = deque(maxlen=1260)
        self.last_spx_date = None

        # Track running SPX high for extra crash-recovery entry
        self.spx_peak = None

        self._reset_base_state()
        self._reset_extra_state()
        self.cooldown_until = self.StartDate

        self.SetWarmUp(timedelta(days=1290))

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

        spx_price = self._get_price(self.spx)
        vix_level = self._get_price(self.vix)

        if spx_price <= 0 or vix_level <= 0:
            return

        # Update running peak
        if self.spx_peak is None or spx_price > self.spx_peak:
            self.spx_peak = spx_price

        # Manage exits
        if self.base_in_trade:
            self._check_base_exits(data, spx_price, vix_level)

        if self.extra_in_trade:
            self._check_extra_exits(data, spx_price, vix_level)

        # Base entry
        if (not self.base_in_trade) and self.Time >= self.cooldown_until and vix_level < self.VIX_THRESHOLD:
            self._try_enter_base(data, spx_price, vix_level)

        # Extra drawdown entry
        if (not self.extra_in_trade) and self._extra_entry_triggered(spx_price):
            self._try_enter_extra(data, spx_price, vix_level)

    def _update_histories(self, data: Slice):
        if data.Bars.ContainsKey(self.spx):
            bar = data.Bars[self.spx]
            bar_date = bar.EndTime.date()

            if self.last_spx_date != bar_date:
                self.spx_window.append(float(bar.Close))
                self.last_spx_date = bar_date

        if self.PUT_MODE == "conditional":
            spy_sec = self.Securities[self.spy] if self.spy in self.Securities else None
            if spy_sec and spy_sec.Fundamentals:
                pe = spy_sec.Fundamentals.ValuationRatios.PERatio
                if pe is not None and pe > 0:
                    self.pe_window.append(float(pe))

    # ---------------------------
    # Base trade logic
    # ---------------------------
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

        budget = float(self.Portfolio.Cash) * self.RISK_PER_TRADE
        n_contracts = int(budget / (call_mid * 100))
        if n_contracts < 1:
            return

        put_contract = None
        put_mid = 0.0
        if self._should_hedge():
            put_budget = call_mid * self.PUT_COST_FRAC * n_contracts * 100
            put_contract = self._select_put(chain, spx_price, put_budget, n_contracts)
            if put_contract:
                put_mid = self._mid(put_contract)

        self.MarketOrder(best_call.Symbol, n_contracts)
        if put_contract and put_mid > 0:
            self.MarketOrder(put_contract.Symbol, n_contracts)
            self.base_put_symbol = put_contract.Symbol
            self.base_put_entry_px = put_mid

        self.base_call_symbol = best_call.Symbol
        self.base_call_entry_px = call_mid
        self.base_num_contracts = n_contracts
        self.base_in_trade = True
        self.base_first_target_hit = False
        self.base_entry_time = self.Time

        call_dte = (best_call.Expiry.date() - self.Time.date()).days
        call_delta = self._get_delta(best_call, spx_price, vix_level)

        self.Log(
            f"BASE ENTRY | {self.Time.date()} | SPX={spx_price:.0f} VIX={vix_level:.1f} | "
            f"Call strike={best_call.Strike:.0f} DTE={call_dte} delta={call_delta:.2f} "
            f"mid=${call_mid:.2f} x{n_contracts} | "
            f"Put={'YES' if put_contract else 'NO'} | "
            f"Portfolio=${self.Portfolio.TotalPortfolioValue:,.0f}"
        )

    def _check_base_exits(self, data, spx_price, vix_level):
        if self.base_call_symbol is None:
            return

        call_sec = self.Securities[self.base_call_symbol] if self.base_call_symbol in self.Securities else None
        if call_sec is None:
            self._exit_base("missing_call_security")
            return

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
            current_qty = self.Portfolio[self.base_call_symbol].Quantity
            sell_qty = int(current_qty * 0.5)

            if sell_qty > 0:
                self.MarketOrder(self.base_call_symbol, -sell_qty)
                self.base_first_target_hit = True
                self.Log(
                    f"BASE PARTIAL EXIT [+100%] | {self.Time.date()} | "
                    f"Sold {sell_qty} contracts | Call mid=${call_mid:.2f}"
                )

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

    def _exit_base(self, reason):
        pv_before = float(self.Portfolio.TotalPortfolioValue)

        for sym in [self.base_call_symbol, self.base_put_symbol]:
            if sym is None:
                continue
            qty = self.Portfolio[sym].Quantity if sym in self.Portfolio else 0
            if qty > 0:
                self.MarketOrder(sym, -qty)

        self.Log(f"BASE EXIT [{reason}] | {self.Time.date()} | Portfolio=${pv_before:,.0f}")

        self._reset_base_state()
        self.cooldown_until = self.Time + timedelta(days=self.COOLDOWN_DAYS)

    # ---------------------------
    # Extra drawdown trade logic
    # ---------------------------
    def _extra_entry_triggered(self, spx_price: float) -> bool:
        if self.spx_peak is None or self.spx_peak <= 0:
            return False
        drawdown = (self.spx_peak - spx_price) / self.spx_peak
        return drawdown >= self.EXTRA_DRAWDOWN_TRIGGER

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

        budget = float(self.Portfolio.Cash) * self.EXTRA_RISK_PER_TRADE
        n_contracts = int(budget / (call_mid * 100))
        if n_contracts < 1:
            return

        self.MarketOrder(best_call.Symbol, n_contracts)

        self.extra_call_symbol = best_call.Symbol
        self.extra_call_entry_px = call_mid
        self.extra_num_contracts = n_contracts
        self.extra_in_trade = True
        self.extra_entry_time = self.Time
        self.extra_trigger_peak = self.spx_peak

        call_dte = (best_call.Expiry.date() - self.Time.date()).days
        call_delta = self._get_delta(best_call, spx_price, vix_level)
        drawdown = (self.spx_peak - spx_price) / self.spx_peak if self.spx_peak else 0.0

        self.Log(
            f"EXTRA ENTRY | {self.Time.date()} | SPX={spx_price:.0f} | Peak={self.spx_peak:.0f} | "
            f"Drawdown={drawdown:.1%} | Call strike={best_call.Strike:.0f} DTE={call_dte} "
            f"delta={call_delta:.2f} mid=${call_mid:.2f} x{n_contracts}"
        )

    def _check_extra_exits(self, data, spx_price, vix_level):
        if self.extra_call_symbol is None:
            return

        call_sec = self.Securities[self.extra_call_symbol] if self.extra_call_symbol in self.Securities else None
        if call_sec is None:
            self._exit_extra("missing_call_security")
            return

        call_mid = self._mid_from_security(call_sec)
        if call_mid <= 0:
            call_mid = self._bs_call(
                spx_price,
                vix_level / 100.0,
                float(self.extra_call_symbol.ID.StrikePrice),
                max((self.extra_call_symbol.ID.Date.date() - self.Time.date()).days, 0)
            )

        # Extra trade exits fully at +200%
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

    def _exit_extra(self, reason):
        pv_before = float(self.Portfolio.TotalPortfolioValue)

        if self.extra_call_symbol is not None:
            qty = self.Portfolio[self.extra_call_symbol].Quantity if self.extra_call_symbol in self.Portfolio else 0
            if qty > 0:
                self.MarketOrder(self.extra_call_symbol, -qty)

        self.Log(f"EXTRA EXIT [{reason}] | {self.Time.date()} | Portfolio=${pv_before:,.0f}")
        self._reset_extra_state()

    # ---------------------------
    # Shared helpers
    # ---------------------------
    def _select_call_contract(self, chain, spx_price, vix_level):
        target_call_exp = self.Time.date() + timedelta(days=self.CALL_DTE)

        calls = [
            c for c in chain
            if c.Right == OptionRight.Call and c.AskPrice > 0 and c.BidPrice > 0
        ]
        if not calls:
            return None

        near_calls = [
            c for c in calls
            if abs((c.Expiry.date() - target_call_exp).days) <= 30
        ]
        pool = near_calls if near_calls else calls

        return min(
            pool,
            key=lambda c: abs(self._get_delta(c, spx_price, vix_level) - self.CALL_DELTA_TGT)
        )

    def _select_put(self, chain, spx_price, budget, n_contracts):
        target_exp = self.Time.date() + timedelta(days=self.PUT_DTE)

        puts = [
            c for c in chain
            if c.Right == OptionRight.Put and c.AskPrice > 0 and c.BidPrice > 0
        ]
        if not puts:
            return None

        puts.sort(key=lambda c: (
            abs((c.Expiry.date() - target_exp).days),
            abs(float(c.Strike) - spx_price)
        ))

        for p in puts[:20]:
            mid = self._mid(p)
            cost = mid * n_contracts * 100
            if mid > 0 and cost <= budget * 1.5:
                return p
        return None

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
        self.extra_trigger_peak = None

    def _should_hedge(self):
        if self.PUT_MODE == "none":
            return False
        if self.PUT_MODE == "always":
            return True

        if len(self.pe_window) < 252:
            return True

        current_pe = self.pe_window[-1]
        avg_pe = sum(self.pe_window) / len(self.pe_window)
        return current_pe > avg_pe

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
        sec = self.Securities[symbol] if symbol in self.Securities else None
        return float(sec.Price) if sec and sec.Price > 0 else 0.0

    def _bs_delta(self, S, sigma, K, T_days):
        T = max(T_days / 365.0, 1e-6)
        sigma = max(sigma, 1e-6)
        d1 = (np.log(S / K) + (self.RF_RATE + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
        return float(norm.cdf(d1))

    def _bs_call(self, S, sigma, K, T_days):
        T = max(T_days / 365.0, 1e-6)
        sigma = max(sigma, 1e-6)
        d1 = (np.log(S / K) + (self.RF_RATE + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
        d2 = d1 - sigma * np.sqrt(T)
        return float(S * norm.cdf(d1) - K * np.exp(-self.RF_RATE * T) * norm.cdf(d2))

    def OnOrderEvent(self, orderEvent):
        if orderEvent.Status == OrderStatus.Filled:
            self.Log(
                f"Fill | {orderEvent.Symbol.Value} "
                f"qty={orderEvent.FillQuantity:+.0f} "
                f"@ ${orderEvent.FillPrice:.2f}"
            )
