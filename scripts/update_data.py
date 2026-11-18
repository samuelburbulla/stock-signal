#!/usr/bin/env python3
"""
Daily Stock Valuation, Reasonable Price Range & Falling-Knife Engine.

Fetches live prices, 1-year daily OHLCV technicals, fundamental valuation metrics,
and per-ticker corporate/governance news RSS feeds (no API key required). Computes:
  1. Per-stock Reasonable Price Range (`Cheap` · `Fair` · `Expensive`)
  2. Live Corporate/Leadership Shock & Technical Falling-Knife guardrails (`🚨`)
  3. Multi-factor Buy Scores (1-99), traffic-light indicators, and plain-English takeaways

Writes `public/data/stocks.json` and `public/data/stocks_snapshot.js`.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import http.cookiejar
import json
import math
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "public" / "data"
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
STOCKS_JSON_FILE = DATA_DIR / "stocks.json"
STOCKS_JS_FILE = DATA_DIR / "stocks_snapshot.js"

DEFAULT_WATCHLIST = [
    # Technology, Cloud, AI, Frontier Tech & Semiconductors
    "NVDA", "GOOGL", "META", "MSFT", "AMZN", "AAPL", "ANT", "SPCX", "SPY",
    "AVGO", "AMD", "TSM", "ASML",
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

TICKER_ALIASES: dict[str, str] = {
    "SPX": "SPY",
    "^SPX": "SPY",
    "^GSPC": "SPY",
    "ANTHROPIC": "ANT",
    "SPACEX": "SPCX",
}

CYCLICAL_MEMORY_SYMS = {"MU", "WDC", "STX"}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

CORP_SUFFIX_RE = re.compile(
    r"(?:,?\s+(?:inc\.?|incorporated|corp\.?|corporation|platforms|holdings|company|co\.?|plc|n\.v\.|s\.e\.|se|a\.g\.|ag|pbc|limited|ltd\.?|group|trust))+$",
    re.IGNORECASE,
)

EXEC_SHOCK_RE = re.compile(
    r"(\bceo\b.{0,45}\b(resign\w*|step\w*\s+down|oust\w*|fired|depart\w*|exit\w*|leave\w*|left)\b"
    r"|\b(resign\w*|step\w*\s+down|oust\w*|depart\w*|exit\w*)\b.{0,45}\b(as\s+ceo|ceo|chief\s+executive|founder)\b"
    r"|\b(interim\s+ceo|acting\s+ceo|ceo\s+search|ceo\s+exit|ceo\s+departure|ceo\s+ouster|after\s+\w+\s+\w+\s+ouster|life\s+after\s+\w+|without\s+founder)\b"
    r"|\b(founder|co-founder)\b.{0,45}\b(resign\w*|step\w*\s+down|oust\w*|exit\w*|left)\b)",
    re.IGNORECASE,
)

PROBE_SHOCK_RE = re.compile(
    r"\b(sec\s+(probe\w*|investigat\w*|subpoena\w*|charg\w*)"
    r"|doj\s+(probe\w*|investigat\w*|subpoena\w*|criminal)"
    r"|justice\s+department\s+(probe\w*|investigat\w*)"
    r"|federal\s+(probe\w*|investigat\w*)"
    r"|board\s+investigat\w*|independent\s+investigat\w*|internal\s+investigat\w*"
    r"|investigat\w+\s+(?:ceo|founder|chief\s+executive|accounting|disclosur\w*)"
    r"|accounting\s+(fraud|probe\w*|irregularit\w*|restatement\w*)"
    r"|restate\w*\s+financial\w*|auditor\s+resign\w*|chapter\s+11|bankruptcy\s+protection)\b",
    re.IGNORECASE,
)


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
                    "User-Agent", USER_AGENT,
                }
                if False
                else {
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
    """Computes Wilder's smoothed Relative Strength Index."""
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


def _clean_brand_name(disp_name: str, sym: str) -> str:
    base = disp_name.split("—")[0].split("(")[0].strip()
    base = CORP_SUFFIX_RE.sub("", base).strip(" ,.-")
    parts = base.split()
    return " ".join(parts[:2]) if parts else sym


IGNORE_SHOCK_TITLE_RE = re.compile(
    r"\b(rival\s+ceo|competitor\w*\s+ceo|peer\w*\s+ceo|evolved\s+since|years\s+(?:since|after)|decades\s+after|customer\s+shock)\b",
    re.IGNORECASE,
)

OTHER_MEGACAP_NAMES = {
    "NVDA": ("nvidia", "jensen huang"),
    "AAPL": ("apple", "tim cook"),
    "MSFT": ("microsoft", "satya nadella"),
    "TSLA": ("tesla", "elon musk"),
    "META": ("meta", "zuckerberg"),
    "GOOGL": ("alphabet", "google", "sundar pichai"),
    "AMZN": ("amazon", "andy jassy"),
    "INTC": ("intel",),
    "AMD": ("amd", "lisa su"),
    "AVGO": ("broadcom", "hock tan"),
}


def fetch_news_shock(
    sym: str,
    disp_name: str,
    dip_20d: float | None,
    dist_50d: float | None,
    drawdown_52w: float | None,
    change_pct: float | None,
) -> dict[str, Any] | None:
    """
    Scans recent per-ticker RSS headlines for material corporate/leadership/regulatory shocks:
      - CEO / founder sudden resignation, ouster, or interim CEO transition
      - SEC / DOJ / board independent investigation, accounting restatement, or fraud probe
    Matches strictly on recent headline titles about `sym` itself (filtering out rival/sector spillover).
    """
    if sym in ("SPY", "SPCX", "ANT"):
        return None

    brand = _clean_brand_name(disp_name, sym)
    urls = [
        f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={urllib.parse.quote(sym)}&region=US&lang=en-US"
    ]
    is_price_stressed = (
        (dip_20d is not None and dip_20d <= -10.0)
        or (dist_50d is not None and dist_50d <= -10.0)
        or (drawdown_52w is not None and drawdown_52w <= -22.0)
        or (change_pct is not None and change_pct <= -4.5)
    )
    if is_price_stressed:
        q = urllib.parse.quote(f"{sym} {brand} stock when:14d")
        urls.append(f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en")

    now_utc = dt.datetime.now(dt.timezone.utc)
    titles: list[str] = []
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=5) as resp:
                root = ET.fromstring(resp.read())
            for it in root.findall(".//item"):
                pub_txt = (it.findtext("pubDate") or "").strip()
                if pub_txt:
                    try:
                        pub_dt = email.utils.parsedate_to_datetime(pub_txt)
                        if pub_dt.tzinfo is None:
                            pub_dt = pub_dt.replace(tzinfo=dt.timezone.utc)
                        if (now_utc - pub_dt).days > 21:
                            continue
                    except Exception:
                        pass
                title = (it.findtext("title") or "").strip()
                if title:
                    titles.append(title)
        except Exception:
            continue

    if not titles:
        return None

    brand_tok = brand.split()[0].lower() if brand.split() else sym.lower()
    mention_re = re.compile(
        rf"\b({re.escape(sym.lower())}|{re.escape(brand_tok)})\b",
        re.IGNORECASE,
    )
    foreign_tokens = [
        tok
        for other_sym, toks in OTHER_MEGACAP_NAMES.items()
        if other_sym != sym
        for tok in toks
    ]

    exec_hits: list[str] = []
    probe_hits: list[str] = []
    seen_titles: set[str] = set()

    for raw_title in titles:
        clean_title = re.sub(r"\s+-\s+[^-]+$", "", raw_title).strip()
        if not mention_re.search(clean_title):
            continue
        if IGNORE_SHOCK_TITLE_RE.search(clean_title):
            continue
        low_t = clean_title.lower()
        # Skip spillover articles that attribute the shock to a different named mega-cap/CEO
        if any(ft in low_t for ft in foreign_tokens):
            continue

        dedup_key = re.sub(r"\W+", " ", low_t).strip()[:60]
        if not dedup_key or dedup_key in seen_titles:
            continue

        matched_exec = bool(EXEC_SHOCK_RE.search(clean_title))
        matched_probe = bool(PROBE_SHOCK_RE.search(clean_title))
        if matched_exec or matched_probe:
            seen_titles.add(dedup_key)
            if matched_exec:
                exec_hits.append(clean_title)
            if matched_probe:
                probe_hits.append(clean_title)

    if len(seen_titles) < 2:
        return None

    headlines = list(dict.fromkeys(exec_hits + probe_hits))[:3]
    if exec_hits and probe_hits:
        return {
            "active": True,
            "type": "ceo_and_probe",
            "badge": "🚨 CEO Exit & Probe",
            "reason": "Leadership departure & active regulatory/board investigation in recent headlines",
            "headlines": headlines,
        }
    if exec_hits:
        return {
            "active": True,
            "type": "ceo_exit",
            "badge": "🚨 CEO Exit / Knife" if (dip_20d or 0.0) <= -12.0 else "🚨 CEO Transition",
            "reason": "Major CEO / founder departure detected in recent company headlines",
            "headlines": headlines,
        }
    return {
        "active": True,
        "type": "probe",
        "badge": "🚨 Probe / Knife" if (dip_20d or 0.0) <= -12.0 else "🚨 Governance Probe",
        "reason": "Active regulatory, board, or accounting investigation detected in recent headlines",
        "headlines": headlines,
    }


def compute_price_range(
    metrics: dict[str, Any],
    shock_alert: dict[str, Any] | None,
    is_waterfall: bool,
) -> dict[str, Any]:
    """
    Computes a fundamental Reasonable Price Range (`Cheap` · `Fair` · `Expensive`) for every stock:
      - `fairPrice`: blended intrinsic value from growth/quality-adjusted Earnings Power,
        Free Cash Flow Yield (or Price/Book for banks / mid-cycle EPS for memory chips),
        and a fundamental-capped consensus check (plus an event-risk haircut on shocks/waterfalls).
      - `cheapPrice`: 18% margin-of-safety discount below Fair Value (`0.82 * fairPrice`)
      - `expensivePrice`: 20% valuation premium above Fair Value (`1.20 * fairPrice`)
    """
    sym = metrics.get("symbol") or ""
    price = float(metrics.get("price") or 0.0)
    sector = metrics.get("sector") or "Other"
    industry = metrics.get("industry") or ""
    shock_active = bool(shock_alert and shock_alert.get("active"))

    f_pe = metrics.get("forwardPE")
    t_pe = metrics.get("trailingPE")
    fcf_yield = metrics.get("fcfYield")
    pb = metrics.get("priceToBook")
    ps = metrics.get("priceToSales")
    rev_growth = metrics.get("revenueGrowth")
    eps_growth = metrics.get("epsGrowth")
    net_margin = metrics.get("profitMargin")
    op_margin = metrics.get("operatingMargin")
    gross_margin = metrics.get("grossMargin")
    roe = metrics.get("roe")
    debt_to_eq = metrics.get("debtToEquity")
    net_cash_pct = metrics.get("netCashPctMarketCap")
    target_mean = metrics.get("targetMeanPrice")

    is_financial = (
        sector == "Real Estate"
        or (
            sector == "Financial Services"
            and ("Bank" in industry or "Insurance" in industry)
            and sym != "BRK-B"
        )
    )
    is_memory_cyclical = (
        sym in CYCLICAL_MEMORY_SYMS
        or "Memory" in industry
        or (
            "Semiconductor" in industry
            and f_pe is not None
            and t_pe is not None
            and 0 < f_pe < 6.0
            and t_pe > f_pe * 1.8
        )
    )

    anchors: list[tuple[float, float]] = []
    basis_parts: list[str] = []

    if sym == "SPY":
        eff_pe = f_pe or 20.8
        fair_pe = 19.5
        anchors.append((price * (fair_pe / eff_pe), 1.0))
        basis_parts.append("19.5x historical S&P 500 fair Fwd P/E")
    elif sym == "ANT":
        eff_pe = f_pe or 56.16
        anchors.append((price * (38.0 / eff_pe), 0.60))
        anchors.append((price * (12.0 / (ps or 17.3)), 0.40))
        basis_parts.append("38x FY27E earnings & 12x ARR multiple")
    elif sym == "SPCX":
        if f_pe and f_pe > 0:
            anchors.append((price * (36.0 / f_pe), 0.75))
        if target_mean and target_mean > 0:
            anchors.append((min(target_mean, price * 1.25), 0.25))
        basis_parts.append("36x high-growth aerospace/Starlink Fwd P/E")
    elif is_memory_cyclical:
        mid_pe_ref = t_pe if (t_pe and t_pe > 0) else ((f_pe * 2.2) if (f_pe and f_pe > 0) else 12.0)
        fair_mid_pe = 11.0
        iv_cyc = price * (fair_mid_pe / mid_pe_ref)
        anchors.append((iv_cyc, 0.85))
        if fcf_yield and fcf_yield > 1.0:
            anchors.append((clamp(price * (fcf_yield / 4.5), iv_cyc * 0.65, iv_cyc * 1.25), 0.15))
        basis_parts.append("11.0x mid-cycle memory P/E (normalized for HBM cycle peak)")
    elif is_financial:
        eff_pe = f_pe if (f_pe and f_pe > 0) else (t_pe if (t_pe and t_pe > 0) else None)
        roe_ref = roe if roe is not None else 12.0
        fair_fin_pe = interp(roe_ref, [(6, 9.5), (10, 11.8), (14, 13.8), (18, 15.2), (24, 16.5)])
        if eps_growth is not None and eps_growth > 10:
            fair_fin_pe += min(1.2, (eps_growth - 10.0) * 0.08)
        if eff_pe:
            anchors.append((price * (fair_fin_pe / eff_pe), 0.65))
            basis_parts.append(f"{fair_fin_pe:.1f}x ROE-adjusted bank P/E")
        if pb and pb > 0:
            fair_pb = clamp(roe_ref / 9.2, 0.85, 2.8)
            anchors.append((price * (fair_pb / pb), 0.35))
            basis_parts.append(f"{fair_pb:.1f}x fair P/Book")
    else:
        eff_margin = net_margin
        if op_margin is not None and (eff_margin is None or (op_margin > 15.0 and eff_margin < op_margin * 0.45)):
            eff_margin = op_margin * 0.78

        rg = clamp(rev_growth if rev_growth is not None else 6.0, -12.0, 42.0)
        eg_cap = max(rg * 1.6, 8.0) if rg >= 0 else 5.0
        eg = clamp(eps_growth if eps_growth is not None else rg, -15.0, eg_cap)
        if rg >= 8.0 and (eff_margin or 0.0) >= 12.0 and eg < rg * 0.6:
            eg = rg * 0.75
        sust_g = clamp(0.68 * rg + 0.32 * eg, -8.0, 35.0)

        base_pe = interp(
            sust_g,
            [(-8, 10.5), (0, 14.0), (6, 17.5), (12, 21.0), (18, 24.5), (26, 28.0), (35, 31.5)],
        )
        qual_adj = 0.0
        if eff_margin is not None:
            if eff_margin >= 26.0:
                qual_adj += 2.2
            elif eff_margin >= 16.0:
                qual_adj += 1.0
            elif eff_margin < 5.0:
                qual_adj -= 2.2
        if roe is not None and roe >= 28.0:
            qual_adj += 0.8
        if net_cash_pct is not None and net_cash_pct >= 5.0:
            qual_adj += 0.7
        elif debt_to_eq is not None and debt_to_eq >= 2.2:
            qual_adj -= 1.5

        fair_pe = clamp(base_pe + qual_adj, 10.5, 33.0)
        if sector == "Energy":
            fair_pe = min(fair_pe, 14.5)
        elif "Auto" in industry and (eff_margin or 0.0) < 12.0:
            fair_pe = min(fair_pe, 24.0)

        eff_pe = f_pe if (f_pe and f_pe > 0) else (t_pe if (t_pe and t_pe > 0 and t_pe < 60) else None)
        if sym == "BRK-B" and t_pe and f_pe and 0 < t_pe < f_pe:
            eff_pe = 0.50 * f_pe + 0.50 * t_pe
        # When an active corporate shock hits, consensus forward EPS often lags reality -> blend TTM P/E
        if shock_active and f_pe and t_pe and 0 < f_pe < t_pe * 0.75 and t_pe < 45:
            eff_pe = 0.40 * f_pe + 0.60 * t_pe

        iv_pe_val: float | None = None
        if eff_pe is not None:
            iv_pe_val = price * (fair_pe / eff_pe)
            anchors.append((iv_pe_val, 0.74))
            basis_parts.append(f"{fair_pe:.1f}x growth/quality-adjusted P/E")

        if fcf_yield is not None and fcf_yield > 1.5:
            fair_fcf_y = interp(sust_g, [(-5, 5.8), (0, 5.0), (8, 4.1), (16, 3.4), (26, 2.8), (35, 2.4)])
            iv_fcf = price * (fcf_yield / fair_fcf_y)
            if iv_pe_val is not None:
                iv_fcf = clamp(iv_fcf, iv_pe_val * 0.60, iv_pe_val * 1.35)
            anchors.append((iv_fcf, 0.26))
            basis_parts.append(f"{fair_fcf_y:.1f}% fair FCF yield")
        elif eff_pe is None and ps and ps > 0:
            gm_factor = clamp((gross_margin or 35.0) / 50.0, 0.35, 1.30)
            fair_ps = interp(rg, [(-10, 0.7), (0, 1.3), (10, 2.5), (22, 4.5), (40, 7.5)]) * gm_factor
            if net_margin is not None and net_margin < -10.0:
                fair_ps *= 0.65
            anchors.append((price * (fair_ps / ps), 1.0))
            basis_parts.append(f"{fair_ps:.1f}x margin-adjusted Price/Sales")

    fund_fair = sum(v * w for v, w in anchors) / sum(w for _, w in anchors) if anchors else price

    if target_mean and target_mean > 0 and sym not in ("SPY", "ANT"):
        if fund_fair > price:
            fair_price = 0.55 * fund_fair + 0.45 * clamp(target_mean, price * 0.85, fund_fair * 1.15)
        else:
            fair_price = 0.82 * fund_fair + 0.18 * min(target_mean, fund_fair * 1.25)
    else:
        fair_price = fund_fair

    # Apply explicit uncertainty haircut for live governance/CEO shocks or violent waterfall breakdowns
    if shock_active:
        fair_price *= 0.72
        basis_parts.append("28% uncertainty haircut for active CEO/governance shock")
    elif is_waterfall:
        fair_price *= 0.88
        basis_parts.append("12% haircut for severe falling-knife breakdown")

    # Smooth upside compression above +18% so undervalued stocks keep distinct, realistic fair targets
    if fair_price > price * 1.18:
        fair_price = price * 1.18 + (fair_price - price * 1.18) * 0.45
    fair_price = clamp(fair_price, price * 0.20, price * 1.62)

    cheap_price = fair_price * 0.82
    expensive_price = fair_price * 1.20

    vs_fair_pct = ((price / fair_price) - 1.0) * 100.0 if fair_price > 0 else 0.0
    if price <= cheap_price:
        zone = "cheap"
        zone_label = "Cheap"
    elif price >= expensive_price:
        zone = "expensive"
        zone_label = "Expensive"
    else:
        zone = "fair"
        zone_label = "Fair"

    # Piecewise visual position (0..100) where Cheap=[0..28], Fair=[28..72] (50=Fair), Expensive=[72..100]
    if price <= cheap_price:
        pos = interp(price, [(cheap_price * 0.72, 4.0), (cheap_price, 28.0)])
    elif price <= fair_price:
        pos = interp(price, [(cheap_price, 28.0), (fair_price, 50.0)])
    elif price <= expensive_price:
        pos = interp(price, [(fair_price, 50.0), (expensive_price, 72.0)])
    else:
        pos = interp(price, [(expensive_price, 72.0), (expensive_price * 1.45, 96.0)])

    return {
        "cheapPrice": round(cheap_price, 2),
        "fairPrice": round(fair_price, 2),
        "expensivePrice": round(expensive_price, 2),
        "vsFairPct": round(vs_fair_pct, 1),
        "valuationZone": zone,
        "valuationZoneLabel": zone_label,
        "rangePosValuation": round(pos, 1),
        "rangeBasis": "Anchored to " + " + ".join(basis_parts) if basis_parts else "Quantitative fair-value estimate",
    }


def score_stock(metrics: dict[str, Any]) -> dict[str, Any]:
    """
    Transparent 5-Pillar Quantitative Stock Scoring Engine (1-99) with
    Reasonable Price Range alignment + Live Shock & Falling-Knife Guardrails.
    """
    sym = metrics.get("symbol") or ""
    sector = metrics.get("sector") or "Unknown"
    industry = metrics.get("industry") or ""
    shock_alert: dict[str, Any] | None = metrics.get("shockAlert")
    shock_active = bool(shock_alert and shock_alert.get("active"))

    is_financial = (
        sector == "Real Estate"
        or (
            sector == "Financial Services"
            and ("Bank" in industry or "Insurance" in industry)
            and sym != "BRK-B"
        )
    )
    is_memory_cyclical = (
        sym in CYCLICAL_MEMORY_SYMS
        or "Memory" in industry
        or (
            "Semiconductor" in industry
            and metrics.get("forwardPE") is not None
            and metrics.get("trailingPE") is not None
            and 0 < metrics["forwardPE"] < 6.0
            and metrics["trailingPE"] > metrics["forwardPE"] * 1.8
        )
    )

    f_pe = metrics.get("forwardPE")
    t_pe = metrics.get("trailingPE")
    peg = metrics.get("pegRatio")
    fcf_yield = metrics.get("fcfYield")
    ev_ebitda = metrics.get("evToEbitda")
    pb = metrics.get("priceToBook")

    rev_growth = metrics.get("revenueGrowth")
    eps_growth = metrics.get("epsGrowth")
    op_margin = metrics.get("operatingMargin")
    net_margin = metrics.get("profitMargin")
    roe = metrics.get("roe")
    debt_to_eq = metrics.get("debtToEquity")
    net_cash_pct = metrics.get("netCashPctMarketCap")

    upside_pct = metrics.get("targetUpsidePct")
    rec_mean = metrics.get("recommendationMean")
    rsi = metrics.get("rsi14")
    rsi5 = metrics.get("rsi5")
    dip_20d = metrics.get("dip20dPct")
    dist_50d = metrics.get("distFrom50dPct")
    dist_200d = metrics.get("distFrom200dPct")
    drawdown_52w = metrics.get("drawdownFrom52wHighPct")

    # Detect severe technical waterfall / falling knife
    is_waterfall_crash = bool(
        (dip_20d is not None and dip_20d <= -25.0 and dist_50d is not None and dist_50d <= -20.0)
        or (
            dip_20d is not None
            and dip_20d <= -22.0
            and dist_50d is not None
            and dist_50d <= -18.0
            and dist_200d is not None
            and dist_200d <= -16.0
        )
    )
    is_shock_selloff = bool(
        shock_active
        and (
            (dip_20d is not None and dip_20d <= -8.0)
            or (dist_50d is not None and dist_50d <= -8.0)
            or (rsi is not None and rsi <= 45.0)
        )
    )
    is_falling_knife = is_waterfall_crash or is_shock_selloff

    # Compute Reasonable Price Range first so Valuation Pillar & Score align 100% with Cheap/Fair/Expensive
    range_info = compute_price_range(metrics, shock_alert, is_waterfall_crash)
    vs_fair_pct = range_info["vsFairPct"]
    val_zone = range_info["valuationZone"]

    # --- 1. VALUATION PILLAR (0-100) ---
    val_parts: list[tuple[float, float]] = []

    if is_memory_cyclical:
        effective_pe = t_pe if (t_pe and t_pe > 0) else (f_pe * 2.2 if (f_pe and f_pe > 0) else None)
    else:
        effective_pe = f_pe if (f_pe and f_pe > 0) else (t_pe if (t_pe and t_pe > 0) else None)

    if effective_pe is not None:
        if is_financial or is_memory_cyclical:
            pe_score = interp(effective_pe, [(7, 94), (11, 80), (14, 64), (18, 45), (25, 22), (35, 8)])
        else:
            pe_score = interp(effective_pe, [(9, 96), (14, 86), (19, 72), (24, 56), (31, 38), (42, 20), (65, 6)])
        val_parts.append((pe_score, 0.30))
    elif (t_pe is not None and t_pe < 0) or (f_pe is not None and f_pe < 0):
        val_parts.append((12.0, 0.30))

    # Price vs. Fundamental Fair Value score (ensures Cheap/Fair/Expensive coherence)
    fair_gap_score = interp(
        vs_fair_pct,
        [(-40, 96), (-22, 86), (-10, 72), (0, 60), (15, 44), (25, 30), (45, 14), (80, 5)],
    )
    val_parts.append((fair_gap_score, 0.30))

    if not is_memory_cyclical and peg is not None and peg > 0:
        peg_score = interp(peg, [(0.4, 96), (0.85, 86), (1.2, 72), (1.65, 56), (2.2, 38), (3.0, 20), (4.5, 6)])
        val_parts.append((peg_score, 0.20))
    elif peg is not None and peg < 0:
        val_parts.append((18.0, 0.16))

    if not is_financial and fcf_yield is not None:
        fcf_score = interp(fcf_yield, [(-2.0, 14), (0.5, 28), (2.0, 48), (3.5, 68), (5.0, 84), (7.5, 95)])
        val_parts.append((fcf_score, 0.12))
    elif is_financial and pb is not None and pb > 0:
        pb_score = interp(pb, [(0.8, 92), (1.3, 78), (2.0, 62), (3.2, 44), (6.0, 25)])
        val_parts.append((pb_score, 0.20))

    if not is_financial and ev_ebitda is not None and ev_ebitda > 0:
        ev_score = interp(ev_ebitda, [(6, 95), (10, 82), (15, 65), (22, 45), (32, 24), (50, 8)])
        val_parts.append((ev_score, 0.08))

    val_score = (
        sum(s * w for s, w in val_parts) / sum(w for _, w in val_parts)
        if val_parts
        else 45.0
    )

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
        eff_upside = upside_pct * 0.55 if shock_active else upside_pct
        u_score = interp(eff_upside, [(-15, 12), (-5, 28), (0, 42), (10, 64), (20, 80), (35, 93), (55, 98)])
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
    fundamental_health = 0.55 * qual_score + 0.45 * grow_score
    if is_falling_knife:
        # Never reward catching an active waterfall crash or CEO/probe shock!
        timing_score = 18.0 if shock_active else 24.0
    else:
        time_parts: list[tuple[float, float]] = []
        if rsi is not None:
            if fundamental_health >= 55:
                rsi_score = interp(rsi, [(24, 86), (33, 92), (45, 78), (58, 62), (68, 42), (78, 20), (85, 10)])
            else:
                rsi_score = interp(rsi, [(25, 42), (40, 56), (55, 55), (70, 32), (80, 15)])
            time_parts.append((rsi_score, 0.50))

        if dist_200d is not None:
            d200_score = interp(dist_200d, [(-30, 44), (-12, 84), (0, 80), (10, 64), (22, 44), (38, 22)])
            time_parts.append((d200_score, 0.30))

        if drawdown_52w is not None:
            dd_score = interp(drawdown_52w, [(-50, 46), (-25, 84), (-14, 78), (-5, 55), (0, 44)])
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

    # Valuation Guardrails (strict alignment with Reasonable Price Range)
    if val_score < 24 or vs_fair_pct >= 42.0:
        total = min(total, 43.0)
    elif val_score < 48 or val_zone == "expensive":
        total = min(total, 61.0)

    if qual_score < 28 and grow_score < 28:
        total = min(total, 42.0)

    # Corporate Shock & Falling-Knife Guardrails (prevents NVDA/NOW/INTC false Strong Buys)
    if shock_active:
        if qual_score >= 55 and val_score >= 50:
            total = min(total, 55.0)  # Hold / Watch until leadership/probe dust settles
        else:
            total = min(total, 42.0)
    elif is_falling_knife:
        if qual_score >= 55 and val_score >= 50 and (net_margin is None or net_margin > 0):
            total = min(total, 57.0)  # Hold / Watch until waterfall stabilizes
        else:
            total = min(total, 41.0)  # Unprofitable / weak falling knife -> Avoid

    score = int(round(clamp(total, 1, 99)))

    if score >= 78 and val_score >= 60 and val_zone != "expensive" and not is_falling_knife and not shock_active:
        verdict = "Strong Buy"
        verdict_key = "strong_buy"
    elif score >= 65 and val_score >= 48 and val_zone != "expensive" and not is_falling_knife and not shock_active:
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

    if shock_active and shock_alert:
        shock_msg = shock_alert["reason"]
        if dip_20d is not None and dip_20d <= -10.0:
            shock_msg += f" ({dip_20d:.0f}% 20d plunge — wait for base stabilization)"
        red_flags.append(shock_msg)
    elif is_falling_knife:
        red_flags.append(
            f"Severe falling-knife breakdown ({dip_20d or 0.0:.0f}% off 20d high, {dist_50d or 0.0:.0f}% vs 50d MA) — wait for trend stabilization"
        )

    cur_sym = metrics.get("currencySymbol") or "$"
    if val_zone == "cheap":
        green_flags.append(
            f"Trading in Cheap zone ({abs(vs_fair_pct):.0f}% below ~{cur_sym}{range_info['fairPrice']:,.0f} fair value)"
        )
    elif val_zone == "expensive":
        red_flags.append(
            f"Trading in Expensive zone (+{vs_fair_pct:.0f}% above ~{cur_sym}{range_info['fairPrice']:,.0f} fair value)"
        )

    if effective_pe is not None:
        if effective_pe <= (12.0 if (is_financial or is_memory_cyclical) else 17.0):
            green_flags.append(f"Attractive earnings multiple ({effective_pe:.1f}x P/E)")
        elif effective_pe >= (22.0 if is_financial else 34.0):
            red_flags.append(f"Rich earnings multiple ({effective_pe:.1f}x Forward P/E)")

    if not is_memory_cyclical and peg is not None and peg > 0:
        if peg <= 1.15:
            green_flags.append(f"Growth at a bargain price (PEG ratio {peg:.2f}x)")
        elif peg >= 2.4:
            red_flags.append(f"High price relative to growth (PEG ratio {peg:.2f}x)")

    if not is_financial and fcf_yield is not None:
        if fcf_yield >= 4.0:
            green_flags.append(f"Strong {fcf_yield:.1f}% Free Cash Flow yield")
        elif fcf_yield < 1.0 and val_zone == "expensive":
            red_flags.append(f"Thin {fcf_yield:.1f}% Free Cash Flow yield at current price")

    if net_margin is not None:
        if net_margin >= 22.0:
            green_flags.append(f"High-margin business ({net_margin:.1f}% net profit margin)")
        elif net_margin < 0:
            red_flags.append(f"Currently unprofitable ({net_margin:.1f}% net margin)")
        elif net_margin < 5.0:
            red_flags.append(f"Slim {net_margin:.1f}% net profit margin")

    if rev_growth is not None:
        if rev_growth >= 14.0:
            green_flags.append(f"Rapid top-line expansion (+{rev_growth:.1f}% YoY revenue growth)")
        elif rev_growth < 0:
            red_flags.append(f"Contracting revenue ({rev_growth:.1f}% YoY)")

    if rsi is not None and not is_falling_knife and not shock_active:
        if rsi <= 38.0 and verdict_key in ("strong_buy", "buy"):
            green_flags.append(f"Healthy oversold pullback (14d RSI {rsi:.0f}) — favorable entry")
        elif rsi >= 71.0:
            red_flags.append(f"Short-term overbought (14d RSI {rsi:.0f}) — risk of pullback")

    if not is_financial and net_cash_pct is not None and net_cash_pct >= 4.0:
        green_flags.append("Fortress net-cash balance sheet")
    elif not is_financial and debt_to_eq is not None and debt_to_eq >= 2.2:
        red_flags.append(f"Elevated leverage ({debt_to_eq:.2f}x Debt/Equity)")

    # Craft concise 1-sentence takeaway
    if shock_active or is_falling_knife:
        warn = red_flags[0]
        if verdict_key == "hold" and val_zone in ("cheap", "fair"):
            summary = f"{warn} — optical multiples look attractive, so watch for stabilization."
        elif len(red_flags) > 1:
            summary = f"{warn}; {red_flags[1][0].lower() + red_flags[1][1:]}."
        else:
            summary = f"{warn}."
    elif verdict_key in ("strong_buy", "buy"):
        lead = green_flags[0] if green_flags else "Solid balance of valuation and profitability"
        if len(green_flags) > 1:
            second = green_flags[1]
            summary = f"{lead}; {second[0].lower() + second[1:]}."
        else:
            summary = f"{lead} backed by healthy fundamentals."
    elif verdict_key == "hold":
        pro = green_flags[0] if green_flags else "Solid underlying business"
        con = red_flags[0] if red_flags else f"trading near fair value (~{cur_sym}{range_info['fairPrice']:,.0f})"
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

    # --- EXPLICIT DAILY DIP vs. FALLING-KNIFE SIGNAL ---
    is_sharp_dip = (
        (rsi is not None and rsi <= 38.0)
        or (rsi5 is not None and rsi5 <= 24.0 and dip_20d is not None and dip_20d <= -7.0)
    )
    is_mild_dip = (
        not is_sharp_dip
        and (
            (rsi is not None and rsi <= 44.0)
            or (dip_20d is not None and dip_20d <= -4.5)
            or (rsi5 is not None and rsi5 <= 34.0)
        )
    )
    is_overheated = (
        (rsi is not None and rsi >= 70.0)
        or (rsi5 is not None and rsi5 >= 82.0 and dist_50d is not None and dist_50d >= 12.0)
    )

    if shock_active or is_falling_knife:
        dip_key = "falling_knife"
        dip_label = shock_alert["badge"] if (shock_active and shock_alert) else "🚨 Falling Knife"
        if shock_active and shock_alert:
            dip_reason = (
                f"{shock_alert['reason']} (20d: {dip_20d or 0.0:.1f}%, vs 200d MA: {dist_200d or 0.0:.1f}%). "
                "Trailing multiples lag sudden leadership/probe shocks — wait for a stable price base before buying."
            )
        else:
            dip_reason = (
                f"Steep waterfall sell-off ({dip_20d or 0.0:.1f}% off 20d high, {dist_50d or 0.0:.1f}% below 50d MA). "
                "Avoid catching a falling knife until price reclaims short-term support."
            )
        dip_rank = 10.0
    elif is_sharp_dip and verdict_key in ("strong_buy", "buy"):
        dip_key = "prime_dip"
        dip_label = "⚡ Prime Dip"
        score = min(99, score + 3)
        if score >= 78 and val_score >= 60 and val_zone != "expensive":
            verdict = "Strong Buy"
            verdict_key = "strong_buy"
        dip_parts_txt = []
        if rsi is not None:
            dip_parts_txt.append(f"14d RSI {rsi:.0f}")
        if dip_20d is not None and dip_20d <= -4.0:
            dip_parts_txt.append(f"{dip_20d:.1f}% off 20d high")
        dip_reason = f"Clean buyable dip ({', '.join(dip_parts_txt)}) on an intact, reasonably valued business with no shock flags."
        dip_rank = round(clamp(80.0 + (40.0 - (rsi or 36.0)) * 0.6 + abs(min(0.0, dip_20d or 0.0)) * 0.5, 80.0, 99.0), 1)
    elif (is_sharp_dip or is_mild_dip) and score >= 52 and verdict_key != "avoid":
        dip_key = "pullback"
        dip_label = "↘ Pullback"
        dip_reason = (
            f"Controlled pullback (RSI {rsi:.0f} / {dip_20d or 0.0:.1f}% vs 20d high) with intact trend and no governance flags."
            if rsi is not None
            else "Moderate pullback from 20-day high."
        )
        dip_rank = round(clamp(62.0 + abs(min(0.0, dip_20d or 0.0)) * 1.1, 60.0, 79.0), 1)
    elif is_sharp_dip:
        dip_key = "falling_knife"
        dip_label = "⚠ Knife Risk"
        dip_reason = "Oversold short-term, but stretched valuation or weak fundamentals make this a value-trap / knife risk."
        dip_rank = 18.0
    elif is_overheated:
        dip_key = "overheated"
        dip_label = "🔥 Overheated"
        dip_reason = (
            f"Extended short-term rally (14d RSI {rsi:.0f}) — wait for a calmer entry."
            if rsi is not None
            else "Extended above short-term moving average."
        )
        dip_rank = 6.0
    else:
        dip_key = "neutral"
        dip_label = "Neutral"
        dip_reason = "Trading in a normal daily momentum range with no acute dip or shock."
        dip_rank = round(clamp(45.0 - ((rsi or 52.0) - 50.0) * 0.3, 35.0, 58.0), 1)

    out = {
        "score": score,
        "verdict": verdict,
        "verdictKey": verdict_key,
        "isFallingKnife": bool(shock_active or is_falling_knife),
        "dipKey": dip_key,
        "dipLabel": dip_label,
        "dipReason": dip_reason,
        "dipRank": dip_rank,
        "pillars": pillars,
        "greenFlags": green_flags[:4],
        "redFlags": red_flags[:4],
        "summary": summary,
    }
    out.update(range_info)
    return out


def fetch_stock(client: YahooClient, symbol: str) -> dict[str, Any]:
    raw_sym = symbol.strip().upper()
    sym = TICKER_ALIASES.get(raw_sym, raw_sym.replace(".", "-"))
    sym = TICKER_ALIASES.get(sym, sym)
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
    is_ant_pre_ipo = False
    if price is None and sym == "ANT":
        price = 78.00
        is_ant_pre_ipo = True
    elif price is None:
        raise ValueError(f"No live market price available for '{sym}'.")

    currency = p.get("currency") or sd.get("currency") or "USD"
    fin_currency = fd.get("financialCurrency") or currency
    fx = client.get_fx_rate(fin_currency, currency)

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
    rsi_5 = compute_rsi(closes, 5) if len(closes) >= 6 else None
    w20 = closes[-20:] if closes else []
    dip_20d_pct = ((price - max(w20)) / max(w20) * 100.0) if (w20 and max(w20) > 0) else 0.0
    ret_5d_pct = ((price - closes[-6]) / closes[-6] * 100.0) if (len(closes) >= 6 and closes[-6] > 0) else None

    sma50_series = compute_sma(closes, 50) if closes else []
    sma200_series = compute_sma(closes, 200) if closes else []

    ma_50 = (sma50_series[-1] if sma50_series and sma50_series[-1] is not None else _raw(sd, "fiftyDayAverage"))
    ma_200 = (sma200_series[-1] if sma200_series and sma200_series[-1] is not None else _raw(sd, "twoHundredDayAverage"))

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

    implied_eps_growth_pct: float | None = None
    if trailing_eps and forward_eps and trailing_eps > 0:
        implied_eps_growth_pct = ((forward_eps - trailing_eps) / trailing_eps) * 100.0

    if earn_growth_raw is not None and implied_eps_growth_pct is not None:
        eps_growth_pct = 0.5 * clamp(earn_growth_raw * 100.0, -80.0, 150.0) + 0.5 * clamp(implied_eps_growth_pct, -50.0, 120.0)
    elif earn_growth_raw is not None:
        eps_growth_pct = clamp(earn_growth_raw * 100.0, -80.0, 150.0)
    else:
        eps_growth_pct = implied_eps_growth_pct

    peg = _raw(ks, "pegRatio")
    if (peg is None or peg <= 0) and forward_pe and forward_pe > 0 and eps_growth_pct and eps_growth_pct > 2.0:
        peg = forward_pe / eps_growth_pct

    target_mean = _raw(fd, "targetMeanPrice")
    target_low = _raw(fd, "targetLowPrice")
    target_high = _raw(fd, "targetHighPrice")
    rec_mean_val = _raw(fd, "recommendationMean")
    analyst_cnt = int(_raw(fd, "numberOfAnalystOpinions") or 0)
    gross_m = (_raw(fd, "grossMargins") * 100.0) if _raw(fd, "grossMargins") is not None else None
    op_m = (_raw(fd, "operatingMargins") * 100.0) if _raw(fd, "operatingMargins") is not None else None
    net_m = (_raw(fd, "profitMargins") * 100.0) if _raw(fd, "profitMargins") is not None else None
    roe_val = (_raw(fd, "returnOnEquity") * 100.0) if _raw(fd, "returnOnEquity") is not None else None
    ps_val = _raw(sd, "priceToSalesTrailing12Months")

    if sym == "ANT":
        diluted_shares = 18_000_000_000.0
        market_cap = market_cap or (price * diluted_shares)
        total_rev = total_rev or 53_000_000_000.0
        ps_val = ps_val or (market_cap / 81_000_000_000.0)
        forward_pe = forward_pe or (market_cap / 25_000_000_000.0)
        peg = peg or (forward_pe / 47.0)
        rev_growth_pct = rev_growth_pct if rev_growth_pct is not None else 150.0
        eps_growth_pct = eps_growth_pct if eps_growth_pct is not None else 80.0
        gross_m = gross_m if gross_m is not None else 43.4
        op_m = op_m if op_m is not None else -3.8
        net_m = net_m if net_m is not None else -3.8
        roe_val = roe_val if roe_val is not None else -4.0
        fcf = fcf if fcf is not None else -24_000_000_000.0
        total_cash = total_cash if total_cash is not None else 95_000_000_000.0
        total_debt = total_debt if total_debt is not None else 8_000_000_000.0
        ev = ev or (market_cap - (total_cash - total_debt))
        target_mean = target_mean or 98.50
        target_low = target_low or 92.00
        target_high = target_high or 105.00
        rec_mean_val = rec_mean_val or 1.8
        analyst_cnt = analyst_cnt or 2
        rsi_14 = rsi_14 if rsi_14 is not None else 55.0

    if sym == "SPY":
        forward_pe = forward_pe or (trailing_pe * 0.89 if trailing_pe else 20.8)
        rev_growth_pct = rev_growth_pct if rev_growth_pct is not None else 6.8
        eps_growth_pct = eps_growth_pct if eps_growth_pct is not None else 11.5
        peg = peg or (forward_pe / eps_growth_pct)
        op_m = op_m if op_m is not None else 16.5
        net_m = net_m if net_m is not None else 12.8
        roe_val = roe_val if roe_val is not None else 18.5

    fcf_yield = (fcf / market_cap * 100.0) if (fcf is not None and market_cap and market_cap > 0) else None
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

    disp_name = p.get("shortName") or p.get("longName") or sym
    long_name = p.get("longName") or disp_name
    sec_name = sp.get("sector") or "Other"
    ind_name = sp.get("industry") or ""

    if sym == "ANT":
        disp_name = "Anthropic, PBC (IPO Est.)" if is_ant_pre_ipo else "Anthropic, PBC"
        long_name = "Anthropic, PBC — Class A (Form S-1/A-3)" if is_ant_pre_ipo else "Anthropic, PBC"
        sec_name = "Technology"
        ind_name = "AI Frontier Models & Cloud"
    elif sym == "SPCX":
        disp_name = "SpaceX (Space Exploration)"
        sec_name = sp.get("sector") or "Industrials"
        ind_name = sp.get("industry") or "Aerospace & Starlink"
    elif sym == "SPY":
        disp_name = "S&P 500 Index (SPY / SPX)"
        long_name = "S&P 500 Benchmark ETF (SPX / SPY)"
        sec_name = "Market Index"
        ind_name = "US Large-Cap Benchmark"

    shock_alert = fetch_news_shock(sym, disp_name, dip_20d_pct, dist_50d, drawdown_52w, change_pct)

    record: dict[str, Any] = {
        "symbol": sym,
        "name": disp_name,
        "longName": long_name,
        "sector": sec_name,
        "industry": ind_name,
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
        "priceToSales": r2(ps_val),
        "priceToBook": r2(_raw(ks, "priceToBook")),
        "evToEbitda": r2(_raw(ks, "enterpriseToEbitda")),
        "fcfYield": r2(fcf_yield),
        "earningsYield": r2(earnings_yield),
        "dividendYield": r2(div_yield),
        # Growth & Quality
        "revenueGrowth": r1(rev_growth_pct),
        "epsGrowth": r1(eps_growth_pct),
        "grossMargin": r1(gross_m),
        "operatingMargin": r1(op_m),
        "profitMargin": r1(net_m),
        "roe": r1(roe_val),
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
        "recommendationMean": r2(rec_mean_val),
        "recommendationKey": fd.get("recommendationKey") or "",
        "analystCount": analyst_cnt,
        # Technicals & Corporate Shock Check
        "low52w": r2(low_52w),
        "high52w": r2(high_52w),
        "rangePos52w": r1(range_pos_52w),
        "drawdownFrom52wHighPct": r1(drawdown_52w),
        "ma50": r2(ma_50),
        "ma200": r2(ma_200),
        "distFrom50dPct": r1(dist_50d),
        "distFrom200dPct": r1(dist_200d),
        "rsi14": r1(rsi_14),
        "rsi5": r1(rsi_5),
        "dip20dPct": r1(dip_20d_pct),
        "ret5dPct": r1(ret_5d_pct),
        "beta": r2(_raw(sd, "beta")),
        "shockAlert": shock_alert,
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
        raw = t.strip().upper()
        u = TICKER_ALIASES.get(raw, raw.replace(".", "-"))
        u = TICKER_ALIASES.get(u, u)
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
        "prime_dip": sum(1 for s in stocks_sorted if s.get("dipKey") == "prime_dip"),
        "cheap_zone": sum(1 for s in stocks_sorted if s.get("valuationZone") == "cheap"),
        "falling_knives": sum(1 for s in stocks_sorted if s.get("isFallingKnife")),
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

    with ThreadPoolExecutor(max_workers=6) as pool:
        future_map = {pool.submit(fetch_stock, client, sym): sym for sym in symbols}
        for fut in as_completed(future_map):
            sym = future_map[fut]
            try:
                results.append(fut.result())
            except Exception as exc:
                errors.append(f"{sym}: {exc}")

    if not results and STOCKS_JSON_FILE.exists():
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
        f"Hold: {c['hold']}, Avoid: {c['avoid']} | "
        f"Cheap Zone: {c['cheap_zone']}, Falling Knives: {c['falling_knives']}"
    )
    for st in data["stocks"][:8]:
        print(
            f"  [{st['score']:2d}] {st['verdict']:17s} {st['symbol']:6s} "
            f"${st['price']:<8} (Cheap<=${st['cheapPrice']:.0f} | Fair~${st['fairPrice']:.0f} | Exp>=${st['expensivePrice']:.0f}) "
            f"{st['dipLabel']} | {st['summary']}"
        )
    if data.get("warnings"):
        print("Warnings:", data["warnings"], file=sys.stderr)
