#!/usr/bin/env python3
"""
Point-in-Time Historical Backtest Engine for Stock Signal (Nov 2021 - Nov 2026).

Methodology (Zero Look-Ahead Bias):
  - Fundamentals: Pulled from official SEC EDGAR XBRL CompanyFacts (`data.sec.gov`)
    for all US GAAP stocks in the Stock Signal universe. At each monthly rebalance
    date `T`, ONLY 10-Q and 10-K filings with `filed <= T` (actual SEC filing date)
    are visible to the scoring algorithm.
  - Stock Splits & Dividends: Daily prices & total-return (`adjclose`) are pulled
    from Yahoo Finance (`range=7y&interval=1d&events=splits`). Share counts and
    per-share figures from SEC filings are normalized across split events strictly
    using split dates so P/E and FCF Yield are 100% split-invariant.
  - Transaction Costs: Deducts 10 bps (0.10%) per one-way portfolio turnover at
    each monthly rebalance.
  - Benchmarks: Compares against BOTH S&P 500 Total Return (`SPY`) AND an
    Equal-Weight Universe benchmark (holding all universe stocks equally) plus the
    `Expensive / Avoid` basket to isolate pure indicator alpha from universe bias.
"""

from __future__ import annotations

import bisect
import datetime as dt
import json
import math
import statistics
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "public" / "data"
BACKTEST_JSON_FILE = DATA_DIR / "backtest.json"
BACKTEST_JS_FILE = DATA_DIR / "backtest_snapshot.js"
SEC_CACHE_DIR = Path("/tmp/stock_signal_sec_cache")

SEC_HEADERS = {
    "User-Agent": "StockSignalBacktest/1.0 (samuelburbulla-stock-signal; research@stocksignal.org)",
    "Accept": "application/json",
}
YF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# All 49 US SEC 10-Q/10-K GAAP stocks from Stock Signal's default universe
UNIVERSE_META: dict[str, str] = {
    "NVDA": "Technology", "GOOGL": "Communication Services", "META": "Communication Services",
    "MSFT": "Technology", "AMZN": "Consumer Cyclical", "AAPL": "Technology",
    "AVGO": "Technology", "AMD": "Technology", "ORCL": "Technology",
    "CRM": "Technology", "ADBE": "Technology", "NOW": "Technology",
    "PLTR": "Technology", "UBER": "Technology", "QCOM": "Technology",
    "MU": "Technology", "INTC": "Technology",
    "NFLX": "Communication Services", "DIS": "Communication Services", "TMUS": "Communication Services",
    "COST": "Consumer Defensive", "WMT": "Consumer Defensive", "HD": "Consumer Cyclical",
    "MCD": "Consumer Cyclical", "SBUX": "Consumer Cyclical", "NKE": "Consumer Cyclical",
    "KO": "Consumer Defensive", "PEP": "Consumer Defensive", "TSLA": "Consumer Cyclical",
    "JPM": "Financial Services", "BAC": "Financial Services", "GS": "Financial Services",
    "V": "Financial Services", "MA": "Financial Services", "AXP": "Financial Services",
    "BRK-B": "Financial Services", "SPGI": "Financial Services", "BLK": "Financial Services",
    "LLY": "Healthcare", "UNH": "Healthcare", "ABBV": "Healthcare",
    "JNJ": "Healthcare", "ISRG": "Healthcare", "MRK": "Healthcare",
    "CAT": "Industrials", "GE": "Industrials", "XOM": "Energy", "CVX": "Energy",
}

BALANCE_SHEET_FIN_SYMS = {"JPM", "BAC", "GS", "AXP", "BRK-B", "BLK"}

_sec_rate_lock = threading.Lock()
_last_sec_req = 0.0


def sec_get_json(url: str) -> dict[str, Any]:
    """Rate-limited SEC EDGAR fetcher (<8 req/sec per SEC fair-access policy) with disk cache."""
    global _last_sec_req
    SEC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_key = url.split("/")[-1]
    cache_path = SEC_CACHE_DIR / cache_key
    if cache_path.exists() and cache_path.stat().st_size > 200:
        return json.loads(cache_path.read_text(encoding="utf-8"))

    for attempt in range(3):
        with _sec_rate_lock:
            elapsed = time.time() - _last_sec_req
            if elapsed < 0.13:
                time.sleep(0.13 - elapsed)
            _last_sec_req = time.time()
        try:
            req = urllib.request.Request(url, headers=SEC_HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                cache_path.write_text(raw, encoding="utf-8")
                return json.loads(raw)
        except Exception as err:
            if attempt == 2:
                raise RuntimeError(f"SEC fetch failed {url}: {err}") from err
            time.sleep(0.6 * (attempt + 1))
    return {}


def yf_get_chart(symbol: str) -> dict[str, Any]:
    q_sym = urllib.parse.quote(symbol)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{q_sym}?range=7y&interval=1d&events=splits"
    for attempt in range(3):
        host = "query1" if attempt % 2 == 0 else "query2"
        u = url.replace("query1", host)
        try:
            req = urllib.request.Request(u, headers=YF_HEADERS)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["chart"]["result"][0]
        except Exception as err:
            if attempt == 2:
                raise RuntimeError(f"YF chart failed {symbol}: {err}") from err
            time.sleep(0.4 * (attempt + 1))
    return {}


def interp(val: float, points: list[tuple[float, float]]) -> float:
    if val <= points[0][0]:
        return points[0][1]
    if val >= points[-1][0]:
        return points[-1][1]
    for i in range(len(points) - 1):
        x0, y0 = points[i]
        x1, y1 = points[i + 1]
        if x0 <= val <= x1:
            return y0 if x1 == x0 else y0 + ((val - x0) / (x1 - x0)) * (y1 - y0)
    return points[-1][1]


def clamp(val: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, val))


# --- SEC XBRL Point-in-Time Parser ---
VALID_FORMS = {"10-Q", "10-K", "10-Q/A", "10-K/A"}


def extract_flow_entries(gaap: dict[str, Any], tags: list[str], unit_name: str = "USD") -> list[dict[str, Any]]:
    """Extracts deduplicated (start, end, val, filed, days) flow items across candidate GAAP tags."""
    out: list[dict[str, Any]] = []
    for tag in tags:
        node = gaap.get(tag)
        if not node:
            continue
        arr = node.get("units", {}).get(unit_name, [])
        for x in arr:
            if x.get("form") not in VALID_FORMS:
                continue
            start_s = x.get("start")
            end_s = x.get("end")
            filed_s = x.get("filed")
            val = x.get("val")
            if not (start_s and end_s and filed_s and isinstance(val, (int, float))):
                continue
            try:
                d_start = dt.date.fromisoformat(start_s)
                d_end = dt.date.fromisoformat(end_s)
            except ValueError:
                continue
            days = (d_end - d_start).days
            if 70 <= days <= 385:
                out.append({
                    "start": start_s,
                    "end": end_s,
                    "d_end": d_end,
                    "days": days,
                    "val": float(val),
                    "filed": filed_s,
                    "tag_rank": tags.index(tag),
                })
    return out


def extract_instant_entries(
    facts: dict[str, Any], candidates: list[tuple[str, str, str]]
) -> list[dict[str, Any]]:
    """Extracts point-in-time balance sheet / share count entries: (taxonomy, tag, unit)."""
    out: list[dict[str, Any]] = []
    for rank, (tax, tag, unit_name) in enumerate(candidates):
        arr = facts.get(tax, {}).get(tag, {}).get("units", {}).get(unit_name, [])
        for x in arr:
            if x.get("form") not in VALID_FORMS:
                continue
            end_s = x.get("end")
            filed_s = x.get("filed")
            val = x.get("val")
            if not (end_s and filed_s and isinstance(val, (int, float))):
                continue
            out.append({
                "end": end_s,
                "val": float(val),
                "filed": filed_s,
                "rank": rank,
            })
    return out


def compute_pit_ttm(
    entries: list[dict[str, Any]],
    cutoff_filed: str,
    max_end_date: dt.date | None = None,
) -> tuple[float | None, dt.date | None]:
    """
    Computes Trailing 12-Month (TTM) total strictly using filings with `filed <= cutoff_filed`
    (and `d_end <= max_end_date` when computing 1-year-ago TTM for YoY growth).
    """
    avail = [
        e for e in entries
        if e["filed"] <= cutoff_filed and (max_end_date is None or e["d_end"] <= max_end_date)
    ]
    if not avail:
        return None, None

    # Deduplicate by (end, duration_bucket) picking preferred tag_rank then earliest filing
    by_period: dict[tuple[str, int], dict[str, Any]] = {}
    for e in avail:
        days = e["days"]
        if 70 <= days <= 112:
            bucket = 1  # single quarter (~3m)
        elif 160 <= days <= 205:
            bucket = 2  # 6m YTD
        elif 250 <= days <= 295:
            bucket = 3  # 9m YTD
        elif 340 <= days <= 385:
            bucket = 4  # 12m Annual
        else:
            continue
        key = (e["end"], bucket)
        prev = by_period.get(key)
        if prev is None or (e["tag_rank"], e["filed"]) < (prev["tag_rank"], prev["filed"]):
            by_period[key] = e

    if not by_period:
        return None, None

    latest_end = max(v["d_end"] for v in by_period.values())
    latest_end_s = latest_end.isoformat()

    # Case 1: Latest period is a full 10-K Annual!
    if (latest_end_s, 4) in by_period:
        return by_period[(latest_end_s, 4)]["val"], latest_end

    # Case 2: Sum of 4 consecutive ~90-day discrete quarters ending at latest_end
    q_entries = sorted(
        [v for ( _, b), v in by_period.items() if b == 1 and v["d_end"] <= latest_end],
        key=lambda x: x["d_end"],
        reverse=True,
    )
    if len(q_entries) >= 4:
        picked = [q_entries[0]]
        for cand in q_entries[1:]:
            gap = (picked[-1]["d_end"] - cand["d_end"]).days
            if 70 <= gap <= 115:
                picked.append(cand)
                if len(picked) == 4:
                    break
        if len(picked) == 4:
            return sum(x["val"] for x in picked), latest_end

    # Case 3: Annual_prev + YTD_curr - YTD_prior_year (essential for Cash Flow YTD statements!)
    for ytd_bucket in (3, 2, 1):
        if (latest_end_s, ytd_bucket) not in by_period:
            continue
        ytd_curr = by_period[(latest_end_s, ytd_bucket)]
        # Find prior Annual ending ~ytd_bucket*91 days before latest_end
        annuals = [
            v for (_, b), v in by_period.items()
            if b == 4 and 65 <= (latest_end - v["d_end"]).days <= 305
        ]
        prior_ytds = [
            v for (_, b), v in by_period.items()
            if b == ytd_bucket and 340 <= (latest_end - v["d_end"]).days <= 385
        ]
        if annuals and prior_ytds:
            ann_prev = max(annuals, key=lambda x: x["d_end"])
            ytd_prev = min(prior_ytds, key=lambda x: abs((latest_end - x["d_end"]).days - 365))
            return ann_prev["val"] + ytd_curr["val"] - ytd_prev["val"], latest_end

    return None, None


def latest_pit_instant(
    entries: list[dict[str, Any]], cutoff_filed: str
) -> tuple[float | None, str | None]:
    avail = [e for e in entries if e["filed"] <= cutoff_filed]
    if not avail:
        return None, None
    # Sort by most recent end date, then preferred tag rank, then earliest filing
    avail.sort(key=lambda e: (e["end"], -e["rank"], e["filed"]), reverse=True)
    best_end = avail[0]["end"]
    same_end = [e for e in avail if e["end"] == best_end]
    same_end.sort(key=lambda e: (e["rank"], e["filed"]))
    return same_end[0]["val"], best_end


def compute_rsi_at(closes: list[float], end_idx: int, period: int = 14) -> float | None:
    if end_idx < period + 1:
        return None
    start = max(1, end_idx - 60)
    gains: list[float] = []
    losses: list[float] = []
    for i in range(start, end_idx + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    if len(gains) < period:
        return None
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_g / avg_l))


def score_pit_snapshot(m: dict[str, Any]) -> dict[str, Any]:
    """
    Point-in-time historical Stock Signal scorer using the exact same pillar curves
    and valuation guardrails as `scripts/update_data.py` (re-normalized across the 4
    strictly observable point-in-time pillars without look-ahead analyst targets).
    """
    is_fin = m["is_financial"]
    pe = m.get("pe")
    peg = m.get("peg")
    fcf_yield = m.get("fcfYield")
    pb = m.get("pb")
    op_margin = m.get("opMargin")
    net_margin = m.get("netMargin")
    roe = m.get("roe")
    debt_to_eq = m.get("debtToEq")
    net_cash_pct = m.get("netCashPct")
    rev_growth = m.get("revGrowth")
    eps_growth = m.get("epsGrowth")
    rsi = m.get("rsi14")
    dist_200d = m.get("dist200d")
    dd_52w = m.get("dd52w")

    # 1. Valuation Pillar
    val_parts: list[tuple[float, float]] = []
    if pe is not None and pe > 0:
        if is_fin:
            pe_s = interp(pe, [(7, 96), (11, 82), (14, 65), (18, 45), (25, 22), (35, 8)])
        else:
            pe_s = interp(pe, [(9, 96), (14, 86), (19, 72), (24, 56), (31, 38), (42, 20), (65, 6)])
        val_parts.append((pe_s, 0.44))
    elif pe is not None and pe < 0:
        val_parts.append((12.0, 0.44))

    if peg is not None and peg > 0:
        peg_s = interp(peg, [(0.4, 98), (0.85, 88), (1.2, 74), (1.65, 58), (2.2, 38), (3.0, 20), (4.5, 6)])
        val_parts.append((peg_s, 0.34))
    elif peg is not None and peg < 0:
        val_parts.append((18.0, 0.25))

    if not is_fin and fcf_yield is not None:
        fcf_s = interp(fcf_yield, [(-2.0, 10), (0.5, 25), (2.0, 46), (3.5, 66), (5.0, 82), (7.5, 95)])
        val_parts.append((fcf_s, 0.22))
    elif is_fin and pb is not None and pb > 0:
        pb_s = interp(pb, [(0.8, 92), (1.3, 78), (2.0, 62), (3.2, 44), (6.0, 25)])
        val_parts.append((pb_s, 0.22))

    val_score = sum(s * w for s, w in val_parts) / sum(w for _, w in val_parts) if val_parts else 45.0

    # 2. Quality Pillar
    qual_parts: list[tuple[float, float]] = []
    m_ref = op_margin if op_margin is not None else net_margin
    if m_ref is not None:
        qual_parts.append((interp(m_ref, [(-5, 10), (5, 34), (12, 55), (20, 74), (30, 88), (45, 97)]), 0.42))
    if net_margin is not None:
        qual_parts.append((interp(net_margin, [(-5, 10), (4, 35), (10, 56), (18, 76), (28, 90), (40, 98)]), 0.23))
    if roe is not None:
        qual_parts.append((interp(roe, [(-5, 12), (6, 38), (12, 58), (20, 76), (35, 90), (60, 96)]), 0.20))
    if not is_fin:
        if net_cash_pct is not None and net_cash_pct > 0:
            qual_parts.append((interp(net_cash_pct, [(0, 68), (5, 82), (15, 95)]), 0.15))
        elif debt_to_eq is not None and debt_to_eq >= 0:
            qual_parts.append((interp(debt_to_eq, [(0.1, 90), (0.5, 76), (1.0, 58), (2.0, 36), (3.5, 18)]), 0.15))
    qual_score = sum(s * w for s, w in qual_parts) / sum(w for _, w in qual_parts) if qual_parts else 55.0

    # 3. Growth Pillar
    grow_parts: list[tuple[float, float]] = []
    if rev_growth is not None:
        grow_parts.append((interp(rev_growth, [(-10, 12), (0, 32), (6, 52), (12, 70), (20, 84), (35, 96)]), 0.50))
    if eps_growth is not None:
        grow_parts.append((interp(eps_growth, [(-20, 10), (0, 32), (8, 54), (15, 72), (25, 86), (45, 97)]), 0.50))
    grow_score = sum(s * w for s, w in grow_parts) / sum(w for _, w in grow_parts) if grow_parts else 50.0

    # 4. Entry Timing / Technicals Pillar
    time_parts: list[tuple[float, float]] = []
    fund_health = 0.55 * qual_score + 0.45 * grow_score
    if rsi is not None:
        if fund_health >= 55:
            rsi_s = interp(rsi, [(22, 88), (32, 92), (45, 78), (58, 62), (68, 42), (78, 20), (85, 10)])
        else:
            rsi_s = interp(rsi, [(25, 45), (40, 58), (55, 55), (70, 32), (80, 15)])
        time_parts.append((rsi_s, 0.50))
    if dist_200d is not None:
        time_parts.append((interp(dist_200d, [(-30, 55), (-10, 84), (0, 80), (10, 64), (22, 44), (38, 22)]), 0.30))
    if dd_52w is not None:
        time_parts.append((interp(dd_52w, [(-50, 58), (-28, 86), (-15, 78), (-5, 55), (0, 44)]), 0.20))
    timing_score = sum(s * w for s, w in time_parts) / sum(w for _, w in time_parts) if time_parts else 55.0

    # Re-normalized weights across the 4 observable point-in-time pillars (35 : 20 : 20 : 10 -> / 0.85)
    total = (
        (0.35 / 0.85) * val_score
        + (0.20 / 0.85) * qual_score
        + (0.20 / 0.85) * grow_score
        + (0.10 / 0.85) * timing_score
    )

    if val_score < 24 and total > 45:
        total = 44.0
    elif val_score < 48 and total > 64:
        total = 62.0
    if qual_score < 28 and grow_score < 28 and total > 45:
        total = 43.0

    score = int(round(clamp(total, 1, 99)))
    if score >= 78 and val_score >= 60:
        vk = "strong_buy"
    elif score >= 65 and val_score >= 48:
        vk = "buy"
    elif score >= 46:
        vk = "hold"
    else:
        vk = "avoid"

    return {"score": score, "verdictKey": vk, "valScore": round(val_score, 1)}


def load_stock_history(symbol: str, cik: str) -> dict[str, Any]:
    chart = yf_get_chart(symbol)
    ts_list = chart.get("timestamp") or []
    quotes = chart.get("indicators", {}).get("quote", [{}])[0]
    raw_close = quotes.get("close") or []
    raw_adj = chart.get("indicators", {}).get("adjclose", [{}])[0].get("adjclose") or raw_close

    dates: list[str] = []
    closes: list[float] = []
    adjs: list[float] = []
    for ts, c, a in zip(ts_list, raw_close, raw_adj):
        if isinstance(c, (int, float)) and isinstance(a, (int, float)) and not math.isnan(c) and not math.isnan(a):
            dates.append(dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).strftime("%Y-%m-%d"))
            closes.append(float(c))
            adjs.append(float(a))

    splits_raw = chart.get("events", {}).get("splits") or {}
    splits: list[tuple[str, float]] = []
    for s in splits_raw.values():
        s_date = dt.datetime.fromtimestamp(s["date"], tz=dt.timezone.utc).strftime("%Y-%m-%d")
        ratio = float(s["numerator"]) / float(s["denominator"])
        if ratio > 0:
            splits.append((s_date, ratio))
    splits.sort()

    sec_data = sec_get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
    facts = sec_data.get("facts", {})
    gaap = facts.get("us-gaap", {})

    rev_entries = extract_flow_entries(gaap, [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "RevenuesNetOfInterestExpense",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ])
    ni_entries = extract_flow_entries(gaap, ["NetIncomeLoss", "ProfitLoss"])
    op_entries = extract_flow_entries(gaap, [
        "OperatingIncomeLoss",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    ])
    ocf_entries = extract_flow_entries(gaap, [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ])
    capex_entries = extract_flow_entries(gaap, [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "PaymentsToAcquireOtherPropertyPlantAndEquipment",
    ])
    eps_entries = extract_flow_entries(gaap, ["EarningsPerShareDiluted", "EarningsPerShareBasic"], unit_name="USD/shares")

    # Diluted shares from flow (90d or 365d) + instant cover-page shares fallback
    diluted_flow = extract_flow_entries(gaap, [
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingBasic",
    ], unit_name="shares")
    share_instants = [
        {"end": e["end"], "val": e["val"], "filed": e["filed"], "rank": 0}
        for e in diluted_flow
    ] + extract_instant_entries(facts, [
        ("dei", "EntityCommonStockSharesOutstanding", "shares"),
        ("us-gaap", "CommonStockSharesOutstanding", "shares"),
    ])

    eq_instants = extract_instant_entries(facts, [
        ("us-gaap", "StockholdersEquity", "USD"),
        ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "USD"),
    ])
    cash_instants = extract_instant_entries(facts, [
        ("us-gaap", "CashCashEquivalentsAndShortTermInvestments", "USD"),
        ("us-gaap", "CashAndCashEquivalentsAtCarryingValue", "USD"),
        ("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "USD"),
    ])
    debt_instants = extract_instant_entries(facts, [
        ("us-gaap", "LongTermDebtAndCapitalLeaseObligations", "USD"),
        ("us-gaap", "LongTermDebt", "USD"),
        ("us-gaap", "LongTermDebtNoncurrent", "USD"),
    ])

    div_paid_entries = extract_flow_entries(gaap, ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"])
    div_ps_entries = extract_flow_entries(
        gaap, ["CommonStockDividendsPerShareDeclared", "CommonStockDividendsPerShareCashPaid"], unit_name="USD/shares"
    )

    # For corporate holding-company reorganizations (e.g. XOM 2026 CIK 0002115436 + legacy 0000034088), merge legacy facts
    if symbol == "XOM" and cik != "0000034088":
        leg = sec_get_json("https://data.sec.gov/api/xbrl/companyfacts/CIK0000034088.json").get("facts", {})
        leg_gaap = leg.get("us-gaap", {})
        rev_entries += extract_flow_entries(leg_gaap, ["Revenues", "SalesRevenueNet", "RevenueFromContractWithCustomerExcludingAssessedTax"])
        ni_entries += extract_flow_entries(leg_gaap, ["NetIncomeLoss", "ProfitLoss"])
        op_entries += extract_flow_entries(leg_gaap, ["OperatingIncomeLoss", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"])
        ocf_entries += extract_flow_entries(leg_gaap, ["NetCashProvidedByUsedInOperatingActivities"])
        capex_entries += extract_flow_entries(leg_gaap, ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"])
        eps_entries += extract_flow_entries(leg_gaap, ["EarningsPerShareDiluted", "EarningsPerShareBasic"], unit_name="USD/shares")
        leg_dil = extract_flow_entries(leg_gaap, ["WeightedAverageNumberOfDilutedSharesOutstanding"], unit_name="shares")
        share_instants += [{"end": e["end"], "val": e["val"], "filed": e["filed"], "rank": 0} for e in leg_dil]
        share_instants += extract_instant_entries(leg, [("dei", "EntityCommonStockSharesOutstanding", "shares")])
        eq_instants += extract_instant_entries(leg, [("us-gaap", "StockholdersEquity", "USD")])
        cash_instants += extract_instant_entries(leg, [("us-gaap", "CashAndCashEquivalentsAtCarryingValue", "USD")])
        debt_instants += extract_instant_entries(leg, [("us-gaap", "LongTermDebt", "USD")])

    return {
        "symbol": symbol,
        "sector": UNIVERSE_META[symbol],
        "is_financial": symbol in BALANCE_SHEET_FIN_SYMS,
        "dates": dates,
        "closes": closes,
        "adjs": adjs,
        "splits": splits,
        "rev": rev_entries,
        "ni": ni_entries,
        "op": op_entries,
        "ocf": ocf_entries,
        "capex": capex_entries,
        "eps": eps_entries,
        "div_paid": div_paid_entries,
        "div_ps": div_ps_entries,
        "shares": share_instants,
        "eq": eq_instants,
        "cash": cash_instants,
        "debt": debt_instants,
    }


def split_factor_after(splits: list[tuple[str, float]], ref_date: str) -> float:
    factor = 1.0
    for s_date, ratio in splits:
        if ref_date < s_date:
            factor *= ratio
    return factor


def evaluate_stock_at_date(st: dict[str, Any], eval_date: str) -> dict[str, Any] | None:
    dates = st["dates"]
    idx = bisect.bisect_right(dates, eval_date) - 1
    if idx < 40:
        return None
    # Ensure stock is actively trading around eval_date
    if (dt.date.fromisoformat(eval_date) - dt.date.fromisoformat(dates[idx])).days > 10:
        return None

    close_p = st["closes"][idx]
    adj_p = st["adjs"][idx]
    if close_p <= 0 or adj_p <= 0:
        return None

    rev_ttm, rev_end = compute_pit_ttm(st["rev"], eval_date)
    ni_ttm, ni_end = compute_pit_ttm(st["ni"], eval_date)
    if ni_ttm is None and rev_ttm is None:
        return None

    op_ttm, _ = compute_pit_ttm(st["op"], eval_date)
    ocf_ttm, _ = compute_pit_ttm(st["ocf"], eval_date)
    capex_ttm, _ = compute_pit_ttm(st["capex"], eval_date)
    fcf_ttm = (ocf_ttm - abs(capex_ttm)) if (ocf_ttm is not None and capex_ttm is not None) else ocf_ttm

    # YoY TTM Growth (comparing against TTM ~1 year prior, strictly filed <= eval_date!)
    rev_growth: float | None = None
    if rev_ttm is not None and rev_end is not None:
        rev_prev, _ = compute_pit_ttm(st["rev"], eval_date, max_end_date=rev_end - dt.timedelta(days=330))
        if rev_prev and rev_prev > 0:
            rev_growth = clamp(((rev_ttm - rev_prev) / rev_prev) * 100.0, -70.0, 150.0)

    eps_growth: float | None = None
    if ni_ttm is not None and ni_end is not None:
        ni_prev, _ = compute_pit_ttm(st["ni"], eval_date, max_end_date=ni_end - dt.timedelta(days=330))
        if ni_prev and ni_prev > 0:
            eps_growth = clamp(((ni_ttm - ni_prev) / ni_prev) * 100.0, -75.0, 150.0)
        elif ni_prev and ni_prev < 0 < ni_ttm:
            eps_growth = 45.0

    # Determine split-adjusted Market Cap at eval_date
    raw_sh, sh_end = latest_pit_instant(st["shares"], eval_date)
    mcap: float | None = None
    if raw_sh and sh_end and st["symbol"] not in ("BRK-B", "V"):
        # Normalize occasional SEC XBRL millions-unit scaling quirks (e.g. 708.3 instead of 708,300,000)
        norm_sh = raw_sh * 1_000_000.0 if (10.0 < raw_sh < 50_000.0) else (raw_sh * 1_000.0 if (50_000.0 <= raw_sh < 5_000_000.0) else raw_sh)
        adj_sh = norm_sh * split_factor_after(st["splits"], sh_end)
        mcap = adj_sh * close_p
    else:
        # Fallback 1: split-adjusted diluted EPS (handles Class-A/B share conversion ratios like BRK-B!)
        eps_ttm, eps_end = compute_pit_ttm(st["eps"], eval_date)
        if eps_ttm and eps_end and abs(eps_ttm) > 1e-4 and ni_ttm:
            adj_eps = eps_ttm / split_factor_after(st["splits"], eps_end)
            if st["symbol"] == "BRK-B" and abs(adj_eps) > 500:
                adj_eps = adj_eps / 1500.0
            implied_adj_shares = abs(ni_ttm / adj_eps)
            mcap = implied_adj_shares * close_p
        else:
            # Fallback 2: Total Cash Dividends Paid / Dividend Per Share (handles multi-class V!)
            dp_ttm, dp_end = compute_pit_ttm(st["div_paid"], eval_date)
            dps_ttm, dps_end = compute_pit_ttm(st["div_ps"], eval_date)
            if dp_ttm and dps_ttm and dps_end and dps_ttm > 0.05:
                adj_dps = dps_ttm / split_factor_after(st["splits"], dps_end)
                mcap = (abs(dp_ttm) / adj_dps) * close_p

    if not mcap or mcap <= 0:
        return None

    pe = (mcap / ni_ttm) if (ni_ttm is not None and abs(ni_ttm) > 0) else None
    # Forward-looking growth-adjusted P/E & PEG
    effective_growth = None
    if eps_growth is not None and rev_growth is not None:
        effective_growth = 0.6 * eps_growth + 0.4 * rev_growth
    elif eps_growth is not None:
        effective_growth = eps_growth
    elif rev_growth is not None:
        effective_growth = rev_growth

    fwd_pe_est = pe
    if pe is not None and pe > 0 and effective_growth is not None and effective_growth > -30:
        fwd_pe_est = pe / (1.0 + clamp(effective_growth, -25.0, 45.0) / 100.0)

    peg = (fwd_pe_est / effective_growth) if (fwd_pe_est and fwd_pe_est > 0 and effective_growth and effective_growth > 3.0) else None
    fcf_yield = (fcf_ttm / mcap * 100.0) if (fcf_ttm is not None and not st["is_financial"]) else None
    if fcf_yield is not None and abs(fcf_yield) > 45.0:
        fcf_yield = None

    eq_val, _ = latest_pit_instant(st["eq"], eval_date)
    cash_val, _ = latest_pit_instant(st["cash"], eval_date)
    debt_val, _ = latest_pit_instant(st["debt"], eval_date)

    pb = (mcap / eq_val) if (eq_val and eq_val > 0) else None
    roe = (ni_ttm / eq_val * 100.0) if (ni_ttm is not None and eq_val and eq_val > 0) else None
    debt_to_eq = (debt_val / eq_val) if (debt_val is not None and eq_val and eq_val > 0) else None
    net_cash_pct = ((cash_val - (debt_val or 0.0)) / mcap * 100.0) if cash_val is not None else None

    op_margin = (op_ttm / rev_ttm * 100.0) if (op_ttm is not None and rev_ttm and rev_ttm > 0) else None
    net_margin = (ni_ttm / rev_ttm * 100.0) if (ni_ttm is not None and rev_ttm and rev_ttm > 0) else None

    # Technicals strictly up to idx (eval_date)
    closes = st["closes"]
    rsi14 = compute_rsi_at(closes, idx, 14)
    w200 = closes[max(0, idx - 199): idx + 1]
    ma200 = sum(w200) / len(w200) if len(w200) >= 120 else None
    dist_200d = ((close_p - ma200) / ma200 * 100.0) if ma200 else None
    w252 = closes[max(0, idx - 251): idx + 1]
    high_52w = max(w252) if w252 else close_p
    dd_52w = ((close_p - high_52w) / high_52w * 100.0) if high_52w > 0 else 0.0

    scored = score_pit_snapshot({
        "is_financial": st["is_financial"],
        "pe": fwd_pe_est,
        "peg": peg,
        "fcfYield": fcf_yield,
        "pb": pb,
        "opMargin": op_margin,
        "netMargin": net_margin,
        "roe": roe,
        "debtToEq": debt_to_eq,
        "netCashPct": net_cash_pct,
        "revGrowth": rev_growth,
        "epsGrowth": eps_growth,
        "rsi14": rsi14,
        "dist200d": dist_200d,
        "dd52w": dd_52w,
    })

    return {
        "symbol": st["symbol"],
        "adj": adj_p,
        "close": round(close_p, 2),
        "pe": round(fwd_pe_est, 1) if fwd_pe_est else None,
        "peg": round(peg, 2) if peg else None,
        "rsi": round(rsi14, 0) if rsi14 else None,
        **scored,
    }


def compute_perf_stats(nav_series: list[float], bench_series: list[float]) -> dict[str, Any]:
    if len(nav_series) < 2:
        return {}
    start_v = nav_series[0]
    end_v = nav_series[-1]
    months = len(nav_series) - 1
    years = months / 12.0
    tot_ret = (end_v / start_v - 1.0) * 100.0
    cagr = ((end_v / start_v) ** (1.0 / years) - 1.0) * 100.0 if years > 0 else 0.0

    m_rets = [nav_series[i] / nav_series[i - 1] - 1.0 for i in range(1, len(nav_series))]
    b_rets = [bench_series[i] / bench_series[i - 1] - 1.0 for i in range(1, len(bench_series))]

    vol = statistics.stdev(m_rets) * math.sqrt(12.0) * 100.0 if len(m_rets) > 1 else 0.0
    rf_monthly = 0.035 / 12.0
    excess = [r - rf_monthly for r in m_rets]
    sharpe = (
        (statistics.mean(excess) / statistics.stdev(m_rets)) * math.sqrt(12.0)
        if len(m_rets) > 1 and statistics.stdev(m_rets) > 0
        else 0.0
    )

    peak = nav_series[0]
    max_dd = 0.0
    for v in nav_series:
        peak = max(peak, v)
        dd = (v / peak - 1.0) * 100.0
        max_dd = min(max_dd, dd)

    win_months = sum(1 for r, br in zip(m_rets, b_rets) if r > br)
    win_rate = (win_months / len(m_rets) * 100.0) if m_rets else 0.0

    return {
        "finalValue10k": round(end_v, 2),
        "totalReturnPct": round(tot_ret, 1),
        "cagrPct": round(cagr, 2),
        "maxDrawdownPct": round(max_dd, 1),
        "volatilityPct": round(vol, 1),
        "sharpe": round(sharpe, 2),
        "winMonthsVsSpyPct": round(win_rate, 1),
    }


def run_backtest() -> dict[str, Any]:
    t0 = time.time()
    print("Fetching SEC CIK map...")
    cik_raw = sec_get_json("https://www.sec.gov/files/company_tickers.json")
    cik_map = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in cik_raw.values()}

    print("Fetching SPY S&P 500 Total-Return benchmark...")
    spy_chart = yf_get_chart("SPY")
    spy_dates: list[str] = []
    spy_adjs: list[float] = []
    for ts, a in zip(
        spy_chart["timestamp"],
        spy_chart["indicators"]["adjclose"][0]["adjclose"],
    ):
        if isinstance(a, (int, float)) and not math.isnan(a):
            spy_dates.append(dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).strftime("%Y-%m-%d"))
            spy_adjs.append(float(a))

    # Build monthly trading dates over the last 5 years: Nov 2021 -> Nov 2026 (61 dates = 60 months)
    by_ym: dict[str, tuple[str, float]] = {}
    for d_s, adj in zip(spy_dates, spy_adjs):
        if d_s >= "2021-11-01":
            by_ym[d_s[:7]] = (d_s, adj)  # keeps last trading day of each month (and latest day of Nov 2026)

    ym_keys = sorted(by_ym.keys())
    rebalance_dates = [by_ym[k][0] for k in ym_keys]
    spy_rebal_adj = [by_ym[k][1] for k in ym_keys]

    print(f"Loading 7Y price + point-in-time SEC EDGAR filings for {len(UNIVERSE_META)} stocks...")
    stocks_db: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = {}
        for sym in UNIVERSE_META:
            cik = cik_map.get(sym) or cik_map.get(sym.replace("-", "."))
            if cik:
                futs[pool.submit(load_stock_history, sym, cik)] = sym
        for fut in as_completed(futs):
            sym = futs[fut]
            try:
                stocks_db[sym] = fut.result()
            except Exception as exc:
                print(f"  [warn] skipped {sym}: {exc}", file=sys.stderr)

    print(f"Loaded {len(stocks_db)} stocks cleanly in {time.time() - t0:.1f}s. Running 60-month simulation...")

    # Initialize $10,000 fictitious depots
    nav_strong = [10000.0]
    nav_all_buys = [10000.0]
    nav_spy = [10000.0]
    nav_eq_univ = [10000.0]
    nav_avoid = [10000.0]

    prev_w_strong: dict[str, float] = {}
    prev_w_buys: dict[str, float] = {}
    TX_COST_BPS = 0.0010  # 0.10% per one-way turnover

    checkpoints_log: list[dict[str, Any]] = []
    pick_forward_hits = 0
    pick_forward_total = 0

    for step in range(len(rebalance_dates) - 1):
        d_curr = rebalance_dates[step]
        d_next = rebalance_dates[step + 1]

        evals_now: list[dict[str, Any]] = []
        next_ret_map: dict[str, float] = {}

        for sym, st in stocks_db.items():
            ev = evaluate_stock_at_date(st, d_curr)
            if not ev:
                continue
            # Forward 1-month dividend-adjusted return from d_curr -> d_next
            idx_next = bisect.bisect_right(st["dates"], d_next) - 1
            if idx_next < 0:
                continue
            adj_next = st["adjs"][idx_next]
            ret_1m = (adj_next / ev["adj"]) - 1.0
            # Guard against extreme data glitches
            if -0.85 <= ret_1m <= 2.5:
                evals_now.append(ev)
                next_ret_map[sym] = ret_1m

        evals_now.sort(key=lambda x: x["score"], reverse=True)

        # 1. Strong Buy Depot (at least 5 stocks for realistic diversification, max 12 top convictions)
        strong_syms = [x["symbol"] for x in evals_now if x["verdictKey"] == "strong_buy"][:12]
        if len(strong_syms) < 5:
            fallback = [x["symbol"] for x in evals_now if x["verdictKey"] in ("strong_buy", "buy")]
            strong_syms = fallback[:max(5, len(strong_syms))]
        if not strong_syms:
            strong_syms = [x["symbol"] for x in evals_now[:5]]

        w_strong = {s: 1.0 / len(strong_syms) for s in strong_syms}
        turnover_strong = 0.5 * sum(
            abs(w_strong.get(s, 0.0) - prev_w_strong.get(s, 0.0))
            for s in set(w_strong) | set(prev_w_strong)
        )
        prev_w_strong = w_strong
        r_strong = sum(w * next_ret_map[s] for s, w in w_strong.items()) - turnover_strong * TX_COST_BPS

        # 2. All Buys (Strong + Good Buy) Depot
        buy_syms = [x["symbol"] for x in evals_now if x["verdictKey"] in ("strong_buy", "buy")]
        if len(buy_syms) < 5:
            buy_syms = [x["symbol"] for x in evals_now[:8]]
        w_buys = {s: 1.0 / len(buy_syms) for s in buy_syms}
        turnover_buys = 0.5 * sum(
            abs(w_buys.get(s, 0.0) - prev_w_buys.get(s, 0.0))
            for s in set(w_buys) | set(prev_w_buys)
        )
        prev_w_buys = w_buys
        r_buys = sum(w * next_ret_map[s] for s, w in w_buys.items()) - turnover_buys * TX_COST_BPS

        # 3. Equal-Weight Universe Benchmark
        r_univ = statistics.mean(next_ret_map[x["symbol"]] for x in evals_now) if evals_now else 0.0

        # 4. Expensive / Avoid Basket (Avoid-rated or bottom 5 lowest-scored stocks)
        avoid_syms = [x["symbol"] for x in evals_now if x["verdictKey"] == "avoid"]
        if len(avoid_syms) < 4:
            avoid_syms = [x["symbol"] for x in evals_now[-5:]]
        r_avoid = statistics.mean(next_ret_map[s] for s in avoid_syms) if avoid_syms else 0.0

        # 5. SPY Benchmark
        r_spy = (spy_rebal_adj[step + 1] / spy_rebal_adj[step]) - 1.0

        # Track individual Strong Buy pick monthly outperformance vs SPY
        for s in strong_syms:
            pick_forward_total += 1
            if next_ret_map[s] > r_spy:
                pick_forward_hits += 1

        nav_strong.append(nav_strong[-1] * (1.0 + r_strong))
        nav_all_buys.append(nav_all_buys[-1] * (1.0 + r_buys))
        nav_spy.append(nav_spy[-1] * (1.0 + r_spy))
        nav_eq_univ.append(nav_eq_univ[-1] * (1.0 + r_univ))
        nav_avoid.append(nav_avoid[-1] * (1.0 + r_avoid))

        # Log quarterly checkpoints + latest rebalance for full transparency in UI
        if step % 3 == 0 or step == len(rebalance_dates) - 2:
            top_map = {x["symbol"]: x for x in evals_now}
            checkpoints_log.append({
                "date": d_curr,
                "nextDate": d_next,
                "depotReturnPct": round(r_strong * 100.0, 2),
                "spyReturnPct": round(r_spy * 100.0, 2),
                "holdingsCount": len(strong_syms),
                "topHoldings": [
                    {
                        "symbol": s,
                        "score": top_map[s]["score"],
                        "pe": top_map[s]["pe"],
                        "peg": top_map[s]["peg"],
                        "next1mPct": round(next_ret_map[s] * 100.0, 1),
                    }
                    for s in strong_syms[:6]
                ],
                "avoidedExamples": [
                    {
                        "symbol": s,
                        "score": top_map[s]["score"],
                        "pe": top_map[s]["pe"],
                    }
                    for s in avoid_syms[-4:]
                ],
            })

    # Annual Calendar Returns Breakdown (2022, 2023, 2024, 2025, 2026 YTD)
    yearly_breakdown: list[dict[str, Any]] = []
    for yr in ("2022", "2023", "2024", "2025", "2026"):
        yr_indices = [i for i, d in enumerate(rebalance_dates) if d.startswith(yr)]
        if not yr_indices:
            continue
        start_i = max(0, yr_indices[0] - 1)
        end_i = yr_indices[-1]
        yearly_breakdown.append({
            "year": f"{yr} YTD" if yr == "2026" else yr,
            "strongBuyPct": round((nav_strong[end_i] / nav_strong[start_i] - 1.0) * 100.0, 1),
            "allBuysPct": round((nav_all_buys[end_i] / nav_all_buys[start_i] - 1.0) * 100.0, 1),
            "spyPct": round((nav_spy[end_i] / nav_spy[start_i] - 1.0) * 100.0, 1),
            "equalUnivPct": round((nav_eq_univ[end_i] / nav_eq_univ[start_i] - 1.0) * 100.0, 1),
            "avoidBasketPct": round((nav_avoid[end_i] / nav_avoid[start_i] - 1.0) * 100.0, 1),
        })

    # Compute Multi-Horizon Summary Stats (5Y, 3Y, 1Y)
    def horizon_slice(months_back: int) -> dict[str, Any]:
        s_idx = max(0, len(rebalance_dates) - 1 - months_back)
        norm = lambda arr: [v / arr[s_idx] * 10000.0 for v in arr[s_idx:]]
        ns = norm(nav_strong)
        nb = norm(nav_all_buys)
        nspy = norm(nav_spy)
        nu = norm(nav_eq_univ)
        na = norm(nav_avoid)
        return {
            "startDate": rebalance_dates[s_idx],
            "endDate": rebalance_dates[-1],
            "months": len(ns) - 1,
            "strongBuy": compute_perf_stats(ns, nspy),
            "allBuys": compute_perf_stats(nb, nspy),
            "spy": compute_perf_stats(nspy, nspy),
            "equalUniverse": compute_perf_stats(nu, nspy),
            "avoidBasket": compute_perf_stats(na, nspy),
        }

    series = [
        {
            "date": rebalance_dates[i],
            "strongBuy": round(nav_strong[i], 2),
            "allBuys": round(nav_all_buys[i], 2),
            "spy": round(nav_spy[i], 2),
            "equalUniv": round(nav_eq_univ[i], 2),
            "avoid": round(nav_avoid[i], 2),
        }
        for i in range(len(rebalance_dates))
    ]

    payload = {
        "generatedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "initialCapital": 10000,
        "universeCount": len(stocks_db),
        "rebalanceFrequency": "Monthly (month-end close, 0.10% turnover fee deducted)",
        "methodology": (
            "Point-in-time historical simulation (Nov 2021 – Nov 2026) using official SEC EDGAR 10-Q/10-K filings "
            "strictly gated by actual SEC filing dates (zero look-ahead bias) + dividend-reinvested Yahoo Finance "
            "total-return series. Compares against both S&P 500 (SPY) and an Equal-Weight Universe benchmark."
        ),
        "pickBeatSpyRatePct": round((pick_forward_hits / pick_forward_total * 100.0) if pick_forward_total else 0.0, 1),
        "horizons": {
            "5y": horizon_slice(60),
            "3y": horizon_slice(36),
            "1y": horizon_slice(12),
        },
        "yearly": yearly_breakdown,
        "series": series,
        "checkpoints": list(reversed(checkpoints_log)),
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, separators=(",", ":"))
    BACKTEST_JSON_FILE.write_text(raw, encoding="utf-8")
    BACKTEST_JS_FILE.write_text(f"window.__BACKTEST_DATA__ = {raw};\n", encoding="utf-8")
    return payload


if __name__ == "__main__":
    res = run_backtest()
    h5 = res["horizons"]["5y"]
    print("\n=== 5-YEAR FICTITIOUS $10,000 DEPOT RESULTS ({} -> {}) ===".format(h5["startDate"], h5["endDate"]))
    for name, key in [
        ("Strong Buy Depot", "strongBuy"),
        ("All Buys Depot  ", "allBuys"),
        ("Equal-Wt Univ   ", "equalUniverse"),
        ("S&P 500 (SPY)   ", "spy"),
        ("Avoid Basket    ", "avoidBasket"),
    ]:
        st = h5[key]
        print(
            "  {} : ${:>9,.2f}  Total={:>+6.1f}%  CAGR={:>+5.2f}%/yr  MaxDD={:>6.1f}%  Sharpe={:.2f}".format(
                name, st["finalValue10k"], st["totalReturnPct"], st["cagrPct"], st["maxDrawdownPct"], st["sharpe"]
            )
        )
    print("\n=== YEAR-BY-YEAR RETURNS ===")
    for y in res["yearly"]:
        print(
            "  {:8s}: StrongBuy={:>+6.1f}% | AllBuys={:>+6.1f}% | EqualUniv={:>+6.1f}% | SPY={:>+6.1f}% | Avoid={:>+6.1f}%".format(
                y["year"], y["strongBuyPct"], y["allBuysPct"], y["equalUnivPct"], y["spyPct"], y["avoidBasketPct"]
            )
        )
