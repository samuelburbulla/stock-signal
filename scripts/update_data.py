#!/usr/bin/env python3
"""
Daily Stock Valuation & Buy-Signal Engine.

Fetches live prices, 1-year daily OHLCV technicals, and fundamental valuation metrics
from Yahoo Finance (no API key required), computes multi-factor Buy Scores (0-100),
traffic-light indicators, and plain-English takeaways, and writes `public/data/stocks.json`.

Can be run:
  1. Automatically by `server.py` on a daily schedule (and on-demand via /api/refresh)
  2. Via cron or GitHub Actions (`.github/workflows/daily-update.yml`)
  3. Directly from CLI: `python3 scripts/update_data.py`
"""

from __future__ import annotations

import datetime as dt
import http.cookiejar
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "public" / "data"
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
STOCKS_JSON_FILE = DATA_DIR / "stocks.json"
STOCKS_JS_FILE = DATA_DIR / "stocks_snapshot.js"

DEFAULT_WATCHLIST = [
    # Technology, Cloud, AI & Semiconductors
    "NVDA", "GOOGL", "META", "MSFT", "AMZN", "AAPL", "AVGO", "AMD", "TSM", "ASML",
    "SAP", "ORCL", "CRM", "ADBE", "NOW", "PLTR", "SHOP", "UBER", "QCOM", "MU", "INTC",
    # Communication, Streaming & Media
    "NFLX", "SPOT", "DIS", "TMUS",
    # Consumer, Retail & Auto
    "COST", "WMT", "HD", "MCD", "SBUX", "NKE", "KO", "PEP", "TSLA",
    # Financials, Payments & Market Data
    "JPM", "BAC", "GS", "V", "MA", "AXP", "BRK-B", "SPGI", "BLK",
    # Healthcare & Pharma
    "LLY", "UNH", "NVO", "ABBV", "JNJ", "ISRG", "MRK",
    # Industrials & Energy
    "CAT", "GE", "XOM", "CVX",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)


import threading


class YahooClient:
    """Thread-safe Yahoo Finance client with automatic cookie + crumb handling."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.crumb: str = ""
        self.cookie_header: str = ""
        self.fx_cache: dict[str, float] = {}
        self._init_session()

    def _init_session(self) -> None:
        with self._lock:
            cj = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(cj)
            )
            opener.addheaders = [
                ("User-Agent", USER_AGENT),
                ("Accept", "application/json,text/plain,*/*"),
            ]
            try:
                opener.open("https://fc.yahoo.com", timeout=6)
            except Exception:
                pass
            for host in ("query2.finance.yahoo.com", "query1.finance.yahoo.com"):
                try:
                    with opener.open(
                        f"https://{host}/v1/test/getcrumb", timeout=6
                    ) as resp:
                        crumb = resp.read().decode("utf-8").strip()
                        if crumb and "Too Many" not in crumb:
                            self.crumb = crumb
                            self.cookie_header = "; ".join(
                                f"{c.name}={c.value}" for c in cj
                            )
                            return
                except Exception:
                    continue

    def get_json(self, url: str, retries: int = 3) -> dict[str, Any]:
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            target_url = url
            if attempt % 2 == 1:
                if "query2.finance.yahoo.com" in target_url:
                    target_url = target_url.replace("query2.finance.yahoo.com", "query1.finance.yahoo.com")
                elif "query1.finance.yahoo.com" in target_url:
                    target_url = target_url.replace("query1.finance.yahoo.com", "query2.finance.yahoo.com")
            req = urllib.request.Request(
                target_url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json,text/plain,*/*",
                    "Cookie": self.cookie_header,
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=8) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as err:
                last_err = err
                if err.code in (401, 403):
                    self._init_session()
                elif err.code in (400, 429, 500, 502, 503, 504):
                    time.sleep(0.35 * (attempt + 1))
                else:
                    break
            except Exception as err:
                last_err = err
                time.sleep(0.3 * (attempt + 1))
        raise RuntimeError(f"Failed request {url}: {last_err}")

    def get_fx_rate(self, from_curr: str | None, to_curr: str | None) -> float:
        """Returns multiplier to convert `from_curr` into `to_curr`."""
        if not from_curr or not to_curr or from_curr.upper() == to_curr.upper():
            return 1.0
        f_c = from_curr.upper()
        t_c = to_curr.upper()
        # Handle GBp (British pence) vs GBP
        pence_factor = 1.0
        if from_curr == "GBp" and t_c == "GBP":
            return 0.01
        if to_curr == "GBp" and f_c == "GBP":
            return 100.0
        if to_curr == "GBp":
            t_c = "GBP"
            pence_factor = 100.0

        key = f"{f_c}{t_c}"
        if key in self.fx_cache:
            return self.fx_cache[key] * pence_factor

        pair = urllib.parse.quote(f"{f_c}{t_c}=X")
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{pair}?range=5d&interval=1d"
        try:
            data = self.get_json(url)
            rate = data["chart"]["result"][0]["meta"].get("regularMarketPrice")
            if rate and rate > 0:
                self.fx_cache[key] = float(rate)
                return float(rate) * pence_factor
        except Exception:
            pass
        return 1.0


def _raw(obj: Any, key: str) -> float | None:
    if not isinstance(obj, dict):
        return None
    val = obj.get(key)
    if isinstance(val, dict):
        raw = val.get("raw")
        if isinstance(raw, (int, float)) and not math.isnan(raw) and not math.isinf(raw):
            return float(raw)
        return None
    if isinstance(val, (int, float)) and not math.isnan(val) and not math.isinf(val):
        return float(val)
    return None


def compute_rsi(closes: list[float], period: int = 14) -> float | None:
    """Computes Wilder's smoothed 14-day Relative Strength Index."""
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_sma(closes: list[float], window: int) -> list[float | None]:
    out: list[float | None] = []
    running = 0.0
    for i, c in enumerate(closes):
        running += c
        if i >= window:
            running -= closes[i - window]
        if i >= window - 1:
            out.append(round(running / window, 2))
        else:
            out.append(None)
    return out


def clamp(val: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, val))


def interp(val: float, points: list[tuple[float, float]]) -> float:
    """Piecewise linear interpolation over sorted (x, score) control points."""
    if val <= points[0][0]:
        return points[0][1]
    if val >= points[-1][0]:
        return points[-1][1]
    for i in range(len(points) - 1):
        x0, y0 = points[i]
        x1, y1 = points[i + 1]
        if x0 <= val <= x1:
            if x1 == x0:
                return y1
            t = (val - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return points[-1][1]


def status_from_score(score: float) -> str:
    if score >= 65:
        return "good"
    if score >= 45:
        return "neutral"
    return "bad"


def score_stock(metrics: dict[str, Any]) -> dict[str, Any]:
    """
    Transparent 5-Pillar Quantitative Stock Scoring Engine (0-100):
      1. Valuation (35%): Forward P/E, PEG Ratio, FCF Yield (or P/B for Financials), EV/EBITDA
      2. Quality & Cash (20%): Profit/Operating Margins, ROE, Net Cash / Debt-to-Equity
      3. Growth (20%): Revenue Growth + Earnings/Forward EPS Growth
      4. Upside & Consensus (15%): Margin of safety vs Analyst Target & Fundamental Fair Value
      5. Entry Timing / Technicals (10%): 14d RSI, 52W Pullback, 200d MA stretch
    """
    sector = metrics.get("sector") or "Unknown"
    industry = metrics.get("industry") or ""
    is_balance_sheet_fin = (
        sector == "Real Estate"
        or (
            sector == "Financial Services"
            and (
                "Bank" in industry
                or "Insurance" in industry
                or "Asset Management" in industry
                or metrics.get("fcfYield") is None
            )
        )
    )
    is_financial = is_balance_sheet_fin

    f_pe = metrics.get("forwardPE")
    t_pe = metrics.get("trailingPE")
    peg = metrics.get("pegRatio")
    fcf_yield = metrics.get("fcfYield")
    ev_ebitda = metrics.get("evToEbitda")
    pb = metrics.get("priceToBook")
    ps = metrics.get("priceToSales")

    rev_growth = metrics.get("revenueGrowth")  # in %
    eps_growth = metrics.get("epsGrowth")  # in %
    op_margin = metrics.get("operatingMargin")  # in %
    net_margin = metrics.get("profitMargin")  # in %
    roe = metrics.get("roe")  # in %
    debt_to_eq = metrics.get("debtToEquity")  # ratio e.g. 0.84
    net_cash_pct = metrics.get("netCashPctMarketCap")  # in %

    upside_pct = metrics.get("targetUpsidePct")  # in %
    rec_mean = metrics.get("recommendationMean")  # 1.0 strong buy .. 5.0 sell
    rsi = metrics.get("rsi14")
    dist_200d = metrics.get("distFrom200dPct")  # in %
    drawdown_52w = metrics.get("drawdownFrom52wHighPct")  # negative %, e.g. -25.0

    # --- 1. VALUATION PILLAR (0-100) ---
    val_parts: list[tuple[float, float]] = []  # (score, weight)

    effective_pe = f_pe if (f_pe and f_pe > 0) else (t_pe if (t_pe and t_pe > 0) else None)
    if effective_pe is not None:
        if is_financial:
            pe_score = interp(effective_pe, [(7, 96), (11, 82), (14, 65), (18, 45), (25, 22), (35, 8)])
        else:
            pe_score = interp(effective_pe, [(9, 96), (14, 86), (19, 72), (24, 56), (31, 38), (42, 20), (65, 6)])
        val_parts.append((pe_score, 0.38))
    elif (t_pe is not None and t_pe < 0) or (f_pe is not None and f_pe < 0):
        # Unprofitable
        val_parts.append((12.0, 0.38))

    if peg is not None and peg > 0:
        peg_score = interp(peg, [(0.4, 98), (0.85, 88), (1.2, 74), (1.65, 58), (2.2, 38), (3.0, 20), (4.5, 6)])
        val_parts.append((peg_score, 0.32))
    elif peg is not None and peg < 0:
        val_parts.append((18.0, 0.25))

    if not is_financial and fcf_yield is not None:
        fcf_score = interp(fcf_yield, [(-2.0, 10), (0.5, 25), (2.0, 46), (3.5, 66), (5.0, 82), (7.5, 95)])
        val_parts.append((fcf_score, 0.18))
    elif is_financial and pb is not None and pb > 0:
        pb_score = interp(pb, [(0.8, 92), (1.3, 78), (2.0, 62), (3.2, 44), (6.0, 25)])
        val_parts.append((pb_score, 0.22))

    if not is_financial and ev_ebitda is not None and ev_ebitda > 0:
        ev_score = interp(ev_ebitda, [(6, 95), (10, 82), (15, 65), (22, 45), (32, 24), (50, 8)])
        val_parts.append((ev_score, 0.12))

    if val_parts:
        val_score = sum(s * w for s, w in val_parts) / sum(w for _, w in val_parts)
    else:
        val_score = 45.0

    # --- 2. QUALITY & BALANCE SHEET PILLAR (0-100) ---
    qual_parts: list[tuple[float, float]] = []
    margin_ref = op_margin if op_margin is not None else net_margin
    if margin_ref is not None:
        m_score = interp(margin_ref, [(-5, 10), (5, 34), (12, 55), (20, 74), (30, 88), (45, 97)])
        qual_parts.append((m_score, 0.42))
    if net_margin is not None:
        nm_score = interp(net_margin, [(-5, 10), (4, 35), (10, 56), (18, 76), (28, 90), (40, 98)])
        qual_parts.append((nm_score, 0.23))
    if roe is not None:
        roe_score = interp(roe, [(-5, 12), (6, 38), (12, 58), (20, 76), (35, 90), (60, 96)])
        qual_parts.append((roe_score, 0.20))
    if not is_financial:
        if net_cash_pct is not None and net_cash_pct > 0:
            bs_score = interp(net_cash_pct, [(0, 68), (5, 82), (15, 95)])
            qual_parts.append((bs_score, 0.15))
        elif debt_to_eq is not None and debt_to_eq >= 0:
            bs_score = interp(debt_to_eq, [(0.1, 90), (0.5, 76), (1.0, 58), (2.0, 36), (3.5, 18)])
            qual_parts.append((bs_score, 0.15))

    qual_score = (
        sum(s * w for s, w in qual_parts) / sum(w for _, w in qual_parts)
        if qual_parts
        else 55.0
    )

    # --- 3. GROWTH PILLAR (0-100) ---
    grow_parts: list[tuple[float, float]] = []
    if rev_growth is not None:
        rg_score = interp(rev_growth, [(-10, 12), (0, 32), (6, 52), (12, 70), (20, 84), (35, 96)])
        grow_parts.append((rg_score, 0.50))
    if eps_growth is not None:
        eg_score = interp(eps_growth, [(-20, 10), (0, 32), (8, 54), (15, 72), (25, 86), (45, 97)])
        grow_parts.append((eg_score, 0.50))
    grow_score = (
        sum(s * w for s, w in grow_parts) / sum(w for _, w in grow_parts)
        if grow_parts
        else 50.0
    )

    # --- 4. UPSIDE & WALL ST CONSENSUS PILLAR (0-100) ---
    up_parts: list[tuple[float, float]] = []
    if upside_pct is not None:
        u_score = interp(upside_pct, [(-15, 12), (-5, 28), (0, 42), (10, 64), (20, 80), (35, 93), (55, 98)])
        up_parts.append((u_score, 0.65))
    if rec_mean is not None:
        r_score = interp(rec_mean, [(1.3, 95), (1.8, 82), (2.3, 66), (2.8, 48), (3.5, 25), (4.2, 10)])
        up_parts.append((r_score, 0.35))
    upside_score = (
        sum(s * w for s, w in up_parts) / sum(w for _, w in up_parts)
        if up_parts
        else 50.0
    )

    # --- 5. ENTRY TIMING / TECHNICALS PILLAR (0-100) ---
    # Rewards healthy pullbacks/oversold entries on solid companies, flags overbought spikes
    time_parts: list[tuple[float, float]] = []
    fundamental_health = 0.55 * qual_score + 0.45 * grow_score
    if rsi is not None:
        if fundamental_health >= 55:
            # Dip in a strong business is an opportunity; >72 RSI means stretched short-term
            rsi_score = interp(rsi, [(22, 88), (32, 92), (45, 78), (58, 62), (68, 42), (78, 20), (85, 10)])
        else:
            # Weak fundamentals: oversold can be a value trap
            rsi_score = interp(rsi, [(25, 45), (40, 58), (55, 55), (70, 32), (80, 15)])
        time_parts.append((rsi_score, 0.50))

    if dist_200d is not None:
        # Near or slightly below 200d MA (-12% to +8%) offers good risk/reward vs +35% extended
        d200_score = interp(dist_200d, [(-30, 55), (-10, 84), (0, 80), (10, 64), (22, 44), (38, 22)])
        time_parts.append((d200_score, 0.30))

    if drawdown_52w is not None:
        # drawdown_52w is <= 0 (e.g. -22%)
        dd_score = interp(drawdown_52w, [(-50, 58), (-28, 86), (-15, 78), (-5, 55), (0, 44)])
        time_parts.append((dd_score, 0.20))

    timing_score = (
        sum(s * w for s, w in time_parts) / sum(w for _, w in time_parts)
        if time_parts
        else 55.0
    )

    # --- WEIGHTED TOTAL SCORE ---
    total = (
        0.35 * val_score
        + 0.20 * qual_score
        + 0.20 * grow_score
        + 0.15 * upside_score
        + 0.10 * timing_score
    )

    # Guardrail: if valuation is extremely stretched (val_score < 25), cap score below Hold/Buy
    if val_score < 24 and total > 45:
        total = 44.0
    elif val_score < 48 and total > 64:
        # Wonderful company at a full/rich price -> Hold / Watch for a better entry
        total = 62.0
    # Guardrail: if business quality + growth are both deeply distressed, cap score
    if qual_score < 28 and grow_score < 28 and total > 45:
        total = 43.0

    score = int(round(clamp(total, 1, 99)))

    if score >= 78 and val_score >= 60:
        verdict = "Strong Buy"
        verdict_key = "strong_buy"
    elif score >= 65 and val_score >= 48:
        verdict = "Good Buy"
        verdict_key = "buy"
    elif score >= 46:
        verdict = "Hold / Watch"
        verdict_key = "hold"
    else:
        verdict = "Expensive / Avoid"
        verdict_key = "avoid"

    # --- PLAIN-ENGLISH GREEN FLAGS, RED FLAGS & 1-LINE SUMMARY ---
    green_flags: list[str] = []
    red_flags: list[str] = []

    if effective_pe is not None:
        if effective_pe <= (12.5 if is_financial else 16.5):
            green_flags.append(f"Attractive earnings multiple at {effective_pe:.1f}x Forward P/E")
        elif effective_pe >= (22.0 if is_financial else 34.0):
            red_flags.append(f"Rich earnings multiple at {effective_pe:.1f}x Forward P/E")

    if peg is not None and peg > 0:
        if peg <= 1.15:
            green_flags.append(f"Growth at a bargain price (PEG ratio {peg:.2f}x, under 1.2x)")
        elif peg >= 2.4:
            red_flags.append(f"High price relative to growth (PEG ratio {peg:.2f}x)")

    if not is_financial and fcf_yield is not None:
        if fcf_yield >= 4.2:
            green_flags.append(f"Strong {fcf_yield:.1f}% Free Cash Flow yield")
        elif fcf_yield < 1.2:
            red_flags.append(f"Thin {fcf_yield:.1f}% Free Cash Flow yield at current valuation")

    if net_margin is not None:
        if net_margin >= 22.0:
            green_flags.append(f"High-margin business ({net_margin:.1f}% net profit margin)")
        elif net_margin < 5.0:
            red_flags.append(f"Slim {net_margin:.1f}% net profit margin")

    if rev_growth is not None:
        if rev_growth >= 14.0:
            green_flags.append(f"Rapid top-line expansion (+{rev_growth:.1f}% YoY revenue growth)")
        elif rev_growth < 0:
            red_flags.append(f"Contracting revenue ({rev_growth:.1f}% YoY)")

    if eps_growth is not None and eps_growth >= 18.0:
        eg_txt = ">100%" if eps_growth > 100.0 else f"+{eps_growth:.1f}%"
        green_flags.append(f"Strong profit growth ({eg_txt} earnings trajectory)")

    if upside_pct is not None:
        if upside_pct >= 18.0:
            green_flags.append(f"+{upside_pct:.0f}% implied upside to Wall St. consensus target")
        elif upside_pct <= -3.0:
            red_flags.append(f"Trading {abs(upside_pct):.0f}% above consensus analyst target")

    if rsi is not None:
        if rsi <= 37.0:
            green_flags.append(f"Oversold / pullback territory (14d RSI {rsi:.0f}) — favorable entry")
        elif rsi >= 71.0:
            red_flags.append(f"Short-term overbought (14d RSI {rsi:.0f}) — risk of pullback")

    if drawdown_52w is not None and drawdown_52w <= -20.0 and fundamental_health >= 60:
        green_flags.append(f"Pulled back {abs(drawdown_52w):.0f}% from 52-week high while fundamentals stay strong")

    if not is_financial and net_cash_pct is not None and net_cash_pct >= 3.0:
        green_flags.append("Fortress balance sheet with more cash than total debt")
    elif not is_financial and debt_to_eq is not None and debt_to_eq >= 2.0:
        red_flags.append(f"Elevated leverage ({debt_to_eq:.2f}x Debt/Equity)")

    # Craft concise 1-sentence takeaway
    if verdict_key in ("strong_buy", "buy"):
        lead = green_flags[0] if green_flags else "Solid balance of valuation and profitability"
        if len(green_flags) > 1:
            second = green_flags[1]
            summary = f"{lead}; {second[0].lower() + second[1:]}."
        elif red_flags:
            summary = f"{lead}. Watch: {red_flags[0].lower()}."
        else:
            summary = f"{lead} backed by healthy balance-sheet fundamentals."
    elif verdict_key == "hold":
        pro = green_flags[0] if green_flags else "Steady business fundamentals"
        con = red_flags[0] if red_flags else "valuation looks fairly priced right now"
        summary = f"{pro}, offset by {con[0].lower() + con[1:]}."
    else:
        con1 = red_flags[0] if red_flags else "Stretched valuation leaves little margin of safety"
        con2 = red_flags[1] if len(red_flags) > 1 else None
        summary = f"{con1}" + (f" and {con2[0].lower() + con2[1:]}." if con2 else ".")

    pillars = {
        "valuation": {"score": round(val_score), "status": status_from_score(val_score), "label": "Valuation"},
        "quality": {"score": round(qual_score), "status": status_from_score(qual_score), "label": "Quality"},
        "growth": {"score": round(grow_score), "status": status_from_score(grow_score), "label": "Growth"},
        "upside": {"score": round(upside_score), "status": status_from_score(upside_score), "label": "Upside"},
        "timing": {"score": round(timing_score), "status": status_from_score(timing_score), "label": "Timing"},
    }

    return {
        "score": score,
        "verdict": verdict,
        "verdictKey": verdict_key,
        "pillars": pillars,
        "greenFlags": green_flags[:4],
        "redFlags": red_flags[:4],
        "summary": summary,
    }


def fetch_stock(client: YahooClient, symbol: str) -> dict[str, Any]:
    sym = symbol.strip().upper().replace(".", "-")
    q_sym = urllib.parse.quote(sym)
    modules = "price,summaryDetail,defaultKeyStatistics,financialData,summaryProfile"
    summary_url = (
        f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{q_sym}"
        f"?modules={modules}&crumb={urllib.parse.quote(client.crumb)}"
    )
    chart_url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{q_sym}"
        f"?range=1y&interval=1d"
    )

    summary_json = client.get_json(summary_url)
    res_list = summary_json.get("quoteSummary", {}).get("result")
    if not res_list:
        raise ValueError(f"Ticker '{sym}' not found on Yahoo Finance.")
    res = res_list[0]

    p = res.get("price", {})
    sd = res.get("summaryDetail", {})
    ks = res.get("defaultKeyStatistics", {})
    fd = res.get("financialData", {})
    sp = res.get("summaryProfile", {})

    price = _raw(p, "regularMarketPrice") or _raw(fd, "currentPrice")
    if price is None:
        raise ValueError(f"No live market price available for '{sym}'.")

    currency = p.get("currency") or sd.get("currency") or "USD"
    fin_currency = fd.get("financialCurrency") or currency
    fx = client.get_fx_rate(fin_currency, currency)

    # Fetch 1-year daily chart for RSI, moving averages, and sparkline
    closes: list[float] = []
    dates: list[str] = []
    try:
        chart_json = client.get_json(chart_url)
        c_res = chart_json["chart"]["result"][0]
        timestamps = c_res.get("timestamp") or []
        raw_closes = c_res.get("indicators", {}).get("quote", [{}])[0].get("close") or []
        for ts, cl in zip(timestamps, raw_closes):
            if isinstance(cl, (int, float)) and not math.isnan(cl):
                closes.append(float(cl))
                dates.append(dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).strftime("%Y-%m-%d"))
    except Exception:
        pass

    rsi_14 = compute_rsi(closes, 14) if len(closes) >= 15 else None
    sma50_series = compute_sma(closes, 50) if closes else []
    sma200_series = compute_sma(closes, 200) if closes else []

    ma_50 = (sma50_series[-1] if sma50_series and sma50_series[-1] is not None else _raw(sd, "fiftyDayAverage"))
    ma_200 = (sma200_series[-1] if sma200_series and sma200_series[-1] is not None else _raw(sd, "twoHundredDayAverage"))

    # Downsample 1Y chart to ~65 points (every 4th trading day + always include latest point)
    chart_points: list[dict[str, Any]] = []
    if closes:
        step = max(1, len(closes) // 64)
        idxs = list(range(0, len(closes), step))
        if idxs[-1] != len(closes) - 1:
            idxs.append(len(closes) - 1)
        for idx in idxs:
            chart_points.append({
                "d": dates[idx],
                "c": round(closes[idx], 2),
                "ma50": sma50_series[idx] if idx < len(sma50_series) else None,
                "ma200": sma200_series[idx] if idx < len(sma200_series) else None,
            })

    market_cap = _raw(p, "marketCap") or _raw(sd, "marketCap")
    ev = _raw(ks, "enterpriseValue")
    fcf_raw = _raw(fd, "freeCashflow")
    fcf = fcf_raw * fx if fcf_raw is not None else None
    total_cash = (_raw(fd, "totalCash") * fx) if _raw(fd, "totalCash") is not None else None
    total_debt = (_raw(fd, "totalDebt") * fx) if _raw(fd, "totalDebt") is not None else None
    total_rev = (_raw(fd, "totalRevenue") * fx) if _raw(fd, "totalRevenue") is not None else None

    trailing_pe = _raw(sd, "trailingPE")
    forward_pe = _raw(sd, "forwardPE") or _raw(ks, "forwardPE")
    trailing_eps = _raw(ks, "trailingEps")
    forward_eps = _raw(ks, "forwardEps")

    rev_growth_raw = _raw(fd, "revenueGrowth")
    earn_growth_raw = _raw(fd, "earningsGrowth")
    rev_growth_pct = rev_growth_raw * 100.0 if rev_growth_raw is not None else None

    # Implied forward EPS growth vs trailing EPS as clean supplement to quarterly YoY earningsGrowth
    implied_eps_growth_pct: float | None = None
    if trailing_eps and forward_eps and trailing_eps > 0:
        implied_eps_growth_pct = ((forward_eps - trailing_eps) / trailing_eps) * 100.0

    if earn_growth_raw is not None and implied_eps_growth_pct is not None:
        # Blend reported YoY quarterly earnings growth with forward EPS trajectory (capped against GAAP base spikes)
        eps_growth_pct = 0.5 * clamp(earn_growth_raw * 100.0, -80.0, 150.0) + 0.5 * clamp(implied_eps_growth_pct, -50.0, 120.0)
    elif earn_growth_raw is not None:
        eps_growth_pct = clamp(earn_growth_raw * 100.0, -80.0, 150.0)
    else:
        eps_growth_pct = implied_eps_growth_pct

    peg = _raw(ks, "pegRatio")
    if (peg is None or peg <= 0) and forward_pe and forward_pe > 0 and eps_growth_pct and eps_growth_pct > 2.0:
        peg = forward_pe / eps_growth_pct

    fcf_yield = (fcf / market_cap * 100.0) if (fcf is not None and market_cap and market_cap > 0) else None
    # Sanity filter against unadjusted ADR share-ratio quirks (>45% FCF yield on mega-caps)
    if fcf_yield is not None and abs(fcf_yield) > 45.0:
        fcf_yield = None

    earnings_yield = (100.0 / forward_pe) if (forward_pe and forward_pe > 0) else (
        (100.0 / trailing_pe) if (trailing_pe and trailing_pe > 0) else None
    )

    div_yield_raw = _raw(sd, "dividendYield") or _raw(sd, "trailingAnnualDividendYield")
    div_yield = div_yield_raw * 100.0 if div_yield_raw is not None else 0.0

    debt_to_eq_raw = _raw(fd, "debtToEquity")
    debt_to_eq = debt_to_eq_raw / 100.0 if debt_to_eq_raw is not None else None

    net_cash = (total_cash - total_debt) if (total_cash is not None and total_debt is not None) else None
    net_cash_pct = (net_cash / market_cap * 100.0) if (net_cash is not None and market_cap and market_cap > 0) else None

    target_mean = _raw(fd, "targetMeanPrice")
    target_low = _raw(fd, "targetLowPrice")
    target_high = _raw(fd, "targetHighPrice")
    target_upside = ((target_mean - price) / price * 100.0) if (target_mean and price > 0) else None

    low_52w = _raw(sd, "fiftyTwoWeekLow")
    high_52w = _raw(sd, "fiftyTwoWeekHigh")
    range_pos_52w = (
        clamp((price - low_52w) / (high_52w - low_52w) * 100.0, 0.0, 100.0)
        if (low_52w is not None and high_52w is not None and high_52w > low_52w)
        else 50.0
    )
    drawdown_52w = ((price - high_52w) / high_52w * 100.0) if (high_52w and high_52w > 0) else None
    dist_200d = ((price - ma_200) / ma_200 * 100.0) if (ma_200 and ma_200 > 0) else None
    dist_50d = ((price - ma_50) / ma_50 * 100.0) if (ma_50 and ma_50 > 0) else None

    change_pct_raw = _raw(p, "regularMarketChangePercent") or _raw(p, "fulldayChangePercent")
    change_pct = change_pct_raw * 100.0 if change_pct_raw is not None else 0.0

    def r2(v: float | None) -> float | None:
        return round(v, 2) if v is not None else None

    def r1(v: float | None) -> float | None:
        return round(v, 1) if v is not None else None

    record: dict[str, Any] = {
        "symbol": sym,
        "name": p.get("shortName") or p.get("longName") or sym,
        "longName": p.get("longName") or p.get("shortName") or sym,
        "sector": sp.get("sector") or "Other",
        "industry": sp.get("industry") or "",
        "currency": currency,
        "currencySymbol": p.get("currencySymbol") or "$",
        "price": r2(price),
        "changePct": r2(change_pct),
        "marketCap": market_cap,
        "enterpriseValue": ev,
        # Valuation
        "trailingPE": r2(trailing_pe),
        "forwardPE": r2(forward_pe),
        "pegRatio": r2(peg),
        "priceToSales": r2(_raw(sd, "priceToSalesTrailing12Months")),
        "priceToBook": r2(_raw(ks, "priceToBook")),
        "evToEbitda": r2(_raw(ks, "enterpriseToEbitda")),
        "fcfYield": r2(fcf_yield),
        "earningsYield": r2(earnings_yield),
        "dividendYield": r2(div_yield),
        # Growth & Quality
        "revenueGrowth": r1(rev_growth_pct),
        "epsGrowth": r1(eps_growth_pct),
        "grossMargin": r1(_raw(fd, "grossMargins") * 100.0) if _raw(fd, "grossMargins") is not None else None,
        "operatingMargin": r1(_raw(fd, "operatingMargins") * 100.0) if _raw(fd, "operatingMargins") is not None else None,
        "profitMargin": r1(_raw(fd, "profitMargins") * 100.0) if _raw(fd, "profitMargins") is not None else None,
        "roe": r1(_raw(fd, "returnOnEquity") * 100.0) if _raw(fd, "returnOnEquity") is not None else None,
        "roa": r1(_raw(fd, "returnOnAssets") * 100.0) if _raw(fd, "returnOnAssets") is not None else None,
        # Balance sheet
        "debtToEquity": r2(debt_to_eq),
        "currentRatio": r2(_raw(fd, "currentRatio")),
        "freeCashflow": fcf,
        "totalCash": total_cash,
        "totalDebt": total_debt,
        "totalRevenue": total_rev,
        "netCash": net_cash,
        "netCashPctMarketCap": r1(net_cash_pct),
        # Consensus & Targets
        "targetMeanPrice": r2(target_mean),
        "targetLowPrice": r2(target_low),
        "targetHighPrice": r2(target_high),
        "targetUpsidePct": r1(target_upside),
        "recommendationMean": r2(_raw(fd, "recommendationMean")),
        "recommendationKey": fd.get("recommendationKey") or "",
        "analystCount": int(_raw(fd, "numberOfAnalystOpinions") or 0),
        # Technicals & Range
        "low52w": r2(low_52w),
        "high52w": r2(high_52w),
        "rangePos52w": r1(range_pos_52w),
        "drawdownFrom52wHighPct": r1(drawdown_52w),
        "ma50": r2(ma_50),
        "ma200": r2(ma_200),
        "distFrom50dPct": r1(dist_50d),
        "distFrom200dPct": r1(dist_200d),
        "rsi14": r1(rsi_14),
        "beta": r2(_raw(sd, "beta")),
        "chart": chart_points,
    }

    scored = score_stock(record)
    record.update(scored)
    return record


def load_watchlist() -> list[str]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if WATCHLIST_FILE.exists():
        try:
            items = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
            if isinstance(items, list) and items:
                return [str(x).strip().upper() for x in items if str(x).strip()]
        except Exception:
            pass
    WATCHLIST_FILE.write_text(json.dumps(DEFAULT_WATCHLIST, indent=2), encoding="utf-8")
    return list(DEFAULT_WATCHLIST)


def save_watchlist(tickers: list[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    seen: list[str] = []
    for t in tickers:
        u = t.strip().upper().replace(".", "-")
        if u and u not in seen:
            seen.append(u)
    WATCHLIST_FILE.write_text(json.dumps(seen, indent=2), encoding="utf-8")


def write_payload(stocks: list[dict[str, Any]]) -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stocks_sorted = sorted(stocks, key=lambda s: s.get("score", 0), reverse=True)
    now_utc = dt.datetime.now(dt.timezone.utc)
    counts = {
        "total": len(stocks_sorted),
        "strong_buy": sum(1 for s in stocks_sorted if s["verdictKey"] == "strong_buy"),
        "buy": sum(1 for s in stocks_sorted if s["verdictKey"] == "buy"),
        "hold": sum(1 for s in stocks_sorted if s["verdictKey"] == "hold"),
        "avoid": sum(1 for s in stocks_sorted if s["verdictKey"] == "avoid"),
    }
    payload = {
        "updatedAt": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updatedDate": now_utc.strftime("%Y-%m-%d"),
        "schedule": "Daily automated update (every trading day + live on-demand refresh)",
        "counts": counts,
        "stocks": stocks_sorted,
    }
    raw_json = json.dumps(payload, separators=(",", ":"))
    STOCKS_JSON_FILE.write_text(raw_json, encoding="utf-8")
    STOCKS_JS_FILE.write_text(f"window.__INITIAL_STOCK_DATA__ = {raw_json};\n", encoding="utf-8")
    return payload


def update_all(tickers: list[str] | None = None) -> dict[str, Any]:
    symbols = tickers if tickers is not None else load_watchlist()
    client = YahooClient()
    results: list[dict[str, Any]] = []
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=8) as pool:
        future_map = {pool.submit(fetch_stock, client, sym): sym for sym in symbols}
        for fut in as_completed(future_map):
            sym = future_map[fut]
            try:
                results.append(fut.result())
            except Exception as exc:
                errors.append(f"{sym}: {exc}")

    if not results and STOCKS_JSON_FILE.exists():
        # Preserve cached snapshot if network temporarily unavailable
        return json.loads(STOCKS_JSON_FILE.read_text(encoding="utf-8"))

    payload = write_payload(results)
    if errors:
        payload["warnings"] = errors
    return payload


if __name__ == "__main__":
    t0 = time.time()
    data = update_all()
    elapsed = time.time() - t0
    c = data["counts"]
    print(
        f"Updated {c['total']} stocks in {elapsed:.1f}s -> "
        f"Strong Buy: {c['strong_buy']}, Good Buy: {c['buy']}, "
        f"Hold: {c['hold']}, Avoid: {c['avoid']}"
    )
    for st in data["stocks"][:8]:
        print(
            f"  [{st['score']:2d}] {st['verdict']:17s} {st['symbol']:6s} "
            f"${st['price']:<8} FwdPE={st['forwardPE']} PEG={st['pegRatio']} "
            f"FCF={st['fcfYield']}% RSI={st['rsi14']} | {st['summary']}"
        )
    if data.get("warnings"):
        print("Warnings:", data["warnings"], file=sys.stderr)
