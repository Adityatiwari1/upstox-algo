# Uptox Trading

A live terminal-based trading dashboard for Nifty options, built with Python and [Textual](https://textual.textualize.io/). Connects to Upstox WebSocket for real-time market data.

---

## Features

- **Live market feed** via Upstox WebSocket (no REST polling during market hours)
- **Multi-timeframe SMA** — 5min, 15min, 1hr, 4hr (switchable with `t`)
- **Overall analysis** — auto-generates BUY/NO TRADE signal with reason
- **Order flow analysis** — delta, cumulative delta, OI interpretation
- **Option chain** — live CE/PE data for ATM ±5 strikes, expiry selector
- **Paper trading book** — auto-records every system signal as a paper trade
- **Trailing SL** — trails peak price to protect profits; switches to step-SL on expiry day
- **Capital management** — configurable capital, lot sizing, delta-based SL/Target, R:R
- **Trade analysis** — win rate, P&L, expectancy, exit breakdown, CE vs PE split
- **Rotating log file** — `logs/uptox.log` (10 MB, 5 backups)

---

## Setup

### Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip
- Upstox trading account with API access and F&O segment activated

### Install

```bash
git clone <repo>
cd uptox-trading
uv sync
```

### Configure `.env`

Create a `.env` file in the project root:

```env
API_KEY=
API_SECRET=
REDIRECT_URL=http://127.0.0.1:8000/callback
MOBILE=             # 10-digit mobile number
PASSWORD=           # 6-digit Upstox PIN (not account password)
TOTP_SECRET=        # base32 TOTP seed — enables fully headless login
ACCESS_TOKEN=       # auto-filled after first login
```

Get `API_KEY` and `API_SECRET` from the [Upstox Developer Portal](https://developer.upstox.com/).  
Set the redirect URI to `http://127.0.0.1:8000/callback` in your Upstox app settings.

---

## Running

```bash
uv run src/uptox/main.py
```

On first run (no `ACCESS_TOKEN` in `.env`):
- **With `TOTP_SECRET`** — logs in automatically
- **Without `TOTP_SECRET`** — opens browser for Upstox OAuth login

After login, `ACCESS_TOKEN` is saved to `.env` automatically.  
Tokens expire daily — clear `ACCESS_TOKEN` from `.env` each morning to re-authenticate.

---

## Navigation

| Key | Action |
|-----|--------|
| `1` | Dashboard |
| `2` | Orders |
| `3` | Option Chain |
| `4` | Trade Book |
| `t` | Cycle timeframe (5min → 15min → 1hr → 4hr) |
| `q` | Quit |

### Dashboard controls

| Key | Action |
|-----|--------|
| `[` / `]` | Capital −₹5,000 / +₹5,000 |
| `c` | Enter custom capital amount |
| `r` | Cycle risk:reward setting |
| `e` | Lock current option price as trade entry (activates trailing SL) |
| `x` | Exit active trailing SL |

### Trade Book controls

| Key | Action |
|-----|--------|
| `n` | Change analysis window (default: last 100 trades) |

---

## Tabs

### Dashboard
Real-time view of signal, SMA stack, order flow, best entry recommendation, candlestick pattern, option greeks, and capital/risk panel.

### Option Chain
Live CE/PE data for ATM ±7 strikes. Expiry selector at top. All data from WebSocket only.

### Trade Book
Four inner tabs:
- **Open Trade** — current paper trade with live P&L and trailing SL status
- **Performance** — win rate, total P&L, expectancy, drawdown (configurable window)
- **Analysis** — exit reason breakdown, CE vs PE, time-of-day P&L, auto-insights
- **History** — last 20 closed trades

---

## Data

- Trade history persists to `logs/trades.json`
- Application logs at `logs/uptox.log`
- All live data is WebSocket-only during market hours (9:15 AM – 3:45 PM IST)

---

## Notes

- Paper trading only — no real orders are placed
- Requires Upstox F&O segment to be activated on your account
- Market data available only during NSE trading hours
