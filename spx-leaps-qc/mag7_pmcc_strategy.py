# region imports
from AlgorithmImports import *
from datetime import timedelta
import math
# endregion

# ============================================================
# Mag7 PMCC Strategy
# Poor Man's Covered Call on top 2 Mag7 stocks by momentum
#
# Structure:
#   Core     : SPY 75% + TLT 15%
#   LEAPS    : 10% sleeve split across top 2 Mag7 stocks
#              Buy deep ITM calls (delta ~0.70, 240-420 DTE)
#   Income   : Sell weekly OTM calls (delta ~0.20, 5-10 DTE)
#              on top of each LEAPS position
#
# Monthly rebalance: rotate into new top-2 if momentum changes
# Period: 2010-01-01 to 2026-02-28 | Start: $100,000
# ============================================================

class Mag7PMCCStrategy(QCAlgorithm):

    # ── Core portfolio weights ──────────────────────────────
    CORE_SPY_WEIGHT = 0.75
    CORE_TLT_WEIGHT = 0.15
    LEAPS_SLEEVE_MAX = 0.10      # total LEAPS budget, split across TOP_N stocks

    # ── Mag7 universe ───────────────────────────────────────
    MAG7_TICKERS = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA"]
    TOP_N        = 2
    MOMENTUM_DAYS  = 63          # 3-month momentum lookback
    RERANK_DAYS    = 21          # re-rank every ~1 month

    # ── Long call (LEAPS) params ────────────────────────────
    LONG_CALL_TARGET_DTE_MIN = 240
    LONG_CALL_TARGET_DTE_MAX = 420
    LONG_CALL_TARGET_DELTA   = 0.70
    LONG_CALL_REPLACE_DTE    = 120   # roll when DTE falls below this
    MAX_SPREAD_PCT           = 0.20  # max bid/ask spread as % of mid

    # ── Short call (weekly) params ──────────────────────────
    SHORT_CALL_TARGET_DTE_MIN = 5
    SHORT_CALL_TARGET_DTE_MAX = 14
    SHORT_CALL_TARGET_DELTA   = 0.20
    SHORT_PROFIT_TAKE         = 0.50  # buy back at 50% profit
    SHORT_ROLL_DELTA          = 0.35  # roll if delta breaches this

    # ── Risk / misc ─────────────────────────────────────────
    VIX_FILTER_ENABLED = False
    VIX_MAX_FOR_ENTRY  = 28
    RF_RATE            = 0.045
    COOLDOWN_DAYS      = 5

    # ────────────────────────────────────────────────────────
    def Initialize(self):
        self.SetStartDate(2010, 1, 1)
        self.SetEndDate(2026, 2, 28)
        self.SetCash(100_000)
        self.SetBenchmark("QQQ")

        # Core holdings
        self.spy = self.AddEquity("SPY", Resolution.Daily).Symbol
        self.tlt = self.AddEquity("TLT", Resolution.Daily).Symbol
        self.vix = self.AddData(CBOE, "VIX", Resolution.Daily).Symbol

        # Mag7 equities + options
        self.mag7_equity  = {}   # ticker -> equity Symbol
        self.mag7_opt_sym = {}   # ticker -> option root Symbol
        for ticker in self.MAG7_TICKERS:
            eq  = self.AddEquity(ticker, Resolution.Daily)
            eq.SetDataNormalizationMode(DataNormalizationMode.Adjusted)
            opt = self.AddOption(ticker, Resolution.Daily)
            opt.SetFilter(self._option_filter)
            self.mag7_equity[ticker]  = eq.Symbol
            self.mag7_opt_sym[ticker] = opt.Symbol

        # Momentum history: ticker -> deque of daily closes
        self.mom_history = {t: RollingWindow[float](self.MOMENTUM_DAYS + 5)
                            for t in self.MAG7_TICKERS}

        # Per-stock PMCC state
        # Each entry: long_call_symbol, long_call_entry_price, long_call_qty,
        #             short_call_symbol, short_call_entry_credit, short_call_qty,
        #             short_call_open_date, last_short_sale_date
        self.stock_state = {t: self._empty_state() for t in self.MAG7_TICKERS}

        self.top_stocks       = []
        self.last_rerank_date = None
        self.last_core_rebal  = None

        self.SetWarmUp(self.MOMENTUM_DAYS + 10)

        # Sell weekly shorts 30 min after open, Mon-Fri
        self.Schedule.On(
            self.DateRules.Every(
                DayOfWeek.Monday, DayOfWeek.Tuesday, DayOfWeek.Wednesday,
                DayOfWeek.Thursday, DayOfWeek.Friday),
            self.TimeRules.AfterMarketOpen("SPY", 30),
            self._scheduled_manage_shorts
        )

    def _empty_state(self):
        return {
            "long_call_symbol":      None,
            "long_call_entry_price": 0.0,
            "long_call_qty":         0,
            "short_call_symbol":     None,
            "short_call_entry_credit": 0.0,
            "short_call_qty":        0,
            "short_call_open_date":  None,
            "last_short_sale_date":  None,
        }

    def _option_filter(self, universe):
        return (
            universe
            .IncludeWeeklys()
            .Strikes(-40, 40)
            .Expiration(3, 450)
        )

    # ────────────────────────────────────────────────────────
    # Main loop
    # ────────────────────────────────────────────────────────
    def OnData(self, data: Slice):
        if self.IsWarmingUp:
            return

        # Update momentum history
        for ticker in self.MAG7_TICKERS:
            sym = self.mag7_equity[ticker]
            if data.Bars.ContainsKey(sym):
                self.mom_history[ticker].Add(float(data.Bars[sym].Close))

        # Monthly rerank
        if self._should_rerank():
            self._rerank_and_rotate(data)
            self.last_rerank_date = self.Time.date()

        # Core rebalance (weekly)
        if self.last_core_rebal is None or (self.Time.date() - self.last_core_rebal).days >= 7:
            self._rebalance_core()
            self.last_core_rebal = self.Time.date()

        vix = self._get_price(self.vix)

        # For each active stock, manage long call and short call
        for ticker in self.top_stocks:
            spot = self._get_price(self.mag7_equity[ticker])
            if spot <= 0:
                continue

            self._manage_long_call(ticker, data, spot, vix)
            self._manage_short_call(ticker, spot, vix)

    # ────────────────────────────────────────────────────────
    # Momentum ranking and rotation
    # ────────────────────────────────────────────────────────
    def _should_rerank(self):
        if self.last_rerank_date is None:
            return True
        return (self.Time.date() - self.last_rerank_date).days >= self.RERANK_DAYS

    def _rerank_and_rotate(self, data):
        scores = {}
        for ticker in self.MAG7_TICKERS:
            hist = self.mom_history[ticker]
            if hist.Count < self.MOMENTUM_DAYS:
                continue
            oldest = hist[hist.Count - 1]
            newest = hist[0]
            if oldest > 0:
                scores[ticker] = (newest - oldest) / oldest

        if not scores:
            return

        ranked = sorted(scores, key=lambda t: scores[t], reverse=True)
        new_top = ranked[:self.TOP_N]

        # Exit positions in stocks dropping out of top N
        for ticker in self.top_stocks:
            if ticker not in new_top:
                self.Log(f"ROTATE OUT: {ticker} (score={scores.get(ticker, 'n/a'):.3f})")
                self._close_all_for_ticker(ticker)

        self.top_stocks = new_top
        self.Log(f"TOP {self.TOP_N}: {self.top_stocks} | scores: {[f'{t}={scores.get(t,0):.3f}' for t in new_top]}")

    def _close_all_for_ticker(self, ticker):
        st = self.stock_state[ticker]
        if self._has_live_short_call(ticker):
            self._close_short_call(ticker, "rotate_out")
        if self._has_live_long_call(ticker):
            self._close_long_call(ticker, "rotate_out")
        self.stock_state[ticker] = self._empty_state()

    # ────────────────────────────────────────────────────────
    # Core portfolio rebalance
    # ────────────────────────────────────────────────────────
    def _rebalance_core(self):
        self.SetHoldings(self.spy, self.CORE_SPY_WEIGHT)
        self.SetHoldings(self.tlt, self.CORE_TLT_WEIGHT)

    # ────────────────────────────────────────────────────────
    # Long call (LEAPS) management
    # ────────────────────────────────────────────────────────
    def _manage_long_call(self, ticker, data, spot, vix):
        if self._has_live_long_call(ticker):
            st = self.stock_state[ticker]
            dte = (st["long_call_symbol"].ID.Date.date() - self.Time.date()).days
            if dte <= self.LONG_CALL_REPLACE_DTE and not self._has_live_short_call(ticker):
                self.Log(f"[{ticker}] Replacing long call DTE={dte}")
                self._close_long_call(ticker, "replace_dte")
                self._open_long_call(ticker, data, spot, vix)
            return

        if self.VIX_FILTER_ENABLED and vix > self.VIX_MAX_FOR_ENTRY:
            return

        self._open_long_call(ticker, data, spot, vix)

    def _open_long_call(self, ticker, data, spot, vix):
        opt_sym = self.mag7_opt_sym[ticker]
        chain = data.OptionChains.get(opt_sym)
        if chain is None:
            return

        candidates = []
        for c in chain:
            if c.Right != OptionRight.Call:
                continue
            dte = (c.Expiry.date() - self.Time.date()).days
            if dte < self.LONG_CALL_TARGET_DTE_MIN or dte > self.LONG_CALL_TARGET_DTE_MAX:
                continue
            mid = self._mid(c)
            if mid <= 0:
                continue
            if not self._spread_ok(c, mid):
                continue
            delta = self._get_delta(c, spot, vix)
            candidates.append((abs(delta - self.LONG_CALL_TARGET_DELTA), c, delta, mid, dte))

        if not candidates:
            self.Log(f"[{ticker}] No valid long call candidate")
            return

        candidates.sort(key=lambda x: (x[0], abs(x[4] - 300)))
        _, contract, delta, mid, dte = candidates[0]

        budget = self.Portfolio.TotalPortfolioValue * self.LEAPS_SLEEVE_MAX / max(len(self.top_stocks), 1)
        qty = max(int(budget / (mid * 100)), 1)

        self.MarketOrder(contract.Symbol, qty)

        st = self.stock_state[ticker]
        st["long_call_symbol"]      = contract.Symbol
        st["long_call_entry_price"] = mid
        st["long_call_qty"]         = qty

        self.Log(
            f"[{ticker}] LONG CALL ENTRY | {self.Time.date()} | "
            f"Strike={contract.Strike:.2f} DTE={dte} Delta={delta:.2f} Mid={mid:.2f} Qty={qty}"
        )

    def _close_long_call(self, ticker, reason):
        st = self.stock_state[ticker]
        sym = st["long_call_symbol"]
        if sym and sym in self.Portfolio:
            qty = self.Portfolio[sym].Quantity
            if qty > 0:
                self.MarketOrder(sym, -qty)
        self.Log(f"[{ticker}] LONG CALL EXIT [{reason}] | {self.Time.date()}")
        st["long_call_symbol"]      = None
        st["long_call_entry_price"] = 0.0
        st["long_call_qty"]         = 0

    # ────────────────────────────────────────────────────────
    # Short call (weekly) management
    # ────────────────────────────────────────────────────────
    def _scheduled_manage_shorts(self):
        if self.IsWarmingUp:
            return

        vix = self._get_price(self.vix)

        for ticker in self.top_stocks:
            spot = self._get_price(self.mag7_equity[ticker])
            if spot <= 0:
                continue
            if not self._has_live_long_call(ticker):
                continue
            if self._has_live_short_call(ticker):
                continue
            st = self.stock_state[ticker]
            if st["last_short_sale_date"] == self.Time.date():
                continue
            if self.VIX_FILTER_ENABLED and vix > self.VIX_MAX_FOR_ENTRY:
                self.Log(f"[{ticker}] Skip short: VIX={vix:.2f}")
                continue
            self._sell_weekly_call(ticker, spot, vix)

    def _sell_weekly_call(self, ticker, spot, vix):
        if not self._has_live_long_call(ticker):
            return

        st = self.stock_state[ticker]
        long_strike = float(st["long_call_symbol"].ID.StrikePrice)

        contracts = self.OptionChainProvider.GetOptionContractList(
            self.mag7_equity[ticker], self.Time)
        if not contracts:
            return

        short_candidates = []
        for symbol in contracts:
            sid = symbol.ID
            if sid.OptionRight != OptionRight.Call:
                continue
            dte = (sid.Date.date() - self.Time.date()).days
            if dte < self.SHORT_CALL_TARGET_DTE_MIN or dte > self.SHORT_CALL_TARGET_DTE_MAX:
                continue
            # Short strike must be above long strike
            if float(sid.StrikePrice) <= long_strike:
                continue

            if symbol not in self.Securities:
                self.AddOptionContract(symbol, Resolution.Daily)

            sec = self.Securities[symbol]
            bid = float(sec.BidPrice) if sec.BidPrice else 0.0
            ask = float(sec.AskPrice) if sec.AskPrice else 0.0
            mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else float(sec.Price or 0)

            if mid <= 0 or bid <= 0:
                continue

            spread_pct = (ask - bid) / mid if mid > 0 else 999
            if spread_pct > self.MAX_SPREAD_PCT:
                continue

            delta = self._security_delta(sec, spot, vix)
            short_candidates.append((abs(delta - self.SHORT_CALL_TARGET_DELTA), symbol, delta, mid, dte))

        if not short_candidates:
            self.Log(f"[{ticker}] No valid short weekly call candidate")
            return

        short_candidates.sort(key=lambda x: (x[0], x[4]))
        _, sym, delta, mid, dte = short_candidates[0]

        qty = min(st["long_call_qty"], 1)
        if qty < 1:
            return

        self.MarketOrder(sym, -qty)

        st["short_call_symbol"]       = sym
        st["short_call_entry_credit"] = mid
        st["short_call_qty"]          = qty
        st["short_call_open_date"]    = self.Time.date()
        st["last_short_sale_date"]    = self.Time.date()

        self.Log(
            f"[{ticker}] SHORT CALL ENTRY | {self.Time.date()} | "
            f"Strike={float(sym.ID.StrikePrice):.2f} DTE={dte} Delta={delta:.2f} Credit={mid:.2f}"
        )

    def _manage_short_call(self, ticker, spot, vix):
        if not self._has_live_short_call(ticker):
            return

        st = self.stock_state[ticker]
        sym = st["short_call_symbol"]
        sec = self.Securities[sym]
        current_mid = self._mid_from_security(sec)

        if current_mid <= 0:
            current_mid = self._bs_call(
                spot,
                max(vix / 100.0, 0.15),
                float(sym.ID.StrikePrice),
                max((sym.ID.Date.date() - self.Time.date()).days, 0)
            )

        entry_credit = st["short_call_entry_credit"]
        dte  = (sym.ID.Date.date() - self.Time.date()).days

        # Take profit at 50%
        if entry_credit > 0 and current_mid <= entry_credit * (1 - self.SHORT_PROFIT_TAKE):
            self._close_short_call(ticker, "take_profit")
            return

        # Roll if threatened (delta too high or ITM)
        delta = self._security_delta(sec, spot, vix)
        is_itm = spot >= float(sym.ID.StrikePrice)

        if delta >= self.SHORT_ROLL_DELTA or is_itm:
            self._close_short_call(ticker, "roll_threatened")
            self._sell_weekly_call(ticker, spot, vix)
            return

        # Close at expiry
        if dte <= 0:
            self._close_short_call(ticker, "expiry")

    def _close_short_call(self, ticker, reason):
        st = self.stock_state[ticker]
        sym = st["short_call_symbol"]
        if sym and sym in self.Portfolio:
            qty = self.Portfolio[sym].Quantity
            if qty < 0:
                self.MarketOrder(sym, -qty)
        self.Log(f"[{ticker}] SHORT CALL EXIT [{reason}] | {self.Time.date()}")
        st["short_call_symbol"]       = None
        st["short_call_entry_credit"] = 0.0
        st["short_call_qty"]          = 0
        st["short_call_open_date"]    = None

    # ────────────────────────────────────────────────────────
    # State helpers
    # ────────────────────────────────────────────────────────
    def _has_live_long_call(self, ticker):
        st = self.stock_state[ticker]
        sym = st["long_call_symbol"]
        return (sym is not None
                and sym in self.Portfolio
                and self.Portfolio[sym].Quantity > 0)

    def _has_live_short_call(self, ticker):
        st = self.stock_state[ticker]
        sym = st["short_call_symbol"]
        return (sym is not None
                and sym in self.Portfolio
                and self.Portfolio[sym].Quantity < 0)

    def _get_price(self, symbol):
        if symbol not in self.Securities:
            return 0.0
        sec = self.Securities[symbol]
        return float(sec.Price) if sec.Price and sec.Price > 0 else 0.0

    def _mid(self, contract):
        bid = float(contract.BidPrice) if contract.BidPrice else 0.0
        ask = float(contract.AskPrice) if contract.AskPrice else 0.0
        if bid > 0 and ask > 0:
            return (bid + ask) / 2.0
        return float(contract.LastPrice) if contract.LastPrice else 0.0

    def _mid_from_security(self, security):
        bid = float(security.BidPrice) if security.BidPrice else 0.0
        ask = float(security.AskPrice) if security.AskPrice else 0.0
        last = float(security.Price) if security.Price else 0.0
        return (bid + ask) / 2.0 if bid > 0 and ask > 0 else last

    def _spread_ok(self, contract, mid):
        bid = float(contract.BidPrice) if contract.BidPrice else 0.0
        ask = float(contract.AskPrice) if contract.AskPrice else 0.0
        if bid <= 0 or ask <= 0 or mid <= 0:
            return False
        return ((ask - bid) / mid) <= self.MAX_SPREAD_PCT

    def _get_delta(self, contract, price, vix):
        if contract.Greeks and contract.Greeks.Delta is not None and abs(contract.Greeks.Delta) > 0:
            return abs(float(contract.Greeks.Delta))
        return self._bs_delta(
            price,
            max(vix / 100.0, 0.15),
            float(contract.Strike),
            (contract.Expiry.date() - self.Time.date()).days
        )

    def _security_delta(self, security, price, vix):
        try:
            if security.Symbol.SecurityType == SecurityType.Option:
                strike = float(security.Symbol.ID.StrikePrice)
                dte    = (security.Symbol.ID.Date.date() - self.Time.date()).days
                return self._bs_delta(price, max(vix / 100.0, 0.15), strike, dte)
        except:
            pass
        return 0.0

    def _bs_delta(self, S, sigma, K, T_days):
        if S <= 0 or K <= 0:
            return 0.0
        T     = max(T_days / 365.0, 1e-6)
        sigma = max(sigma, 1e-6)
        d1    = (math.log(S / K) + (self.RF_RATE + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
        return 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))

    def _bs_call(self, S, sigma, K, T_days):
        if S <= 0 or K <= 0:
            return 0.0
        T     = max(T_days / 365.0, 1e-6)
        sigma = max(sigma, 1e-6)
        d1    = (math.log(S / K) + (self.RF_RATE + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
        d2    = d1 - sigma * math.sqrt(T)
        nd1   = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
        nd2   = 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))
        return S * nd1 - K * math.exp(-self.RF_RATE * T) * nd2

    def OnOrderEvent(self, orderEvent):
        if orderEvent.Status == OrderStatus.Filled:
            self.Log(
                f"FILL | {self.Time} | {orderEvent.Symbol.Value} | "
                f"qty={orderEvent.FillQuantity:+.0f} @ {orderEvent.FillPrice:.2f}"
            )
