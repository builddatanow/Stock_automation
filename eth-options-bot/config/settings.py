from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


ROOT = Path(__file__).parent.parent


class DeribitConfig(BaseModel):
    base_url: str = "https://www.deribit.com"
    ws_url: str = "wss://www.deribit.com/ws/api/v2"
    testnet_base_url: str = "https://test.deribit.com"
    testnet_ws_url: str = "wss://test.deribit.com/ws/api/v2"
    client_id: str = ""
    client_secret: str = ""
    use_testnet: bool = True

    @property
    def api_url(self) -> str:
        return self.testnet_base_url if self.use_testnet else self.base_url

    @property
    def websocket_url(self) -> str:
        return self.testnet_ws_url if self.use_testnet else self.ws_url


class StrategyConfig(BaseModel):
    name: str = "weekly_iron_condor"
    underlying: str = "ETH"
    currency: str = "ETH"
    target_dte_min: int = 5
    target_dte_max: int = 10
    short_delta_min: float = 0.10
    short_delta_max: float = 0.15
    wing_delta_min: float = 0.03
    wing_delta_max: float = 0.05
    take_profit_pct: float = 0.50
    stop_loss_multiplier: float = 2.0
    close_dte: int = 1
    iv_percentile_min: float = 50.0
    max_daily_move_pct: float = 6.0


class RiskConfig(BaseModel):
    account_size: float = 2200.0
    max_risk_per_trade_pct: float = 0.10
    max_open_positions: int = 1
    daily_loss_limit_pct: float = 0.05

    @property
    def max_risk_per_trade(self) -> float:
        return self.account_size * self.max_risk_per_trade_pct

    @property
    def daily_loss_limit(self) -> float:
        return self.account_size * self.daily_loss_limit_pct


class BacktestConfig(BaseModel):
    start_date: str = "2023-01-01"
    end_date: str = "2024-12-31"
    initial_capital: float = 2200.0
    fee_per_contract: float = 0.0003
    slippage_pct: float = 0.001
    fill_model: str = "mid"  # "mid" | "bid_ask"


class ExecutionConfig(BaseModel):
    order_retry_attempts: int = 3
    order_retry_delay_s: float = 2.0
    order_timeout_s: float = 30.0


class MonitoringConfig(BaseModel):
    log_level: str = "INFO"
    log_file: str = "logs/trading.log"
    telegram_token: str = ""
    telegram_chat_id: str = ""
    slack_webhook: str = ""
    whatsapp_phone: str = ""    # international format, no + or spaces e.g. 447911123456
    whatsapp_apikey: str = ""   # received from CallMeBot


class StorageConfig(BaseModel):
    db_path: str = "data/options.db"
    parquet_dir: str = "data/parquet"


class AppConfig(BaseModel):
    deribit: DeribitConfig = Field(default_factory=DeribitConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)


def load_config(path: str | Path | None = None) -> AppConfig:
    if path is None:
        path = ROOT / "config" / "config.yaml"

    with open(path) as f:
        raw = yaml.safe_load(f)

    # allow env var overrides
    if os.getenv("DERIBIT_CLIENT_ID"):
        raw.setdefault("deribit", {})["client_id"] = os.environ["DERIBIT_CLIENT_ID"]
    if os.getenv("DERIBIT_CLIENT_SECRET"):
        raw.setdefault("deribit", {})["client_secret"] = os.environ["DERIBIT_CLIENT_SECRET"]
    if os.getenv("DERIBIT_TESTNET"):
        raw.setdefault("deribit", {})["use_testnet"] = os.environ["DERIBIT_TESTNET"].lower() == "true"

    return AppConfig(**raw)
