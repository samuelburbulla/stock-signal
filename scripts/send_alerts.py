#!/usr/bin/env python3
"""
Stock Signal — Automated Buy & Exit Signal Email Engine
Monitors daily stock valuations & RSI dip indicators after `update_data.py` runs,
tracks the 6-Slot S&P 100 Mega-Cap Prime Dip depot state in `public/data/alert_state.json`,
and sends zero-spam email notifications ONLY when a genuine signal triggers:

  1. 🟢 BUY SIGNAL: An S&P 100 / $80B+ Mega-Cap newly enters `⚡ Prime Dip`
     (Score >= 64, Valuation >= 48, and 14d RSI <= 42 / 5d RSI <= 28 / 20d dip <= -6%).
  2. 🔴 SELL / ROTATE SIGNAL: An active 6-Slot position held >= 10 trading days hits
     `14d RSI >= 72`, `5d RSI >= 78`, or `Score < 50` (or emergency breakdown `Score < 42`).
  3. 🚀 STRONG BUY UPGRADE: A tracked stock newly crosses up into `Strong Buy`.

Supported delivery channels (zero third-party pip packages required):
  - Direct Rich HTML Email via SMTP (`ALERT_EMAIL_TO` + `SMTP_USER` + `SMTP_PASS`, auto-detects host/port)
  - Direct Rich HTML Email via Resend API (`ALERT_EMAIL_TO` + `RESEND_API_KEY`)
  - Zero-Config GitHub Email Relay (`GITHUB_TOKEN` + `GITHUB_REPOSITORY` -> posts to a single
    dedicated Signal Alert issue mentioning `@<owner>`, triggering instant GitHub email delivery)
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.message
import html
import json
import os
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "public" / "data"
STOCKS_JSON = DATA_DIR / "stocks.json"
ALERT_STATE_JSON = DATA_DIR / "alert_state.json"
ALERT_SNAPSHOT_JS = DATA_DIR / "alert_snapshot.js"

LIVE_SITE_URL = "https://samuelburbulla.github.io/stock-signal/"

# S&P 100 / Global Mega-Cap Blue-Chip Universe (strict filter for Dip-Buying)
SP100_MEGACAP_SET = {
    "AAPL", "ABBV", "ABT", "ACN", "ADBE", "ADI", "ADP", "AMD", "AMGN", "AMZN",
    "ASML", "AVGO", "AXP", "BA", "BAC", "BK", "BKNG", "BLK", "BMY", "BRK-B",
    "BSX", "CAT", "CB", "CHTR", "CI", "CL", "CMCSA", "COF", "COP", "COST",
    "CRM", "CSCO", "CVS", "CVX", "DE", "DHR", "DIS", "DOW", "DUK", "EMR",
    "FDX", "GD", "GE", "GILD", "GM", "GOOGL", "GS", "HD", "HON", "IBM",
    "INTC", "INTU", "ISRG", "JNJ", "JPM", "KHC", "KO", "LIN", "LLY", "LMT",
    "LOW", "MA", "MCD", "MDLZ", "MDT", "MET", "META", "MMM", "MO", "MRK",
    "MS", "MSFT", "MU", "NEE", "NFLX", "NKE", "NOW", "NVDA", "NVO", "ORCL",
    "PEP", "PFE", "PG", "PLTR", "PM", "PYPL", "QCOM", "RTX", "SAP", "SBUX",
    "SCHW", "SHW", "SO", "SPG", "SPGI", "SYK", "T", "TGT", "TJX", "TMO",
    "TMUS", "TSLA", "TSM", "TXN", "UBER", "UNH", "UNP", "UPS", "USB", "V",
    "VZ", "WFC", "WMT", "XOM",
}

EXCLUDED_FROM_SLOTS = {"SPY", "ANT", "SPCX"}
MAX_SLOTS = 6
MAX_PER_SECTOR = 3
MIN_HOLD_TRADING_DAYS = 10

SMTP_PROVIDERS: dict[str, tuple[str, int, str]] = {
    "gmail.com": ("smtp.gmail.com", 465, "ssl"),
    "googlemail.com": ("smtp.gmail.com", 465, "ssl"),
    "icloud.com": ("smtp.mail.me.com", 587, "starttls"),
    "me.com": ("smtp.mail.me.com", 587, "starttls"),
    "mac.com": ("smtp.mail.me.com", 587, "starttls"),
    "outlook.com": ("smtp-mail.outlook.com", 587, "starttls"),
    "hotmail.com": ("smtp-mail.outlook.com", 587, "starttls"),
    "live.com": ("smtp-mail.outlook.com", 587, "starttls"),
    "msn.com": ("smtp-mail.outlook.com", 587, "starttls"),
    "gmx.de": ("mail.gmx.net", 465, "ssl"),
    "gmx.net": ("mail.gmx.net", 465, "ssl"),
    "gmx.at": ("mail.gmx.net", 465, "ssl"),
    "gmx.ch": ("mail.gmx.net", 465, "ssl"),
    "gmx.com": ("mail.gmx.net", 465, "ssl"),
    "web.de": ("smtp.web.de", 587, "starttls"),
    "t-online.de": ("securesmtp.t-online.de", 465, "ssl"),
    "yahoo.com": ("smtp.mail.yahoo.com", 465, "ssl"),
    "yahoo.de": ("smtp.mail.yahoo.com", 465, "ssl"),
    "fastmail.com": ("smtp.fastmail.com", 465, "ssl"),
}


def is_megacap_eligible(st: dict[str, Any]) -> bool:
    sym = st.get("symbol", "")
    if sym in EXCLUDED_FROM_SLOTS:
        return False
    if sym in SP100_MEGACAP_SET:
        return True
    mcap = st.get("marketCap") or 0
    return float(mcap) >= 80_000_000_000.0


def is_strategy_dip_candidate(st: dict[str, Any]) -> bool:
    """Matches the exact 6-Slot S&P 100 Prime Dip entry criteria."""
    if not is_megacap_eligible(st):
        return False
    score = st.get("score") or 0
    val_score = (st.get("pillars", {}).get("valuation") or {}).get("score") or 0
    if score < 64 or val_score < 48:
        return False
    if st.get("dipKey") == "prime_dip":
        return True
    rsi14 = st.get("rsi14")
    rsi5 = st.get("rsi5")
    dip20 = st.get("dip20dPct")
    return bool(
        (rsi14 is not None and rsi14 <= 42.0)
        or (rsi5 is not None and rsi5 <= 28.0)
        or (dip20 is not None and dip20 <= -6.0)
    )


def fmt_price(v: float | None, cur: str = "$") -> str:
    if v is None:
        return "–"
    return f"{cur}{v:,.2f}"


def fmt_pct(v: float | None, signed: bool = True) -> str:
    if v is None:
        return "–"
    sign = "+" if (signed and v > 0) else ""
    return f"{sign}{v:.1f}%"


DEPOT_JSON = DATA_DIR / "four_rule_depot.json"
DEPOT_SNAPSHOT_JS = DATA_DIR / "four_rule_depot_snapshot.js"


def load_previous_state() -> dict[str, Any]:
    if ALERT_STATE_JSON.exists():
        try:
            return json.loads(ALERT_STATE_JSON.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def sync_live_depot_file(active_slots: list[dict[str, Any]], exit_signals: list[dict[str, Any]], trading_date: str, stocks_by_sym: dict[str, Any], prev_signals: dict[str, Any]) -> None:
    """Keeps public/data/four_rule_depot.json marked-to-market on every daily GitHub Action run."""
    if not DEPOT_JSON.exists():
        return
    try:
        depot = json.loads(DEPOT_JSON.read_text(encoding="utf-8"))
        cd = depot.get("currentDepot") or {}
        last_d = cd.get("asOfDate")
        if last_d and trading_date > last_d and prev_signals:
            # Mark-to-market active slots + SPY from previous close
            prev_spy_p = (prev_signals.get("SPY") or {}).get("price")
            curr_spy_p = (stocks_by_sym.get("SPY") or {}).get("price")
            spy_r = ((curr_spy_p / prev_spy_p) - 1.0) if (prev_spy_p and curr_spy_p and prev_spy_p > 0) else 0.0
            old_slots = cd.get("activeSlots") or []
            if old_slots:
                rets = []
                for sl in old_slots:
                    sym = sl["symbol"]
                    p0 = (prev_signals.get(sym) or {}).get("price") or sl.get("currentPrice")
                    p1 = (stocks_by_sym.get(sym) or {}).get("price") or p0
                    if p0 and p1 and p0 > 0:
                        rets.append((p1 / p0) - 1.0)
                port_r = (sum(rets) / len(rets)) if rets else spy_r
            else:
                port_r = spy_r

            cd["nav10k"] = round(float(cd.get("nav10k", 23263.49)) * (1.0 + port_r), 2)
            cd["profit10k"] = round(cd["nav10k"] - 10000.0, 2)
            cd["spyNav10k"] = round(float(cd.get("spyNav10k", 19567.56)) * (1.0 + spy_r), 2)
            cd["extraVsSpy10k"] = round(cd["nav10k"] - cd["spyNav10k"], 2)
            cd["asOfDate"] = trading_date

            w4 = (depot.get("windows") or {}).get("4y")
            if w4:
                w4["endDate"] = trading_date
                w4["finalValue10k"] = cd["nav10k"]
                w4["profit10k"] = cd["profit10k"]
                w4["totalReturnPct"] = round((cd["nav10k"] / 10000.0 - 1.0) * 100.0, 1)
                w4["spyFinal10k"] = cd["spyNav10k"]
                w4["spyProfit10k"] = round(cd["spyNav10k"] - 10000.0, 2)
                w4["spyReturnPct"] = round((cd["spyNav10k"] / 10000.0 - 1.0) * 100.0, 1)
                w4["extraProfitVsSpy10k"] = cd["extraVsSpy10k"]
                if w4.get("curve"):
                    w4["curve"].append({"d": trading_date, "depot": cd["nav10k"], "spy": cd["spyNav10k"]})

        cd["activeSlots"] = active_slots
        if exit_signals:
            closed = list(cd.get("recentClosedTrades") or [])
            for ex in exit_signals:
                closed.insert(0, {
                    "symbol": ex["symbol"],
                    "name": ex.get("name", ex["symbol"]),
                    "sector": ex.get("sector", ""),
                    "entryDate": ex.get("entryDate", ""),
                    "exitDate": trading_date,
                    "entryPrice": ex["entryPrice"],
                    "exitPrice": ex["price"],
                    "daysHeld": ex["daysHeld"],
                    "returnPct": ex["pnlPct"],
                    "reason": ex["reason"],
                })
            cd["recentClosedTrades"] = closed[:24]
        depot["currentDepot"] = cd
        depot["generatedAt"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw_d = json.dumps(depot, separators=(",", ":"))
        DEPOT_JSON.write_text(raw_d, encoding="utf-8")
        DEPOT_SNAPSHOT_JS.write_text(f"window.__FOUR_RULE_DEPOT__ = {raw_d};\n", encoding="utf-8")
    except Exception as exc:
        print(f"[alerts] Warning syncing four_rule_depot.json: {exc}")


def evaluate_signals(stocks_payload: dict[str, Any], prev_state: dict[str, Any], init_only: bool = False) -> dict[str, Any]:
    stocks_list: list[dict[str, Any]] = stocks_payload.get("stocks", [])
    by_sym = {s["symbol"]: s for s in stocks_list}

    now_utc = dt.datetime.now(dt.timezone.utc)
    now_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    spy_chart = (by_sym.get("SPY") or (stocks_list[0] if stocks_list else {})).get("chart") or []
    trading_date = spy_chart[-1]["d"] if spy_chart else now_utc.strftime("%Y-%m-%d")

    prev_trading_date = prev_state.get("lastTradingDate")
    is_new_trading_day = bool(prev_trading_date and trading_date > prev_trading_date)
    prev_signals: dict[str, dict[str, Any]] = prev_state.get("prevSignals") or {}
    is_first_seed = init_only or not prev_signals

    seeded_slots = prev_state.get("activeSlots")
    if is_first_seed and DEPOT_JSON.exists():
        try:
            depot_seed = json.loads(DEPOT_JSON.read_text(encoding="utf-8"))
            seeded_slots = (depot_seed.get("currentDepot") or {}).get("activeSlots") or seeded_slots
        except Exception:
            pass

    active_slots: list[dict[str, Any]] = [dict(x) for x in (seeded_slots or [])]
    history: list[dict[str, Any]] = list(prev_state.get("alertHistory") or [])

    exit_signals: list[dict[str, Any]] = []
    new_dip_signals: list[dict[str, Any]] = []
    slot_entries: list[dict[str, Any]] = []
    watchlist_exits: list[dict[str, Any]] = []
    new_strong_buys: list[dict[str, Any]] = []

    # 1. Update & Check Exit Rule on Active 6-Slot Positions
    kept_slots: list[dict[str, Any]] = []
    for slot in active_slots:
        sym = slot["symbol"]
        st = by_sym.get(sym)
        if not st:
            kept_slots.append(slot)
            continue

        days_held = int(slot.get("tradingDaysHeld", 1)) + (1 if is_new_trading_day else 0)
        curr_price = float(st.get("price") or slot["entryPrice"])
        entry_price = float(slot["entryPrice"])
        pnl_pct = ((curr_price / entry_price) - 1.0) * 100.0 if entry_price > 0 else 0.0
        rsi14 = st.get("rsi14")
        rsi5 = st.get("rsi5")
        score = int(st.get("score") or 50)

        slot.update({
            "name": st.get("name", sym),
            "currencySymbol": st.get("currencySymbol", "$"),
            "currentPrice": round(curr_price, 2),
            "pnlPct": round(pnl_pct, 2),
            "tradingDaysHeld": days_held,
            "currentRsi14": rsi14,
            "currentRsi5": rsi5,
            "currentScore": score,
            "verdict": st.get("verdict", ""),
            "forwardPE": st.get("forwardPE"),
        })

        exit_reason = None
        if days_held >= MIN_HOLD_TRADING_DAYS:
            if rsi14 is not None and rsi14 >= 72.0:
                exit_reason = f"14d RSI reached {rsi14:.1f} (>= 72 overbought target after {days_held}d hold)"
            elif rsi5 is not None and rsi5 >= 78.0:
                exit_reason = f"5d RSI surged to {rsi5:.1f} (>= 78 snap-back target after {days_held}d hold)"
            elif score < 50:
                exit_reason = f"Buy Score dropped to {score}/99 (< 50 fundamental floor)"
        elif score < 42:
            exit_reason = f"Emergency fundamental breakdown: Score dropped to {score}/99"

        if exit_reason and not is_first_seed:
            ev = {
                "date": trading_date,
                "timestamp": now_iso,
                "type": "SELL_EXIT",
                "symbol": sym,
                "name": st.get("name", sym),
                "sector": st.get("sector", ""),
                "currencySymbol": st.get("currencySymbol", "$"),
                "entryDate": slot.get("entryDate"),
                "entryPrice": round(entry_price, 2),
                "price": round(curr_price, 2),
                "pnlPct": round(pnl_pct, 1),
                "daysHeld": days_held,
                "score": score,
                "rsi14": rsi14,
                "reason": exit_reason,
            }
            exit_signals.append(ev)
            history.insert(0, ev)
        else:
            kept_slots.append(slot)

    active_slots = kept_slots

    # 2. Detect Newly Triggered Signals Across All Tracked Stocks
    dip_candidates: list[dict[str, Any]] = []
    for st in stocks_list:
        sym = st["symbol"]
        if sym in EXCLUDED_FROM_SLOTS:
            continue
        prev = prev_signals.get(sym) or {}
        curr_dip = st.get("dipKey")
        prev_dip = prev.get("dipKey")
        curr_verdict = st.get("verdictKey")
        prev_verdict = prev.get("verdictKey")
        rsi14 = st.get("rsi14")
        rsi5 = st.get("rsi5")
        prev_rsi14 = prev.get("rsi14")
        score = int(st.get("score") or 0)
        prev_score = int(prev.get("score") or score)

        if is_strategy_dip_candidate(st):
            dip_candidates.append(st)
            # Newly entered Prime Dip!
            if not is_first_seed and curr_dip == "prime_dip" and prev_dip != "prime_dip":
                new_dip_signals.append({
                    "date": trading_date,
                    "timestamp": now_iso,
                    "type": "BUY_DIP",
                    "symbol": sym,
                    "name": st.get("name", sym),
                    "sector": st.get("sector", ""),
                    "currencySymbol": st.get("currencySymbol", "$"),
                    "price": st.get("price"),
                    "score": score,
                    "verdict": st.get("verdict"),
                    "forwardPE": st.get("forwardPE"),
                    "rsi14": rsi14,
                    "rsi5": rsi5,
                    "dip20dPct": st.get("dip20dPct"),
                    "dipRank": st.get("dipRank") or score,
                    "reason": st.get("dipReason") or "Entered Mega-Cap Prime Dip zone",
                })

        # Secondary exit check for non-slot watchlist stocks
        if not is_first_seed and sym not in {s["symbol"] for s in active_slots} and sym not in {e["symbol"] for e in exit_signals}:
            crossed_rsi = rsi14 is not None and rsi14 >= 72.0 and (prev_rsi14 is None or prev_rsi14 < 72.0)
            crossed_score = score < 50 and prev_score >= 50
            if crossed_rsi or crossed_score:
                why = f"14d RSI hit {rsi14:.1f} (>= 72)" if crossed_rsi else f"Score fell to {score}/99 (< 50)"
                watchlist_exits.append({
                    "symbol": sym,
                    "name": st.get("name", sym),
                    "price": st.get("price"),
                    "currencySymbol": st.get("currencySymbol", "$"),
                    "score": score,
                    "rsi14": rsi14,
                    "reason": why,
                })

        if not is_first_seed and curr_verdict == "strong_buy" and prev_verdict and prev_verdict != "strong_buy":
            new_strong_buys.append({
                "symbol": sym,
                "name": st.get("name", sym),
                "price": st.get("price"),
                "currencySymbol": st.get("currencySymbol", "$"),
                "score": score,
                "forwardPE": st.get("forwardPE"),
                "rsi14": rsi14,
            })

    new_dip_signals.sort(key=lambda x: (x.get("dipRank") or 0, x.get("score") or 0), reverse=True)

    # 3. Fill Open Slots in the 6-Slot Model Depot (Max 3 per sector, ranked by dipRank + score)
    dip_candidates.sort(
        key=lambda s: ((1 if s.get("dipKey") == "prime_dip" else 0), s.get("dipRank") or 0, s.get("score") or 0),
        reverse=True,
    )
    held_syms = {s["symbol"] for s in active_slots}
    sector_counts: dict[str, int] = {}
    for s in active_slots:
        sec = s.get("sector", "Other")
        sector_counts[sec] = sector_counts.get(sec, 0) + 1

    for cand in dip_candidates:
        if len(active_slots) >= MAX_SLOTS:
            break
        sym = cand["symbol"]
        if sym in held_syms:
            continue
        sec = cand.get("sector") or "Other"
        if sector_counts.get(sec, 0) >= MAX_PER_SECTOR:
            continue

        new_slot = {
            "symbol": sym,
            "name": cand.get("name", sym),
            "sector": sec,
            "currencySymbol": cand.get("currencySymbol", "$"),
            "entryDate": trading_date,
            "entryPrice": round(float(cand["price"]), 2),
            "currentPrice": round(float(cand["price"]), 2),
            "pnlPct": 0.0,
            "tradingDaysHeld": 1,
            "entryRsi14": cand.get("rsi14"),
            "currentRsi14": cand.get("rsi14"),
            "currentRsi5": cand.get("rsi5"),
            "currentScore": cand.get("score"),
            "verdict": cand.get("verdict"),
            "forwardPE": cand.get("forwardPE"),
        }
        active_slots.append(new_slot)
        held_syms.add(sym)
        sector_counts[sec] = sector_counts.get(sec, 0) + 1

        entry_ev = {
            "date": trading_date,
            "timestamp": now_iso,
            "type": "SLOT_BUY",
            "symbol": sym,
            "name": cand.get("name", sym),
            "sector": sec,
            "currencySymbol": cand.get("currencySymbol", "$"),
            "price": round(float(cand["price"]), 2),
            "score": cand.get("score"),
            "verdict": cand.get("verdict"),
            "forwardPE": cand.get("forwardPE"),
            "rsi14": cand.get("rsi14"),
            "dip20dPct": cand.get("dip20dPct"),
            "reason": cand.get("dipReason") or "Added to 6-Slot Mega-Cap Prime Dip Depot",
        }
        if not is_first_seed:
            slot_entries.append(entry_ev)
        history.insert(0, entry_ev)

    # Record history for non-slotted new dip signals as well
    slotted_now = {e["symbol"] for e in slot_entries}
    for nd in new_dip_signals:
        if nd["symbol"] not in slotted_now:
            history.insert(0, nd)

    # Snapshot current state for next comparison
    next_prev_signals = {
        s["symbol"]: {
            "dipKey": s.get("dipKey"),
            "verdictKey": s.get("verdictKey"),
            "score": s.get("score"),
            "rsi14": s.get("rsi14"),
            "rsi5": s.get("rsi5"),
            "price": s.get("price"),
        }
        for s in stocks_list
    }

    should_alert = bool(exit_signals or new_dip_signals or slot_entries)
    if not is_first_seed:
        sync_live_depot_file(active_slots, exit_signals, trading_date, by_sym, prev_signals)

    subject, html_body, md_body = build_alert_messages(
        trading_date=trading_date,
        exit_signals=exit_signals,
        new_dip_signals=new_dip_signals,
        slot_entries=slot_entries,
        watchlist_exits=watchlist_exits,
        new_strong_buys=new_strong_buys,
        active_slots=active_slots,
        is_digest=(not should_alert),
    )

    return {
        "updatedAt": now_iso,
        "lastTradingDate": trading_date,
        "shouldAlert": should_alert,
        "summaryCounts": {
            "newDipBuys": len(new_dip_signals),
            "slotEntries": len(slot_entries),
            "slotExits": len(exit_signals),
            "watchlistExits": len(watchlist_exits),
            "newStrongBuys": len(new_strong_buys),
            "activeSlotsFilled": len(active_slots),
            "maxSlots": MAX_SLOTS,
        },
        "activeSlots": active_slots,
        "latestSignals": {
            "exitSignals": exit_signals,
            "newDipSignals": new_dip_signals,
            "slotEntries": slot_entries,
            "watchlistExits": watchlist_exits,
            "newStrongBuys": new_strong_buys,
        },
        "alertHistory": history[:30],
        "latestEmailSubject": subject,
        "latestEmailPreviewHtml": html_body,
        "latestMarkdownBody": md_body,
        "prevSignals": next_prev_signals,
    }


def build_alert_messages(
    trading_date: str,
    exit_signals: list[dict[str, Any]],
    new_dip_signals: list[dict[str, Any]],
    slot_entries: list[dict[str, Any]],
    watchlist_exits: list[dict[str, Any]],
    new_strong_buys: list[dict[str, Any]],
    active_slots: list[dict[str, Any]],
    is_digest: bool,
) -> tuple[str, str, str]:
    # Merge unique buy triggers (prioritizing slot_entries then new_dip_signals)
    buy_items: list[dict[str, Any]] = []
    seen_b: set[str] = set()
    for x in slot_entries + new_dip_signals:
        if x["symbol"] not in seen_b:
            seen_b.add(x["symbol"])
            buy_items.append(x)

    # Subject line
    parts = []
    if buy_items:
        syms = ", ".join(x["symbol"] for x in buy_items[:4])
        parts.append(f"⚡ BUY Prime Dip: {syms}")
    if exit_signals:
        syms = ", ".join(x["symbol"] for x in exit_signals[:4])
        parts.append(f"🔴 SELL / Rotate: {syms}")
    if not parts:
        active_syms = ", ".join(s["symbol"] for s in active_slots) or "SPY Parked"
        subject = f"[Stock Signal] 6-Slot Depot Status ({trading_date}): {active_syms}"
    else:
        subject = f"[Stock Signal] {' · '.join(parts)} ({trading_date})"

    # --- Markdown Body (for GitHub Issue Email Relay & Plaintext) ---
    md_lines = [
        f"### 🔔 Stock Signal Alert — `{trading_date}`",
        f"Strategy: **6-Slot S&P 100 Mega-Cap `⚡ Prime Dip` Rotation** ([Open Live Dashboard]({LIVE_SITE_URL}#alerts))",
        "",
    ]
    if is_digest:
        md_lines.append("> **Live Status Snapshot / Test Alert**: No new slot transitions occurred in this check; below is your current 6-slot depot status.")
        md_lines.append("")

    if buy_items:
        md_lines.append("#### 🟢 New Mega-Cap `⚡ Prime Dip` Buy Signals")
        md_lines.append("| Ticker | Price | Buy Score | Fwd P/E | 14d RSI | 20d Dip | Action |")
        md_lines.append("| :--- | ---: | :---: | ---: | ---: | ---: | :--- |")
        slotted_set = {s["symbol"] for s in slot_entries}
        for b in buy_items:
            cur = b.get("currencySymbol", "$")
            pe_s = f"{b['forwardPE']:.1f}x" if b.get("forwardPE") else "–"
            rsi_s = f"{b['rsi14']:.1f}" if b.get("rsi14") is not None else "–"
            dip_s = fmt_pct(b.get("dip20dPct"))
            act = "✅ **Open 16.7% Slot**" if b["symbol"] in slotted_set else "⚡ Eligible Dip Candidate"
            md_lines.append(f"| **{b['symbol']}** ({b['name'][:18]}) | `{fmt_price(b.get('price'), cur)}` | **{b.get('score')}/99** | `{pe_s}` | `{rsi_s}` | `{dip_s}` | {act} |")
        md_lines.append("")

    if exit_signals:
        md_lines.append("#### 🔴 6-Slot Sell / Rotation Signals (`>= 10d` Hold Rule Reached)")
        md_lines.append("| Ticker | Entry → Exit | Return | Days Held | Trigger Reason |")
        md_lines.append("| :--- | ---: | ---: | :---: | :--- |")
        for ex in exit_signals:
            cur = ex.get("currencySymbol", "$")
            md_lines.append(
                f"| **{ex['symbol']}** | `{fmt_price(ex['entryPrice'], cur)} → {fmt_price(ex['price'], cur)}` | **{fmt_pct(ex['pnlPct'])}** | `{ex['daysHeld']}d` | {ex['reason']} |"
            )
        md_lines.append("")

    md_lines.append(f"#### 📊 Current 6-Slot Mega-Cap Depot ({len(active_slots)}/{MAX_SLOTS} Slots Active)")
    md_lines.append("| Slot | Ticker | Entry Price | Current | P&L | 14d RSI (Sell >=72) | Score (Sell <50) |")
    md_lines.append("| :---: | :--- | ---: | ---: | ---: | :---: | :---: |")
    for idx, sl in enumerate(active_slots, 1):
        cur = sl.get("currencySymbol", "$")
        rsi_s = f"{sl['currentRsi14']:.1f}" if sl.get("currentRsi14") is not None else "–"
        md_lines.append(
            f"| #{idx} | **{sl['symbol']}** ({sl['sector'][:12]}) | `{fmt_price(sl['entryPrice'], cur)}` | `{fmt_price(sl['currentPrice'], cur)}` | `{fmt_pct(sl['pnlPct'])}` | `{rsi_s}` | `{sl.get('currentScore')}/99` |"
        )
    for idle_i in range(len(active_slots) + 1, MAX_SLOTS + 1):
        md_lines.append(f"| #{idle_i} | **SPY** (Idle Cash Parked) | – | – | – | – | Parked until next dip |")

    if watchlist_exits:
        md_lines.append("")
        md_lines.append("**Other Watchlist Overbought / Downgrade Alerts:** " + ", ".join(
            f"`{w['symbol']}` ({w['reason']})" for w in watchlist_exits[:6]
        ))
    if new_strong_buys:
        md_lines.append("")
        md_lines.append("**New Strong Buy Upgrades:** " + ", ".join(
            f"`{u['symbol']}` (Score {u['score']})" for u in new_strong_buys[:6]
        ))

    md_body = "\n".join(md_lines)

    # --- Rich HTML Email Body ---
    def esc(val: Any) -> str:
        return html.escape(str(val if val is not None else ""))

    buy_rows_html = ""
    if buy_items:
        slotted_set = {s["symbol"] for s in slot_entries}
        rows = []
        for b in buy_items:
            cur = b.get("currencySymbol", "$")
            pe_s = f"{b['forwardPE']:.1f}x" if b.get("forwardPE") else "–"
            rsi_s = f"{b['rsi14']:.1f}" if b.get("rsi14") is not None else "–"
            dip_s = fmt_pct(b.get("dip20dPct"))
            badge = "OPEN 16.7% SLOT" if b["symbol"] in slotted_set else "PRIME DIP"
            rows.append(f"""
              <tr>
                <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;">
                  <strong style="font-family:monospace;font-size:14px;color:#111827;">{esc(b['symbol'])}</strong>
                  <div style="font-size:12px;color:#6b7280;">{esc(b.get('name',''))}</div>
                </td>
                <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:right;font-family:monospace;font-weight:600;">{esc(fmt_price(b.get('price'), cur))}</td>
                <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:center;">
                  <span style="background:#dcfce7;color:#166534;padding:2px 8px;border-radius:999px;font-weight:700;font-size:12px;">{esc(b.get('score'))}/99</span>
                </td>
                <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:right;font-family:monospace;">{esc(pe_s)}</td>
                <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:right;font-family:monospace;color:#6d28d9;font-weight:700;">RSI {esc(rsi_s)} ({esc(dip_s)})</td>
                <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:right;">
                  <span style="background:#ede9fe;color:#5b21b6;padding:3px 8px;border-radius:6px;font-size:11px;font-weight:700;">⚡ {badge}</span>
                </td>
              </tr>""")
        buy_rows_html = f"""
        <div style="margin-bottom:20px;">
          <h3 style="margin:0 0 8px;font-size:15px;color:#166534;">🟢 New S&amp;P 100 Mega-Cap Prime Dip Buy Signals</h3>
          <table style="width:100%;border-collapse:collapse;font-size:13px;background:#ffffff;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">
            <thead>
              <tr style="background:#f9fafb;color:#4b5563;font-size:11px;text-transform:uppercase;">
                <th style="padding:8px 12px;text-align:left;">Stock</th>
                <th style="padding:8px 12px;text-align:right;">Price</th>
                <th style="padding:8px 12px;text-align:center;">Score</th>
                <th style="padding:8px 12px;text-align:right;">Fwd P/E</th>
                <th style="padding:8px 12px;text-align:right;">Dip / RSI</th>
                <th style="padding:8px 12px;text-align:right;">Signal</th>
              </tr>
            </thead>
            <tbody>{''.join(rows)}</tbody>
          </table>
        </div>"""

    exit_rows_html = ""
    if exit_signals:
        rows = []
        for ex in exit_signals:
            cur = ex.get("currencySymbol", "$")
            col = "#166534" if ex["pnlPct"] >= 0 else "#b91c1c"
            rows.append(f"""
              <tr>
                <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;"><strong style="font-family:monospace;">{esc(ex['symbol'])}</strong> ({ex['daysHeld']}d held)</td>
                <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:right;font-family:monospace;">{esc(fmt_price(ex['entryPrice'], cur))} → {esc(fmt_price(ex['price'], cur))}</td>
                <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:right;font-family:monospace;font-weight:700;color:{col};">{esc(fmt_pct(ex['pnlPct']))}</td>
                <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;font-size:12px;color:#374151;">{esc(ex['reason'])}</td>
              </tr>""")
        exit_rows_html = f"""
        <div style="margin-bottom:20px;">
          <h3 style="margin:0 0 8px;font-size:15px;color:#b91c1c;">🔴 Sell / Rotation Signals Reached</h3>
          <table style="width:100%;border-collapse:collapse;font-size:13px;background:#ffffff;border:1px solid #fecaca;border-radius:8px;overflow:hidden;">
            <thead>
              <tr style="background:#fef2f2;color:#7f1d1d;font-size:11px;text-transform:uppercase;">
                <th style="padding:8px 12px;text-align:left;">Position</th>
                <th style="padding:8px 12px;text-align:right;">Entry → Now</th>
                <th style="padding:8px 12px;text-align:right;">Return</th>
                <th style="padding:8px 12px;text-align:left;">Exit Rule Triggered</th>
              </tr>
            </thead>
            <tbody>{''.join(rows)}</tbody>
          </table>
        </div>"""

    slot_rows = []
    for idx, sl in enumerate(active_slots, 1):
        cur = sl.get("currencySymbol", "$")
        rsi_v = sl.get("currentRsi14")
        rsi_s = f"{rsi_v:.1f}" if rsi_v is not None else "–"
        pnl_col = "#15803d" if (sl.get("pnlPct") or 0) >= 0 else "#b91c1c"
        slot_rows.append(f"""
          <tr>
            <td style="padding:8px 10px;border-bottom:1px solid #e5e7eb;font-family:monospace;color:#6b7280;">#{idx}</td>
            <td style="padding:8px 10px;border-bottom:1px solid #e5e7eb;"><strong style="font-family:monospace;">{esc(sl['symbol'])}</strong> <span style="font-size:11px;color:#6b7280;">({esc(sl.get('sector',''))})</span></td>
            <td style="padding:8px 10px;border-bottom:1px solid #e5e7eb;text-align:right;font-family:monospace;">{esc(fmt_price(sl['entryPrice'], cur))}</td>
            <td style="padding:8px 10px;border-bottom:1px solid #e5e7eb;text-align:right;font-family:monospace;font-weight:600;">{esc(fmt_price(sl['currentPrice'], cur))}</td>
            <td style="padding:8px 10px;border-bottom:1px solid #e5e7eb;text-align:right;font-family:monospace;font-weight:600;color:{pnl_col};">{esc(fmt_pct(sl.get('pnlPct')))}</td>
            <td style="padding:8px 10px;border-bottom:1px solid #e5e7eb;text-align:right;font-family:monospace;">{esc(rsi_s)} <span style="color:#9ca3af;font-size:11px;">/ 72</span></td>
            <td style="padding:8px 10px;border-bottom:1px solid #e5e7eb;text-align:right;font-family:monospace;">{esc(sl.get('currentScore'))} <span style="color:#9ca3af;font-size:11px;">/ 50</span></td>
          </tr>""")
    for idle_i in range(len(active_slots) + 1, MAX_SLOTS + 1):
        slot_rows.append(f"""
          <tr>
            <td style="padding:8px 10px;border-bottom:1px solid #e5e7eb;font-family:monospace;color:#9ca3af;">#{idle_i}</td>
            <td colspan="6" style="padding:8px 10px;border-bottom:1px solid #e5e7eb;color:#6b7280;font-size:12px;"><strong>SPY</strong> — Idle slot parked in S&amp;P 500 ETF until next Mega-Cap Prime Dip</td>
          </tr>""")

    digest_banner = (
        '<div style="padding:10px 12px;margin-bottom:16px;background:#f0fdfa;border:1px solid #99f6e4;border-radius:8px;font-size:12px;color:#0f766e;">'
        '<strong>Live Depot Snapshot:</strong> No new buy/exit transitions occurred in this check — showing your current 6-Slot Mega-Cap Prime Dip positions.'
        '</div>'
        if is_digest else ""
    )

    html_body = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:680px;margin:0 auto;color:#111827;line-height:1.45;">
      <div style="padding:16px 20px;background:#0f172a;color:#f8fafc;border-radius:10px 10px 0 0;display:flex;justify-content:space-between;align-items:center;">
        <div>
          <div style="font-size:17px;font-weight:700;">⚡ Stock Signal — Automated Depot Alert</div>
          <div style="font-size:12px;color:#94a3b8;">6-Slot S&amp;P 100 Mega-Cap Prime Dip Strategy · Market Date {esc(trading_date)}</div>
        </div>
      </div>
      <div style="padding:20px;background:#f8fafc;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 10px 10px;">
        {digest_banner}
        {buy_rows_html}
        {exit_rows_html}
        <h3 style="margin:0 0 8px;font-size:14px;color:#1e293b;">📊 Active 6-Slot Mega-Cap Depot ({len(active_slots)}/{MAX_SLOTS} Slots Filled)</h3>
        <table style="width:100%;border-collapse:collapse;font-size:12px;background:#ffffff;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">
          <thead>
            <tr style="background:#f9fafb;color:#4b5563;font-size:11px;text-transform:uppercase;">
              <th style="padding:7px 10px;text-align:left;">Slot</th>
              <th style="padding:7px 10px;text-align:left;">Ticker</th>
              <th style="padding:7px 10px;text-align:right;">Entry</th>
              <th style="padding:7px 10px;text-align:right;">Now</th>
              <th style="padding:7px 10px;text-align:right;">P&amp;L</th>
              <th style="padding:7px 10px;text-align:right;">14d RSI</th>
              <th style="padding:7px 10px;text-align:right;">Score</th>
            </tr>
          </thead>
          <tbody>{''.join(slot_rows)}</tbody>
        </table>
        <div style="margin-top:16px;padding-top:12px;border-top:1px solid #e2e8f0;font-size:12px;color:#64748b;display:flex;justify-content:space-between;">
          <span>Rules: Buy S&amp;P 100 Prime Dip · Hold &ge;10d · Sell at RSI &ge;72 or Score &lt;50</span>
          <a href="{LIVE_SITE_URL}#alerts" style="color:#0f766e;font-weight:600;text-decoration:none;">Open Live Screener &rarr;</a>
        </div>
      </div>
    </div>
    """.strip()

    return subject, html_body, md_body


def send_via_smtp(to_addrs: list[str], subject: str, html_body: str, text_body: str) -> bool:
    smtp_user = os.environ.get("SMTP_USER", "").strip()
    smtp_pass = os.environ.get("SMTP_PASS", "").strip()
    if not (smtp_user and smtp_pass and to_addrs):
        return False

    domain = smtp_user.split("@")[-1].lower() if "@" in smtp_user else ""
    default_host, default_port, default_mode = SMTP_PROVIDERS.get(domain, ("smtp.gmail.com", 465, "ssl"))
    host = os.environ.get("SMTP_HOST", "").strip() or default_host
    port = int(os.environ.get("SMTP_PORT", "").strip() or default_port)
    mode = "ssl" if port == 465 else default_mode

    msg = email.message.EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"Stock Signal Alerts <{smtp_user}>"
    msg["To"] = ", ".join(to_addrs)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    ctx = ssl.create_default_context()
    if mode == "ssl":
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=25) as server:
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=25) as server:
            server.ehlo()
            server.starttls(context=ctx)
            server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
    print(f"[alerts] Sent rich HTML email via SMTP ({host}:{port}) to {', '.join(to_addrs)}")
    return True


def send_via_resend(to_addrs: list[str], subject: str, html_body: str, text_body: str) -> bool:
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not (api_key and to_addrs):
        return False
    from_addr = os.environ.get("RESEND_FROM", "").strip() or "Stock Signal <onboarding@resend.dev>"
    payload = json.dumps({
        "from": from_addr,
        "to": to_addrs,
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "StockSignalAlerts/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()
    print(f"[alerts] Sent rich HTML email via Resend API to {', '.join(to_addrs)}")
    return True


def send_via_github_issue_relay(subject: str, md_body: str) -> bool:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not (token and repo and "/" in repo):
        return False

    owner = repo.split("/")[0]
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "StockSignalAlerts/1.0",
    }

    # Find existing open alert-stream issue so we never clutter the repo with dozens of issues
    issue_title = "🔔 Stock Signal — Live Buy & Exit Alert Stream"
    list_url = f"https://api.github.com/repos/{repo}/issues?state=open&per_page=30"
    req = urllib.request.Request(list_url, headers=headers)
    issue_num = None
    with urllib.request.urlopen(req, timeout=20) as resp:
        issues = json.loads(resp.read().decode("utf-8"))
        for iss in issues:
            if iss.get("title") == issue_title and "pull_request" not in iss:
                issue_num = iss["number"]
                break

    comment_body = f"**{subject}** — cc @{owner}\n\n{md_body}"
    if issue_num is None:
        create_payload = json.dumps({
            "title": issue_title,
            "body": (
                f"Automated email-notification thread for @{owner} following the **6-Slot S&P 100 Mega-Cap `⚡ Prime Dip` Strategy**.\n"
                f"GitHub automatically emails you whenever a new Buy or Sell/Rotate comment is posted below.\n\n---\n\n{comment_body}"
            ),
            "assignees": [owner],
        }).encode("utf-8")
        c_req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/issues",
            data=create_payload,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(c_req, timeout=20) as resp:
            created = json.loads(resp.read().decode("utf-8"))
            print(f"[alerts] Created GitHub Alert Stream Issue #{created.get('number')} (notifying @{owner})")
    else:
        c_payload = json.dumps({"body": comment_body}).encode("utf-8")
        c_req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/issues/{issue_num}/comments",
            data=c_payload,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(c_req, timeout=20) as resp:
            resp.read()
            print(f"[alerts] Posted new alert to GitHub Issue #{issue_num} (emailing @{owner})")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Stock Signal transitions and dispatch email alerts.")
    parser.add_argument("--init-state", action="store_true", help="Seed alert_state.json without sending external notifications.")
    parser.add_argument("--no-send", action="store_true", help="Update alert_state.json only (used on git push deploys).")
    parser.add_argument("--force", action="store_true", help="Send a test / live-digest email even if no new signal changed.")
    args = parser.parse_args()

    if not STOCKS_JSON.exists():
        raise SystemExit(f"Missing {STOCKS_JSON} — run update_data.py first.")

    stocks_payload = json.loads(STOCKS_JSON.read_text(encoding="utf-8"))
    prev_state = load_previous_state()
    next_state = evaluate_signals(stocks_payload, prev_state, init_only=args.init_state)

    # Save state JSON + JS snapshot for static GitHub Pages frontend (`#alerts` tab)
    public_view = {k: v for k, v in next_state.items() if k != "prevSignals"}
    ALERT_STATE_JSON.write_text(json.dumps(next_state, indent=2), encoding="utf-8")
    ALERT_SNAPSHOT_JS.write_text(
        "window.__ALERT_DATA__ = " + json.dumps(public_view, separators=(",", ":")) + ";\n",
        encoding="utf-8",
    )

    sc = next_state["summaryCounts"]
    print(
        f"[alerts] Evaluated {next_state['lastTradingDate']}: "
        f"activeSlots={sc['activeSlotsFilled']}/{sc['maxSlots']}, "
        f"newDipBuys={sc['newDipBuys']}, slotEntries={sc['slotEntries']}, slotExits={sc['slotExits']}"
    )

    force_env = os.environ.get("FORCE_SEND_EMAIL", "").strip().lower() == "true"
    do_force = args.force or force_env

    if args.init_state or args.no_send:
        print("[alerts] State updated (--init-state / --no-send); skipping outbound email dispatch.")
        return

    if not next_state["shouldAlert"] and not do_force:
        print("[alerts] No new Buy or Exit signal transitions since previous check — skipping email (zero spam).")
        return

    raw_to = os.environ.get("ALERT_EMAIL_TO", "") or os.environ.get("SMTP_USER", "")
    to_addrs = [x.strip() for x in raw_to.replace(";", ",").split(",") if "@" in x]

    sent_direct = False
    try:
        if send_via_resend(to_addrs, next_state["latestEmailSubject"], next_state["latestEmailPreviewHtml"], next_state["latestMarkdownBody"]):
            sent_direct = True
    except Exception as exc:
        print(f"[alerts] Resend delivery error: {exc}")

    if not sent_direct:
        try:
            if send_via_smtp(to_addrs, next_state["latestEmailSubject"], next_state["latestEmailPreviewHtml"], next_state["latestMarkdownBody"]):
                sent_direct = True
        except Exception as exc:
            print(f"[alerts] SMTP delivery error: {exc}")

    gh_relay_pref = os.environ.get("GITHUB_ISSUE_ALERTS", "").strip().lower()
    # Default to GitHub Issue @mention email relay if no direct SMTP/Resend secret is configured yet (unless explicitly 'false')
    if gh_relay_pref == "true" or (not sent_direct and gh_relay_pref != "false"):
        try:
            send_via_github_issue_relay(next_state["latestEmailSubject"], next_state["latestMarkdownBody"])
        except Exception as exc:
            print(f"[alerts] GitHub Issue email relay error: {exc}")


if __name__ == "__main__":
    main()
