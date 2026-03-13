# region imports
from AlgorithmImports import *
import numpy as np
from scipy.stats import norm
from datetime import timedelta
from collections import deque
# endregion

# ============================================================
# BASELINE: Profit Target Change — Two-Stage Exit
# Two-stage profit exit: sell 50% at +100%, sell remaining 50% at +150%
# vs. original: sell 100% at +100%
# Base: Case 1 (no put hedge) — compare vs spx_leaps_qc_case1_baseline.py
# Period: 2012-01-01 to 2026-02-28 | Start: $100,000
# ============================================================

class SPXLeapsStrategy(QCAlgorithm):

    PUT_MODE       = "none"

    VIX_THRESHOLD  = 20.0
    CALL_DELTA_TGT = 0.40
    CALL_DTE       = 300
    PUT_DTE        = 90
    RISK_PER_TRADE = 0.30
    PUT_COST_FRAC  = 0.10
    COOLDOWN_DAYS  = 5
    RF_RATE        = 0.045

    FIRST_PROFIT_TARGET  = 1.00   # +100% => sell 50%
    SECOND_PROFIT_TARGET = 1.50   # +150% => sell remaining 50%

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

        self._reset_state()
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

        if self.in_trade:
            self._check_exits(data, spx_price, vix_level)
        else:
            if self.Time >= self.cooldown_until and vix_level < self.VIX_THRESHOLD:
                self._try_enter(data, spx_price, vix_level)

    def _update_histories(self, data):
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
                if pe and pe > 0:
                    self.pe_window.append(float(pe))

    def _try_enter(self, data, spx_price, vix_level):
        chain = data.OptionChains.get(self.option_symbol)
        if chain is None:
            return

        target_call_exp = self.Time.date() + timedelta(days=self.CALL_DTE)

        calls = [
            c for c in chain
            if c.Right == OptionRight.Call
            and c.AskPrice > 0
            and c.BidPrice > 0
        ]
        if not calls:
            return

        near_calls = [
            c for c in calls
            if abs((c.Expiry.date() - target_call_exp).days) <= 30
        ]
        pool = near_calls if near_calls else calls

        best_call = min(
            pool,
            key=lambda c: abs(self._get_delta(c, spx_price, vix_level) - self.CALL_DELTA_TGT)
        )
        call_mid = self._mid(best_call)
        call_dte = (best_call.Expiry.date() - self.Time.date()).days

        if call_mid <= 0:
            return

        free_cash = self.Portfolio.Cash
        budget = free_cash * self.RISK_PER_TRADE
        multiplier = 100
        n_contracts = int(budget / (call_mid * multiplier))
        if n_contracts < 1:
            return

        put_contract = None
        put_mid = 0.0
        if self._should_hedge():
            put_budget = call_mid * self.PUT_COST_FRAC * n_contracts * multiplier
            put_contract = self._select_put(chain, spx_price, put_budget, n_contracts)
            if put_contract:
                put_mid = self._mid(put_contract)

        self.MarketOrder(best_call.Symbol, n_contracts)
        if put_contract and put_mid > 0:
            self.MarketOrder(put_contract.Symbol, n_contracts)
            self.put_symbol = put_contract.Symbol
            self.put_entry_px = put_mid

        self.call_symbol = best_call.Symbol
        self.call_entry_px = call_mid
        self.num_contracts = n_contracts
        self.in_trade = True

        call_delta = self._get_delta(best_call, spx_price, vix_level)
        self.Log(
            f"ENTRY | {self.Time.date()} | SPX={spx_price:.0f} VIX={vix_level:.1f} | "
            f"Call strike={best_call.Strike:.0f} DTE={call_dte} delta={call_delta:.2f} "
            f"mid=${call_mid:.2f} x{n_contracts} | "
            f"Put={'YES' if put_contract else 'NO'} | "
            f"Deployed=${n_contracts * call_mid * multiplier:,.0f} | "
            f"Portfolio=${self.Portfolio.TotalPortfolioValue:,.0f}"
        )

    def _select_put(self, chain, spx_price, budget, n_contracts):
        target_exp = self.Time.date() + timedelta(days=self.PUT_DTE)

        puts = [
            c for c in chain
            if c.Right == OptionRight.Put
            and c.AskPrice > 0
            and c.BidPrice > 0
        ]
        if not puts:
            return None

        puts.sort(key=lambda c: (
            abs((c.Expiry.date() - target_exp).days),
            abs(c.Strike - spx_price)
        ))

        for p in puts[:20]:
            mid = self._mid(p)
            cost = mid * n_contracts * 100
            if mid > 0 and cost <= budget * 1.5:
                return p
        return None

    def _check_exits(self, data, spx_price, vix_level):
        if self.call_symbol is None:
            return

        call_sec = self.Securities[self.call_symbol] if self.call_symbol in self.Securities else None
        if call_sec is None:
            self._exit("expiry")
            return

        call_mid = self._mid_from_security(call_sec)
        if call_mid <= 0:
            call_mid = self._bs_call(
                spx_price,
                vix_level / 100,
                self.call_symbol.ID.StrikePrice,
                max((self.call_symbol.ID.Date.date() - self.Time.date()).days, 0)
            )

        # Stage 1: sell 50% at +100%
        if (
            self.call_entry_px > 0
            and not self.first_target_hit
            and call_mid >= self.call_entry_px * (1 + self.FIRST_PROFIT_TARGET)
        ):
            current_qty = self.Portfolio[self.call_symbol].Quantity
            sell_qty = int(current_qty * 0.5)

            if sell_qty > 0:
                self.MarketOrder(self.call_symbol, -sell_qty)
                self.first_target_hit = True
                self.Log(
                    f"PARTIAL EXIT [+100%] | {self.Time.date()} | "
                    f"Sold {sell_qty} contracts | "
                    f"Call mid=${call_mid:.2f}"
                )

        # Stage 2: sell remaining 50% at +150%
        if (
            self.call_entry_px > 0
            and self.first_target_hit
            and call_mid >= self.call_entry_px * (1 + self.SECOND_PROFIT_TARGET)
        ):
            self._exit("profit_target_150")
            return

        # Crash rules
        for lb, threshold in sorted(self.CRASH_RULES.items()):
            if len(self.spx_window) > lb:
                past_px = self.spx_window[-(lb + 1)]
                if past_px > 0:
                    pct_chg = (spx_price - past_px) / past_px
                    if pct_chg <= threshold:
                        self._exit(f"crash_{lb}d")
                        return

        # Expiry
        dte = (self.call_symbol.ID.Date.date() - self.Time.date()).days
        if dte <= 1:
            self._exit("expiry")

    def _exit(self, reason):
        pv_before = self.Portfolio.TotalPortfolioValue

        for sym in [self.call_symbol, self.put_symbol]:
            if sym is None:
                continue
            qty = self.Portfolio[sym].Quantity if sym in self.Portfolio else 0
            if qty > 0:
                self.MarketOrder(sym, -qty)

        self.Log(
            f"EXIT [{reason}] | {self.Time.date()} | "
            f"Portfolio=${pv_before:,.0f}"
        )

        self._reset_state()
        self.cooldown_until = self.Time + timedelta(days=self.COOLDOWN_DAYS)

    def _reset_state(self):
        self.in_trade = False
        self.call_symbol = None
        self.put_symbol = None
        self.call_entry_px = 0.0
        self.put_entry_px = 0.0
        self.num_contracts = 0
        self.first_target_hit = False

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
        if contract.Greeks and contract.Greeks.Delta and contract.Greeks.Delta > 0:
            return float(contract.Greeks.Delta)
        T_days = (contract.Expiry.date() - self.Time.date()).days
        return self._bs_delta(spx_price, vix_level / 100, contract.Strike, T_days)

    def _mid(self, contract):
        if contract.AskPrice > 0 and contract.BidPrice > 0:
            return (contract.AskPrice + contract.BidPrice) / 2.0
        return contract.LastPrice or 0.0

    def _mid_from_security(self, security):
        ask = security.AskPrice
        bid = security.BidPrice
        last = security.Price
        if ask > 0 and bid > 0:
            return (ask + bid) / 2.0
        return last or 0.0

    def _get_price(self, symbol):
        sec = self.Securities[symbol] if symbol in self.Securities else None
        return sec.Price if sec and sec.Price > 0 else 0.0

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
        return float(
            S * norm.cdf(d1) - K * np.exp(-self.RF_RATE * T) * norm.cdf(d2)
        )

    def OnOrderEvent(self, orderEvent):
        if orderEvent.Status == OrderStatus.Filled:
            self.Log(
                f"  Fill | {orderEvent.Symbol.Value} "
                f"qty={orderEvent.FillQuantity:+.0f} "
                f"@ ${orderEvent.FillPrice:.2f}"
            )
