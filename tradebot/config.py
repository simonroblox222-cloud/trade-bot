import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


@dataclass
class RiskConfig:
    risk_per_trade: float = 0.01      # max 1 % of equity lost if the stop is hit
    max_position_pct: float = 0.20    # one position at most 20 % of equity
    max_open_positions: int = 5
    max_daily_loss_pct: float = 0.03  # stop trading for the day after -3 %
    max_drawdown_pct: float = 0.15    # kill switch: stop trading after -15 % from peak


@dataclass
class CostConfig:
    commission_per_share: float = 0.0  # Alpaca stocks are commission free
    slippage_bps: float = 5.0          # 0.05 % per side, conservative for liquid ETFs


@dataclass
class Settings:
    api_key: str = field(default_factory=lambda: os.getenv("ALPACA_API_KEY", ""))
    secret_key: str = field(default_factory=lambda: os.getenv("ALPACA_SECRET_KEY", ""))
    paper: bool = field(default_factory=lambda: os.getenv("ALPACA_PAPER", "true").lower() == "true")
    symbols: list = field(default_factory=lambda: ["SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "USO", "TLT"])
    strategy: str = "rsi2"
    risk: RiskConfig = field(default_factory=RiskConfig)
    costs: CostConfig = field(default_factory=CostConfig)

    def require_keys(self):
        if not self.api_key or not self.secret_key:
            raise SystemExit("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY (see .env.example).")
        if not self.paper:
            raise SystemExit("ALPACA_PAPER is not 'true'. This bot only trades paper accounts.")
