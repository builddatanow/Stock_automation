# region imports
from AlgorithmImports import *
from datetime import timedelta, date, datetime
import math
# endregion

# ============================================================
# MSFT PMCC Strategy with Conditional Put Protection
#
# Structure:
#   Core       : SPY 75% + TLT 15%
#   LEAPS      : 15% in deep-ITM MSFT calls (delta ~0.70, 360-540 DTE / ~450 DTE)
#   Extra Call : If MSFT drops 25% from LEAPS entry spot → buy 1 extra 600-DTE call
#   Income     : Sell 30-DTE OTM calls (delta ~0.20, 21-35 DTE)
#   Hedge      : Buy OTM put (delta ~0.25, 45-60 DTE) when:
#                - VIX > 25, OR
#                - MSFT drops > 8% from 20-day rolling high
#                Close when VIX < 22 AND drawdown < 5%, or at 50% profit
#
# LEAPS profit-taking:
#   - Close 50% of position when LEAPS gains 100% (2x entry price)
#   - Close remaining 50% when LEAPS gains 150% (2.5x entry price)
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
    LEAPS_BUDGET_PCT = 0.15      # 15% risk per trade

    # ── Long call (LEAPS) params — target 450 DTE ───────────
    LONG_CALL_TARGET_DTE_MIN  = 360
    LONG_CALL_TARGET_DTE_MAX  = 540
    LONG_CALL_TARGET_DELTA    = 0.70
    LONG_CALL_REPLACE_DTE     = 180  # roll when DTE falls below this
    MAX_SPREAD_PCT            = 0.20

    # ── LEAPS profit-take levels ────────────────────────────
    LEAPS_TAKE_50PCT_AT   = 1.00   # close 50% of LEAPS when up 100% (2x)
    LEAPS_TAKE_REST_AT    = 1.50   # close remaining when up 150% (2.5x)

    # ── Extra call (600 DTE) on 25% MSFT drawdown ──────────
    EXTRA_CALL_DTE_MIN         = 540
    EXTRA_CALL_DTE_MAX         = 660
    EXTRA_CALL_DRAWDOWN_TRIGGER = 0.25   # buy extra when MSFT -25% from entry spot

    # ── Short call (30-DTE) params ──────────────────────────
    SHORT_CALL_TARGET_DTE_MIN = 38
    SHORT_CALL_TARGET_DTE_MAX = 52
    SHORT_CALL_TARGET_DELTA   = 0.15
    SHORT_PROFIT_TAKE         = 0.50
    SHORT_ROLL_DELTA          = 0.35

    # ── Put protection params ───────────────────────────────
    PUT_TARGET_DTE_MIN    = 45
    PUT_TARGET_DTE_MAX    = 60
    PUT_TARGET_DELTA      = 0.25
    PUT_PROFIT_TAKE       = 0.50
    VIX_HEDGE_ON          = 25
    VIX_HEDGE_OFF         = 22
    DRAWDOWN_HEDGE_ON     = 0.08
    DRAWDOWN_HEDGE_OFF    = 0.05
    ROLLING_HIGH_DAYS     = 20

    # ── Earnings filter ─────────────────────────────────────
    EARNINGS_PRECLOSE_DAYS = 2
    EARNINGS_WINDOW_BUFFER = 5

    # ── FOMC dates ──────────────────────────────────────────
    FOMC_DATES = {
        date(2015,1,28), date(2015,3,18), date(2015,4,29), date(2015,6,17),
        date(2015,7,29), date(2015,9,17), date(2015,10,28), date(2015,12,16),
        date(2016,1,27), date(2016,3,16), date(2016,4,27), date(2016,6,15),
        date(2016,7,27), date(2016,9,21), date(2016,11,2), date(2016,12,14),
        date(2017,2,1),  date(2017,3,15), date(2017,5,3),  date(2017,6,14),
        date(2017,7,26), date(2017,9,20), date(2017,11,1), date(2017,12,13),
        date(2018,1,31), date(2018,3,21), date(2018,5,2),  date(2018,6,13),
        date(2018,8,1),  date(2018,9,26), date(2018,11,8), date(2018,12,19),
        date(2019,1,30), date(2019,3,20), date(2019,5,1),  date(2019,6,19),
        date(2019,7,31), date(2019,9,18), date(2019,10,30), date(2019,12,11),
        date(2020,1,29), date(2020,3,3),  date(2020,3,15), date(2020,4,29),
        date(2020,6,10), date(2020,7,29), date(2020,9,16), date(2020,11,5),
        date(2020,12,16),
        date(2021,1,27), date(2021,3,17), date(2021,4,28), date(2021,6,16),
        date(2021,7,28), date(2021,9,22), date(2021,11,3), date(2021,12,15),
        date(2022,1,26), date(2022,3,16), date(2022,5,4),  date(2022,6,15),
        date(2022,7,27), date(2022,9,21), date(2022,11,2), date(2022,12,14),
        date(2023,2,1),  date(2023,3,22), date(2023,5,3),  date(2023,6,14),
        date(2023,7,26), date(2023,9,20), date(2023,11,1), date(2023,12,13),
        date(2024,1,31), date(2024,3,20), date(2024,5,1),  date(2024,6,12),
        date(2024,7,31), date(2024,9,18), date(2024,11,7), date(2024,12,18),
        date(2025,1,29), date(2025,3,19), date(2025,5,7),  date(2025,6,18),
        date(2025,7,30), date(2025,9,17), date(2025,10,29), date(2025,12,17),
        date(2026,1,28), date(2026,3,18), date(2026,4,29), date(2026,6,17),
    }

    RF_RATE = 0.045

    # ────────────────────────────────────────────────────────
    def Initialize(self):
        self.SetStartDate(2015, 1, 1)
        self.SetEndDate(2026, 2, 28)
        self.SetCash(100_000)
        self.SetBenchmark("MSFT")

        self.spy = self.AddEquity("SPY", Resolution.Daily).Symbol
        self.tlt = self.AddEquity("TLT", Resolution.Daily).Symbol
        self.vix = self.AddData(CBOE, "VIX", Resolution.Daily).Symbol

        msft_eq = self.AddEquity("MSFT", Resolution.Daily)
        msft_eq.SetDataNormalizationMode(DataNormalizationMode.Adjusted)
        self.msft = msft_eq.Symbol

        msft_opt = self.AddOption("MSFT", Resolution.Daily)
        msft_opt.SetFilter(self._option_filter)
        self.msft_opt = msft_opt.Symbol

        self.msft_high_window = RollingWindow[float](self.ROLLING_HIGH_DAYS)

        # ── Primary LEAPS state ──
        self.long_call_symbol        = None
        self.long_call_entry_price   = 0.0
        self.long_call_qty           = 0
        self.long_call_50pct_taken   = False   # flag: 50% profit already closed
        self.leaps_entry_spot        = 0.0     # MSFT spot when LEAPS was opened

        # ── Extra call (600 DTE on -25% drawdown) state ──
        self.extra_call_symbol       = None
        self.extra_call_entry_price  = 0.0
        self.extra_call_qty          = 0
        self.extra_call_50pct_taken  = False
        self.extra_call_triggered    = False   # only buy once per LEAPS cycle

        # ── Short call state ──
        self.short_call_symbol       = None
        self.short_call_entry_credit = 0.0
        self.short_call_qty          = 0
        self.short_call_open_date    = None
        self.last_short_sale_date    = None

        # ── Put hedge state ──
        self.put_symbol     = None
        self.put_entry_cost = 0.0
        self.put_qty        = 0
        self.put_open_date  = None

        self.last_core_rebal = None

        self.SetWarmUp(self.ROLLING_HIGH_DAYS + 5)

        self.Schedule.On(
            self.DateRules.Every(
                DayOfWeek.Monday, DayOfWeek.Tuesday, DayOfWeek.Wednesday,
                DayOfWeek.Thursday, DayOfWeek.Friday),
            self.TimeRules.AfterMarketOpen("SPY", 30),
            self._scheduled_manage
        )

    def _option_filter(self, universe):
        # Extend to 660 to cover 600-DTE extra call
        return (
            universe
            .IncludeWeeklys()
            .Strikes(-20, 10)
            .Expiration(3, 660)
        )

    # ────────────────────────────────────────────────────────
    # Main loop
    # ────────────────────────────────────────────────────────
    def OnData(self, data: Slice):
        if self.IsWarmingUp:
            return

        if data.Bars.ContainsKey(self.msft):
            self.msft_high_window.Add(float(data.Bars[self.msft].Close))

        if self.last_core_rebal is None or (self.Time.date() - self.last_core_rebal).days >= 7:
            self._rebalance_core()
            self.last_core_rebal = self.Time.date()

        spot = self._get_price(self.msft)
        vix  = self._get_price(self.vix)

        self._manage_long_call(data, spot, vix)
        self._manage_extra_call(data, spot, vix)
        self._manage_short_call(spot, vix)
        self._manage_put(spot, vix)

    # ────────────────────────────────────────────────────────
    # Scheduled: open short + put entries
    # ────────────────────────────────────────────────────────
    def _scheduled_manage(self):
        if self.IsWarmingUp:
            return
        spot = self._get_price(self.msft)
        vix  = self._get_price(self.vix)
        if spot <= 0:
            return

        if (self._has_long_call()
                and self.short_call_symbol is None
                and self.last_short_sale_date != self.Time.date()):
            self._sell_short_call(spot, vix)

        if self._has_long_call() and self.put_symbol is None:
            if self._hedge_needed(spot, vix):
                self._buy_put(spot, vix)

    # ────────────────────────────────────────────────────────
    # Core rebalance
    # ────────────────────────────────────────────────────────
    def _rebalance_core(self):
        self.SetHoldings(self.spy, self.CORE_SPY_WEIGHT)
        self.SetHoldings(self.tlt, self.CORE_TLT_WEIGHT)

    # ────────────────────────────────────────────────────────
    # Primary LEAPS management (450 DTE, 15% budget)
    # ────────────────────────────────────────────────────────
    def _manage_long_call(self, data, spot, vix):
        if self._has_long_call():
            sym = self.long_call_symbol
            dte = (sym.ID.Date.date() - self.Time.date()).days

            # ── Partial profit-taking ──
            sec = self.Securities.get(sym)
            if sec is not None:
                current_mid = self._mid_from_security(sec)
                entry       = self.long_call_entry_price

                if entry > 0 and current_mid > 0:
                    gain = (current_mid - entry) / entry

                    # First take: close 50% at +100%
                    if not self.long_call_50pct_taken and gain >= self.LEAPS_TAKE_50PCT_AT:
                        qty_held = self.Portfolio[sym].Quantity
                        close_qty = max(int(qty_held / 2), 1)
                        self.MarketOrder(sym, -close_qty)
                        self.long_call_50pct_taken = True
                        self.Log(
                            f"[MSFT] LEAPS 50% PROFIT TAKE | {self.Time.date()} | "
                            f"gain={gain:.0%} mid={current_mid:.2f} close={close_qty}"
                        )
                        return

                    # Second take: close all at +150%
                    if self.long_call_50pct_taken and gain >= self.LEAPS_TAKE_REST_AT:
                        qty_held = self.Portfolio[sym].Quantity
                        if qty_held > 0:
                            self.MarketOrder(sym, -qty_held)
                            self.Log(
                                f"[MSFT] LEAPS FULL PROFIT TAKE | {self.Time.date()} | "
                                f"gain={gain:.0%} mid={current_mid:.2f}"
                            )
                            self._reset_leaps_state()
                        return

            # Roll when DTE < threshold and no short open
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
            self.Log("[MSFT] No valid LEAPS candidate (450 DTE)")
            return

        candidates.sort(key=lambda x: (x[0], abs(x[4] - 450)))
        _, contract, delta, mid, dte = candidates[0]

        budget = self.Portfolio.TotalPortfolioValue * self.LEAPS_BUDGET_PCT
        qty    = max(int(budget / (mid * 100)), 1)

        self.MarketOrder(contract.Symbol, qty)
        self.long_call_symbol      = contract.Symbol
        self.long_call_entry_price = mid
        self.long_call_qty         = qty
        self.long_call_50pct_taken = False
        self.leaps_entry_spot      = spot
        self.extra_call_triggered  = False   # reset extra call flag for new LEAPS cycle

        self.Log(
            f"[MSFT] LEAPS ENTRY | {self.Time.date()} | "
            f"Strike={contract.Strike:.2f} DTE={dte} Delta={delta:.2f} "
            f"Mid={mid:.2f} Qty={qty} EntrySpot={spot:.2f}"
        )

    def _close_long_call(self, reason):
        if self.long_call_symbol and self.long_call_symbol in self.Portfolio:
            qty = self.Portfolio[self.long_call_symbol].Quantity
            if qty > 0:
                self.MarketOrder(self.long_call_symbol, -qty)
        self.Log(f"[MSFT] LEAPS EXIT [{reason}] | {self.Time.date()}")
        self._reset_leaps_state()

    def _reset_leaps_state(self):
        self.long_call_symbol      = None
        self.long_call_entry_price = 0.0
        self.long_call_qty         = 0
        self.long_call_50pct_taken = False
        self.leaps_entry_spot      = 0.0

    # ────────────────────────────────────────────────────────
    # Extra call: 600 DTE when MSFT -25% from LEAPS entry spot
    # ────────────────────────────────────────────────────────
    def _manage_extra_call(self, data, spot, vix):
        # Close extra call: same profit-taking rules
        if self._has_extra_call():
            sym = self.extra_call_symbol
            sec = self.Securities.get(sym)
            dte = (sym.ID.Date.date() - self.Time.date()).days

            if sec is not None:
                current_mid = self._mid_from_security(sec)
                entry       = self.extra_call_entry_price

                if entry > 0 and current_mid > 0:
                    gain = (current_mid - entry) / entry

                    if not self.extra_call_50pct_taken and gain >= self.LEAPS_TAKE_50PCT_AT:
                        qty_held  = self.Portfolio[sym].Quantity
                        close_qty = max(int(qty_held / 2), 1)
                        self.MarketOrder(sym, -close_qty)
                        self.extra_call_50pct_taken = True
                        self.Log(
                            f"[MSFT] EXTRA CALL 50% TAKE | {self.Time.date()} | "
                            f"gain={gain:.0%} mid={current_mid:.2f}"
                        )
                        return

                    if self.extra_call_50pct_taken and gain >= self.LEAPS_TAKE_REST_AT:
                        qty_held = self.Portfolio[sym].Quantity
                        if qty_held > 0:
                            self.MarketOrder(sym, -qty_held)
                            self.Log(
                                f"[MSFT] EXTRA CALL FULL TAKE | {self.Time.date()} | "
                                f"gain={gain:.0%}"
                            )
                            self._reset_extra_call_state()
                        return

            # Roll or close at near-expiry
            if dte <= self.LONG_CALL_REPLACE_DTE:
                qty_held = self.Portfolio.get(sym, None)
                if qty_held and self.Portfolio[sym].Quantity > 0:
                    self.MarketOrder(sym, -self.Portfolio[sym].Quantity)
                    self.Log(f"[MSFT] EXTRA CALL EXPIRY CLOSE | {self.Time.date()}")
                    self._reset_extra_call_state()
            return

        # Open extra call when MSFT drops 25% from LEAPS entry spot (once per cycle)
        if (not self.extra_call_triggered
                and self._has_long_call()
                and self.leaps_entry_spot > 0
                and spot <= self.leaps_entry_spot * (1 - self.EXTRA_CALL_DRAWDOWN_TRIGGER)):
            self._open_extra_call(data, spot, vix)

    def _open_extra_call(self, data, spot, vix):
        chain = data.OptionChains.get(self.msft_opt)
        if chain is None:
            return

        candidates = []
        for c in chain:
            if c.Right != OptionRight.Call:
                continue
            dte = (c.Expiry.date() - self.Time.date()).days
            if dte < self.EXTRA_CALL_DTE_MIN or dte > self.EXTRA_CALL_DTE_MAX:
                continue
            mid = self._mid(c)
            if mid <= 0 or not self._spread_ok(c, mid):
                continue
            delta = self._get_delta(c, spot, vix)
            candidates.append((abs(delta - self.LONG_CALL_TARGET_DELTA), c, delta, mid, dte))

        if not candidates:
            self.Log("[MSFT] No valid 600-DTE extra call candidate")
            return

        candidates.sort(key=lambda x: (x[0], abs(x[4] - 600)))
        _, contract, delta, mid, dte = candidates[0]

        budget = self.Portfolio.TotalPortfolioValue * self.LEAPS_BUDGET_PCT
        qty    = max(int(budget / (mid * 100)), 1)

        self.MarketOrder(contract.Symbol, qty)
        self.extra_call_symbol      = contract.Symbol
        self.extra_call_entry_price = mid
        self.extra_call_qty         = qty
        self.extra_call_50pct_taken = False
        self.extra_call_triggered   = True

        drawdown = (self.leaps_entry_spot - spot) / self.leaps_entry_spot
        self.Log(
            f"[MSFT] EXTRA CALL ENTRY (600 DTE) | {self.Time.date()} | "
            f"Strike={contract.Strike:.2f} DTE={dte} Delta={delta:.2f} "
            f"Mid={mid:.2f} Qty={qty} MSFT_DD={drawdown:.1%}"
        )

    def _reset_extra_call_state(self):
        self.extra_call_symbol      = None
        self.extra_call_entry_price = 0.0
        self.extra_call_qty         = 0
        self.extra_call_50pct_taken = False

    # ────────────────────────────────────────────────────────
    # Short call (30-DTE) management
    # ────────────────────────────────────────────────────────
    def _sell_short_call(self, spot, vix):
        if not self._has_long_call():
            return

        if self._is_fomc_nearby():
            self.Log("[MSFT] Skip short: FOMC nearby")
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

        if self._earnings_within_days(self.EARNINGS_PRECLOSE_DAYS):
            self.Log(f"[MSFT] Pre-earnings short close")
            self._close_short_call("pre_earnings")
            return

        current_mid = self._mid_from_security(sec)
        if current_mid <= 0:
            current_mid = self._bs_call(
                spot, max(vix / 100.0, 0.15),
                float(sym.ID.StrikePrice),
                max((sym.ID.Date.date() - self.Time.date()).days, 0)
            )

        dte = (sym.ID.Date.date() - self.Time.date()).days

        if self.short_call_entry_credit > 0 and current_mid <= self.short_call_entry_credit * (1 - self.SHORT_PROFIT_TAKE):
            self._close_short_call("take_profit")
            return

        delta  = self._security_delta(sec, spot, vix)
        strike = float(sym.ID.StrikePrice)
        is_itm = spot >= strike

        # If spot has crossed the strike and only 1 day left → close before assignment
        if is_itm and dte <= 1:
            self.Log(
                f"[MSFT] Short call ITM at expiry: spot={spot:.2f} strike={strike:.2f} DTE={dte}"
            )
            self._close_short_call("itm_close_pre_expiry")
            return

        # Roll early only on aggressive delta breach (not on mere ITM crossing)
        if delta >= self.SHORT_ROLL_DELTA and not is_itm:
            self._close_short_call("roll_delta_breach")
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
        if vix > self.VIX_HEDGE_ON:
            return True
        if self.msft_high_window.IsReady:
            peak = max([self.msft_high_window[i] for i in range(self.msft_high_window.Count)])
            if peak > 0 and (peak - spot) / peak >= self.DRAWDOWN_HEDGE_ON:
                return True
        return False

    def _hedge_clear(self, spot, vix):
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

        self.Log(
            f"[MSFT] PUT ENTRY | {self.Time.date()} | "
            f"Strike={float(sym.ID.StrikePrice):.2f} DTE={dte} Delta={delta:.2f} "
            f"Cost={mid:.2f} VIX={vix:.1f}"
        )

    def _manage_put(self, spot, vix):
        if not self._has_put():
            return

        sym = self.put_symbol
        sec = self.Securities[sym]
        dte = (sym.ID.Date.date() - self.Time.date()).days

        current_mid = self._mid_from_security(sec)

        if self.put_entry_cost > 0 and current_mid >= self.put_entry_cost * (1 + self.PUT_PROFIT_TAKE):
            self._close_put("take_profit")
            return

        if self._hedge_clear(spot, vix):
            self._close_put("hedge_clear")
            return

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
    # FOMC helpers
    # ────────────────────────────────────────────────────────
    def _is_fomc_nearby(self):
        today = self.Time.date()
        return any((fd - today).days in (0, 1) for fd in self.FOMC_DATES)

    def _fomc_in_window(self, expiry_date):
        today = self.Time.date()
        return any(today <= fd <= expiry_date for fd in self.FOMC_DATES)

    # ────────────────────────────────────────────────────────
    # State helpers
    # ────────────────────────────────────────────────────────
    def _has_long_call(self):
        return (self.long_call_symbol is not None
                and self.long_call_symbol in self.Portfolio
                and self.Portfolio[self.long_call_symbol].Quantity > 0)

    def _has_extra_call(self):
        return (self.extra_call_symbol is not None
                and self.extra_call_symbol in self.Portfolio
                and self.Portfolio[self.extra_call_symbol].Quantity > 0)

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
