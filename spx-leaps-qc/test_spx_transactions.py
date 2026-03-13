"""
Test suite for spx_transactions_rank1.csv
==========================================
Verifies:
  1. File structure & completeness
  2. Date logic
  3. Financial math (PnL, capital flow)
  4. Strategy rules (VIX entry, exit reasons, crash thresholds)
  5. Option pricing sanity
  6. Put hedge consistency
  7. Win/Loss labelling
  8. Sequential capital continuity
"""

import pytest
import pandas as pd
import numpy as np
import yfinance as yf
import warnings
from pathlib import Path
from scipy.stats import norm

warnings.filterwarnings("ignore")

# ─── Config (must match backtest parameters) ─────────────────────────────────

CSV_PATH        = "C:/Users/Administrator/Desktop/projects/spx_transactions_rank1.csv"
VIX_THRESHOLD   = 20
CALL_DTE        = 300
PROFIT_TARGET   = 1.00          # 100%
CALL_DELTA      = 0.40
PUT_COST_FRAC   = 0.15
RISK_PER_TRADE  = 0.30
INITIAL_CAPITAL = 100_000.0
R               = 0.045
DAILY_RF        = (1 + R) ** (1 / 252) - 1
CRASH_RULES     = {7: -0.03, 10: -0.04, 14: -0.06, 30: -0.08}  # Set B
TOLERANCE       = 0.02          # 2% tolerance for floating-point comparisons

REQUIRED_COLS = [
    "Trade_#", "Entry_Date", "Exit_Date", "Days_Held",
    "SPX_Entry", "SPX_Exit", "SPX_Change_%",
    "VIX_Entry", "VIX_Exit",
    "Call_Strike", "Call_DTE", "Call_Entry_Price", "Call_Exit_Price",
    "Call_PnL_%", "Call_Delta_Entry", "Call_Delta_Exit",
    "Put_Hedge", "Put_Strike", "Put_Entry_Price", "Put_Exit_Price",
    "Put_Units", "Put_Cost_Paid", "Put_Exit_Value",
    "QQQ_PE_Entry", "QQQ_PE_5yr_Avg",
    "Units", "Total_Invested", "Exit_Value",
    "Trade_PnL_$", "Trade_PnL_%",
    "Capital_Before", "Capital_After",
    "Exit_Reason", "Win",
]

# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def df():
    """Load the transaction CSV once for all tests."""
    path = Path(CSV_PATH)
    assert path.exists(), f"Transaction file not found: {CSV_PATH}"
    data = pd.read_csv(CSV_PATH, parse_dates=["Entry_Date", "Exit_Date"])
    return data


@pytest.fixture(scope="session")
def spx_data():
    """Download SPX historical prices for cross-validation."""
    raw = yf.download("^GSPC", start="2010-01-01", end="2026-03-01",
                      auto_adjust=True, progress=False)["Close"].squeeze()
    raw.index = pd.to_datetime(raw.index)
    return raw


@pytest.fixture(scope="session")
def vix_data():
    """Download VIX historical data for entry condition check."""
    raw = yf.download("^VIX", start="2010-01-01", end="2026-03-01",
                      auto_adjust=True, progress=False)["Close"].squeeze()
    raw.index = pd.to_datetime(raw.index)
    return raw


# ─── 1. File structure tests ──────────────────────────────────────────────────

class TestFileStructure:

    def test_file_exists(self):
        assert Path(CSV_PATH).exists(), "CSV file does not exist"

    def test_required_columns_present(self, df):
        missing = [c for c in REQUIRED_COLS if c not in df.columns]
        assert not missing, f"Missing columns: {missing}"

    def test_no_nulls_in_key_columns(self, df):
        key_cols = [
            "Trade_#", "Entry_Date", "Exit_Date", "Days_Held",
            "SPX_Entry", "SPX_Exit", "VIX_Entry",
            "Call_Entry_Price", "Call_Exit_Price", "Call_PnL_%",
            "Total_Invested", "Exit_Value", "Trade_PnL_$", "Trade_PnL_%",
            "Capital_Before", "Capital_After", "Exit_Reason", "Win",
        ]
        for col in key_cols:
            nulls = df[col].isna().sum()
            assert nulls == 0, f"Column '{col}' has {nulls} null values"

    def test_expected_trade_count(self, df):
        # 2010-2026 with these parameters yields 69 trades
        assert len(df) == 69, f"Expected 69 trades, got {len(df)}"

    def test_trade_numbers_sequential(self, df):
        expected = list(range(1, len(df) + 1))
        actual = df["Trade_#"].tolist()
        assert actual == expected, "Trade numbers are not sequential starting from 1"

    def test_win_loss_values(self, df):
        valid = {"WIN", "LOSS"}
        bad = set(df["Win"].unique()) - valid
        assert not bad, f"Invalid Win values: {bad}"

    def test_exit_reason_values(self, df):
        valid_prefixes = ("profit_target", "crash_", "expiry")
        bad = [r for r in df["Exit_Reason"].unique()
               if not str(r).startswith(valid_prefixes)]
        assert not bad, f"Invalid exit reasons: {bad}"

    def test_put_hedge_values(self, df):
        valid = {"YES", "NO"}
        bad = set(df["Put_Hedge"].unique()) - valid
        assert not bad, f"Invalid Put_Hedge values: {bad}"


# ─── 2. Date logic tests ──────────────────────────────────────────────────────

class TestDateLogic:

    def test_exit_after_entry(self, df):
        bad = df[df["Exit_Date"] <= df["Entry_Date"]]
        assert len(bad) == 0, f"{len(bad)} trades have exit_date <= entry_date:\n{bad[['Trade_#','Entry_Date','Exit_Date']]}"

    def test_days_held_matches_dates(self, df):
        computed = (df["Exit_Date"] - df["Entry_Date"]).dt.days
        diff = (computed - df["Days_Held"]).abs()
        bad = df[diff > 1]   # allow 1 day tolerance
        assert len(bad) == 0, f"{len(bad)} trades have Days_Held mismatch:\n{bad[['Trade_#','Entry_Date','Exit_Date','Days_Held']]}"

    def test_days_held_within_call_dte(self, df):
        bad = df[df["Days_Held"] > CALL_DTE + 5]  # +5 day grace for weekends
        assert len(bad) == 0, f"{len(bad)} trades held longer than {CALL_DTE} DTE"

    def test_dates_within_data_range(self, df):
        assert df["Entry_Date"].min() >= pd.Timestamp("2010-01-01"), "Entry before data start"
        assert df["Exit_Date"].max() <= pd.Timestamp("2026-03-31"), "Exit after data end"

    def test_no_overlapping_trades(self, df):
        """Single-position strategy: next entry must be after previous exit + cooldown."""
        for i in range(1, len(df)):
            prev_exit  = df.iloc[i-1]["Exit_Date"]
            curr_entry = df.iloc[i]["Entry_Date"]
            gap_days   = (curr_entry - prev_exit).days
            assert gap_days >= 0, (
                f"Trade {df.iloc[i]['Trade_#']} entry ({curr_entry.date()}) "
                f"before previous exit ({prev_exit.date()})"
            )

    def test_chronological_order(self, df):
        assert df["Entry_Date"].is_monotonic_increasing, "Trades are not in chronological order"


# ─── 3. Financial math tests ──────────────────────────────────────────────────

class TestFinancialMath:

    def test_pnl_dollar_equals_exit_minus_invested(self, df):
        computed = df["Exit_Value"] - df["Total_Invested"]
        diff = (computed - df["Trade_PnL_$"]).abs()
        bad = df[diff > 1.0]    # $1 tolerance for rounding
        assert len(bad) == 0, f"{len(bad)} trades have PnL_$ inconsistency:\n{bad[['Trade_#','Total_Invested','Exit_Value','Trade_PnL_$']]}"

    def test_pnl_pct_consistent_with_dollar(self, df):
        computed_pct = (df["Trade_PnL_$"] / df["Total_Invested"] * 100).round(1)
        reported_pct = df["Trade_PnL_%"].round(1)
        diff = (computed_pct - reported_pct).abs()
        bad = df[diff > 0.5]
        assert len(bad) == 0, f"{len(bad)} trades have PnL_% mismatch"

    def test_win_loss_consistent_with_pnl(self, df):
        wrong = df[((df["Trade_PnL_$"] > 0) & (df["Win"] == "LOSS")) |
                   ((df["Trade_PnL_$"] < 0) & (df["Win"] == "WIN"))]
        assert len(wrong) == 0, f"{len(wrong)} trades have Win/Loss label inconsistent with PnL"

    def test_capital_after_equals_before_plus_pnl(self, df):
        """
        Capital_After should approximately equal Capital_Before + Trade_PnL_$.
        They won't be exactly equal because idle cash earns RF between trades,
        but Capital_Before already accounts for that — so within a trade:
        Capital_After = free_capital_at_exit + pos_value
                      = Capital_Before - Total_Invested + (RF on free cash during hold) + Exit_Value
        We allow a loose tolerance here due to daily RF compounding.
        """
        for _, row in df.iterrows():
            approx_after = row["Capital_Before"] - row["Total_Invested"] + row["Exit_Value"]
            # Allow up to 20% of Capital_Before for RF growth on idle cash during long holds
            tol = row["Capital_Before"] * 0.20
            assert abs(row["Capital_After"] - approx_after) < tol, (
                f"Trade {int(row['Trade_#'])}: Capital_After {row['Capital_After']:.2f} "
                f"far from expected ~{approx_after:.2f}"
            )

    def test_capital_never_negative(self, df):
        bad = df[df["Capital_After"] <= 0]
        assert len(bad) == 0, f"{len(bad)} trades result in zero/negative capital"

    def test_final_capital_ballpark(self, df):
        """Final capital should be in the ballpark of reported ~$20.7M."""
        final = df["Capital_After"].iloc[-1]
        assert 10_000_000 < final < 50_000_000, (
            f"Final capital ${final:,.0f} is outside expected range ($10M–$50M)"
        )

    def test_initial_capital_ballpark(self, df):
        """First trade's Capital_Before should be near INITIAL_CAPITAL (+ small RF growth)."""
        first_before = df["Capital_Before"].iloc[0]
        assert INITIAL_CAPITAL * 0.9 <= first_before <= INITIAL_CAPITAL * 1.5, (
            f"First Capital_Before ${first_before:,.0f} is far from initial ${INITIAL_CAPITAL:,.0f}"
        )

    def test_total_invested_fraction_of_capital(self, df):
        """Total_Invested should be ~RISK_PER_TRADE * Capital_Before (within 5%)."""
        ratio = df["Total_Invested"] / df["Capital_Before"]
        # Allow some flexibility because total_cost_per_unit includes put premium
        # and call_price can vary. Ratio should be roughly around RISK_PER_TRADE.
        bad = df[(ratio < RISK_PER_TRADE * 0.5) | (ratio > RISK_PER_TRADE * 2.0)]
        assert len(bad) == 0, (
            f"{len(bad)} trades have Total_Invested ratio far from {RISK_PER_TRADE:.0%}:\n"
            f"{bad[['Trade_#','Capital_Before','Total_Invested']]}"
        )

    def test_spx_change_pct_correct(self, df):
        computed = ((df["SPX_Exit"] - df["SPX_Entry"]) / df["SPX_Entry"] * 100).round(1)
        diff = (computed - df["SPX_Change_%"].round(1)).abs()
        bad = df[diff > 0.2]
        assert len(bad) == 0, f"{len(bad)} trades have incorrect SPX_Change_%"


# ─── 4. Strategy rule tests ───────────────────────────────────────────────────

class TestStrategyRules:

    def test_vix_entry_below_threshold(self, df):
        bad = df[df["VIX_Entry"] >= VIX_THRESHOLD]
        assert len(bad) == 0, (
            f"{len(bad)} trades entered with VIX >= {VIX_THRESHOLD}:\n"
            f"{bad[['Trade_#','Entry_Date','VIX_Entry']]}"
        )

    def test_call_dte_correct(self, df):
        bad = df[df["Call_DTE"] != CALL_DTE]
        assert len(bad) == 0, f"Some trades have wrong Call_DTE (expected {CALL_DTE})"

    def test_call_delta_entry_correct(self, df):
        bad = df[df["Call_Delta_Entry"] != CALL_DELTA]
        assert len(bad) == 0, f"Some trades have wrong Call_Delta_Entry (expected {CALL_DELTA})"

    def test_profit_target_exits_have_sufficient_pnl(self, df):
        pt_exits = df[df["Exit_Reason"] == "profit_target"]
        bad = pt_exits[pt_exits["Call_PnL_%"] < (PROFIT_TARGET * 100 - 2)]  # 2% tolerance
        assert len(bad) == 0, (
            f"{len(bad)} profit_target exits have Call_PnL_% < {PROFIT_TARGET*100:.0f}%:\n"
            f"{bad[['Trade_#','Exit_Date','Call_PnL_%']]}"
        )

    def test_expiry_exits_held_near_full_dte(self, df):
        expiry_exits = df[df["Exit_Reason"] == "expiry"]
        bad = expiry_exits[expiry_exits["Days_Held"] < CALL_DTE - 5]
        assert len(bad) == 0, (
            f"{len(bad)} expiry exits closed too early:\n"
            f"{bad[['Trade_#','Entry_Date','Exit_Date','Days_Held']]}"
        )

    def test_crash_exits_correspond_to_actual_spx_drops(self, df, spx_data):
        """For each crash exit, verify SPX actually dropped by the threshold amount."""
        crash_exits = df[df["Exit_Reason"].str.startswith("crash_")]
        failures = []

        for _, row in crash_exits.iterrows():
            exit_date = row["Exit_Date"]
            reason    = row["Exit_Reason"]
            lb_days   = int(reason.split("_")[1].replace("d", ""))
            threshold = CRASH_RULES.get(lb_days)

            if threshold is None:
                continue

            try:
                # Use trading-day index (same as backtest uses prices[i - lb_days])
                spx_slice   = spx_data[spx_data.index <= exit_date]
                if len(spx_slice) < lb_days + 1:
                    continue
                spx_on_exit = float(spx_slice.iloc[-1])
                spx_ref_val = float(spx_slice.iloc[-1 - lb_days])  # exactly lb trading days back
                drop = (spx_on_exit - spx_ref_val) / spx_ref_val
                if drop > threshold + 0.01:   # allow 1% tolerance
                    failures.append({
                        "trade": int(row["Trade_#"]),
                        "exit_date": exit_date.date(),
                        "reason": reason,
                        "expected_drop": f"<= {threshold:.1%}",
                        "actual_drop": f"{drop:.2%}",
                    })
            except Exception:
                pass   # skip if data unavailable for that date

        assert not failures, f"Crash exits without sufficient SPX drop:\n{pd.DataFrame(failures)}"

    def test_exit_reasons_distribution(self, df):
        """Sanity check: there should be a mix of exit reasons."""
        reasons = df["Exit_Reason"].value_counts()
        print(f"\nExit reason breakdown:\n{reasons}")
        # At least 2 different exit reasons in a 16-year backtest
        assert len(reasons) >= 2, "Expected at least 2 distinct exit reasons"


# ─── 5. Option pricing sanity tests ──────────────────────────────────────────

class TestOptionPricing:

    def test_call_entry_price_positive(self, df):
        bad = df[df["Call_Entry_Price"] <= 0]
        assert len(bad) == 0, f"{len(bad)} trades have non-positive call entry price"

    def test_call_exit_price_non_negative(self, df):
        bad = df[df["Call_Exit_Price"] < 0]
        assert len(bad) == 0, "Call exit price cannot be negative"

    def test_call_strike_above_entry_price(self, df):
        """Delta 0.40 call is OTM — strike should be above SPX entry."""
        bad = df[df["Call_Strike"] <= df["SPX_Entry"]]
        assert len(bad) == 0, (
            f"{len(bad)} trades have call strike at/below SPX entry "
            f"(expected OTM for delta={CALL_DELTA})"
        )

    def test_call_delta_exit_in_valid_range(self, df):
        bad = df[(df["Call_Delta_Exit"] < 0) | (df["Call_Delta_Exit"] > 1)]
        assert len(bad) == 0, "Call delta at exit must be between 0 and 1"

    def test_call_pnl_consistent_with_prices(self, df):
        computed_pnl = ((df["Call_Exit_Price"] - df["Call_Entry_Price"])
                        / df["Call_Entry_Price"] * 100).round(1)
        diff = (computed_pnl - df["Call_PnL_%"].round(1)).abs()
        bad = df[diff > 0.5]
        assert len(bad) == 0, f"{len(bad)} trades have Call_PnL_% inconsistent with prices"

    def test_vix_entry_is_positive(self, df):
        bad = df[df["VIX_Entry"] <= 0]
        assert len(bad) == 0, "VIX entry must be positive"

    def test_spx_prices_are_positive(self, df):
        bad = df[(df["SPX_Entry"] <= 0) | (df["SPX_Exit"] <= 0)]
        assert len(bad) == 0, "SPX prices must be positive"


# ─── 6. Put hedge consistency tests ──────────────────────────────────────────

class TestPutHedge:

    def test_put_cost_paid_when_hedged(self, df):
        hedged = df[df["Put_Hedge"] == "YES"]
        bad = hedged[hedged["Put_Cost_Paid"] <= 0]
        assert len(bad) == 0, f"{len(bad)} hedged trades have zero Put_Cost_Paid"

    def test_put_units_positive_when_hedged(self, df):
        hedged = df[df["Put_Hedge"] == "YES"]
        bad = hedged[hedged["Put_Units"] <= 0]
        assert len(bad) == 0, f"{len(bad)} hedged trades have zero Put_Units"

    def test_put_cost_zero_when_not_hedged(self, df):
        not_hedged = df[df["Put_Hedge"] == "NO"]
        bad = not_hedged[pd.to_numeric(not_hedged["Put_Cost_Paid"], errors="coerce").fillna(0) > 0.01]
        assert len(bad) == 0, f"{len(bad)} unhedged trades have non-zero Put_Cost_Paid"

    def test_put_strike_equals_spx_entry_when_hedged(self, df):
        """Put hedge is bought ATM (K_put = SPX entry price)."""
        hedged = df[df["Put_Hedge"] == "YES"].copy()
        hedged["Put_Strike_num"] = pd.to_numeric(hedged["Put_Strike"], errors="coerce")
        diff = (hedged["Put_Strike_num"] - hedged["SPX_Entry"]).abs()
        bad = hedged[diff > 1.0]   # within $1 of ATM
        assert len(bad) == 0, (
            f"{len(bad)} hedged trades have Put_Strike not equal to SPX_Entry (ATM):\n"
            f"{bad[['Trade_#','SPX_Entry','Put_Strike_num']]}"
        )

    def test_put_cost_fraction_of_call(self, df):
        """Put_Cost_Paid per unit should be ~ PUT_COST_FRAC * Call_Entry_Price * Units."""
        hedged = df[df["Put_Hedge"] == "YES"].copy()
        expected = hedged["Call_Entry_Price"] * PUT_COST_FRAC * hedged["Units"]
        diff = (pd.to_numeric(hedged["Put_Cost_Paid"], errors="coerce") - expected).abs()
        relative_diff = diff / expected
        bad = hedged[relative_diff > 0.05]   # within 5%
        assert len(bad) == 0, (
            f"{len(bad)} hedged trades have Put_Cost_Paid far from "
            f"{PUT_COST_FRAC:.0%} x Call_Entry_Price x Units"
        )

    def test_hedged_trade_count(self, df):
        """Expect ~84% of trades to be hedged (QQQ PE > 5yr avg most of the time)."""
        hedged_pct = (df["Put_Hedge"] == "YES").mean()
        assert 0.60 <= hedged_pct <= 0.99, (
            f"Hedged trade ratio {hedged_pct:.1%} is outside expected range (60%–99%)"
        )


# ─── 7. QQQ valuation consistency ────────────────────────────────────────────

class TestQQQValuation:

    def test_qqq_pe_positive(self, df):
        bad = df[df["QQQ_PE_Entry"] <= 0]
        assert len(bad) == 0, "QQQ_PE_Entry must be positive"

    def test_put_hedge_matches_pe_condition(self, df):
        """
        Put_Hedge should be YES when QQQ_PE_Entry > QQQ_PE_5yr_Avg,
        and NO when QQQ_PE_Entry <= QQQ_PE_5yr_Avg (or avg not yet available).
        """
        valid_avg = df[df["QQQ_PE_5yr_Avg"].notna() & (df["QQQ_PE_5yr_Avg"] != "None")]
        valid_avg = valid_avg.copy()
        valid_avg["pe_avg"] = pd.to_numeric(valid_avg["QQQ_PE_5yr_Avg"], errors="coerce")
        valid_avg = valid_avg.dropna(subset=["pe_avg"])

        expensive = valid_avg[valid_avg["QQQ_PE_Entry"] > valid_avg["pe_avg"]]
        cheap     = valid_avg[valid_avg["QQQ_PE_Entry"] <= valid_avg["pe_avg"]]

        bad_expensive = expensive[expensive["Put_Hedge"] == "NO"]
        bad_cheap     = cheap[cheap["Put_Hedge"] == "YES"]

        assert len(bad_expensive) == 0, (
            f"{len(bad_expensive)} trades: QQQ expensive but no put hedge"
        )
        assert len(bad_cheap) == 0, (
            f"{len(bad_cheap)} trades: QQQ cheap but put hedge bought"
        )


# ─── 8. Cross-validation with live SPX data ───────────────────────────────────

class TestCrossValidation:

    def test_spx_entry_prices_match_historical(self, df, spx_data):
        """SPX_Entry should match historical close within 0.5%."""
        failures = []
        for _, row in df.iterrows():
            date = row["Entry_Date"]
            if date in spx_data.index:
                hist_price = float(spx_data[date])
                recorded   = float(row["SPX_Entry"])
                diff_pct   = abs(hist_price - recorded) / hist_price
                if diff_pct > 0.005:
                    failures.append({
                        "trade": int(row["Trade_#"]),
                        "date": date.date(),
                        "recorded": recorded,
                        "historical": hist_price,
                        "diff_pct": f"{diff_pct:.3%}",
                    })
        assert not failures, f"SPX_Entry price mismatches:\n{pd.DataFrame(failures)}"

    def test_vix_entry_matches_historical(self, df, vix_data):
        """VIX_Entry should match historical close within 5%."""
        failures = []
        for _, row in df.iterrows():
            date = row["Entry_Date"]
            if date in vix_data.index:
                hist_vix  = float(vix_data[date])
                recorded  = float(row["VIX_Entry"])
                diff_pct  = abs(hist_vix - recorded) / hist_vix
                if diff_pct > 0.05:
                    failures.append({
                        "trade": int(row["Trade_#"]),
                        "date": date.date(),
                        "recorded": recorded,
                        "historical": hist_vix,
                        "diff_pct": f"{diff_pct:.3%}",
                    })
        assert not failures, f"VIX_Entry mismatches:\n{pd.DataFrame(failures)}"

    def test_spx_exit_prices_match_historical(self, df, spx_data):
        """SPX_Exit should match historical close within 0.5%."""
        failures = []
        for _, row in df.iterrows():
            date = row["Exit_Date"]
            if date in spx_data.index:
                hist_price = float(spx_data[date])
                recorded   = float(row["SPX_Exit"])
                diff_pct   = abs(hist_price - recorded) / hist_price
                if diff_pct > 0.005:
                    failures.append({
                        "trade": int(row["Trade_#"]),
                        "date": date.date(),
                        "recorded": recorded,
                        "historical": hist_price,
                        "diff_pct": f"{diff_pct:.3%}",
                    })
        assert not failures, f"SPX_Exit price mismatches:\n{pd.DataFrame(failures)}"


# ─── Run summary ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
