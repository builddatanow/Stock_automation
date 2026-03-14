# region imports
from AlgorithmImports import *
from collections import deque
from datetime import timedelta
import math
# endregion

# ============================================================
# Mag7 LEAPS Strategy
# Buy LEAPS calls on the top 2 Mag7 stocks by 3-month momentum
# Mag7: AAPL, MSFT, NVDA, GOOGL, AMZN, META, TSLA
# Core: SPY 75% + TLT 15% | LEAPS sleeve: 10% split across top 2
# Crash Call: when SPY drops 25% from 52wk high, sell 15% SPY,
#             buy a 300 DTE SPY LEAPS call (recovery trade)
# Re-rank monthly — rotate into new top 2 if changed
# Period: 2010-01-01 to 2026-02-28 | Start: $100,000
# ============================================================

class Mag7LeapsStrategy(QCAlgorithm):

    CORE_SPY_WEIGHT  = 0.75
    CORE_TLT_WEIGHT  = 0.15
    LEAPS_SLEEVE_MAX = 0.10   # total LEAPS budget, split across top N stocks

    TOP_N          = 2        # number of Mag7 stocks to hold LEAPS on
    MOMENTUM_DAYS  = 63       # 3-month momentum lookback
    RERANK_DAYS    = 21       # re-rank every ~1 month

    VIX_THRESHOLD  = 20.0
    CALL_DELTA_TGT = 0.40
    CALL_DTE       = 300

    FIRST_PROFIT_TARGET  = 1.00
    SECOND_PROFIT_TARGET = 1.50

    # ── Crash Recovery Call (SPY -25% from 52wk high) ──────────────────────
    CRASH_CALL_THRESHOLD  = 0.25   # SPY must be down 25% from 52wk peak
    CRASH_CALL_SPY_TRIM   = 0.15   # sell this much of portfolio from SPY core
    CRASH_CALL_DTE        = 300    # target DTE for recovery call
    CRASH_CALL_DELTA_TGT  = 0.40   # target delta (slightly OTM to ATM)
    CRASH_CALL_PROFIT_TGT = 1.00   # exit full position at +100%
    CRASH_CALL_RECOVER_DD = 0.10   # exit if drawdown recovers above this level

    CRASH_RULES = {
        7:  -0.03,
        10: -0.04,
        14: -0.06,
        30: -0.08,
    }

    EARNINGS_AVOID_DAYS = 14   # skip entry if earnings within this many days
    RF_RATE             = 0.045
    COOLDOWN_DAYS       = 5

    MAG7_TICKERS = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA"]

    def Initialize(self):
        self.SetStartDate(2010, 1, 1)
        self.SetEndDate(2026, 2, 28)
        self.SetCash(100_000)
        self.SetBenchmark("QQQ")

        self.spy = self.AddEquity("SPY", Resolution.Daily).Symbol
        self.tlt = self.AddEquity("TLT", Resolution.Daily).Symbol
        self.vix = self.AddData(CBOE, "VIX", Resolution.Daily).Symbol

        # SPY options for crash recovery call
        spy_opt = self.AddOption("SPY", Resolution.Daily)
        spy_opt.SetFilter(self._spy_option_filter)
        self.spy_opt = spy_opt.Symbol

        # Add Mag7 equities + options
        self.mag7_symbols = {}
        self.mag7_option_symbols = {}
        for ticker in self.MAG7_TICKERS:
            eq  = self.AddEquity(ticker, Resolution.Daily)
            eq.SetDataNormalizationMode(DataNormalizationMode.Adjusted)
            opt = self.AddOption(ticker, Resolution.Daily)
            opt.SetFilter(self._option_filter)
            self.mag7_symbols[ticker]        = eq.Symbol
            self.mag7_option_symbols[ticker] = opt.Symbol

        # Price history windows per stock
        self.price_windows  = {t: deque(maxlen=self.MOMENTUM_DAYS + 5) for t in self.MAG7_TICKERS}
        self.spy_window     = deque(maxlen=35)
        self.spy_52wk       = deque(maxlen=260)
        self.last_spy_date  = None

        # LEAPS positions: {ticker: {symbol, entry_px, qty, first_hit}}
        self.leaps_positions = {}

        # Crash recovery call state
        self._reset_crash_call_state()

        self.core_allocated  = False
        self.cooldown_until  = self.StartDate
        self.last_rerank     = self.StartDate
        self.current_top2    = []

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

    def _spy_option_filter(self, universe):
        return (
            universe
            .IncludeWeeklys()
            .Strikes(-50, 10)        # ATM to slightly OTM calls (SPY is large-cap)
            .Expiration(200, 400)    # 300 DTE ± 100 days
        )

    def OnData(self, data: Slice):
        self._update_histories(data)

        if self.IsWarmingUp:
            return

        self._rebalance_core()

        spy_price = self._get_price(self.spy)
        vix_level = self._get_price(self.vix)

        if spy_price <= 0 or vix_level <= 0:
            return

        # Monthly re-rank
        if (self.Time - self.last_rerank).days >= self.RERANK_DAYS:
            self._rerank_and_rotate(data, vix_level)
            self.last_rerank = self.Time

        # Check exits for all open LEAPS positions
        for ticker in list(self.leaps_positions.keys()):
            stock_price = self._get_price(self.mag7_symbols[ticker])
            if stock_price > 0:
                self._check_leaps_exit(ticker, stock_price, vix_level)

        # Crash recovery call: SPY LEAPS call when SPY down 25% from 52wk high
        if self.crash_call_in_trade:
            self._check_crash_call_exit(spy_price)
        elif self._crash_call_triggered(spy_price):
            self._try_enter_crash_call(data, spy_price, vix_level)

    # ── Core rebalance ─────────────────────────────────────────────────────────

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

    # ── History ────────────────────────────────────────────────────────────────

    def _update_histories(self, data: Slice):
        for ticker, sym in self.mag7_symbols.items():
            if data.Bars.ContainsKey(sym):
                self.price_windows[ticker].append(float(data.Bars[sym].Close))

        if data.Bars.ContainsKey(self.spy):
            bar      = data.Bars[self.spy]
            bar_date = bar.EndTime.date()
            if self.last_spy_date != bar_date:
                price = float(bar.Close)
                self.spy_window.append(price)
                self.spy_52wk.append(price)
                self.last_spy_date = bar_date

    # ── Momentum ranking ───────────────────────────────────────────────────────

    def _rank_mag7(self):
        ranked = []
        for ticker in self.MAG7_TICKERS:
            w = self.price_windows[ticker]
            if len(w) >= self.MOMENTUM_DAYS:
                ret = (w[-1] - w[-self.MOMENTUM_DAYS]) / w[-self.MOMENTUM_DAYS]
                ranked.append((ticker, ret))
        ranked.sort(key=lambda x: x[1], reverse=True)
        return [t for t, _ in ranked[:self.TOP_N]]

    def _rerank_and_rotate(self, data: Slice, vix_level: float):
        new_top = self._rank_mag7()
        if not new_top:
            return

        # Exit positions no longer in top N
        for ticker in list(self.leaps_positions.keys()):
            if ticker not in new_top:
                self._exit_leaps(ticker, "rotated_out")

        # Enter new top N if not already holding and VIX allows
        if vix_level < self.VIX_THRESHOLD and self.Time >= self.cooldown_until:
            for ticker in new_top:
                if ticker not in self.leaps_positions:
                    stock_price = self._get_price(self.mag7_symbols[ticker])
                    if stock_price > 0 and not self._near_earnings(ticker):
                        self._try_enter_leaps(ticker, data, stock_price, vix_level)

        self.current_top2 = new_top

    def _near_earnings(self, ticker: str) -> bool:
        # Earnings date check disabled — QC EarningReports.FileDate type incompatible
        return False

    # ── LEAPS entry ────────────────────────────────────────────────────────────

    def _try_enter_leaps(self, ticker: str, data: Slice, stock_price: float, vix_level: float):
        option_sym = self.mag7_option_symbols[ticker]
        chain = data.OptionChains.get(option_sym)
        if chain is None:
            return

        contract = self._select_call(chain, stock_price, vix_level)
        if contract is None:
            return

        mid = self._mid(contract)
        if mid <= 0:
            return

        # Budget = LEAPS sleeve / TOP_N per stock
        total  = float(self.Portfolio.TotalPortfolioValue)
        budget = (total * self.LEAPS_SLEEVE_MAX / self.TOP_N) - self._leaps_value_for(ticker)
        n = int(budget / (mid * 100))
        if n < 1:
            return

        self.MarketOrder(contract.Symbol, n)
        self.leaps_positions[ticker] = {
            "symbol":    contract.Symbol,
            "entry_px":  mid,
            "qty":       n,
            "first_hit": False,
        }

        dte   = (contract.Expiry.date() - self.Time.date()).days
        delta = self._get_delta(contract, stock_price, vix_level)
        mom   = (self.price_windows[ticker][-1] - self.price_windows[ticker][-self.MOMENTUM_DAYS]) \
                / self.price_windows[ticker][-self.MOMENTUM_DAYS] if len(self.price_windows[ticker]) >= self.MOMENTUM_DAYS else 0.0
        self.Log(f"LEAPS ENTRY [{ticker}] | {self.Time.date()} | Price={stock_price:.2f} Mom={mom:.1%} | "
                 f"Strike={contract.Strike:.2f} DTE={dte} Delta={delta:.2f} Mid=${mid:.2f} Qty={n}")

    # ── LEAPS exit ─────────────────────────────────────────────────────────────

    def _check_leaps_exit(self, ticker: str, stock_price: float, vix_level: float):
        pos = self.leaps_positions.get(ticker)
        if pos is None:
            return
        sym = pos["symbol"]
        if sym not in self.Securities:
            self._exit_leaps(ticker, "missing"); return

        mid = self._mid_from_security(self.Securities[sym])
        if mid <= 0:
            mid = self._bs_call(stock_price, vix_level / 100.0,
                                float(sym.ID.StrikePrice),
                                max((sym.ID.Date.date() - self.Time.date()).days, 0))

        entry_px = pos["entry_px"]

        # Stage 1: sell 50% at +100%
        if entry_px > 0 and not pos["first_hit"] and mid >= entry_px * (1 + self.FIRST_PROFIT_TARGET):
            qty  = self.Portfolio[sym].Quantity
            sell = int(qty * 0.5)
            if sell > 0:
                self.MarketOrder(sym, -sell)
                pos["first_hit"] = True
                self.Log(f"LEAPS PARTIAL [+100%] [{ticker}] | {self.Time.date()} | Sold {sell}")

        # Stage 2: sell rest at +150%
        if entry_px > 0 and pos["first_hit"] and mid >= entry_px * (1 + self.SECOND_PROFIT_TARGET):
            self._exit_leaps(ticker, "profit_150"); return

        # Crash rules (using SPY window as market proxy)
        for lb, thr in sorted(self.CRASH_RULES.items()):
            if len(self.spy_window) > lb:
                past = self.spy_window[-(lb + 1)]
                spy_price = self._get_price(self.spy)
                if past > 0 and spy_price > 0 and (spy_price - past) / past <= thr:
                    self._exit_leaps(ticker, f"crash_{lb}d"); return

        # Expiry
        if (sym.ID.Date.date() - self.Time.date()).days <= 1:
            self._exit_leaps(ticker, "expiry")

    def _exit_leaps(self, ticker: str, reason: str):
        pos = self.leaps_positions.get(ticker)
        if pos is None:
            return
        sym = pos["symbol"]
        if sym in self.Portfolio:
            qty = self.Portfolio[sym].Quantity
            if qty > 0:
                self.MarketOrder(sym, -qty)
        self.Log(f"LEAPS EXIT [{ticker}] [{reason}] | {self.Time.date()}")
        del self.leaps_positions[ticker]
        self.cooldown_until = self.Time + timedelta(days=self.COOLDOWN_DAYS)

    # ── Crash Recovery Call (SPY -25% from 52wk high) ─────────────────────────

    def _crash_call_triggered(self, spy_price: float) -> bool:
        """True when SPY is down >= 25% from its 52-week high."""
        if len(self.spy_52wk) < 30:
            return False
        peak = max(self.spy_52wk)
        return peak > 0 and (spy_price - peak) / peak <= -self.CRASH_CALL_THRESHOLD

    def _try_enter_crash_call(self, data: Slice, spy_price: float, vix_level: float):
        """Sell 15% SPY core, use proceeds to buy 300 DTE SPY LEAPS call."""
        chain = data.OptionChains.get(self.spy_opt)
        if chain is None:
            return

        contract = self._select_spy_call(chain, spy_price, vix_level)
        if contract is None:
            return

        mid = self._mid(contract)
        if mid <= 0:
            return

        # Trim SPY: reduce from 75% to 60% (sell 15% of portfolio)
        total  = float(self.Portfolio.TotalPortfolioValue)
        trim   = total * self.CRASH_CALL_SPY_TRIM
        spy_target_weight = self.CORE_SPY_WEIGHT - self.CRASH_CALL_SPY_TRIM
        self.SetHoldings(self.spy, spy_target_weight)

        # Buy SPY LEAPS call with the freed 15%
        budget = total * self.CRASH_CALL_SPY_TRIM
        n = int(budget / (mid * 100))
        if n < 1:
            self.Log(f"CRASH CALL | {self.Time.date()} | Not enough budget (need ${mid*100:.0f}, have ${budget:.0f})")
            return

        self.MarketOrder(contract.Symbol, n)

        dd = (spy_price - max(self.spy_52wk)) / max(self.spy_52wk)
        dte = (contract.Expiry.date() - self.Time.date()).days
        delta = self._get_delta(contract, spy_price, vix_level)
        self.crash_call_symbol   = contract.Symbol
        self.crash_call_entry_px = mid
        self.crash_call_qty      = n
        self.crash_call_in_trade = True

        self.Log(f"CRASH CALL ENTRY | {self.Time.date()} | SPY={spy_price:.2f} DD={dd:.1%} "
                 f"Strike={contract.Strike:.2f} DTE={dte} Delta={delta:.2f} Mid=${mid:.2f} Qty={n} "
                 f"Budget=${budget:.0f} SPY trimmed to {spy_target_weight:.0%}")

    def _check_crash_call_exit(self, spy_price: float):
        """Exit the crash recovery call at +100% profit or when SPY recovers."""
        sym = self.crash_call_symbol
        if sym not in self.Securities:
            self._exit_crash_call("missing"); return

        sec = self.Securities[sym]
        mid = self._mid_from_security(sec)
        if mid <= 0:
            mid = self._bs_call(spy_price, 0.20, float(sym.ID.StrikePrice),
                                max((sym.ID.Date.date() - self.Time.date()).days, 0))

        entry_px = self.crash_call_entry_px

        # Profit target: +100%
        if entry_px > 0 and mid >= entry_px * (1 + self.CRASH_CALL_PROFIT_TGT):
            self._exit_crash_call("profit_100"); return

        # Drawdown recovered above threshold — market healed, exit
        if len(self.spy_52wk) >= 30:
            peak = max(self.spy_52wk)
            dd   = (spy_price - peak) / peak if peak > 0 else 0
            if dd > -self.CRASH_CALL_RECOVER_DD:
                self._exit_crash_call("spy_recovered"); return

        # Expiry
        if (sym.ID.Date.date() - self.Time.date()).days <= 1:
            self._exit_crash_call("expiry")

    def _exit_crash_call(self, reason: str):
        sym = self.crash_call_symbol
        if sym and sym in self.Portfolio:
            qty = self.Portfolio[sym].Quantity
            if qty > 0:
                self.MarketOrder(sym, -qty)

        # Restore SPY back to full 75% core weight
        self.SetHoldings(self.spy, self.CORE_SPY_WEIGHT)
        self.Log(f"CRASH CALL EXIT [{reason}] | {self.Time.date()} | SPY core restored to {self.CORE_SPY_WEIGHT:.0%}")
        self._reset_crash_call_state()

    def _reset_crash_call_state(self):
        self.crash_call_in_trade = False
        self.crash_call_symbol   = None
        self.crash_call_entry_px = 0.0
        self.crash_call_qty      = 0

    def _select_spy_call(self, chain, spy_price: float, vix_level: float):
        """Select the SPY call nearest to 300 DTE and 0.40 delta."""
        target_exp = self.Time.date() + timedelta(days=self.CRASH_CALL_DTE)
        calls = [c for c in chain if c.Right == OptionRight.Call and c.AskPrice > 0 and c.BidPrice > 0]
        if not calls:
            return None
        near = [c for c in calls if abs((c.Expiry.date() - target_exp).days) <= 60]
        pool = near if near else calls
        return min(pool, key=lambda c: abs(self._get_delta(c, spy_price, vix_level) - self.CRASH_CALL_DELTA_TGT))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _leaps_value_for(self, ticker: str) -> float:
        pos = self.leaps_positions.get(ticker)
        if pos is None:
            return 0.0
        sym = pos["symbol"]
        return abs(float(self.Portfolio[sym].HoldingsValue)) if sym in self.Portfolio else 0.0

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

    def OnOrderEvent(self, orderEvent):
        if orderEvent.Status == OrderStatus.Filled:
            self.Log(f"Fill | {orderEvent.Symbol.Value} "
                     f"qty={orderEvent.FillQuantity:+.0f} @ ${orderEvent.FillPrice:.2f}")
