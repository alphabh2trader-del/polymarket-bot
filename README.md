# Polymarket Trading Bot

A quantitative prediction market scanner that identifies mispriced markets on Polymarket using news analysis, Claude AI probability estimation, and Kelly criterion position sizing.

## Quick Start

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure API keys

```bash
cp config/.env.example .env
# Edit .env and add your API keys (see API Keys section below)
```

### 3. Verify connectivity

```bash
python src/main.py check
```

### 4. Start the scanner

```bash
python src/main.py scan
```

### 5. Open the dashboard

```bash
python src/main.py dashboard
# → http://localhost:8501
```

---

## Commands

| Command | Description |
|---|---|
| `python src/main.py scan` | Start 15-minute scan loop |
| `python src/main.py dashboard` | Launch Streamlit dashboard |
| `python src/main.py report` | Print top opportunities to console |
| `python src/main.py backtest --start 2024-01-01 --end 2024-12-31` | Run backtest |
| `python src/main.py check` | Test API connectivity |

---

## API Keys Required

### 1. Anthropic Claude API (required)
- Go to: https://console.anthropic.com/keys
- Create a new API key
- Add to `.env`: `ANTHROPIC_API_KEY=sk-ant-...`

### 2. NewsAPI (recommended, free tier)
- Go to: https://newsapi.org/register
- Free tier: 100 requests/day
- Add to `.env`: `NEWSAPI_KEY=...`

### 3. GNews (optional backup)
- Go to: https://gnews.io
- Free tier: 100 requests/day
- Add to `.env`: `GNEWS_API_KEY=...`

### 4. Polymarket (read-only is free, no key needed for market data)
- Trading requires a Polygon wallet private key
- Add to `.env`: `POLYMARKET_PRIVATE_KEY=...`

> **Note:** RSS feeds (Reuters, AP, BBC) work without any API key.

---

## Project Structure

```
POLYMARKET BOT/
├── src/
│   ├── api/            # Polymarket + news clients
│   ├── analysis/       # EV, Kelly, Claude probability estimator
│   ├── scanner/        # 15-min scan loop + anomaly detection
│   ├── risk/           # Risk management rules
│   ├── database/       # SQLAlchemy models + SQLite
│   ├── dashboard/      # Streamlit UI
│   ├── backtest/       # Historical backtest framework
│   ├── utils/          # Logger
│   └── main.py         # CLI entry point
├── config/
│   ├── settings.py     # Pydantic settings
│   └── .env.example    # Environment variable template
├── tests/              # Unit tests
├── data/               # SQLite database (auto-created)
├── logs/               # Log files (auto-created)
└── requirements.txt
```

---

## Risk Management

The bot enforces strict risk rules:

- **1% max** per trade of account equity
- **25% fractional Kelly** criterion sizing
- **5% max daily loss** limit
- **20% max category** exposure
- **$10k minimum** 24h market volume
- **48h minimum** time to resolution
- **5% minimum EV** threshold

---

## How It Works

1. Every 15 minutes, the scanner fetches all active Polymarket markets
2. Markets below $5k volume are filtered out
3. For each eligible market, recent news is fetched from NewsAPI + RSS
4. Claude analyzes the news and estimates an independent probability
5. The EV is calculated: `EV = estimated_prob × (1/price − 1) − (1 − estimated_prob)`
6. Markets where EV > 5% pass through the risk manager
7. Kelly criterion determines position size (capped at 1% equity)
8. Opportunities are saved to SQLite and shown in the dashboard

---

## Running Tests

```bash
pytest tests/ -v
```

---

## Docker

```bash
docker-compose up
```

Runs scanner + dashboard in separate containers. Dashboard available at http://localhost:8501.
