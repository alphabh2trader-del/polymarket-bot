# Polymarket Bot

An automated prediction market scanner that finds mispriced markets on [Polymarket](https://polymarket.com), tracks every prediction it makes, and reports wins and losses to Telegram in real time.

**Live dashboard:** https://polymarket-bot-production-f7e4.up.railway.app

---

## What It Does

Every hour the bot:

1. **Fetches the 30 highest-volume active markets** from Polymarket (min $25k 24h volume)
2. **Searches the news** for recent articles related to each market question
3. **Asks Claude AI** to estimate the true probability of the market resolving YES — independently of what the crowd currently prices it at
4. **Calculates the edge**: if Claude's estimate differs significantly from the market price, there is a potential edge
5. **Filters by EV > 5%**: only records a prediction if the expected value is positive enough to be worth tracking
6. **Saves the prediction** to the database with the market question, predicted side (YES or NO), estimated probability, and EV
7. **Checks all pending predictions** for resolution — when a market closes, it marks each one WIN or LOSS and sends a Telegram alert

---

## The Strategy

### Core Idea

Prediction markets are priced by the crowd. If Claude can identify cases where the crowd is systematically over- or under-estimating a probability, that gap is the edge.

The bot only records a prediction when two conditions are met:
- **Edge > 5%** — Claude's estimate differs from the market price by at least 5 percentage points
- **EV > 5%** — the expected payout justifies taking the position

### Expected Value Formula

```
EV (YES) = estimated_prob × (1 / market_price - 1) - (1 - estimated_prob)
Edge     = estimated_prob - market_price
```

**Example:** market says 40% chance of YES, Claude estimates 55%
- Edge = 55% − 40% = **+15%**
- EV = 0.55 × (1/0.40 − 1) − 0.45 = **+37.5%**

### Position Sizing

Uses **fractional Kelly criterion** (25% of full Kelly), capped at 1% of account equity per trade. This is conservative by design — the goal right now is to measure prediction accuracy across many markets before sizing up.

### What Claude Brings to the Table

Polymarket prices reflect the average view of many traders. Claude's potential edge:
- Reasoning about complex multi-factor questions that are hard to price intuitively
- Anchoring on historical base rates rather than just recent news sentiment
- Avoiding overreaction to short-term news that doesn't fundamentally change the probability

Whether Claude actually has an edge is measured over 50–100+ resolved predictions. The win rate on the dashboard is the ground truth.

---

## Architecture

```
Railway Service 1 — Dashboard (Streamlit)
  src/dashboard/app.py
  └── Reads from PostgreSQL, shows win-rate donut + live positions

Railway Service 2 — Scanner (APScheduler)
  src/main.py scan
  ├── src/scanner/market_scanner.py     ← hourly scan loop
  ├── src/api/polymarket.py             ← fetches markets from Polymarket
  ├── src/api/news.py                   ← searches recent news articles
  ├── src/analysis/probability.py       ← calls Claude AI for probability estimate
  ├── src/analysis/ev_calculator.py     ← calculates edge & EV
  ├── src/scanner/resolution_checker.py ← detects when markets resolve
  └── src/notifications/telegram.py    ← sends alerts + listens for commands
```

### Database Tables

| Table | What it stores |
|---|---|
| `markets` | Every market seen, with current prices |
| `opportunities` | Markets where EV > 5% |
| `predictions` | Every prediction: WIN / LOSS / PENDING |
| `scan_runs` | Log of every scan run (duration, markets scanned) |
| `price_history` | Historical prices per market |

---

## Telegram Integration

### Automatic Alerts (bot texts you)

| When | Message |
|---|---|
| Market resolves | WIN or LOSS alert with the question, predicted side, and EV |
| Daily at 8 PM UTC | Win rate + all-time W/L summary |
| Sunday at 8 PM UTC | Weekly summary |
| 1st of month at 8 PM UTC | Monthly summary |

### Commands (you text the bot)

Send these to **@glitchi332bot** at any time:

| Command | What you get back |
|---|---|
| `/status` | Win rate, total wins, losses, pending predictions |
| `/top` | Top 5 current opportunities ranked by EV |
| `/pending` | Last 10 open predictions waiting for resolution |
| `/ping` | Confirms the bot is alive and running |
| `/help` | List of all commands |

---

## Dashboard

**Home page** — donut chart showing win % (green) vs loss % (red) with the win rate in the center. Below it: a live scrolling feed of all positions. Green rows = wins, red rows = losses, white = still pending.

**Wins page** — searchable table of every winning prediction with EV, confidence level, and resolution date.

**Losses page** — searchable table of every losing prediction for review.

The dashboard auto-refreshes every 30 seconds.

---

## Cost

| Resource | Monthly cost |
|---|---|
| Claude Haiku API (30 markets × 24 scans/day) | ~$60 |
| Railway (2 services + PostgreSQL) | ~$10–20 |
| NewsAPI (free tier) | $0 |
| **Total** | **~$70–80/month** |

---

## Local Setup

### 1. Clone and install

```bash
git clone https://github.com/alphabh2trader-del/polymarket-bot.git
cd polymarket-bot
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp config/.env.example .env
# Fill in your API keys in .env
```

Required keys:

| Variable | Where to get it |
|---|---|
| `ANTHROPIC_API_KEY` | https://console.anthropic.com/keys |
| `DATABASE_URL` | Railway PostgreSQL URL (or leave blank for local SQLite) |
| `TELEGRAM_BOT_TOKEN` | Create a bot via @BotFather on Telegram |
| `TELEGRAM_CHAT_ID` | Get your ID from @userinfobot on Telegram |
| `NEWSAPI_KEY` | https://newsapi.org/register (free tier) |

### 3. Start the scanner

```bash
python src/main.py scan
```

### 4. Start the dashboard

```bash
python src/main.py dashboard
# Opens at http://localhost:8501
```

---

## Railway Deployment

Two services, one shared PostgreSQL database:

**Service 1 — Dashboard**
```
Start command: streamlit run src/dashboard/app.py --server.port $PORT --server.address 0.0.0.0
```

**Service 2 — Scanner**
```
Start command: python src/main.py scan
```

Both services use the same 6 environment variables. The scanner writes predictions to PostgreSQL; the dashboard reads them.

---

## Risk Management Rules

- Max **1%** of account equity per trade
- **25% fractional Kelly** for position sizing
- Max **5% daily loss** limit
- Min **$25k** 24h market volume required
- Min **48 hours** to resolution required
- Min **5% EV** required to record a prediction

---

## Project Structure

```
POLYMARKET BOT/
├── src/
│   ├── api/               # Polymarket + news clients
│   ├── analysis/          # EV, Kelly, Claude probability estimator
│   ├── scanner/           # Hourly scan loop + anomaly detection + resolution checker
│   ├── risk/              # Risk management rules
│   ├── database/          # SQLAlchemy models + PostgreSQL
│   ├── dashboard/         # Streamlit multi-page UI
│   ├── notifications/     # Telegram alerts + two-way command listener
│   ├── utils/             # Logger
│   └── main.py            # CLI entry point
├── config/
│   ├── settings.py        # All settings (loaded from environment variables)
│   └── .env.example       # Template — copy to .env and fill in
├── tests/                 # Unit tests
├── logs/                  # Log files (auto-created)
└── requirements.txt
```
