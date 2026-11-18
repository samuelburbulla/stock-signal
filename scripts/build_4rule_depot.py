#!/usr/bin/env python3
"""
Builds the historical + live-seeded 4-Rule Playbook Depot (`public/data/four_rule_depot.json`
and `public/data/four_rule_depot_snapshot.js`) over the survivorship-free Nov-2021 S&P 100
Mega-Cap cohort, starting exactly 4 years ago (Nov 2022 -> Today) as well as 5Y/3Y/2Y/1Y windows.

Exact 4-Rule Playbook simulated:
  1. Universe: S&P 100 (^OEX) Mega-Caps only (survivorship-free cohort, zero hindsight).
  2. Position Sizing: 6 equal fixed slots (16.67% each); any unfilled slot stays parked in SPY.
  3. Entry Trigger: Prime Dip (Score >= 64, Valuation >= 48, and 14d RSI <= 42 / 5d RSI <= 28 / 20d dip <= -6%),
     with a sector cap of max 3 slots per sector.
  4. Exit Trigger: Minimum 10 trading days hold, then sell at 14d RSI >= 72, 5d RSI >= 78, or Score < 50
     (0.10% trading fee deducted on all slot entries/exits).
"""

from __future__ import annotations

import datetime as dt
import json
import math
import statistics
import time
from pathlib import Path
from typing import Any

import run_backtest as rb

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "public" / "data"
DEPOT_JSON = DATA_DIR / "four_rule_depot.json"
DEPOT_JS = DATA_DIR / "four_rule_depot_snapshot.js"
STOCKS_JSON = DATA_DIR / "stocks.json"

MAX_SLOTS = 6
MAX_PER_SECTOR = 3
MIN_HOLD_DAYS = 10
TX_FEE = 0.0010

SP100_META: dict[str, tuple[str, str]] = {
    "AAPL": ("Apple Inc.", "Technology"),
    "ABBV": ("AbbVie Inc.", "Healthcare"),
    "ABT": ("Abbott Laboratories", "Healthcare"),
    "ACN": ("Accenture plc", "Technology"),
    "ADBE": ("Adobe Inc.", "Technology"),
    "AIG": ("American International Group", "Financial Services"),
    "AMGN": ("Amgen Inc.", "Healthcare"),
    "AMT": ("American Tower Corp.", "Real Estate"),
    "AMZN": ("Amazon.com, Inc.", "Consumer Cyclical"),
    "AVGO": ("Broadcom Inc.", "Technology"),
    "AXP": ("American Express Co.", "Financial Services"),
    "BA": ("Boeing Company", "Industrials"),
    "BAC": ("Bank of America Corp.", "Financial Services"),
    "BIIB": ("Biogen Inc.", "Healthcare"),
    "BKNG": ("Booking Holdings Inc.", "Consumer Cyclical"),
    "BLK": ("BlackRock, Inc.", "Financial Services"),
    "BMY": ("Bristol-Myers Squibb", "Healthcare"),
    "BNY": ("BNY Mellon Corp.", "Financial Services"),
    "C": ("Citigroup Inc.", "Financial Services"),
    "CAT": ("Caterpillar Inc.", "Industrials"),
    "CHTR": ("Charter Communications", "Communication Services"),
    "CL": ("Colgate-Palmolive Co.", "Consumer Defensive"),
    "CMCSA": ("Comcast Corporation", "Communication Services"),
    "COF": ("Capital One Financial", "Financial Services"),
    "COP": ("ConocoPhillips", "Energy"),
    "COST": ("Costco Wholesale Corp.", "Consumer Defensive"),
    "CRM": ("Salesforce, Inc.", "Technology"),
    "CSCO": ("Cisco Systems, Inc.", "Technology"),
    "CVS": ("CVS Health Corp.", "Healthcare"),
    "CVX": ("Chevron Corporation", "Energy"),
    "DD": ("DuPont de Nemours", "Basic Materials"),
    "DHR": ("Danaher Corporation", "Healthcare"),
    "DIS": ("Walt Disney Company", "Communication Services"),
    "DOW": ("Dow Inc.", "Basic Materials"),
    "DUK": ("Duke Energy Corp.", "Utilities"),
    "EMR": ("Emerson Electric Co.", "Industrials"),
    "EXC": ("Exelon Corporation", "Utilities"),
    "F": ("Ford Motor Company", "Consumer Cyclical"),
    "FDX": ("FedEx Corporation", "Industrials"),
    "GD": ("General Dynamics Corp.", "Industrials"),
    "GE": ("GE Aerospace", "Industrials"),
    "GILD": ("Gilead Sciences, Inc.", "Healthcare"),
    "GM": ("General Motors Co.", "Consumer Cyclical"),
    "GOOGL": ("Alphabet Inc.", "Communication Services"),
    "GS": ("Goldman Sachs Group", "Financial Services"),
    "HD": ("Home Depot, Inc.", "Consumer Cyclical"),
    "HON": ("Honeywell International", "Industrials"),
    "IBM": ("IBM Corporation", "Technology"),
    "INTC": ("Intel Corporation", "Technology"),
    "JNJ": ("Johnson & Johnson", "Healthcare"),
    "JPM": ("JPMorgan Chase & Co.", "Financial Services"),
    "KHC": ("Kraft Heinz Company", "Consumer Defensive"),
    "KO": ("Coca-Cola Company", "Consumer Defensive"),
    "LIN": ("Linde plc", "Basic Materials"),
    "LLY": ("Eli Lilly and Company", "Healthcare"),
    "LMT": ("Lockheed Martin Corp.", "Industrials"),
    "LOW": ("Lowe's Companies", "Consumer Cyclical"),
    "MA": ("Mastercard Inc.", "Financial Services"),
    "MCD": ("McDonald's Corporation", "Consumer Cyclical"),
    "MDLZ": ("Mondelez International", "Consumer Defensive"),
    "MDT": ("Medtronic plc", "Healthcare"),
    "MET": ("MetLife, Inc.", "Financial Services"),
    "META": ("Meta Platforms, Inc.", "Communication Services"),
    "MMM": ("3M Company", "Industrials"),
    "MO": ("Altria Group, Inc.", "Consumer Defensive"),
    "MRK": ("Merck & Co., Inc.", "Healthcare"),
    "MS": ("Morgan Stanley", "Financial Services"),
    "MSFT": ("Microsoft Corporation", "Technology"),
    "NEE": ("NextEra Energy, Inc.", "Utilities"),
    "NFLX": ("Netflix, Inc.", "Communication Services"),
    "NKE": ("NIKE, Inc.", "Consumer Cyclical"),
    "NVDA": ("NVIDIA Corporation", "Technology"),
    "ORCL": ("Oracle Corporation", "Technology"),
    "PEP": ("PepsiCo, Inc.", "Consumer Defensive"),
    "PFE": ("Pfizer Inc.", "Healthcare"),
    "PG": ("Procter & Gamble Co.", "Consumer Defensive"),
    "PM": ("Philip Morris International", "Consumer Defensive"),
    "PYPL": ("PayPal Holdings, Inc.", "Financial Services"),
    "QCOM": ("QUALCOMM Incorporated", "Technology"),
    "RTX": ("RTX Corporation", "Industrials"),
    "SBUX": ("Starbucks Corporation", "Consumer Cyclical"),
    "SO": ("Southern Company", "Utilities"),
    "SPG": ("Simon Property Group", "Real Estate"),
    "T": ("AT&T Inc.", "Communication Services"),
    "TGT": ("Target Corporation", "Consumer Defensive"),
    "TMO": ("Thermo Fisher Scientific", "Healthcare"),
    "TMUS": ("T-Mobile US, Inc.", "Communication Services"),
    "TSLA": ("Tesla, Inc.", "Consumer Cyclical"),
    "TXN": ("Texas Instruments Inc.", "Technology"),
    "UNH": ("UnitedHealth Group", "Healthcare"),
    "UNP": ("Union Pacific Corp.", "Industrials"),
    "UPS": ("United Parcel Service", "Industrials"),
    "USB": ("U.S. Bancorp", "Financial Services"),
    "V": ("Visa Inc.", "Financial Services"),
    "VZ": ("Verizon Communications", "Communication Services"),
    "WFC": ("Wells Fargo & Company", "Financial Services"),
    "WMT": ("Walmart Inc.", "Consumer Defensive"),
    "XOM": ("Exxon Mobil Corporation", "Energy"),
}


def simulate_window(
    dates: list[str],
    spy_adjs: list[float],
    daily_ev: list[dict[str, dict[str, Any]]],
    sectors_map: dict[str, str],
    names_map: dict[str, str],
) -> dict[str, Any]:
    nav_daily = [10000.0]
    spy_daily = [10000.0]
    holdings: dict[str, dict[str, Any]] = {}
    prev_w: dict[str, float] = {"__SPY__": 1.0}
    closed_trades: list[dict[str, Any]] = []
    daily_slot_counts: list[int] = []

    for t in range(len(dates) - 1):
        d_today = dates[t]
        today_ev = daily_ev[t]
        tom_ev = daily_ev[t + 1]

        # Rule 4: Sell Check (>= 10 trading days hold)
        to_exit: list[tuple[str, str]] = []
        for sym, h in holdings.items():
            ev_d = today_ev.get(sym)
            if not ev_d or sym not in tom_ev:
                to_exit.append((sym, "Quote series ended / corporate action"))
                continue
            held_d = t - h["t"]
            if held_d >= MIN_HOLD_DAYS:
                if ev_d["rsi14"] is not None and ev_d["rsi14"] >= 72.0:
                    to_exit.append((sym, f"14d RSI reached {ev_d['rsi14']:.1f} (>= 72 target)"))
                elif ev_d["rsi5"] is not None and ev_d["rsi5"] >= 78.0:
                    to_exit.append((sym, f"5d RSI surged to {ev_d['rsi5']:.1f} (>= 78 snap-back)"))
                elif ev_d["score"] < 50:
                    to_exit.append((sym, f"Buy Score fell to {ev_d['score']} (< 50 floor)"))
            elif ev_d["score"] < 42:
                to_exit.append((sym, f"Emergency score breakdown ({ev_d['score']}/99)"))

        for sym, reason in to_exit:
            h = holdings.pop(sym)
            ev_d = today_ev.get(sym)
            ret_pct = ((ev_d["adj"] / h["entry_adj"]) - 1.0) * 100.0 if (ev_d and h["entry_adj"] > 0) else 0.0
            closed_trades.append({
                "symbol": sym,
                "name": names_map.get(sym, sym),
                "sector": h["sector"],
                "entryDate": h["entry_date"],
                "exitDate": d_today,
                "entryPrice": round(h["entry_close"], 2),
                "exitPrice": round(ev_d["close"], 2) if ev_d else round(h["entry_close"], 2),
                "daysHeld": max(1, t - h["t"]),
                "returnPct": round(ret_pct, 2),
                "reason": reason,
            })

        # Rule 3: Buy S&P 100 Prime Dip (up to 6 slots, max 3 per sector)
        if len(holdings) < MAX_SLOTS:
            sec_counts: dict[str, int] = {}
            for h in holdings.values():
                sec_counts[h["sector"]] = sec_counts.get(h["sector"], 0) + 1

            cands: list[tuple[float, str]] = []
            for sym, ev_d in today_ev.items():
                if sym in holdings or sym not in tom_ev:
                    continue
                if ev_d["score"] >= 64 and ev_d["valScore"] >= 48:
                    is_dip = (
                        (ev_d["rsi14"] is not None and ev_d["rsi14"] <= 42.0)
                        or (ev_d["rsi5"] is not None and ev_d["rsi5"] <= 28.0)
                        or (ev_d["dip20"] <= -6.0)
                    )
                    if is_dip:
                        dip_rank = (
                            ev_d["score"]
                            + max(0.0, 45.0 - (ev_d["rsi14"] or 50.0)) * 0.6
                            + abs(min(0.0, ev_d["dip20"])) * 0.8
                        )
                        cands.append((dip_rank, sym))
            cands.sort(reverse=True)

            for _, sym in cands:
                if len(holdings) >= MAX_SLOTS:
                    break
                sec = sectors_map.get(sym, "Other")
                if sec_counts.get(sec, 0) >= MAX_PER_SECTOR:
                    continue
                ev_d = today_ev[sym]
                holdings[sym] = {
                    "t": t,
                    "entry_date": d_today,
                    "entry_adj": ev_d["adj"],
                    "entry_close": ev_d["close"],
                    "entry_rsi14": ev_d["rsi14"],
                    "entry_score": ev_d["score"],
                    "sector": sec,
                }
                sec_counts[sec] = sec_counts.get(sec, 0) + 1

        # Rule 2: Up to 6 Equal Positions across active S&P 100 dips; 100% parked in SPY when 0 qualify!
        valid_syms = [s for s in holdings if s in today_ev and s in tom_ev]
        daily_slot_counts.append(len(valid_syms))
        w: dict[str, float] = {}
        if valid_syms:
            for s in valid_syms:
                w[s] = 1.0 / len(valid_syms)
        else:
            w["__SPY__"] = 1.0

        turnover = 0.5 * sum(abs(w.get(k, 0.0) - prev_w.get(k, 0.0)) for k in set(w) | set(prev_w))
        prev_w = w

        spy_r = (spy_adjs[t + 1] / spy_adjs[t]) - 1.0
        r_day = 0.0
        for k, wk in w.items():
            if k == "__SPY__":
                r_day += wk * spy_r
            else:
                sr = (tom_ev[k]["adj"] / today_ev[k]["adj"]) - 1.0
                if -1.0 <= sr <= 1.5:
                    r_day += wk * sr
        r_day -= turnover * TX_FEE

        nav_daily.append(nav_daily[-1] * (1.0 + r_day))
        spy_daily.append(spy_daily[-1] * (1.0 + spy_r))

    # Build active open slots on the final day (`len(dates) - 1`)
    last_idx = len(dates) - 1
    last_ev = daily_ev[last_idx]
    live_prices_by_sym = {}
    if STOCKS_JSON.exists():
        try:
            for s in json.loads(STOCKS_JSON.read_text(encoding="utf-8")).get("stocks", []):
                live_prices_by_sym[s["symbol"]] = s
        except Exception:
            pass

    active_slots: list[dict[str, Any]] = []
    for sym, h in holdings.items():
        ev_last = last_ev.get(sym)
        live_st = live_prices_by_sym.get(sym, {})
        curr_close = live_st.get("price") or (ev_last["close"] if ev_last else h["entry_close"])
        curr_adj = ev_last["adj"] if ev_last else h["entry_adj"]
        pnl_pct = ((curr_adj / h["entry_adj"]) - 1.0) * 100.0 if h["entry_adj"] > 0 else 0.0
        active_slots.append({
            "symbol": sym,
            "name": live_st.get("name") or names_map.get(sym, sym),
            "sector": h["sector"],
            "currencySymbol": "$",
            "entryDate": h["entry_date"],
            "entryPrice": round(h["entry_close"], 2),
            "currentPrice": round(float(curr_close), 2),
            "pnlPct": round(pnl_pct, 2),
            "tradingDaysHeld": max(1, last_idx - h["t"] + 1),
            "entryRsi14": round(h["entry_rsi14"], 1) if h["entry_rsi14"] is not None else None,
            "currentRsi14": live_st.get("rsi14") if live_st.get("rsi14") is not None else (round(ev_last["rsi14"], 1) if ev_last and ev_last["rsi14"] is not None else None),
            "currentRsi5": live_st.get("rsi5") if live_st.get("rsi5") is not None else (round(ev_last["rsi5"], 1) if ev_last and ev_last["rsi5"] is not None else None),
            "currentScore": live_st.get("score") if live_st.get("score") is not None else (ev_last["score"] if ev_last else h["entry_score"]),
            "verdict": live_st.get("verdict") or (
                "Strong Buy" if (ev_last and ev_last.get("verdictKey") == "strong_buy") else "Good Buy"
            ),
            "forwardPE": live_st.get("forwardPE") if live_st.get("forwardPE") is not None else (ev_last["pe"] if ev_last else None),
        })

    # Compute summary performance metrics
    end_v = nav_daily[-1]
    spy_end = spy_daily[-1]
    days_span = (dt.date.fromisoformat(dates[-1]) - dt.date.fromisoformat(dates[0])).days
    years = max(0.25, days_span / 365.25)

    tot_ret = (end_v / 10000.0 - 1.0) * 100.0
    spy_ret = (spy_end / 10000.0 - 1.0) * 100.0
    cagr = ((end_v / 10000.0) ** (1.0 / years) - 1.0) * 100.0
    spy_cagr = ((spy_end / 10000.0) ** (1.0 / years) - 1.0) * 100.0

    d_rets = [nav_daily[i] / nav_daily[i - 1] - 1.0 for i in range(1, len(nav_daily))]
    vol = statistics.stdev(d_rets) * math.sqrt(252.0) * 100.0 if len(d_rets) > 2 else 0.0
    rf_d = 0.035 / 252.0
    sharpe = (
        ((statistics.mean(d_rets) - rf_d) / statistics.stdev(d_rets)) * math.sqrt(252.0)
        if len(d_rets) > 2 and statistics.stdev(d_rets) > 0
        else 0.0
    )

    pk, mdd = nav_daily[0], 0.0
    for v in nav_daily:
        pk = max(pk, v)
        mdd = min(mdd, (v / pk - 1.0) * 100.0)

    pk_s, spy_mdd = spy_daily[0], 0.0
    for v in spy_daily:
        pk_s = max(pk_s, v)
        spy_mdd = min(spy_mdd, (v / pk_s - 1.0) * 100.0)

    wins = sum(1 for tr in closed_trades if tr["returnPct"] > 0)
    win_rate = (wins / len(closed_trades) * 100.0) if closed_trades else 0.0
    avg_trade = statistics.mean(tr["returnPct"] for tr in closed_trades) if closed_trades else 0.0
    avg_hold = statistics.mean(tr["daysHeld"] for tr in closed_trades) if closed_trades else 0.0
    avg_slots = statistics.mean(daily_slot_counts) if daily_slot_counts else 0.0

    # Downsample equity curve to weekly (~210 points for 4Y) + exact start/end for crisp charting
    step = max(1, len(dates) // 160)
    idxs = list(range(0, len(dates), step))
    if idxs[-1] != len(dates) - 1:
        idxs.append(len(dates) - 1)
    curve = [
        {"d": dates[i], "depot": round(nav_daily[i], 2), "spy": round(spy_daily[i], 2)}
        for i in idxs
    ]

    # Yearly breakdown inside this window
    yearly = []
    all_years = sorted({d[:4] for d in dates[1:]})
    for yr in all_years:
        yr_idxs = [i for i, d in enumerate(dates) if d.startswith(yr)]
        if not yr_idxs:
            continue
        base_i = max(0, yr_idxs[0] - 1)
        end_i = yr_idxs[-1]
        if end_i <= base_i:
            continue
        yearly.append({
            "year": f"{yr} YTD" if yr == dates[-1][:4] else yr,
            "depotPct": round((nav_daily[end_i] / nav_daily[base_i] - 1.0) * 100.0, 1),
            "spyPct": round((spy_daily[end_i] / spy_daily[base_i] - 1.0) * 100.0, 1),
            "alphaPct": round(((nav_daily[end_i] / nav_daily[base_i]) - (spy_daily[end_i] / spy_daily[base_i])) * 100.0, 1),
        })

    return {
        "startDate": dates[0],
        "endDate": dates[-1],
        "years": round(years, 2),
        "finalValue10k": round(end_v, 2),
        "profit10k": round(end_v - 10000.0, 2),
        "totalReturnPct": round(tot_ret, 1),
        "cagrPct": round(cagr, 2),
        "maxDrawdownPct": round(mdd, 1),
        "volatilityPct": round(vol, 1),
        "sharpe": round(sharpe, 2),
        "spyFinal10k": round(spy_end, 2),
        "spyProfit10k": round(spy_end - 10000.0, 2),
        "spyReturnPct": round(spy_ret, 1),
        "spyCagrPct": round(spy_cagr, 2),
        "spyMaxDrawdownPct": round(spy_mdd, 1),
        "extraProfitVsSpy10k": round(end_v - spy_end, 2),
        "closedTradesCount": len(closed_trades),
        "winningTradesCount": wins,
        "winRatePct": round(win_rate, 1),
        "avgTradeReturnPct": round(avg_trade, 2),
        "avgDaysHeld": round(avg_hold, 1),
        "avgSlotsInvested": round(avg_slots, 2),
        "yearly": yearly,
        "curve": curve,
        "activeSlots": active_slots,
        "recentClosedTrades": list(reversed(closed_trades[-24:])),
    }


def main() -> None:
    t0 = time.time()
    u2021 = json.loads((ROOT_DIR / "scripts" / "universes_2021.json").read_text(encoding="utf-8"))
    sp100_list = u2021["sp100_2021"]

    print("Loading SPY daily total-return series...", flush=True)
    spy_chart = rb.yf_get_chart("SPY")
    spy_dates: list[str] = []
    spy_adjs: list[float] = []
    for ts, a in zip(spy_chart["timestamp"], spy_chart["indicators"]["adjclose"][0]["adjclose"]):
        if isinstance(a, (int, float)) and not math.isnan(a):
            spy_dates.append(dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).strftime("%Y-%m-%d"))
            spy_adjs.append(float(a))

    start_5y_idx = next(i for i, d in enumerate(spy_dates) if d >= "2021-11-17")
    dates_all = spy_dates[start_5y_idx:]
    spy_all = spy_adjs[start_5y_idx:]

    print(f"Loading 98 survivorship-free Nov-2021 S&P 100 stocks ({dates_all[0]} -> {dates_all[-1]})...", flush=True)
    stocks_db: dict[str, dict[str, Any]] = {}
    sectors_map: dict[str, str] = {}
    names_map: dict[str, str] = {}
    for it in sp100_list:
        sym = it["yfSym"]
        full_name, gics_sec = SP100_META.get(sym, (sym, "Other"))
        ciks = [it["cikCurr"], it["cik2021"]] + (["0000034088"] if sym == "XOM" else [])
        stocks_db[sym] = rb.load_stock_entry(sym, gics_sec, ciks)
        sectors_map[sym] = gics_sec
        names_map[sym] = full_name

    print(f"Precomputing daily PIT fundamental + RSI evaluations across {len(dates_all)} sessions...", flush=True)
    daily_ev_all: list[dict[str, dict[str, Any]]] = []
    for d_s in dates_all:
        row: dict[str, dict[str, Any]] = {}
        for sym, st in stocks_db.items():
            ev = rb.eval_stock_fast(st, d_s, exact_or_prev=False)
            if ev:
                row[sym] = ev
        daily_ev_all.append(row)

    windows_cfg = {
        "4y": "2022-11-17",
        "5y": "2021-11-17",
        "3y": "2023-11-17",
        "2y": "2024-11-17",
        "1y": "2025-11-17",
    }
    windows_out: dict[str, Any] = {}
    for wk, start_d in windows_cfg.items():
        sub_i = next(i for i, d in enumerate(dates_all) if d >= start_d)
        res = simulate_window(
            dates=dates_all[sub_i:],
            spy_adjs=spy_all[sub_i:],
            daily_ev=daily_ev_all[sub_i:],
            sectors_map=sectors_map,
            names_map=names_map,
        )
        windows_out[wk] = res
        print(
            f"  [{wk.upper()}] {res['startDate']} -> {res['endDate']} | "
            f"$10,000 -> ${res['finalValue10k']:,.2f} (Profit +${res['profit10k']:,.2f}, +{res['totalReturnPct']:.1f}%, CAGR +{res['cagrPct']:.2f}%/yr, MaxDD {res['maxDrawdownPct']:.1f}%, Sharpe {res['sharpe']:.2f}) | "
            f"vs SPY ${res['spyFinal10k']:,.2f} (+{res['spyReturnPct']:.1f}%) | "
            f"Trades={res['closedTradesCount']} WinRate={res['winRatePct']:.1f}% AvgTrade=+{res['avgTradeReturnPct']:.2f}%",
            flush=True,
        )

    w4 = windows_out["4y"]
    payload = {
        "generatedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "defaultWindow": "4y",
        "initialCapital": 10000,
        "rules": [
            {"num": 1, "title": "S&P 100 Mega-Caps Only", "desc": "Trade strictly dominant S&P 100 (^OEX) blue chips — zero mid-caps or speculative falling knives."},
            {"num": 2, "title": "6 Fixed 16.7% Slots (Idle in SPY)", "desc": "Split depot into 6 equal slots; any unfilled slot stays invested in SPY so cash never drags."},
            {"num": 3, "title": "Buy ⚡ Prime Dip (Max 3/Sector)", "desc": "Enter when Score >= 64, Val >= 48, and 14d RSI <= 42, 5d RSI <= 28, or 20d dip <= -6%."},
            {"num": 4, "title": "Hold >=10d · Sell RSI >=72 or Score <50", "desc": "Hold at least 10 trading days, then take profit on RSI >= 72 / 5d RSI >= 78 or exit if Score < 50."},
        ],
        "currentDepot": {
            "inceptionDate": w4["startDate"],
            "asOfDate": w4["endDate"],
            "nav10k": w4["finalValue10k"],
            "profit10k": w4["profit10k"],
            "spyNav10k": w4["spyFinal10k"],
            "extraVsSpy10k": w4["extraProfitVsSpy10k"],
            "activeSlots": w4["activeSlots"],
            "recentClosedTrades": w4["recentClosedTrades"],
        },
        "windows": windows_out,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, separators=(",", ":"))
    DEPOT_JSON.write_text(raw, encoding="utf-8")
    DEPOT_JS.write_text(f"window.__FOUR_RULE_DEPOT__ = {raw};\n", encoding="utf-8")
    print(f"Saved {DEPOT_JSON} in {time.time() - t0:.1f}s.", flush=True)


if __name__ == "__main__":
    main()
