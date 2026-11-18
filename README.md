# Stock Signal — Daily Stock Valuation & Buy-Readiness Screener

An effortless, daily-updated stock screener that shows **at a glance** which stocks are **Strong Buys**, **Good Buys**, **Holds**, or **Expensive / Avoids** — backed by live Yahoo Finance valuation multiples, profitability, growth, Wall Street upside, and 14-day RSI entry timing.

## How the Buy Score (1–99) Works

Each stock is scored across 5 traffic-light indicators:

1. **Valuation (35%)** — Forward P/E, PEG Ratio (P/E vs. expected growth), Free Cash Flow Yield (or Price/Book for banks), and EV/EBITDA. *Strict valuation guardrails prevent great companies at overpriced multiples from getting a "Buy" badge.*
2. **Quality & Cash (20%)** — Operating & Net Profit Margins, Return on Equity (ROE), and Net Cash vs. Debt/Equity.
3. **Growth (20%)** — Year-over-year Revenue Growth and blended EPS Growth.
4. **Wall St. Upside (15%)** — Margin of safety relative to consensus analyst price targets and ratings.
5. **Entry Timing (10%)** — 14-day Wilder RSI, distance from 200-day moving average, and pullback from 52-week highs.

## Running Locally

Zero third-party Python packages are required (uses Python 3 standard library):

```bash
python3 server.py
```

The server:
- Serves the web app + live API (`/api/stocks`, `/api/refresh`, `/api/add`, `/api/remove`)
- Runs a background daily scheduler that refreshes valuations automatically after US market close every trading day (21:30 UTC) and whenever cached data is >20 hours old.

## Automated Cloud Daily Updates (GitHub Actions)

[`.github/workflows/daily-update.yml`](.github/workflows/daily-update.yml) runs `python3 scripts/update_data.py` automatically every weekday at 21:30 UTC and commits the updated snapshot (`public/data/stocks.json` and `public/data/stocks_snapshot.js`), so the app also works 24/7 on static hosts such as GitHub Pages.
