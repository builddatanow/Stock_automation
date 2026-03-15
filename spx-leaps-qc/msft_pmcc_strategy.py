# region imports
from AlgorithmImports import *
from datetime import timedelta, date, datetime
import math
# endregion

# ============================================================
# MSFT PMCC Strategy with Conditional Put Protection
#
# Structure:
#   Core     : SPY 75% + TLT 15%
#   LEAPS    : 10% in deep-ITM MSFT calls (delta ~0.70, 240-420 DTE)
#   Income   : Sell 30-DTE OTM calls (delta ~0.20, 21-35 DTE)
#   Hedge    : Buy OTM put (delta ~0.25, 45-60 DTE) when:
#              - VIX > 25, OR
#              - MSFT drops > 8% from its 20-day rolling high
#              Close put when VIX < 22 AND drawdown < 5%, or at 50% profit
#
# Filters:
#   - Skip short if earnings falls within the option's expiry window
#   - Pre-close short 2 days before earnings
#   - Skip short on FOMC meeting days and the day before
#
# Period: 2015-01-01 to 2026-02-28 | Start: $100,000
# ============================================================

class MsftPMCCStrategy(QCAlgorithm):

    # ── Core portfolio weights ──────────────────────────────
    CORE_SPY_WEIGHT  = 0.75
    CORE_TLT_WEIGHT  = 0.15
    LEAPS_BUDGET_PCT = 0.10      # % of portfolio for MSFT LEAPS

    # ── Long call (LEAPS) params ────────────────────────────
    LONG_CALL_TARGET_DTE_MIN = 240
    LONG_CALL_TARGET_DTE_MAX = 420
    LONG_CALL_TARGET_DELTA   = 0.70
    LONG_CALL_REPLACE_DTE    = 120   # roll when DTE falls below this
    MAX_SPREAD_PCT           = 0.20  # max bid/ask spread as % of mid

    # ── Short call (30-DTE) params ──────────────────────────
    SHORT_CALL_TARGET_DTE_MIN = 21
    SHORT_CALL_TARGET_DTE_MAX = 35
    SHORT_CALL_TARGET_DELTA   = 0.20
    SHORT_PROFIT_TAKE         = 0.50   # buy back at 50% profit
    SHORT_ROLL_DELTA          = 0.35   # roll if delta breaches this

    # ── Put protection params ───────────────────────────────
    PUT_TARGET_DTE_MIN    = 45
    PUT_TARGET_DTE_MAX    = 60
    PUT_TARGET_DELTA      = 0.25    # OTM put target delta
    PUT_PROFIT_TAKE       = 0.50    # close put at 50% gain
    VIX_HEDGE_ON          = 25      # buy put when VIX crosses above
    VIX_HEDGE_OFF         = 22      # close put when VIX drops below (AND drawdown clear)
    DRAWDOWN_HEDGE_ON     = 0.08    # buy put when MSFT drops 8% from 20-day high
    DRAWDOWN_HEDGE_OFF    = 0.05    # close put when drawdown recovers below 5%
    ROLLING_HIGH_DAYS     = 20      # lookback for drawdown trigger

    # ── Earnings filter ─────────────────────────────────────
    EARNINGS_PRECLOSE_DAYS = 2
    EARNINGS_WINDOW_BUFFER = 5

    # ── FOMC dates (skip selling on day-of and day-before) ──
    FOMC_DATES = {
        # 2015
        date(2015,1,28), date(2015,3,18), date(2015,4,29), date(2015,6,17),
        date(2015,7,29), date(2015,9,17), date(2015,10,28), date(2015,12,16),
        # 2016
        date(2016,1,27), date(2016,3,16), date(2016,4,27), date(2016,6,15),
        date(2016,7,27), date(2016,9,21), date(2016,11,2), date(2016,12,14),
        # 2017
        date(2017,2,1),  date(2017,3,15), date(2017,5,3),  date(2017,6,14),
        date(2017,7,26), date(2017,9,20), date(2017,11,1), date(2017,12,13),
        # 2018
        date(2018,1,31), date(2018,3,21), date(2018,5,2),  date(2018,6,13),
        date(2018,8,1),  date(2018,9,26), date(2018,11,8), date(2018,12,19),
        # 2019
        date(2019,1,30), date(2019,3,20), date(2019,5,1),  date(2019,6,19),
        date(2019,7,31), date(2019,9,18), date(2019,10,30), date(2019,12,11),
        # 2020
        date(2020,1,29), date(2020,3,3),  date(2020,3,15), date(2020,4,29),
        date(2020,6,10), date(2020,7,29), date(2020,9,16), date(2020,11,5),
        date(2020,12,16),
        # 2021
        date(2021,1,27), date(2021,3,17), date(2021,4,28), date(2021,6,16),
        date(2021,7,28), date(2021,9,22), date(2021,11,3), date(2021,12,15),
        # 2022
        date(2022,1,26), date(2022,3,16), date(2022,5,4),  date(2022,6,15),
        date(2022,7,27), date(2022,9,21), date(2022,11,2), date(2022,12,14),
        # 2023
        date(2023,2,1),  date(2023,3,22), date(2023,5,3),  date(2023,6,14),
        date(2023,7,26), date(2023,9,20), date(2023,11,1), date(2023,12,13),
        # 2024
        date(2024,1,31), date(2024,3,20), date(2024,5,1),  date(2024,6,12),
        date(2024,7,31), date(2024,9,18), date(2024,11,7), date(2024,12,18),
        # 2025
        date(2025,1,29), date(2025,3,19), date(2025,5,7),  date(2025,6,18),
        date(2025,7,30), date(2025,9,17), date(2025,10,29), date(2025,12,17),
        # 2026
        date(2026,1,28), date(2026,3,18), date(2026,4,29), date(2026,6,17),
    }

    # ── Risk / misc ─────────────────────────────────────────
    RF_RATE = 0.045

    # ────────────────────────────────────────────────────────
    def Initialize(self):
        self.SetStartDate(2015, 1, 1)
        self.SetEndDate(2026, 2, 28)
        self.SetCash(100_000)
        self.SetBenchmark("MSFT")

        # Core
        self.spy = self.AddEquity("SPY", Resolution.Daily).Symbol
        self.tlt = self.AddEquity("TLT", Resolution.Daily).Symbol
        self.vix = self.AddData(CBOE, "VIX", Resolution.Daily).Symbol

        # MSFT equity + options
        msft_eq = self.AddEquity("MSFT", Resolution.Daily)
        msft_eq.SetDataNormalizationMode(DataNormalizationMode.Adjusted)
        self.msft = msft_eq.Symbol

        msft_opt = self.AddOption("MSFT", Resolution.Daily)
        msft_opt.SetFilter(self._option_filter)
        self.msft_opt = msft_opt.Symbol

        # Rolling high for drawdown trigger
        self.msft_high_window = RollingWindow[float](self.ROLLING_HIGH_DAYS)

        # Position state
        self.long_call_symbol      = None
        self.long_call_entry_price = 0.0
        self.long_call_qty         = 0

        self.short_call_symbol       = None
        self.short_call_entry_credit = 0.0
        self.short_call_qty          = 0
        self.short_call_open_date    = None
        self.last_short_sale_date    = None

        self.put_symbol       = None
        self.put_entry_cost   = 0.0
        self.put_qty          = 0
        self.put_open_date    = None

        self.last_core_rebal = None

        self.SetWarmUp(self.ROLLING_HIGH_DAYS + 5)

        # Manage shorts and puts 30 min after open, Mon-Fri
        self.Schedule.On(
            self.DateRules.Every(
                DayOfWeek.Monday, DayOfWeek.Tuesday, DayOfWeek.Wednesday,
                DayOfWeek.Thursday, DayOfWeek.Friday),
            self.TimeRules.AfterMarketOpen("SPY", 30),
            self._scheduled_manage
        )

    def _option_filter(self, universe):
        return (
            universe
            .IncludeWeeklys()
            .Strikes(-20, 10)
            .Expiration(3, 420)
        )

    # ────────────────────────────────────────────────────────
    # Main loop
    # ────────────────────────────────────────────────────────
    def OnData(self, data: Slice):
        if self.IsWarmingUp:
            return

        # Track MSFT rolling high
        if data.Bars.ContainsKey(self.msft):
            self.msft_high_window.Add(float(data.Bars[self.msft].Close))

        # Core rebalance weekly
        if self.last_core_rebal is None or (self.Time.date() - self.last_core_rebal).days >= 7:
            self._rebalance_core()
            self.last_core_rebal = self.Time.date()

        spot = self._get_price(self.msft)
        vix  = self._get_price(self.vix)

        self._manage_long_call(data, spot, vix)
        self._manage_short_call(spot, vix)
        self._manage_put(spot, vix)

    # ────────────────────────────────────────────────────────
    # Scheduled: sell short call + buy/close put
    # ────────────────────────────────────────────────────────
    def _scheduled_manage(self):
        if self.IsWarmingUp:
            return

        spot = self._get_price(self.msft)
        vix  = self._get_price(self.vix)

        if spot <= 0:
            return

        # Short call entry
        if (self.long_call_symbol is not None
                and self.short_call_symbol is None
                and self.last_short_sale_date != self.Time.date()):
            self._sell_short_call(spot, vix)

        # Put protection entry
        if self.long_call_symbol is not None and self.put_symbol is None:
            if self._hedge_needed(spot, vix):
                self._buy_put(spot, vix)

    # ────────────────────────────────────────────────────────
    # Core rebalance
    # ────────────────────────────────────────────────────────
    def _rebalance_core(self):
        self.SetHoldings(self.spy, self.CORE_SPY_WEIGHT)
        self.SetHoldings(self.tlt, self.CORE_TLT_WEIGHT)

    # ────────────────────────────────────────────────────────
    # Long call (LEAPS) management
    # ────────────────────────────────────────────────────────
    def _manage_long_call(self, data, spot, vix):
        if self._has_long_call():
            dte = (self.long_call_symbol.ID.Date.date() - self.Time.date()).days
            if dte <= self.LONG_CALL_REPLACE_DTE and not self._has_short_call():
                self.Log(f"[MSFT] Roll LEAPS DTE={dte}")
                self._close_long_call("replace_dte")
                self._open_long_call(data, spot, vix)
            return
        self._open_long_call(data, spot, vix)

    def _open_long_call(self, data, spot, vix):
        chain = data.OptionChains.get(self.msft_opt)
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
            if mid <= 0 or not self._spread_ok(c, mid):
                continue
            delta = self._get_delta(c, spot, vix)
            candidates.append((abs(delta - self.LONG_CALL_TARGET_DELTA), c, delta, mid, dte))

        if not candidates:
            self.Log("[MSFT] No valid LEAPS candidate")
            return

        candidates.sort(key=lambda x: (x[0], abs(x[4] - 300)))
        _, contract, delta, mid, dte = candidates[0]

        budget = self.Portfolio.TotalPortfolioValue * self.LEAPS_BUDGET_PCT
        qty    = max(int(budget / (mid * 100)), 1)

        self.MarketOrder(contract.Symbol, qty)
        self.long_call_symbol      = contract.Symbol
        self.long_call_entry_price = mid
        self.long_call_qty         = qty

        self.Log(
            f"[MSFT] LEAPS ENTRY | {self.Time.date()} | "
            f"Strike={contract.Strike:.2f} DTE={dte} Delta={delta:.2f} Mid={mid:.2f} Qty={qty}"
        )

    def _close_long_call(self, reason):
        if self.long_call_symbol and self.long_call_symbol in self.Portfolio:
            qty = self.Portfolio[self.long_call_symbol].Quantity
            if qty > 0:
                self.MarketOrder(self.long_call_symbol, -qty)
        self.Log(f"[MSFT] LEAPS EXIT [{reason}] | {self.Time.date()}")
        self.long_call_symbol      = None
        self.long_call_entry_price = 0.0
        self.long_call_qty         = 0

    # ────────────────────────────────────────────────────────
    # Short call (30-DTE) management
    # ────────────────────────────────────────────────────────
    def _sell_short_call(self, spot, vix):
        if not self._has_long_call():
            return

        # FOMC filter
        if self._is_fomc_nearby():
            self.Log(f"[MSFT] Skip short: FOMC nearby")
            return

        long_strike = float(self.long_call_symbol.ID.StrikePrice)

        contracts = self.OptionChainProvider.GetOptionContractList(self.msft, self.Time)
        if not contracts:
            return

        short_candidates = []
        for symbol in contracts:
            sid         = symbol.ID
            dte         = (sid.Date.date() - self.Time.date()).days
            expiry_date = sid.Date.date()

            if sid.OptionRight != OptionRight.Call:
                continue
            if dte < self.SHORT_CALL_TARGET_DTE_MIN or dte > self.SHORT_CALL_TARGET_DTE_MAX:
                continue
            if float(sid.StrikePrice) <= long_strike:
                continue
            if self._earnings_in_window(expiry_date):
                self.Log(f"[MSFT] Skip short: earnings in window Exp={expiry_date}")
                continue
            if self._fomc_in_window(expiry_date):
                self.Log(f"[MSFT] Skip short: FOMC in window Exp={expiry_date}")
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
            self.Log("[MSFT] No valid 30-DTE short call")
            return

        short_candidates.sort(key=lambda x: (x[0], x[4]))
        _, sym, delta, mid, dte = short_candidates[0]

        qty = min(self.long_call_qty, 1)
        if qty < 1:
            return

        self.MarketOrder(sym, -qty)
        self.short_call_symbol       = sym
        self.short_call_entry_credit = mid
        self.short_call_qty          = qty
        self.short_call_open_date    = self.Time.date()
        self.last_short_sale_date    = self.Time.date()

        self.Log(
            f"[MSFT] SHORT ENTRY | {self.Time.date()} | "
            f"Strike={float(sym.ID.StrikePrice):.2f} DTE={dte} Delta={delta:.2f} Credit={mid:.2f}"
        )

    def _manage_short_call(self, spot, vix):
        if not self._has_short_call():
            return

        sym = self.short_call_symbol
        sec = self.Securities[sym]

        # Pre-earnings close
        if self._earnings_within_days(self.EARNINGS_PRECLOSE_DAYS):
            ed = self._next_earnings_date()
            self.Log(f"[MSFT] Pre-earnings short close (earnings={ed})")
            self._close_short_call("pre_earnings")
            return

        current_mid = self._mid_from_security(sec)
        if current_mid <= 0:
            current_mid = self._bs_call(
                spot, max(vix / 100.0, 0.15),
                float(sym.ID.StrikePrice),
                max((sym.ID.Date.date() - self.Time.date()).days, 0)
            )

        entry_credit = self.short_call_entry_credit
        dte          = (sym.ID.Date.date() - self.Time.date()).days

        if entry_credit > 0 and current_mid <= entry_credit * (1 - self.SHORT_PROFIT_TAKE):
            self._close_short_call("take_profit")
            return

        delta  = self._security_delta(sec, spot, vix)
        is_itm = spot >= float(sym.ID.StrikePrice)

        if delta >= self.SHORT_ROLL_DELTA or is_itm:
            self._close_short_call("roll_threatened")
            self._sell_short_call(spot, vix)
            return

        if dte <= 0:
            self._close_short_call("expiry")

    def _close_short_call(self, reason):
        sym = self.short_call_symbol
        if sym and sym in self.Portfolio:
            qty = self.Portfolio[sym].Quantity
            if qty < 0:
                self.MarketOrder(sym, -qty)
        self.Log(f"[MSFT] SHORT EXIT [{reason}] | {self.Time.date()}")
        self.short_call_symbol       = None
        self.short_call_entry_credit = 0.0
        self.short_call_qty          = 0
        self.short_call_open_date    = None

    # ────────────────────────────────────────────────────────
    # Put protection management
    # ────────────────────────────────────────────────────────
    def _hedge_needed(self, spot, vix):
        """True if VIX is elevated OR MSFT is in significant drawdown."""
        if vix > self.VIX_HEDGE_ON:
            return True
        if self.msft_high_window.IsReady:
            peak = max([self.msft_high_window[i] for i in range(self.msft_high_window.Count)])
            if peak > 0 and (peak - spot) / peak >= self.DRAWDOWN_HEDGE_ON:
                return True
        return False

    def _hedge_clear(self, spot, vix):
        """True when both VIX and drawdown have recovered — safe to close put."""
        if vix > self.VIX_HEDGE_OFF:
            return False
        if self.msft_high_window.IsReady:
            peak = max([self.msft_high_window[i] for i in range(self.msft_high_window.Count)])
            if peak > 0 and (peak - spot) / peak >= self.DRAWDOWN_HEDGE_OFF:
                return False
        return True

    def _buy_put(self, spot, vix):
        contracts = self.OptionChainProvider.GetOptionContractList(self.msft, self.Time)
        if not contracts:
            return

        put_candidates = []
        for symbol in contracts:
            sid = symbol.ID
            if sid.OptionRight != OptionRight.Put:
                continue
            dte = (sid.Date.date() - self.Time.date()).days
            if dte < self.PUT_TARGET_DTE_MIN or dte > self.PUT_TARGET_DTE_MAX:
                continue

            if symbol not in self.Securities:
                self.AddOptionContract(symbol, Resolution.Daily)

            sec = self.Securities[symbol]
            bid = float(sec.BidPrice) if sec.BidPrice else 0.0
            ask = float(sec.AskPrice) if sec.AskPrice else 0.0
            mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else float(sec.Price or 0)

            if mid <= 0 or ask <= 0:
                continue
            spread_pct = (ask - bid) / mid if mid > 0 else 999
            if spread_pct > self.MAX_SPREAD_PCT:
                continue

            delta = abs(self._security_delta(sec, spot, vix))
            put_candidates.append((abs(delta - self.PUT_TARGET_DELTA), symbol, delta, mid, dte))

        if not put_candidates:
            self.Log("[MSFT] No valid put candidate")
            return

        put_candidates.sort(key=lambda x: (x[0], x[4]))
        _, sym, delta, mid, dte = put_candidates[0]

        qty = max(self.long_call_qty, 1)
        self.MarketOrder(sym, qty)

        self.put_symbol     = sym
        self.put_entry_cost = mid
        self.put_qty        = qty
        self.put_open_date  = self.Time.date()

        vix_val = self._get_price(self.vix)
        self.Log(
            f"[MSFT] PUT HEDGE ENTRY | {self.Time.date()} | "
            f"Strike={float(sym.ID.StrikePrice):.2f} DTE={dte} Delta={delta:.2f} "
            f"Cost={mid:.2f} VIX={vix_val:.1f}"
        )

    def _manage_put(self, spot, vix):
        if not self._has_put():
            return

        sym = self.put_symbol
        sec = self.Securities[sym]
        dte = (sym.ID.Date.date() - self.Time.date()).days

        current_mid = self._mid_from_security(sec)
        if current_mid <= 0:
            current_mid = 0.0

        entry_cost = self.put_entry_cost

        # Take profit at 50%
        if entry_cost > 0 and current_mid >= entry_cost * (1 + self.PUT_PROFIT_TAKE):
            self._close_put("take_profit")
            return

        # Close if hedge no longer needed
        if self._hedge_clear(spot, vix):
            self._close_put("hedge_clear")
            return

        # Close at expiry
        if dte <= 0:
            self._close_put("expiry")

    def _close_put(self, reason):
        sym = self.put_symbol
        if sym and sym in self.Portfolio:
            qty = self.Portfolio[sym].Quantity
            if qty > 0:
                self.MarketOrder(sym, -qty)
        self.Log(f"[MSFT] PUT EXIT [{reason}] | {self.Time.date()}")
        self.put_symbol     = None
        self.put_entry_cost = 0.0
        self.put_qty        = 0
        self.put_open_date  = None

    # ────────────────────────────────────────────────────────
    # Earnings filter helpers
    # ────────────────────────────────────────────────────────
    def _next_earnings_date(self):
        try:
            fund = self.Securities[self.msft].Fundamentals
            if fund is None:
                return None
            fd_raw = fund.FinancialStatements.FileDate.ThreeMonths
            if fd_raw is None:
                return None
            fd_date = None
            try:
                fd_date = date(int(fd_raw.Year), int(fd_raw.Month), int(fd_raw.Day))
            except AttributeError:
                pass
            if fd_date is None:
                try:
                    fd_date = fd_raw.date()
                except:
                    pass
            if fd_date is None:
                try:
                    fd_date = datetime.strptime(str(fd_raw)[:10], "%Y-%m-%d").date()
                except:
                    pass
            if fd_date is None:
                return None
            if (self.Time.date() - fd_date).days > 150:
                return None
            return fd_date + timedelta(days=77)
        except Exception as e:
            self.Log(f"[MSFT] earnings_date error: {e}")
        return None

    def _earnings_in_window(self, expiry_date):
        ed = self._next_earnings_date()
        if ed is None:
            return False
        ed_start = ed - timedelta(days=self.EARNINGS_WINDOW_BUFFER)
        ed_end   = ed + timedelta(days=self.EARNINGS_WINDOW_BUFFER)
        return ed_start <= expiry_date and ed_end >= self.Time.date()

    def _earnings_within_days(self, days):
        ed = self._next_earnings_date()
        if ed is None:
            return False
        diff = (ed - self.Time.date()).days
        return -self.EARNINGS_WINDOW_BUFFER <= diff <= days + self.EARNINGS_WINDOW_BUFFER

    # ────────────────────────────────────────────────────────
    # FOMC filter helpers
    # ────────────────────────────────────────────────────────
    def _is_fomc_nearby(self):
        today = self.Time.date()
        for fd in self.FOMC_DATES:
            if (fd - today).days in (0, 1):
                return True
        return False

    def _fomc_in_window(self, expiry_date):
        today = self.Time.date()
        for fd in self.FOMC_DATES:
            if today <= fd <= expiry_date:
                return True
        return False

    # ────────────────────────────────────────────────────────
    # State helpers
    # ────────────────────────────────────────────────────────
    def _has_long_call(self):
        return (self.long_call_symbol is not None
                and self.long_call_symbol in self.Portfolio
                and self.Portfolio[self.long_call_symbol].Quantity > 0)

    def _has_short_call(self):
        return (self.short_call_symbol is not None
                and self.short_call_symbol in self.Portfolio
                and self.Portfolio[self.short_call_symbol].Quantity < 0)

    def _has_put(self):
        return (self.put_symbol is not None
                and self.put_symbol in self.Portfolio
                and self.Portfolio[self.put_symbol].Quantity > 0)

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
        bid  = float(security.BidPrice) if security.BidPrice else 0.0
        ask  = float(security.AskPrice) if security.AskPrice else 0.0
        last = float(security.Price)    if security.Price    else 0.0
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
            price, max(vix / 100.0, 0.15),
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
