from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Credentials pasted into hosting dashboards often pick up a stray leading
    # or trailing space. A space in an API key/header value makes the HTTP
    # client raise a bare "Connection error", and a space in a chat id makes
    # Telegram return 400. Strip whitespace from every credential so this can
    # never silently break the bot again.
    @field_validator(
        "polymarket_api_key",
        "polymarket_private_key",
        "newsapi_key",
        "gnews_api_key",
        "thenewsapi_key",
        "anthropic_api_key",
        "anthropic_model",
        "telegram_bot_token",
        "telegram_chat_id",
        "database_url",
        mode="after",
    )
    @classmethod
    def _strip_whitespace(cls, v: str) -> str:
        return v.strip() if isinstance(v, str) else v

    # --- Polymarket ---
    polymarket_api_key: str = Field(default="", description="Polymarket CLOB API key")
    polymarket_private_key: str = Field(default="", description="Wallet private key (hex)")
    polymarket_host: str = "https://clob.polymarket.com"
    polymarket_chain_id: int = 137  # Polygon mainnet

    # --- News ---
    newsapi_key: str = Field(default="", description="NewsAPI.org key")
    gnews_api_key: str = Field(default="", description="GNews API key (backup)")
    thenewsapi_key: str = Field(default="", description="TheNewsAPI.com token (primary)")

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
    scan_interval_minutes: int = 60        # how often Claude scans for new edges (costs money)
    position_check_minutes: int = 1        # how often open positions are priced (free Polymarket calls)
    profit_hold_hours: int = 24            # close a position in profit after this many hours
    max_hold_hours: int = 36               # hard cap: force-close ANY open position after this many hours
    stop_loss_pct: float = 0.05            # cut a position once it's down this % from entry (5%)
    max_markets_per_scan: int = 30
    min_ev_threshold: float = 0.05         # 5% minimum EV
    min_volume_usd: float = 50_000.0       # Min market volume — higher = more liquid = smaller price gaps
    min_implied_prob: float = 0.10         # Skip markets priced below 10% or above 90%
    min_win_probability: float = 0.55      # Only bet sides we expect to win >=55% of the time
    max_edge: float = 0.35                 # Reject implausibly large disagreements with the market
    min_hours_to_resolution: int = 168     # Skip markets resolving within 7 days (avoids resolution-driven price jumps)
    one_bet_per_market: bool = True        # Never re-enter a market the bot has already traded

    # --- Anomaly Detection ---
    volume_spike_multiplier: float = 3.0   # 3× average = spike
    price_move_threshold: float = 0.10     # 10% price move = anomaly

    # --- Schedule / time zone ---
    # IANA timezone used for the scheduler (when daily/weekly/monthly summaries
    # fire) and for the times shown in Telegram messages. Default: Eastern (Canada).
    timezone: str = "America/Toronto"

    # --- Telegram ---
    telegram_bot_token: str = Field(default="", description="Telegram Bot API token from @BotFather")
    telegram_chat_id: str = Field(default="", description="Telegram chat ID to receive notifications")

    # --- Dashboard ---
    dashboard_port: int = 8501
    top_opportunities: int = 10


settings = Settings()
