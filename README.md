# Polymarket Bot

An automated **paper-trading** bot that finds mispriced markets on [Polymarket](https://polymarket.com), trades the price move, and tracks every position's win/loss and profit in real time on a live dashboard and via Telegram.

**Live dashboard:** https://polymarket-bot-production-f7e4.up.railway.app

> **Paper trading:** the bot does **not** place real trades or risk real money. It records each position (entry price, target, stop) and tracks what you *would* have made. This is by design — you measure whether the strategy actually works before risking a dollar.

---

## The Strategy — Trade the Price Move

Prediction market prices reflect the crowd's view. The bot uses **Claude AI** to independently estimate the true probability of each market, then bets that the price will move toward Claude's estimate.

It is **not** a bet on the final event outcome — it's a bet on the **price correcting**, like buying a stock you think is underpriced and selling when it rises.

### How a position works

Using a football market as an example — **market price 55¢, Claude estimates 60¢:**

| | |
|---|---|
| **Entry** | Buy the side at its market price (55¢) |
| **Target** | Claude's estimate (60¢) → take-profit. Price rises here → **WIN** 🎯 |
| **Stop** | Symmetric distance below entry (50¢) → cut the loss. Price drops here → **LOSS** 🛑 |

The bot bets whichever side (YES or NO) it thinks the market has underpriced.

### Exit rules

A position closes the moment **any** of these happens:

1. **Price hits the target** → WIN (sold for profit)
2. **Price hits the stop** → LOSS (cut)
3. **24 hours pass while in profit** → close at the current price (lock in the gain) ⏱
4. **36 hours pass** (hard cap) → close no matter what, at the current price ⏱

No position ever stays open longer than 36 hours, so the win rate fills in within days — not months.

### Entry filters (quality bar)

A position is only opened when **all** of these hold:

| Filter | Rule | Why |
|---|---|---|
| Liquidity | 24h volume ≥ $25,000 | Only sharp, liquid markets |
| Price range | Priced between 10% and 90% | Avoids junk long shots |
| Minimum edge / EV | Expected value ≥ 5% | The gap is worth trading |
| Win probability | Chosen side expected to win ≥ 55% | Targets a winning record, not just payout |
| Plausibility | Disagreement with the market ≤ 35 points | A huge gap usually means Claude is wrong, not the market |

The win-probability rule plus the symmetric stop is what makes the strategy positive-expectation: with equal reward and risk, it profits as long as the win rate beats 50%.

---

## How It Runs

The scanner runs **two independent jobs**:

| Job | What it does | Cost | Frequency |
|---|---|---|---|
| **Scan** | Claude analyses the top 30 markets for new edges | 💰 paid | every **hour** |
| **Position check** | Reads live prices, applies target / stop / time exits | 🆓 free | every **5 minutes** |

The price-checking uses Polymarket's free public API, so positions are watched closely (every 5 min) without paying for an AI scan each time. The scan rotates through the full pool of eligible markets so it covers everything over a few hours.

---

## Dashboard

**Home** — a win-rate donut (green = win %, red = loss %) that auto-adjusts as positions close, plus a live feed of every position with:

| Column | Meaning |
|---|---|
| **Entry / Target / Current** | The prices: where you bought, your goal, and the live price |
| **Expected/$100** | Profit on a $100 stake **if it hits the target** (e.g. `+$9`) |
| **Live/$100** | Profit on $100 **if you cashed out right now** (e.g. `+$4` or `-$5`) |
| **Outcome** | PENDING / WIN / LOSS |

**Wins / Losses** — searchable tables of every closed position. Auto-refreshes every 30 seconds.

---

## Telegram

### Automatic alerts (the bot texts you)

| When | Message |
|---|---|
| Position closes | WIN/LOSS with entry, exit, reason (🎯 target / 🛑 stop / ⏱ 24h / 🏁 resolved) and realized profit |
| After each scan | "Scan complete" with each new edge and its expected return |
| Daily 8 PM UTC | Win rate + all-time W/L summary |
| Sunday 8 PM UTC | Weekly summary |
| 1st of month 8 PM UTC | Monthly summary |

### Commands (you text the bot)

Send these to **@glitchi332bot** anytime:

| Command | Reply |
|---|---|
| `/status` | Win rate, wins, losses, pending |
| `/top` | Top 5 current opportunities |
| `/pending` | Open positions |
| `/ping` | Confirms the bot is alive |
| `/help` | List of commands |

---

## What You Should Expect

The strategy is structurally **positive-expectation if Claude is right more than 50% of the time**. A Monte Carlo of the mechanics (symmetric stop, 1% sizing, compounding):

```
Yearly return on $1000 (≈100 trades/month):

  Claude win rate    Return
       50%           ~0%      ← break-even (no edge)
       55%          +32%
       60%          +74%
```

**Nobody knows Claude's real win rate yet** — that's the whole point of running it on paper. After **50–100 resolved positions** (days to a couple of weeks, thanks to the 24/36h exits), the win-rate donut gives you the honest answer:

- Settles **above ~55%** → a real edge worth scaling with real money
- Hovers around **50%** → the market is too efficient; you risked $0 to find out

---

## Architecture

```
Railway Service 1 — Dashboard (Streamlit)
  streamlit run src/dashboard/app.py
  └── Reads PostgreSQL → win-rate donut + live positions

Railway Service 2 — Scanner (APScheduler)
  python src/main.py scan
  ├── src/scanner/market_scanner.py      ← hourly scan + 5-min position check
  ├── src/api/polymarket.py              ← market data (free Gamma API)
  ├── src/analysis/probability.py        ← Claude probability estimate
  ├── src/analysis/ev_calculator.py      ← edge & EV
  ├── src/scanner/resolution_checker.py  ← position tracker (target/stop/time/settle)
  └── src/notifications/telegram.py     ← alerts + two-way commands
```

### Key database fields (`predictions` table)

| Field | Meaning |
|---|---|
| `implied_prob` | Entry price |
| `predicted_prob` | Target price (Claude's estimate) |
| `current_price` | Latest live price (refreshed every 5 min) |
| `exit_price` | Price the position closed at |
| `outcome` | PENDING / WIN / LOSS |
| `exit_reason` | TARGET_HIT / STOP_LOSS / TIME_EXIT / RESOLVED |

---

## Cost

| Resource | Monthly |
|---|---|
| Claude Haiku 4.5 (30 markets × 24 hourly scans) | ~$60 |
| Railway (2 services + PostgreSQL) | ~$10–20 |
| Polymarket market data (public) | $0 |
| **Total** | **~$70–80/month** |

Position price-checking is free, so the 5-minute cadence adds nothing — only the hourly AI scan costs money.

---

## Setup

```bash
git clone https://github.com/alphabh2trader-del/polymarket-bot.git
cd polymarket-bot
pip install -r requirements.txt
cp config/.env.example .env   # fill in keys
python src/main.py scan        # start the scanner
python src/main.py dashboard   # start the dashboard (localhost:8501)
```

Required environment variables:

| Variable | Where to get it |
|---|---|
| `ANTHROPIC_API_KEY` | https://console.anthropic.com/keys |
| `DATABASE_URL` | Railway PostgreSQL URL (or omit for local SQLite) |
| `TELEGRAM_BOT_TOKEN` | @BotFather on Telegram |
| `TELEGRAM_CHAT_ID` | @userinfobot on Telegram |
| `NEWSAPI_KEY` | https://newsapi.org/register (optional — the bot runs without news) |

> Polymarket needs **no API key** for market data. A wallet key is only required for *real* trading, which this bot does not do.

---

## Key Settings (`config/settings.py`)

| Setting | Default | Meaning |
|---|---|---|
| `scan_interval_minutes` | 60 | How often Claude scans (paid) |
| `position_check_minutes` | 5 | How often prices are checked (free) |
| `profit_hold_hours` | 24 | Close a winning position after this long |
| `max_hold_hours` | 36 | Hard cap — close any position after this long |
| `max_markets_per_scan` | 30 | Markets analysed per scan |
| `min_volume_usd` | 25,000 | Minimum 24h volume |
| `min_win_probability` | 0.55 | Minimum expected win rate for the chosen side |
| `min_ev_threshold` | 0.05 | Minimum EV to open a position |

---

## Project Structure

```
POLYMARKET BOT/
├── src/
│   ├── api/               # Polymarket + news clients
│   ├── analysis/          # EV, Kelly, Claude probability estimator
│   ├── scanner/           # Scan loop + position tracker (target/stop/time/settle)
│   ├── risk/              # Risk management rules
│   ├── database/          # SQLAlchemy models + PostgreSQL (+ startup migration)
│   ├── dashboard/         # Streamlit dashboard
│   ├── notifications/     # Telegram alerts + two-way command listener
│   ├── utils/             # Logger
│   └── main.py            # CLI entry point
├── config/
│   ├── settings.py        # All settings (loaded from environment variables)
│   └── .env.example       # Template — copy to .env and fill in
├── tests/                 # Unit tests
└── requirements.txt
```
