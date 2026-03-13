# region imports
from AlgorithmImports import *
import numpy as np
from scipy.stats import norm
# endregion

# ============================================================
# BASELINE: Case 1 — No Put Hedge
# CAGR: 21.37% | Drawdown: 56.5% | Win Rate: 45% | Sharpe: 0.573
# Period: 2012-01-01 to 2026-02-28 | Start: $100,000 | End: $1,493,185
# DO NOT MODIFY — this is the reference version
# ============================================================

class SPXLeapsStrategy(QCAlgorithm):
    """
    SPX LEAPS Call Strategy — QuantConnect Implementation

    Buys deep OTM 300-DTE SPX calls using real option chain data.
    Uses live Greeks (delta) for strike selection instead of B-S inversion.
    Exits on profit target (100% gain) or crash rules.

    Three modes — set PUT_MODE before running:
      "none"        — Case 1: Never buy put hedge
      "conditional" — Case 2: Buy 90-day ATM put when SPY P/E > 5yr average
      "always"      — Case 3: Always buy 90-day ATM put

    Key differences vs. the Python backtest (spx_case_comparison.py):
      + Uses REAL option chain data and live Greeks (not Black-Scholes model)
      + Uses REAL bid/ask spreads (mid-price fills)
      + Option data availability: SPX index options from ~2012 in QC
      - Cash interest (4.5% RF) not modeled — QuantConnect does not auto-accrue
      - SPY P/E (Case 2) uses Morningstar fundamentals; may have data gaps

    Setup:
      1. Paste into a new QuantConnect algorithm
      2. Set PUT_MODE = "none" / "conditional" / "always"
      3. Backtest period: 2012-01-01 to present
      4. Initial capital: $100,000
    """

    # ── Strategy Parameters ─────────────────────────────────────────────────
    PUT_MODE       = "none"   # "none" | "conditional" | "always"

    VIX_THRESHOLD  = 20.0    # only enter when VIX < this
    CALL_DELTA_TGT = 0.40    # target delta for call selection
    CALL_DTE       = 300     # target days-to-expiry for call
    PUT_DTE        = 90      # target days-to-expiry for put hedge
    PROFIT_TARGET  = 1.00    # exit when call gains 100% (doubles)
    RISK_PER_TRADE = 0.30    # fraction of free cash deployed per trade
    PUT_COST_FRAC  = 0.10    # put budget = 10% of call premium spent
    COOLDOWN_DAYS  = 5       # calendar days to wait after any exit
    RF_RATE        = 0.045   # risk-free rate (used only in B-S fallback delta)

    # Crash exit rules: {lookback_trading_days: drop_threshold}
    CRASH_RULES = {
        7:  -0.03,   # exit if SPX falls 3% in 7 trading days
        10: -0.04,
        14: -0.06,
        30: -0.08,
    }

    # ── Initialisation ──────────────────────────────────────────────────────
    def Initialize(self):
        self.SetStartDate(2012, 1, 1)
        self.SetEndDate(2026, 2, 28)
        self.SetCash(100_000)
        self.SetBenchmark("SPY")

        # SPX spot index
        self.spx = self.AddIndex("SPX", Resolution.Daily).Symbol

        # SPX index options (SPXW weeklies + SPX monthlies)
        opt = self.AddIndexOption("SPX", Resolution.Daily)
        opt.SetFilter(self._option_filter)
        self.option_symbol = opt.Symbol

        # VIX  — used as implied volatility proxy and entry gate
        self.vix = self.AddData(CBOE, "VIX", Resolution.Daily).Symbol

        # SPY equity — for P/E fundamentals (Case 2 only)
        self.spy = self.AddEquity("SPY", Resolution.Daily).Symbol

        # Rolling SPX price history: [0] = most recent, [lb] = lb days ago
        # Need 31 entries to check 30-day crash rule
        self.spx_window = RollingWindow[float](35)

        # Rolling P/E window for Case 2 (5 years of trading days ≈ 1260)
        self.pe_window = RollingWindow[float](1260)

        # Trade state
        self._reset_state()
        self.cooldown_until = self.StartDate

        # Warm up so SPX window and P/E window have data before trading
        self.SetWarmUp(timedelta(days=1290))

    def _option_filter(self, universe):
        """
        Wide filter: QuantConnect will cache all contracts in this range.
        We then pick the specific contract ourselves in OnData.
        """
        return (
            universe
            .IncludeWeeklys()
            .Strikes(-150, 150)      # wide strike range
            .Expiration(60, 400)     # covers 90-DTE puts and 300-DTE calls
        )

    # ── Data Handler ────────────────────────────────────────────────────────
    def OnData(self, data: Slice):
        # Always update rolling histories (including during warm-up)
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
        """Update SPX price window and SPY P/E window each bar."""
        spx = self._get_price(self.spx)
        if spx > 0:
            self.spx_window.Add(spx)

        # Track SPY P/E for conditional hedge (Case 2)
        if self.PUT_MODE == "conditional":
            spy_sec = self.Securities.get(self.spy)
            if spy_sec and spy_sec.Fundamentals:
                pe = spy_sec.Fundamentals.ValuationRatios.PERatio
                if pe and pe > 0:
                    self.pe_window.Add(float(pe))

    # ── Entry Logic ─────────────────────────────────────────────────────────
    def _try_enter(self, data, spx_price, vix_level):
        chain = data.OptionChains.get(self.option_symbol)
        if chain is None:
            return

        # ── 1. Select call: ~300 DTE, delta nearest 0.40 ──────────────────
        target_call_exp = self.Time.date() + timedelta(days=self.CALL_DTE)

        calls = [
            c for c in chain
            if c.Right == OptionRight.Call
            and c.AskPrice > 0
            and c.BidPrice > 0
        ]
        if not calls:
            return

        # Narrow to contracts within ±30 days of target DTE
        near_calls = [
            c for c in calls
            if abs((c.Expiry.date() - target_call_exp).days) <= 30
        ]
        pool = near_calls if near_calls else calls

        # Pick contract with delta closest to target
        best_call = min(pool, key=lambda c: abs(self._get_delta(c, spx_price, vix_level) - self.CALL_DELTA_TGT))
        call_mid  = self._mid(best_call)
        call_dte  = (best_call.Expiry.date() - self.Time.date()).days

        if call_mid <= 0:
            return

        # ── 2. Size position: 30% of free cash ────────────────────────────
        free_cash   = self.Portfolio.Cash
        budget      = free_cash * self.RISK_PER_TRADE
        multiplier  = 100   # SPX options: $100 per contract per point
        n_contracts = int(budget / (call_mid * multiplier))
        if n_contracts < 1:
            return

        # ── 3. Optional put hedge ──────────────────────────────────────────
        put_contract = None
        put_mid      = 0.0
        if self._should_hedge():
            put_budget   = call_mid * self.PUT_COST_FRAC * n_contracts * multiplier
            put_contract = self._select_put(chain, spx_price, put_budget, n_contracts)
            if put_contract:
                put_mid = self._mid(put_contract)

        # ── 4. Place orders ────────────────────────────────────────────────
        self.MarketOrder(best_call.Symbol, n_contracts)
        if put_contract and put_mid > 0:
            self.MarketOrder(put_contract.Symbol, n_contracts)
            self.put_symbol   = put_contract.Symbol
            self.put_entry_px = put_mid

        self.call_symbol   = best_call.Symbol
        self.call_entry_px = call_mid
        self.num_contracts = n_contracts
        self.in_trade      = True

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
        """Find the ATM put closest to 90 DTE that fits within budget."""
        target_exp = self.Time.date() + timedelta(days=self.PUT_DTE)

        puts = [
            c for c in chain
            if c.Right == OptionRight.Put
            and c.AskPrice > 0
            and c.BidPrice > 0
        ]
        if not puts:
            return None

        # Sort: closest to 90 DTE first, then closest to ATM strike
        puts.sort(key=lambda c: (
            abs((c.Expiry.date() - target_exp).days),
            abs(c.Strike - spx_price)
        ))

        for p in puts[:20]:
            mid  = self._mid(p)
            cost = mid * n_contracts * 100
            if mid > 0 and cost <= budget * 1.5:
                return p
        return None

    # ── Exit Logic ──────────────────────────────────────────────────────────
    def _check_exits(self, data, spx_price, vix_level):
        if self.call_symbol is None:
            return

        call_sec = self.Securities.get(self.call_symbol)
        if call_sec is None:
            self._exit("expiry")
            return

        # Current call price (mid, fallback to B-S if no market data)
        call_mid = self._mid_from_security(call_sec)
        if call_mid <= 0:
            call_mid = self._bs_call(
                spx_price, vix_level / 100,
                self.call_symbol.ID.StrikePrice,
                max((self.call_symbol.ID.Date.date() - self.Time.date()).days, 0)
            )

        # Priority 1: Profit target (call doubled)
        if self.call_entry_px > 0 and call_mid >= self.call_entry_px * (1 + self.PROFIT_TARGET):
            self._exit("profit_target")
            return

        # Priority 2: Crash rules — compare today vs N trading days ago
        # spx_window[0] = today, spx_window[lb] = lb trading days ago
        for lb, threshold in sorted(self.CRASH_RULES.items()):
            if self.spx_window.Count > lb:
                past_px = self.spx_window[lb]
                if past_px > 0:
                    pct_chg = (spx_price - past_px) / past_px
                    if pct_chg <= threshold:
                        self._exit(f"crash_{lb}d")
                        return

        # Priority 3: Expiry reached
        dte = (self.call_symbol.ID.Date.date() - self.Time.date()).days
        if dte <= 1:
            self._exit("expiry")

    def _exit(self, reason):
        """Close all legs and reset state."""
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

    # ── Helpers ─────────────────────────────────────────────────────────────
    def _reset_state(self):
        self.in_trade      = False
        self.call_symbol   = None
        self.put_symbol    = None
        self.call_entry_px = 0.0
        self.put_entry_px  = 0.0
        self.num_contracts = 0

    def _should_hedge(self):
        if self.PUT_MODE == "none":
            return False
        if self.PUT_MODE == "always":
            return True
        # conditional: hedge when current SPY P/E > 5-year rolling average
        if not self.pe_window.IsReady:
            return True
        current_pe = self.pe_window[0]
        avg_pe = sum(self.pe_window[i] for i in range(self.pe_window.Count)) / self.pe_window.Count
        return current_pe > avg_pe

    def _get_delta(self, contract, spx_price, vix_level):
        """Use live Greeks delta if available, else fall back to B-S model delta."""
        if contract.Greeks and contract.Greeks.Delta and contract.Greeks.Delta > 0:
            return float(contract.Greeks.Delta)
        T_days = (contract.Expiry.date() - self.Time.date()).days
        return self._bs_delta(spx_price, vix_level / 100, contract.Strike, T_days)

    def _mid(self, contract):
        """Mid-price of a contract from the option chain."""
        if contract.AskPrice > 0 and contract.BidPrice > 0:
            return (contract.AskPrice + contract.BidPrice) / 2.0
        return contract.LastPrice or 0.0

    def _mid_from_security(self, security):
        """Mid-price from a Security object (after entry, during position)."""
        ask  = security.AskPrice
        bid  = security.BidPrice
        last = security.Price
        if ask > 0 and bid > 0:
            return (ask + bid) / 2.0
        return last or 0.0

    def _get_price(self, symbol):
        sec = self.Securities.get(symbol)
        return sec.Price if sec and sec.Price > 0 else 0.0

    def _bs_delta(self, S, sigma, K, T_days):
        """Black-Scholes call delta (fallback when live Greeks unavailable)."""
        T = max(T_days / 365.0, 1e-6)
        sigma = max(sigma, 1e-6)
        d1 = (np.log(S / K) + (self.RF_RATE + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
        return float(norm.cdf(d1))

    def _bs_call(self, S, sigma, K, T_days):
        """Black-Scholes call price (fallback when market price unavailable)."""
        T = max(T_days / 365.0, 1e-6)
        sigma = max(sigma, 1e-6)
        d1 = (np.log(S / K) + (self.RF_RATE + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
        d2 = d1 - sigma * np.sqrt(T)
        return float(
            S * norm.cdf(d1) - K * np.exp(-self.RF_RATE * T) * norm.cdf(d2)
        )

    # ── Order Events ────────────────────────────────────────────────────────
    def OnOrderEvent(self, orderEvent):
        if orderEvent.Status == OrderStatus.Filled:
            self.Log(
                f"  Fill | {orderEvent.Symbol.Value} "
                f"qty={orderEvent.FillQuantity:+.0f} "
                f"@ ${orderEvent.FillPrice:.2f}"
            )
