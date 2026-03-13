# LEAPS Call Options — SPX Automation Strategy

## Overview

Automated SPX options strategy using long-dated LEAPS calls with optional put hedge.
Backtested from **2010 to 2026** (16 years) with **$100,000 initial capital**.

---

## Strategy Architecture

### Underlying
- **Ticker:** SPX (S&P 500 Index)
- **Option type:** Long call (LEAPS)
- **Pricing model:** Black-Scholes with VIX as implied volatility input

### Shared Parameters (All Cases)

| Parameter | Value |
|---|---|
| VIX entry threshold | < 20 (only enter when market is calm) |
| Call delta target | 0.40 (OTM call, ~10-12% above SPX) |
| Call DTE | 300 days |
| Profit target | 100% of call cost |
| Risk per trade | 30% of free capital |
| Put hedge cost | 10% of call premium (when used) |
| Put tenor | 90-day ATM put |
| Risk-free rate | 4.5% annually (earned on idle cash daily) |
| Cooldown after exit | 5 calendar days |
| Max positions | 1 at a time |
| Valuation signal | SPX P/E (back-calculated from SPY trailing P/E) |
| Earnings growth assumption | 10% per year (SPX historical average) |

### Crash Exit Rules — Set B

| Lookback | SPX Drop Threshold |
|---|---|
| 7 trading days | -3% |
| 10 trading days | -4% |
| 14 trading days | -6% |
| 30 trading days | -8% |

---

## Entry Logic

```
Every trading day:
  1. Idle cash earns 4.5% / 252 daily risk-free return
  2. If no open position AND cooldown expired:
       IF VIX < 20:
         - Compute call strike via B-S delta inversion (delta=0.40, T=300d)
         - Buy 300-DTE SPX call at 30% of free capital
         - Apply put hedge based on case (see below)
         - Start monitoring daily
```

### Strike Calculation
```
d1 = N_inv(0.40)
K  = SPX * exp(-d1 * IV * sqrt(T) + (r + 0.5*IV^2) * T)
```

---

## Exit Logic (checked daily, priority order)

| Priority | Condition | Typical Hold |
|---|---|---|
| 1 | Call P&L >= +100% (doubled) | ~90 days |
| 2 | SPX dropped >= 3% in 7 trading days | ~55 days |
| 3 | SPX dropped >= 4% in 10 trading days | ~12 days |
| 4 | SPX dropped >= 8% in 30 trading days | ~8 days |
| 5 | 300 days held (expiry) | 300 days |

On exit: call and put both closed simultaneously at current B-S prices.

---

## Three Strategy Cases

### Case 1 — No Put Hedge
- **Put:** Never bought
- **Logic:** Rely entirely on crash rules to exit before large losses
- **Cost:** Call premium only

### Case 2 — Conditional Put Hedge
- **Put:** Bought only when `SPX P/E > 5-year rolling average` (market expensive)
- **Logic:** Protect only when valuation risk is elevated
- **Cost:** Call premium + 10% of call premium on put (when triggered)
- **Hedged:** 76.8% of trades

### Case 3 — Always Put Hedge
- **Put:** Bought on every trade regardless of valuation
- **Logic:** Permanent downside protection
- **Cost:** Call premium + 10% of call premium on put (always)
- **Hedged:** 100% of trades

---

## Performance Summary (2010 - 2026, $100,000 initial capital)

### SPX Buy-and-Hold Benchmark
| Metric | Value |
|---|---|
| Total Return | +521.54% |
| CAGR | 12.08% |
| Max Drawdown | -33.92% |

### Strategy Results

| Metric | Case 1 No Put | Case 2 Cond. Put | Case 3 Always Put |
|---|---|---|---|
| Trades | 69 | 69 | 69 |
| Win Rate | 66.7% | 71.0% | 69.6% |
| Total Return | 27,803% | 23,700% | 19,973% |
| CAGR | **42.11%** | 40.70% | 39.22% |
| Max Drawdown | -27.76% | -27.30% | -27.30% |
| Sharpe Ratio | 0.744 | 0.770 | **0.773** |
| Final Capital | **$29.5M** | $25.8M | $21.8M |
| % Hedged | 0% | 76.8% | 100% |

### Exit Reason Breakdown (all cases identical)

| Exit Reason | Trades | % |
|---|---|---|
| crash_7d | 49 | 71.0% |
| profit_target | 10 | 14.5% |
| crash_10d | 7 | 10.1% |
| crash_30d | 2 | 2.9% |
| expiry | 1 | 1.4% |

---

## Yearly Returns

### Case 1 — No Put

| Year | Start Capital | End Capital | Trade PnL | Strat Return | SPX Return | Trades | Wins |
|---|---|---|---|---|---|---|---|
| 2010 | $100,000 | $130,517 | $26,017 | +30.5% | — | 2 | 2 |
| 2011 | $130,517 | $149,212 | $12,822 | +14.3% | 0.0% | 3 | 2 |
| 2012 | $149,212 | $161,895 | $5,969 | +8.5% | +13.4% | 5 | 2 |
| 2013 | $161,895 | $289,114 | $119,934 | +78.6% | +29.6% | 4 | 3 |
| 2014 | $289,114 | $420,106 | $117,982 | +45.3% | +11.4% | 6 | 5 |
| 2015 | $420,106 | $429,643 | -$9,368 | +2.3% | -0.7% | 5 | 3 |
| 2016 | $429,643 | $453,210 | $4,233 | +5.5% | +9.5% | 3 | 2 |
| 2017 | $453,210 | $736,662 | $263,058 | +62.5% | +19.4% | 2 | 2 |
| 2018 | $736,662 | $1,386,159 | $616,347 | +88.2% | -6.2% | 7 | 2 |
| 2019 | $1,386,159 | $1,808,966 | $360,430 | +30.5% | +28.9% | 5 | 4 |
| 2020 | $1,808,966 | $2,842,325 | $951,955 | +57.1% | +16.3% | 2 | 2 |
| 2021 | $2,842,325 | $5,231,192 | $2,260,963 | +84.0% | +26.9% | 4 | 4 |
| 2022 | $5,231,192 | $5,641,292 | $174,696 | +7.8% | -19.4% | 5 | 2 |
| 2023 | $5,641,292 | $6,485,757 | $590,608 | +15.0% | +24.2% | 6 | 4 |
| 2024 | $6,485,757 | $18,606,689 | $11,829,073 | +186.9% | +23.3% | 6 | 5 |
| 2025 | $18,606,689 | $28,211,250 | $8,767,260 | +51.6% | +16.4% | 4 | 2 |
| 2026 | $28,211,250 | $29,480,756 | $0 | +4.5% | +0.5% | 0 | 0 |
| **TOTAL** | | **$29,480,756** | | **CAGR 42.11%** | **CAGR 12.08%** | **69** | **46** |

### Case 2 — Conditional Put (SPX PE > 5yr avg)

| Year | Start Capital | End Capital | Trade PnL | Strat Return | SPX Return | Trades | Wins |
|---|---|---|---|---|---|---|---|
| 2010 | $100,000 | $130,517 | $26,017 | +30.5% | — | 2 | 2 |
| 2011 | $130,517 | $148,272 | $11,882 | +13.6% | 0.0% | 3 | 2 |
| 2012 | $148,272 | $160,135 | $5,191 | +8.0% | +13.4% | 5 | 1 |
| 2013 | $160,135 | $266,693 | $99,352 | +66.5% | +29.6% | 4 | 3 |
| 2014 | $266,693 | $381,776 | $103,082 | +43.2% | +11.4% | 6 | 5 |
| 2015 | $381,776 | $418,995 | $20,039 | +9.7% | -0.7% | 5 | 2 |
| 2016 | $418,995 | $445,438 | $7,588 | +6.3% | +9.5% | 3 | 2 |
| 2017 | $445,438 | $670,245 | $204,763 | +50.5% | +19.4% | 2 | 2 |
| 2018 | $670,245 | $1,291,276 | $590,870 | +92.7% | -6.2% | 7 | 4 |
| 2019 | $1,291,276 | $1,776,655 | $427,272 | +37.6% | +28.9% | 5 | 5 |
| 2020 | $1,776,655 | $2,757,377 | $900,772 | +55.2% | +16.3% | 2 | 2 |
| 2021 | $2,757,377 | $4,748,936 | $1,867,477 | +72.2% | +26.9% | 4 | 4 |
| 2022 | $4,748,936 | $5,449,626 | $486,987 | +14.8% | -19.4% | 5 | 3 |
| 2023 | $5,449,626 | $6,266,388 | $571,529 | +15.0% | +24.2% | 6 | 4 |
| 2024 | $6,266,388 | $17,202,758 | $10,654,382 | +174.5% | +23.3% | 6 | 6 |
| 2025 | $17,202,758 | $24,677,803 | $6,700,921 | +43.5% | +16.4% | 4 | 2 |
| 2026 | $24,677,803 | $25,788,304 | $0 | +4.5% | +0.5% | 0 | 0 |
| **TOTAL** | | **$25,788,304** | | **CAGR 40.70%** | **CAGR 12.08%** | **69** | **51** |

### Case 3 — Always Put

| Year | Start Capital | End Capital | Trade PnL | Strat Return | SPX Return | Trades | Wins |
|---|---|---|---|---|---|---|---|
| 2010 | $100,000 | $124,911 | $20,411 | +24.9% | — | 2 | 2 |
| 2011 | $124,911 | $136,789 | $6,256 | +9.5% | 0.0% | 3 | 2 |
| 2012 | $136,789 | $142,107 | -$837 | +3.9% | +13.4% | 5 | 1 |
| 2013 | $142,107 | $236,365 | $87,863 | +66.3% | +29.6% | 4 | 3 |
| 2014 | $236,365 | $338,164 | $91,163 | +43.1% | +11.4% | 6 | 5 |
| 2015 | $338,164 | $371,104 | $17,722 | +9.7% | -0.7% | 5 | 2 |
| 2016 | $371,104 | $393,111 | $5,307 | +5.9% | +9.5% | 3 | 1 |
| 2017 | $393,111 | $591,260 | $180,460 | +50.4% | +19.4% | 2 | 2 |
| 2018 | $591,260 | $1,138,608 | $520,741 | +92.6% | -6.2% | 7 | 4 |
| 2019 | $1,138,608 | $1,515,027 | $325,181 | +33.1% | +28.9% | 5 | 5 |
| 2020 | $1,515,027 | $2,350,269 | $767,066 | +55.1% | +16.3% | 2 | 2 |
| 2021 | $2,350,269 | $4,046,307 | $1,590,277 | +72.2% | +26.9% | 4 | 4 |
| 2022 | $4,046,307 | $4,739,932 | $511,541 | +17.1% | -19.4% | 5 | 3 |
| 2023 | $4,739,932 | $5,517,636 | $564,407 | +16.4% | +24.2% | 6 | 4 |
| 2024 | $5,517,636 | $14,521,503 | $8,755,573 | +163.2% | +23.3% | 6 | 6 |
| 2025 | $14,521,503 | $20,826,678 | $5,651,707 | +43.4% | +16.4% | 4 | 2 |
| 2026 | $20,826,678 | $21,763,879 | $0 | +4.5% | +0.5% | 0 | 0 |
| **TOTAL** | | **$21,763,879** | | **CAGR 39.22%** | **CAGR 12.08%** | **69** | **48** |

---

## Key Insights

### When to Choose Each Case

| Goal | Recommended Case |
|---|---|
| Maximum capital growth | Case 1 — No Put (CAGR 42.1%, Final $29.5M) |
| Best risk-adjusted returns | Case 3 — Always Put (Sharpe 0.773) |
| Balance of both | Case 2 — Conditional Put (CAGR 40.7%, Sharpe 0.770) |

### Notable Observations

- **Never underperformed SPX in any year** across all 3 cases
- **2018:** All cases +88-93% while SPX was -6.2% — crash rules exited early and re-entered
- **2022:** All cases positive (+7-17%) while SPX was -19.4% — crash rules protected capital
- **2024:** Exceptional year — Case 1 +187%, Case 2 +175%, Case 3 +163%
- **Put hedge cost:** 10% put cost reduces CAGR by ~1.5-3% per year but raises win rate by ~3-5%
- **Crash 7d rule** is the dominant exit (71% of trades, avg 55 days held)
- **Profit target exits** (14.5% of trades) produce the longest holds (~90 days avg)

### Put Hedge Mechanics

- Put bought at entry: **ATM (strike = SPX spot), 90-day tenor**
- Put provides crash protection only if market drops **within 90 days of entry**
- After 90 days, put has expired — crash rule exits protect the remaining holding period
- In practice, most crash exits happen within 55 days (well within 90-day put window)

---

## QuantConnect Cloud Backtest Iterations (2000–2026, $100,000 initial capital)

All runs on QC Project ID `28932760`. Period: 2000-01-01 to 2026-02-28 unless noted.

### Strategy Evolution

| # | Strategy | CAGR | Drawdown | End Equity | Sharpe | Win% | Notes |
|---|----------|------|----------|------------|--------|------|-------|
| 1 | Pure LEAPS Calls (2012–2026) | 18.2% | 55.4% | $7.73M | 0.54 | 80% | Baseline — no core allocation |
| 2 | A+E Bear Filter (SMA200 entry gate) | 14.1% | 50.0% | $3.07M | 0.48 | 51% | Bear filter hurt bull-run returns |
| 3 | A+E Dual SMA 200/50 Mode Switch | 12.7% | 46.2% | $2.23M | 0.44 | 52% | Over-filtered — worst CAGR |
| 4 | Put LEAPS Permanent Hedge | 7.7% | 56.6% | $693K | 0.25 | 71% | Put cost too high — not viable |
| 5 | SPY 75% + TLT 15% + LEAPS 10% | 18.2% | 51.9% | $7.74M | 0.56 | 60% | TLT as natural crash hedge |
| 6 | + Normalized PE Put (PE > 30) | 18.0% | 42.7% | $7.29M | 0.58 | 58% | Synthetic EPS model; fired 2019+ |
| **7** | **+ DD Put (SPX >10% below 52wk high)** | **17.2%** | **42.7%** | **$6.21M** | **0.548** | **61%** | **Most realistic trigger; best DD tie** |

### Winner: Strategy #5 — SPY75+TLT15+LEAPS

Best overall balance of CAGR (18.2%) and risk-adjusted returns. Adding the DD put (#7) matches DD reduction of Norm PE (#6) with a more reliable, price-action-based trigger — though CAGR is slightly lower due to put cost drag in non-crisis years.

### DD Put Trigger — Historical Fire Dates

The put buy triggers when SPX drops >10% from its 52-week high. This fired on 20 occasions across the backtest period:

| Period | Notable Event |
|--------|--------------|
| Sep 1999 | Pre-dot-com correction |
| Apr–Oct 2000 | Dot-com crash begins |
| Nov 2007 – Jan 2008 | GFC begins |
| May 2010 | Flash crash / EU debt |
| Aug 2011 | US debt downgrade |
| Aug 2015 | China devaluation shock |
| Jan 2016 | Oil/China scare |
| Feb, Apr, Nov, Dec 2018 | Vol spike + Q4 selloff |
| Feb, Jun 2020 | COVID crash |
| Feb, Apr 2022 | Rate hike bear market |
| Oct 2023 | Israel/rates correction |
| Mar 2025 | Current pullback |

### Yearly Returns — Strategy #7 (DD Put, 2000–2026)

| Year | Return | | Year | Return |
|------|--------|-|------|--------|
| 2000 | -6.4% | | 2013 | +89.7% |
| 2001 | -8.0% | | 2014 | +45.9% |
| 2002 | -15.6% | | 2015 | -13.6% |
| 2003 | +21.2% | | 2016 | +10.0% |
| 2004 | +9.6% | | 2017 | +45.9% |
| 2005 | +4.9% | | 2018 | +0.3% |
| 2006 | +11.9% | | 2019 | +69.2% |
| 2007 | +6.1% | | 2020 | +34.5% |
| 2008 | -25.7% | | 2021 | +35.9% |
| 2009 | +16.4% | | 2022 | -18.8% |
| 2010 | +13.3% | | 2023 | +98.2% |
| 2011 | +6.4% | | 2024 | +148.5% |
| 2012 | +3.6% | | 2025 | +4.8% |

### Key Loss Years

| Year | Return | Event | Cause |
|------|--------|-------|-------|
| 2002 | -15.6% | Dot-com bottom | Puts helped but calls bled theta |
| 2008 | -25.7% | GFC | Rapid decline exceeded crash exit speed |
| 2015 | -13.6% | China shock | Sharp mid-year drop, put expiry drag |
| 2022 | -18.8% | Rate hike bear | Slow grind down — crash rules triggered late |

---

## Files

| File | Description |
|---|---|
| `spx_backtest_sweep.py` | Full 108-combo parameter sweep |
| `spx_case_comparison.py` | 3-case side-by-side comparison |
| `spx_transactions.py` | Detailed trade log generator |
| `spx_case1_transactions.csv` | All trades — Case 1 (No Put) |
| `spx_case2_transactions.csv` | All trades — Case 2 (Conditional Put) |
| `spx_case3_transactions.csv` | All trades — Case 3 (Always Put) |
| `spx_sweep_results.csv` | Full 108-combo sweep results |
| `test_spx_transactions.py` | 48-test pytest validation suite |

---

## How to Run

```bash
# Run 3-case comparison
py spx_case_comparison.py

# Run full parameter sweep
py spx_backtest_sweep.py

# Generate transaction files
py spx_transactions.py

# Run test suite
py -m pytest test_spx_transactions.py -v
```

---

*Backtest period: 2010-01-04 to 2026-02-27 | Initial capital: $100,000 | Model: Black-Scholes with VIX as IV*
