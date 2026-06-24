from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # --- Polymarket ---
    polymarket_api_key: str = Field(default="", description="Polymarket CLOB API key")
    polymarket_private_key: str = Field(default="", description="Wallet private key (hex)")
    polymarket_host: str = "https://clob.polymarket.com"
    polymarket_chain_id: int = 137  # Polygon mainnet

    # --- News ---
    newsapi_key: str = Field(default="", description="NewsAPI.org key")
    gnews_api_key: str = Field(default="", description="GNews API key (backup)")

    # --- LLM ---
    anthropic_api_key: str = Field(default="", description="Anthropic Claude API key")
    anthropic_model: str = "claude-haiku-4-5-20251001"

    # --- Database ---
    # Railway provides DATABASE_URL with postgres:// prefix; SQLAlchemy needs postgresql://
    database_url: str = f"sqlite:///{BASE_DIR}/data/polymarket.db"

    @property
    def db_url(self) -> str:
        url = self.database_url
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        return url

    # --- Risk Management ---
    account_equity_usd: float = Field(default=1000.0, description="Total account equity in USD")
    max_trade_risk_pct: float = 0.01       # 1% max per trade
    kelly_fraction: float = 0.25           # 25% fractional Kelly
    max_daily_risk_pct: float = 0.05       # 5% max daily loss
    max_category_exposure_pct: float = 0.20  # 20% max in one category
    min_liquidity_usd: float = 10_000.0    # Min 24h volume

    # --- Scanner ---
    scan_interval_minutes: int = 60
    max_markets_per_scan: int = 30
    min_ev_threshold: float = 0.05         # 5% minimum EV
    min_volume_usd: float = 25_000.0       # Min market volume to consider
    min_hours_to_resolution: int = 48      # Skip markets resolving too soon

    # --- Anomaly Detection ---
    volume_spike_multiplier: float = 3.0   # 3× average = spike
    price_move_threshold: float = 0.10     # 10% price move = anomaly

    # --- Telegram ---
    telegram_bot_token: str = Field(default="", description="Telegram Bot API token from @BotFather")
    telegram_chat_id: str = Field(default="", description="Telegram chat ID to receive notifications")

    # --- Dashboard ---
    dashboard_port: int = 8501
    top_opportunities: int = 10


settings = Settings()
