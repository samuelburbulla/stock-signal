# Stock Signal — Good vs. Expensive Stocks & Reasonable Price Ranges

A clean, daily-updated stock screener that shows **at a glance** which stocks are **Strong Buys**, **Good Buys**, **Holds**, or **Expensive / Avoids** — complete with per-stock **Cheap · Fair · Expensive** price ranges and automatic **Falling-Knife & Governance-Shock** detection.

## How Each Stock Is Evaluated

1. **Reasonable Price Range (`Cheap` · `Fair` · `Expensive`)** — Estimates fundamental **Fair Value** by blending growth- and margin-adjusted forward earnings power, Free Cash Flow yield (or Price/Book for banks and mid-cycle EPS for cyclical memory chips), and a fundamental-capped consensus check:
   - **Cheap (`≤ -18%` below Fair Value)** — Margin-of-safety entry zone
   - **Fair (`-18%` to `+20%` around Fair Value)** — Reasonable hold/accumulate zone
   - **Expensive (`≥ +20%` above Fair Value)** — Stretched multiple; capped out of Buy badges
2. **5-Pillar Buy Score (`1–99`)** — Valuation (`35%`), Quality & Balance Sheet (`20%`), Growth (`20%`), Upside (`15%`), and Entry Timing (`10%`).
3. **Dip vs. Falling-Knife & Corporate Shock Guardrail (`⚡` vs. `🚨`)** — Distinguishes healthy oversold pullbacks (`⚡ Prime Dip`) from violent multi-moving-average breakdowns (`-24%+` 20-day waterfalls below both 50d and 200d MAs) and live **CEO/founder exits or SEC/DOJ/board investigations** detected from per-ticker news feeds (`🚨 Falling Knife`).

## Running Locally

Zero third-party Python packages are required (uses Python 3 standard library):

```bash
python3 server.py
```

## Automated Cloud Daily Updates (GitHub Actions)

[`.github/workflows/daily-update.yml`](.github/workflows/daily-update.yml) runs [`scripts/update_data.py`](scripts/update_data.py) every weekday at 14:00 UTC and 21:30 UTC, evaluates optional stock-level email alerts via [`scripts/send_alerts.py`](scripts/send_alerts.py), and publishes `public/` to GitHub Pages.
