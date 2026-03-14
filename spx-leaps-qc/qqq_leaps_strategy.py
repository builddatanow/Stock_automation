# region imports
from AlgorithmImports import *
from collections import deque
from datetime import timedelta
import math
# endregion

# ============================================================
# QQQ LEAPS Strategy
# Same logic as SPX version but on QQQ equity options
# Core: SPY 75% + TLT 15% | LEAPS sleeve: QQQ calls 10%
# DD put: fires when QQQ drops >10% from 52-week high
# Period: 2000-01-01 to 2026-02-28 | Start: $100,000
# ============================================================

class QQQLeapsStrategy(QCAlgorithm):

    CORE_SPY_WEIGHT  = 0.75
    CORE_TLT_WEIGHT  = 0.15
    LEAPS_SLEEVE_MAX = 0.10

    VIX_THRESHOLD  = 20.0
    CALL_DELTA_TGT = 0.40
    CALL_DTE       = 300

    FIRST_PROFIT_TARGET  = 1.00
    SECOND_PROFIT_TARGET = 1.50

    DD_PUT_THRESHOLD  = 0.10
    DD_PUT_SLEEVE     = 0.15
    DD_PUT_DTE        = 90
    DD_PUT_DELTA_TGT  = 0.30
    DD_PUT_PROFIT_TGT = 2.00

    CRASH_RULES = {
        7:  -0.025,
        10: -0.04,
        14: -0.06,
        30: -0.08,
    }

    RF_RATE       = 0.045
    COOLDOWN_DAYS = 5

    def Initialize(self):
        self.SetStartDate(2000, 1, 1)
        self.SetEndDate(2026, 2, 28)
        self.SetCash(100_000)
        self.SetBenchmark("QQQ")

        self.spy = self.AddEquity("SPY", Resolution.Daily).Symbol
        self.tlt = self.AddEquity("TLT", Resolution.Daily).Symbol
        self.qqq = self.AddEquity("QQQ", Resolution.Daily).Symbol

        opt = self.AddOption("QQQ", Resolution.Daily)
        opt.SetFilter(self._option_filter)
        self.option_symbol = opt.Symbol

        self.vix = self.AddData(CBOE, "VIX", Resolution.Daily).Symbol

        self.qqq_window      = deque(maxlen=35)
        self.qqq_52wk_window = deque(maxlen=260)
        self.last_qqq_date   = None
        self.qqq_peak        = None
        self.extra_trade_allowed = True

        self._reset_base_state()
        self._reset_extra_state()
        self._reset_dd_put_state()

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
            .Strikes(-80, 80)
            .Expiration(60, 400)
        )

    def OnData(self, data: Slice):
        self._update_histories(data)

        if self.IsWarmingUp:
            return

        self._rebalance_core()

        qqq_price = self._get_price(self.qqq)
        vix_level = self._get_price(self.vix)

        if qqq_price <= 0 or vix_level <= 0:
            return

        if self.qqq_peak is None or qqq_price > self.qqq_peak:
            self.qqq_peak = qqq_price
            self.extra_trade_allowed = True

        if self.base_in_trade:
            self._check_base_exits(qqq_price, vix_level)

        if not self.base_in_trade and self.Time >= self.cooldown_until and vix_level < self.VIX_THRESHOLD:
            self._try_enter_base(data, qqq_price, vix_level)

        if self.dd_in_trade:
            self._check_dd_put_exits(qqq_price, vix_level)
        elif self._dd_put_triggered(qqq_price):
            self._try_enter_dd_put(data, qqq_price, vix_level)

    def _rebalance_core(self):
        if self.spy not in self.Securities or self.tlt not in self.Securities:
            return
        total       = max(float(self.Portfolio.TotalPortfolioValue), 1.0)
        spy_current = self.Portfolio[self.spy].HoldingsValue / total
        tlt_current = self.Portfolio[self.tlt].HoldingsValue / total
        if (not self.core_allocated) \
                or abs(spy_current - self.CORE_SPY_WEIGHT) > 0.03 \
                or abs(tlt_current - self.CORE_TLT_WEIGHT) > 0.03:
            self.SetHoldings(self.spy, self.CORE_SPY_WEIGHT)
            self.SetHoldings(self.tlt, self.CORE_TLT_WEIGHT)
            self.core_allocated = True

    def _update_histories(self, data: Slice):
        if data.Bars.ContainsKey(self.qqq):
            bar      = data.Bars[self.qqq]
            bar_date = bar.EndTime.date()
            if self.last_qqq_date != bar_date:
                price = float(bar.Close)
                self.qqq_window.append(price)
                self.qqq_52wk_window.append(price)
                self.last_qqq_date = bar_date

    # ── Entries ──────────────────────────────────────────────────────────────

    def _try_enter_base(self, data: Slice, qqq_price: float, vix_level: float):
        chain = data.OptionChains.get(self.option_symbol)
        if chain is None:
            return
        contract = self._select_call(chain, qqq_price, vix_level)
        if contract is None:
            return
        mid = self._mid(contract)
        if mid <= 0:
            return
        budget = max(0.0, float(self.Portfolio.TotalPortfolioValue) * self.LEAPS_SLEEVE_MAX
                     - self._leaps_value())
        n = int(budget / (mid * 100))
        if n < 1:
            return
        self.MarketOrder(contract.Symbol, n)
        self.base_call_symbol   = contract.Symbol
        self.base_call_entry_px = mid
        self.base_num_contracts = n
        self.base_in_trade      = True
        self.base_first_hit     = False
        dte   = (contract.Expiry.date() - self.Time.date()).days
        delta = self._get_delta(contract, qqq_price, vix_level)
        self.Log(f"BASE ENTRY | {self.Time.date()} | QQQ={qqq_price:.2f} VIX={vix_level:.1f} | "
                 f"Strike={contract.Strike:.2f} DTE={dte} Delta={delta:.2f} Mid=${mid:.2f} Qty={n}")

    def _try_enter_dd_put(self, data: Slice, qqq_price: float, vix_level: float):
        chain = data.OptionChains.get(self.option_symbol)
        if chain is None:
            return
        target_exp = self.Time.date() + timedelta(days=self.DD_PUT_DTE)
        puts = [c for c in chain if c.Right == OptionRight.Put and c.AskPrice > 0 and c.BidPrice > 0]
        if not puts:
            return
        near = [c for c in puts if abs((c.Expiry.date() - target_exp).days) <= 20]
        pool = near if near else puts
        contract = min(pool, key=lambda c: abs(self._get_delta(c, qqq_price, vix_level) - self.DD_PUT_DELTA_TGT))
        mid = self._mid(contract)
        if mid <= 0:
            return
        budget = float(self.Portfolio.TotalPortfolioValue) * self.DD_PUT_SLEEVE
        n = int(budget / (mid * 100))
        if n < 1:
            return
        self.MarketOrder(contract.Symbol, n)
        self.dd_put_symbol   = contract.Symbol
        self.dd_put_entry_px = mid
        self.dd_in_trade     = True
        peak = max(self.qqq_52wk_window) if self.qqq_52wk_window else qqq_price
        dd   = (qqq_price - peak) / peak if peak > 0 else 0.0
        dte  = (contract.Expiry.date() - self.Time.date()).days
        self.Log(f"DD PUT ENTRY | {self.Time.date()} | DD={dd:.1%} QQQ={qqq_price:.2f} | "
                 f"Strike={contract.Strike:.2f} DTE={dte} Mid=${mid:.2f} Qty={n}")

    # ── Exits ─────────────────────────────────────────────────────────────────

    def _check_base_exits(self, qqq_price, vix_level):
        if self.base_call_symbol is None or self.base_call_symbol not in self.Securities:
            self._exit_base("missing"); return
        mid = self._mid_from_security(self.Securities[self.base_call_symbol])
        if mid <= 0:
            mid = self._bs_call(qqq_price, vix_level / 100.0,
                                float(self.base_call_symbol.ID.StrikePrice),
                                max((self.base_call_symbol.ID.Date.date() - self.Time.date()).days, 0))
        if self.base_call_entry_px > 0 and not self.base_first_hit \
                and mid >= self.base_call_entry_px * (1 + self.FIRST_PROFIT_TARGET):
            qty  = self.Portfolio[self.base_call_symbol].Quantity
            sell = int(qty * 0.5)
            if sell > 0:
                self.MarketOrder(self.base_call_symbol, -sell)
                self.base_first_hit = True
                self.Log(f"BASE PARTIAL [+100%] | {self.Time.date()} | Sold {sell}")
        if self.base_call_entry_px > 0 and self.base_first_hit \
                and mid >= self.base_call_entry_px * (1 + self.SECOND_PROFIT_TARGET):
            self._exit_base("profit_150"); return
        for lb, thr in sorted(self.CRASH_RULES.items()):
            if len(self.qqq_window) > lb:
                past = self.qqq_window[-(lb + 1)]
                if past > 0 and (qqq_price - past) / past <= thr:
                    self._exit_base(f"crash_{lb}d"); return
        if (self.base_call_symbol.ID.Date.date() - self.Time.date()).days <= 1:
            self._exit_base("expiry")

    def _check_dd_put_exits(self, qqq_price, vix_level):
        if self.dd_put_symbol is None or self.dd_put_symbol not in self.Securities:
            self._exit_dd_put("missing"); return
        mid = self._mid_from_security(self.Securities[self.dd_put_symbol])
        if mid <= 0:
            mid = self._bs_put(qqq_price, vix_level / 100.0,
                               float(self.dd_put_symbol.ID.StrikePrice),
                               max((self.dd_put_symbol.ID.Date.date() - self.Time.date()).days, 0))
        if self.dd_put_entry_px > 0 and mid >= self.dd_put_entry_px * (1 + self.DD_PUT_PROFIT_TGT):
            self._exit_dd_put("profit_200"); return
        if (self.dd_put_symbol.ID.Date.date() - self.Time.date()).days <= 1:
            self._exit_dd_put("expiry")

    def _exit_base(self, reason):
        if self.base_call_symbol and self.base_call_symbol in self.Portfolio:
            qty = self.Portfolio[self.base_call_symbol].Quantity
            if qty > 0:
                self.MarketOrder(self.base_call_symbol, -qty)
        self.Log(f"BASE EXIT [{reason}] | {self.Time.date()}")
        self._reset_base_state()
        self.cooldown_until = self.Time + timedelta(days=self.COOLDOWN_DAYS)

    def _exit_dd_put(self, reason):
        if self.dd_put_symbol and self.dd_put_symbol in self.Portfolio:
            qty = self.Portfolio[self.dd_put_symbol].Quantity
            if qty > 0:
                self.MarketOrder(self.dd_put_symbol, -qty)
        self.Log(f"DD PUT EXIT [{reason}] | {self.Time.date()}")
        self._reset_dd_put_state()

    # ── State resets ──────────────────────────────────────────────────────────

    def _reset_base_state(self):
        self.base_in_trade      = False
        self.base_call_symbol   = None
        self.base_call_entry_px = 0.0
        self.base_num_contracts = 0
        self.base_first_hit     = False

    def _reset_extra_state(self):
        self.extra_in_trade = False

    def _reset_dd_put_state(self):
        self.dd_in_trade     = False
        self.dd_put_symbol   = None
        self.dd_put_entry_px = 0.0

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _dd_put_triggered(self, price: float) -> bool:
        if len(self.qqq_52wk_window) < 30:
            return False
        peak = max(self.qqq_52wk_window)
        return peak > 0 and (price - peak) / peak <= -self.DD_PUT_THRESHOLD

    def _leaps_value(self):
        total = 0.0
        for sym in [self.base_call_symbol]:
            if sym and sym in self.Portfolio:
                total += abs(float(self.Portfolio[sym].HoldingsValue))
        return total

    def _select_call(self, chain, price, vix_level):
        target_exp = self.Time.date() + timedelta(days=self.CALL_DTE)
        calls = [c for c in chain if c.Right == OptionRight.Call and c.AskPrice > 0 and c.BidPrice > 0]
        if not calls:
            return None
        near = [c for c in calls if abs((c.Expiry.date() - target_exp).days) <= 30]
        pool = near if near else calls
        return min(pool, key=lambda c: abs(self._get_delta(c, price, vix_level) - self.CALL_DELTA_TGT))

    def _get_delta(self, contract, price, vix_level):
        if contract.Greeks is not None and contract.Greeks.Delta is not None and contract.Greeks.Delta > 0:
            return float(contract.Greeks.Delta)
        return self._bs_delta(price, vix_level / 100.0, float(contract.Strike),
                              (contract.Expiry.date() - self.Time.date()).days)

    def _mid(self, contract):
        if contract.AskPrice > 0 and contract.BidPrice > 0:
            return float((contract.AskPrice + contract.BidPrice) / 2.0)
        return float(contract.LastPrice) if contract.LastPrice else 0.0

    def _mid_from_security(self, security):
        ask  = float(security.AskPrice) if security.AskPrice else 0.0
        bid  = float(security.BidPrice) if security.BidPrice else 0.0
        last = float(security.Price)    if security.Price    else 0.0
        return (ask + bid) / 2.0 if ask > 0 and bid > 0 else last

    def _get_price(self, symbol):
        if symbol not in self.Securities:
            return 0.0
        sec = self.Securities[symbol]
        return float(sec.Price) if sec.Price and sec.Price > 0 else 0.0

    def _bs_delta(self, S, sigma, K, T_days):
        T     = max(T_days / 365.0, 1e-6)
        sigma = max(sigma, 1e-6)
        d1    = (math.log(S / K) + (self.RF_RATE + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
        return 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))

    def _bs_call(self, S, sigma, K, T_days):
        T     = max(T_days / 365.0, 1e-6)
        sigma = max(sigma, 1e-6)
        d1    = (math.log(S / K) + (self.RF_RATE + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
        d2    = d1 - sigma * math.sqrt(T)
        nd1   = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
        nd2   = 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))
        return S * nd1 - K * math.exp(-self.RF_RATE * T) * nd2

    def _bs_put(self, S, sigma, K, T_days):
        T     = max(T_days / 365.0, 1e-6)
        sigma = max(sigma, 1e-6)
        d1    = (math.log(S / K) + (self.RF_RATE + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
        d2    = d1 - sigma * math.sqrt(T)
        nd1   = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
        nd2   = 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))
        return K * math.exp(-self.RF_RATE * T) * (1 - nd2) - S * (1 - nd1)

    def OnOrderEvent(self, orderEvent):
        if orderEvent.Status == OrderStatus.Filled:
            self.Log(f"Fill | {orderEvent.Symbol.Value} "
                     f"qty={orderEvent.FillQuantity:+.0f} @ ${orderEvent.FillPrice:.2f}")
