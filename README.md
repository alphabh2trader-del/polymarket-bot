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
| **Stop** | **Fixed 5% below entry** (≈52.25¢) → cut the loss. Price drops here → **LOSS** 🛑 |

The bot bets whichever side (YES or NO) it thinks the market has underpriced. The stop is a **fixed 5%** of the stake, while winners run to Claude's target — so reward is larger than risk, which makes the strategy positive-expectation even below a 50% win rate.

### Exit rules

A position closes the moment **any** of these happens:

1. **Price hits the target** → WIN (sold for profit) 🎯
2. **Price drops 5% from entry** → LOSS (cut) 🛑
3. **The news flips against it** → if a position moves against us, the bot re-reads the news and re-runs Claude; if the edge is gone, it closes immediately → `THESIS_EXIT` 📰
4. **24 hours pass while in profit** → close at the current price (lock in the gain) ⏱
5. **36 hours pass** (hard cap) → close no matter what ⏱
6. **Market resolves** before any of the above → the close is an artifact, so it's marked **VOID** and excluded from the win rate (keeps stats honest)

No position ever stays open longer than 36 hours, so the win rate fills in within days — not months.

### Thesis re-check (news-driven exit)

The bot doesn't just watch the price after entering — it watches the *reason*. When a position moves **≥3% against you**, the bot re-reads the latest news and asks Claude again. If Claude no longer values your side above what you paid, the trade's premise is dead and it exits (`THESIS_EXIT`) rather than waiting for the 5% stop. This catches "the news changed" events (e.g. a key tweet) instead of bleeding into the stop. It's **triggered** (only fires on an adverse move), **capped** per cycle, and **cooled-down** per position, so it barely adds to the Claude bill.

### Entry filters (quality bar)

A position is only opened when **all** of these hold:

| Filter | Rule | Why |
|---|---|---|
| Liquidity | 24h volume ≥ **$50,000** | Liquid markets gap less, so stops behave |
| Resolution buffer | Resolves **≥ 7 days** out | Avoids resolution-driven price *jumps* that blow through stops |
| One bet per market | Never re-enter a market already traded | Prevents piling into one volatile question |
| Price range | Priced between 10% and 90% | Avoids junk long shots |
| Minimum edge / EV | Expected value ≥ 5% | The gap is worth trading |
| Win probability | Chosen side expected to win ≥ 55% | Targets a winning record |
| Plausibility | Disagreement with the market ≤ 35 points | A huge gap usually means Claude is wrong, not the market |

> **Gap protection:** the liquidity floor, 7-day buffer, and one-bet-per-market rule exist to prevent **price gaps** — sudden jumps that make a stop fill far below 5%. No order type can guarantee a max loss in a true gap; the real protection is avoiding gap-prone markets (these filters) plus small position sizing (1% per bet on a live account).

---

## How It Runs

The scanner runs **two independent jobs**:

| Job | What it does | Cost | Frequency |
|---|---|---|---|
| **Scan** | Claude analyses the top 30 markets for new edges | 💰 paid | every **hour** |
| **Position check** | Reads live prices; applies target / stop / time / thesis exits | 🆓 mostly free | every **1 minute** |

The price-checking uses Polymarket's free public API, so positions are watched closely (every minute) without paying for an AI scan each time. The only paid part of a position check is the occasional triggered thesis re-check. The scan rotates through the full pool of eligible markets so it covers everything over a few hours.

---

## Dashboard

**Home**

- A **win-rate donut** (green = win %, red = loss %) that auto-adjusts as positions close.
- A **Performance panel**:

  | Metric | Meaning |
  |---|---|
  | **Compounded Return** | Total % made since launch (reinvesting each bet) |
  | **Total Profit ($100/bet)** | Dollars made at a flat $100 per bet |
  | **Avg Profit / Bet** | Average % per bet (return if you split a flat stake equally across every bet) |
  | **Avg Profit / Day** | The average % a typical trading day earned |

- A **live feed** of every position:

  | Column | Meaning |
  |---|---|
  | **Entry / Target / Current** | Where you bought, your goal, the live price |
  | **Expected/$100** | Profit on a $100 stake **if it hits the target** (e.g. `+$9`) |
  | **Live/$100** | Profit on $100 **if you cashed out right now** (e.g. `+$4` / `-$5`) |
  | **Confidence** | Claude's confidence in the estimate (High / Medium / Low) |
  | **Outcome** | PENDING / WIN / LOSS |

**Wins / Losses** — searchable tables of every closed position. Auto-refreshes every 30 seconds. All times shown in **Eastern (America/Toronto)**.

---

## Telegram

### Automatic alerts (the bot texts you)

| When | Message |
|---|---|
| Position closes | WIN/LOSS with entry, exit, reason (🎯 target / 🛑 stop / ⏱ time / 📰 thesis / 🏁 resolved) and realized profit |
| After each scan | "Scan complete" with each new edge and its expected return |
| Daily 8 PM ET | Win rate + all-time W/L summary |
| Sunday 8 PM ET | Weekly summary |
| 1st of month 8 PM ET | Monthly summary |

VOID closes (market resolved) get no alert and don't count toward the win rate.

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

The strategy is structurally **positive-expectation if Claude is right more than ~50% of the time** — and because the fixed 5% stop is smaller than the average winning target, it can profit even slightly below 50%. Still, **nobody knows Claude's real win rate yet** — that's the whole point of running it on paper.

After **50–100 resolved positions** (days to a couple of weeks, thanks to the 24/36h exits), the win-rate donut and Performance panel give you the honest answer:

- Settles **comfortably profitable** → a real edge worth scaling with real money
- Hovers around **break-even** → the market is too efficient; you risked $0 to find out

> Every strategy change resets the statistical clock. Let it run untouched for a couple of weeks before trusting the numbers.

---

## Architecture

```
Railway Service 1 — Dashboard (Streamlit)
  streamlit run src/dashboard/app.py
  └── Reads PostgreSQL → win-rate donut + performance + live positions

Railway Service 2 — Scanner (APScheduler)
  python src/main.py scan
  ├── src/scanner/market_scanner.py      ← hourly scan + 1-min position check
  ├── src/api/polymarket.py              ← market data (free Gamma API)
  ├── src/api/news.py                    ← TheNewsAPI → NewsAPI → GNews → RSS
  ├── src/analysis/probability.py        ← Claude probability estimate
  ├── src/analysis/ev_calculator.py      ← edge & EV
  ├── src/scanner/resolution_checker.py  ← position tracker (target/stop/time/thesis/settle)
  └── src/notifications/telegram.py      ← alerts + two-way commands
```

### Key database fields (`predictions` table)

| Field | Meaning |
|---|---|
| `implied_prob` | Entry price |
| `predicted_prob` | Target price (Claude's estimate) |
| `current_price` | Latest live price (refreshed every minute) |
| `exit_price` | Price the position closed at |
| `outcome` | PENDING / WIN / LOSS / VOID |
| `exit_reason` | TARGET_HIT / STOP_LOSS / TIME_EXIT / THESIS_EXIT / RESOLVED |
| `last_recheck_at` | When the thesis was last re-evaluated by Claude |

---

## Cost

| Resource | Monthly |
|---|---|
| Claude Haiku 4.5 (30 markets × 24 hourly scans + occasional thesis re-checks) | ~$60–85 |
| Railway (2 services + PostgreSQL + Redis) | ~$15–20 |
| TheNewsAPI (primary news source) | ~$15 (fixed) |
| Polymarket market data (public) | $0 |
| **Total** | **~$90–120/month** |

Position price-checking is free; only the hourly AI scan and the rare triggered thesis re-check cost money. To lower the bill, reduce `max_markets_per_scan` or raise `scan_interval_minutes`.

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
| `THENEWSAPI_KEY` | https://www.thenewsapi.com (primary news; optional — falls back to free RSS) |
| `NEWSAPI_KEY` / `GNEWS_API_KEY` | Optional backup news sources |

> Polymarket needs **no API key** for market data. A wallet key is only required for *real* trading, which this bot does not do.

---

## Key Settings (`config/settings.py`)

| Setting | Default | Meaning |
|---|---|---|
| `scan_interval_minutes` | 60 | How often Claude scans (paid) |
| `position_check_minutes` | 1 | How often prices are checked (free) |
| `stop_loss_pct` | 0.05 | Cut a position once it's down 5% from entry |
| `profit_hold_hours` | 24 | Close a winning position after this long |
| `max_hold_hours` | 36 | Hard cap — close any position after this long |
| `min_hours_to_resolution` | 168 | Skip markets resolving within 7 days (gap protection) |
| `one_bet_per_market` | true | Never re-enter a market already traded |
| `min_volume_usd` | 50,000 | Minimum 24h volume (liquidity floor) |
| `max_markets_per_scan` | 30 | Markets analysed per scan |
| `min_win_probability` | 0.55 | Minimum expected win rate for the chosen side |
| `min_ev_threshold` | 0.05 | Minimum EV to open a position |
| `thesis_recheck_enabled` | true | Re-read news + re-run Claude on adverse moves |
| `recheck_trigger_pct` | 0.03 | Re-check when a position is down ≥3% from entry |
| `recheck_cooldown_hours` | 2.0 | Don't re-check the same position more often than this |
| `timezone` | America/Toronto | Timezone for schedules + all displayed times |

---

## Project Structure

```
POLYMARKET BOT/
├── src/
│   ├── api/               # Polymarket + news clients
│   ├── analysis/          # EV, Kelly, Claude probability estimator
│   ├── scanner/           # Scan loop + position tracker (target/stop/time/thesis/settle)
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
