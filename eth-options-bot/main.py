#!/usr/bin/env python3
"""
ETH Options Bot — CLI entry point.

Commands:
    collect-data    Fetch and save live option chain snapshots
    backtest        Run historical backtest
    paper-trade     Run paper trading loop (simulated broker, live data)
    live            Run live trading on Deribit
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure src/ is importable
sys.path.insert(0, str(Path(__file__).parent))

import logging
from datetime import datetime, timezone

import typer

from config.settings import load_config
from src.monitoring.logger import TradeJournal, setup_logging

app = typer.Typer(help="ETH Options Bot — Iron Condor Strategy on Deribit", add_completion=False)


def _get_config(config_path: str = "config/config.yaml"):
    return load_config(config_path)


# ---------------------------------------------------------------------------
# collect-data
# ---------------------------------------------------------------------------

@app.command("collect-data")
def collect_data(
    config_path: str = typer.Option("config/config.yaml", help="Path to config file"),
    duration_hours: float = typer.Option(1.0, help="How long to collect (hours). 0 = one snapshot."),
    interval_s: int = typer.Option(300, help="Seconds between snapshots"),
):
    """Fetch live ETH option chain snapshots from Deribit and store as Parquet."""
    import time
    from src.data.ingestion import DataIngestionService
    from src.data.storage import ParquetStorage
    from src.deribit.rest_client import DeribitRESTClient

    cfg = _get_config(config_path)
    setup_logging(cfg.monitoring.log_level, cfg.monitoring.log_file)
    logger = logging.getLogger(__name__)

    client = DeribitRESTClient(
        base_url=cfg.deribit.api_url,
        client_id=cfg.deribit.client_id,
        client_secret=cfg.deribit.client_secret,
    )
    ingestion = DataIngestionService(client, currency=cfg.strategy.currency)
    storage = ParquetStorage(cfg.storage.parquet_dir)

    deadline = time.time() + duration_hours * 3600 if duration_hours > 0 else time.time() + 1
    snapshots = 0

    logger.info("Starting data collection (duration=%.1fh, interval=%ds)", duration_hours, interval_s)

    while time.time() < deadline:
        try:
            quotes = ingestion.fetch_snapshot()
            storage.save_quotes(quotes)
            snapshots += 1
            logger.info("Snapshot #%d saved (%d quotes)", snapshots, len(quotes))
        except Exception as exc:
            logger.error("Snapshot failed: %s", exc)

        if duration_hours == 0:
            break
        time.sleep(interval_s)

    typer.echo(f"Data collection complete. Saved {snapshots} snapshot(s).")


# ---------------------------------------------------------------------------
# backtest
# ---------------------------------------------------------------------------

@app.command("backtest")
def backtest(
    config_path: str = typer.Option("config/config.yaml", help="Path to config file"),
    start: str = typer.Option("", help="Start date YYYY-MM-DD (overrides config)"),
    end: str = typer.Option("", help="End date YYYY-MM-DD (overrides config)"),
    plot: bool = typer.Option(False, help="Plot equity curve"),
    save_trades: bool = typer.Option(True, help="Save trade records to SQLite"),
):
    """Run historical backtest on stored Parquet data."""
    from src.backtest.engine import BacktestEngine
    from src.data.storage import ParquetStorage, SQLiteStorage

    cfg = _get_config(config_path)
    setup_logging(cfg.monitoring.log_level, cfg.monitoring.log_file)
    logger = logging.getLogger(__name__)

    if start:
        cfg.backtest.start_date = start
    if end:
        cfg.backtest.end_date = end

    storage = ParquetStorage(cfg.storage.parquet_dir)
    engine = BacktestEngine(cfg, storage)

    logger.info("Running backtest %s → %s", cfg.backtest.start_date, cfg.backtest.end_date)
    results = engine.run()

    if not results:
        typer.echo("No results — check that historical data exists in the parquet directory.")
        raise typer.Exit(1)

    engine.print_summary(results)

    if save_trades:
        db = SQLiteStorage(cfg.storage.db_path)
        for trade in results.get("trades", []):
            db.save_trade(trade)
        db.close()
        logger.info("Trades saved to %s", cfg.storage.db_path)

    if plot:
        _plot_equity_curve(results["equity_curve"], results.get("trades", []))


def _plot_equity_curve(equity_curve, trades):
    try:
        import matplotlib.pyplot as plt
        import pandas as pd

        fig, axes = plt.subplots(2, 1, figsize=(14, 8), gridspec_kw={"height_ratios": [3, 1]})

        # Equity curve
        axes[0].plot(equity_curve.index, equity_curve.values, color="steelblue", linewidth=1.5)
        axes[0].set_title("ETH Iron Condor — Equity Curve", fontsize=13)
        axes[0].set_ylabel("Equity (USD)")
        axes[0].grid(True, alpha=0.3)

        # Drawdown
        rolling_max = equity_curve.cummax()
        drawdown = (equity_curve - rolling_max) / rolling_max * 100
        axes[1].fill_between(drawdown.index, drawdown.values, 0, color="red", alpha=0.4)
        axes[1].set_title("Drawdown (%)")
        axes[1].set_ylabel("%")
        axes[1].grid(True, alpha=0.3)

        plt.tight_layout()
        out_path = "logs/equity_curve.png"
        plt.savefig(out_path, dpi=150)
        typer.echo(f"Equity curve saved to {out_path}")
        plt.show()
    except ImportError:
        typer.echo("matplotlib not installed — skipping plot.")


# ---------------------------------------------------------------------------
# paper-trade
# ---------------------------------------------------------------------------

@app.command("paper-trade")
def paper_trade(
    config_path: str = typer.Option("config/config.yaml", help="Path to config file"),
):
    """Run paper trading: live Deribit data, simulated execution."""
    from src.data.ingestion import DataIngestionService
    from src.data.storage import SQLiteStorage, ParquetStorage
    from src.deribit.rest_client import DeribitRESTClient
    from src.execution.simulated_broker import SimulatedBroker
    from src.execution.trading_loop import TradingLoop
    from src.monitoring.notifier import AlertManager, TelegramNotifier, SlackNotifier

    cfg = _get_config(config_path)
    setup_logging(cfg.monitoring.log_level, cfg.monitoring.log_file)
    logger = logging.getLogger(__name__)

    client = DeribitRESTClient(
        base_url=cfg.deribit.api_url,
        client_id=cfg.deribit.client_id,
        client_secret=cfg.deribit.client_secret,
    )
    ingestion = DataIngestionService(client, currency=cfg.strategy.currency)
    broker = SimulatedBroker(
        initial_capital=cfg.backtest.initial_capital,
        fee_per_contract=cfg.backtest.fee_per_contract,
        slippage_pct=cfg.backtest.slippage_pct,
        fill_model=cfg.backtest.fill_model,
    )
    db = SQLiteStorage(cfg.storage.db_path)
    journal = TradeJournal()
    alerts = _build_alerts(cfg)

    loop = TradingLoop(
        config=cfg,
        broker=broker,
        ingestion=ingestion,
        db=db,
        alerts=alerts,
        journal=journal,
        mode="paper",
    )

    typer.echo("Starting paper trading loop. Press Ctrl+C to stop.")
    loop.run()
    db.close()


# ---------------------------------------------------------------------------
# live
# ---------------------------------------------------------------------------

@app.command("live")
def live_trade(
    config_path: str = typer.Option("config/config.yaml", help="Path to config file"),
    confirm: bool = typer.Option(False, "--yes", help="Skip confirmation prompt"),
):
    """Run LIVE trading on Deribit. Requires valid API credentials."""
    from src.data.ingestion import DataIngestionService
    from src.data.storage import SQLiteStorage
    from src.deribit.rest_client import DeribitRESTClient
    from src.execution.deribit_broker import DeribitBroker
    from src.execution.trading_loop import TradingLoop

    cfg = _get_config(config_path)
    setup_logging(cfg.monitoring.log_level, cfg.monitoring.log_file)

    if not cfg.deribit.client_id or not cfg.deribit.client_secret:
        typer.echo("ERROR: DERIBIT_CLIENT_ID and DERIBIT_CLIENT_SECRET must be set.", err=True)
        raise typer.Exit(1)

    if not confirm:
        typer.confirm(
            f"⚠️  LIVE TRADING on {'TESTNET' if cfg.deribit.use_testnet else 'MAINNET'}. "
            "Are you sure?",
            abort=True,
        )

    client = DeribitRESTClient(
        base_url=cfg.deribit.api_url,
        client_id=cfg.deribit.client_id,
        client_secret=cfg.deribit.client_secret,
    )
    ingestion = DataIngestionService(client, currency=cfg.strategy.currency)
    broker = DeribitBroker(client, cfg.execution, currency=cfg.strategy.currency)
    db = SQLiteStorage(cfg.storage.db_path)
    journal = TradeJournal()
    alerts = _build_alerts(cfg)

    loop = TradingLoop(
        config=cfg,
        broker=broker,
        ingestion=ingestion,
        db=db,
        alerts=alerts,
        journal=journal,
        mode="live",
    )

    typer.echo("Starting LIVE trading loop. Press Ctrl+C to stop.")
    loop.run()
    db.close()


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

@app.command("report")
def report(
    config_path: str = typer.Option("config/config.yaml", help="Path to config file"),
):
    """Print trade history and performance report from the SQLite database."""
    import pandas as pd
    from src.data.storage import SQLiteStorage
    from src.backtest.engine import compute_metrics

    cfg = _get_config(config_path)
    setup_logging(cfg.monitoring.log_level, cfg.monitoring.log_file)

    db = SQLiteStorage(cfg.storage.db_path)
    trades_df = db.load_trades()
    snapshots_df = db.load_account_snapshots()
    db.close()

    if trades_df.empty:
        typer.echo("No trades recorded yet.")
        return

    typer.echo(f"\nTrade History ({len(trades_df)} trades):\n")
    typer.echo(trades_df[["entry_time", "exit_time", "short_call_strike", "short_put_strike",
                            "credit_received", "max_loss", "realized_pnl", "exit_reason"]].to_string(index=False))

    if not snapshots_df.empty:
        equity = pd.Series(
            snapshots_df["equity"].values,
            index=pd.to_datetime(snapshots_df["timestamp"]),
        )
        from src.data.models import TradeRecord
        from datetime import datetime
        trade_records = []
        for _, row in trades_df.iterrows():
            if row.get("realized_pnl") is not None:
                from src.data.models import TradeRecord
                tr = TradeRecord(
                    id=row["id"], entry_time=datetime.fromisoformat(row["entry_time"]),
                    exit_time=None, underlying_at_entry=0, underlying_at_exit=None,
                    short_call_strike=0, long_call_strike=0, short_put_strike=0, long_put_strike=0,
                    expiry=datetime.fromisoformat(row["expiry"]) if row.get("expiry") else datetime.utcnow(),
                    credit_received=row["credit_received"], max_loss=row["max_loss"],
                    realized_pnl=row["realized_pnl"], exit_reason=row["exit_reason"],
                    iv_percentile_at_entry=row.get("iv_percentile_at_entry", 0),
                )
                trade_records.append(tr)

        metrics = compute_metrics(equity, trade_records)
        typer.echo("\nPerformance Metrics:\n")
        for k, v in metrics.items():
            typer.echo(f"  {k:<28}: {v:.4f}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_alerts(cfg) -> "AlertManager":
    from src.monitoring.notifier import AlertManager, TelegramNotifier, SlackNotifier

    telegram = None
    slack = None
    if cfg.monitoring.telegram_token and cfg.monitoring.telegram_chat_id:
        telegram = TelegramNotifier(cfg.monitoring.telegram_token, cfg.monitoring.telegram_chat_id)
    if cfg.monitoring.slack_webhook:
        slack = SlackNotifier(cfg.monitoring.slack_webhook)
    return AlertManager(telegram=telegram, slack=slack)


if __name__ == "__main__":
    app()
