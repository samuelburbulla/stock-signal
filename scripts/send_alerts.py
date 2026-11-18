#!/usr/bin/env python3
"""
Stock Signal — Optional Background Signal Email Alert Script.

Monitors stock-level rating transitions in `public/data/stocks.json` (no Depot or
portfolio slot logic) and sends a concise notification ONLY when a stock:
  1. Newly enters `⚡ Prime Dip` (`Strong Buy` / `Good Buy` with clean pullback, zero knife flags)
  2. Newly upgrades to `Strong Buy`
  3. Newly triggers a `🚨` Corporate Shock / Falling-Knife downgrade
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
import urllib.request
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "public" / "data"
STOCKS_JSON = DATA_DIR / "stocks.json"
ALERT_STATE_JSON = DATA_DIR / "alert_state.json"
LIVE_SITE_URL = "https://samuelburbulla.github.io/stock-signal/"

SMTP_PROVIDERS: dict[str, tuple[str, int, str]] = {
    "gmail.com": ("smtp.gmail.com", 465, "ssl"),
    "googlemail.com": ("smtp.gmail.com", 465, "ssl"),
    "icloud.com": ("smtp.mail.me.com", 587, "starttls"),
    "me.com": ("smtp.mail.me.com", 587, "starttls"),
    "outlook.com": ("smtp-mail.outlook.com", 587, "starttls"),
    "hotmail.com": ("smtp-mail.outlook.com", 587, "starttls"),
    "gmx.de": ("mail.gmx.net", 465, "ssl"),
    "gmx.net": ("mail.gmx.net", 465, "ssl"),
    "web.de": ("smtp.web.de", 587, "starttls"),
}


def load_prev() -> dict[str, Any]:
    if ALERT_STATE_JSON.exists():
        try:
            return json.loads(ALERT_STATE_JSON.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def send_smtp(to_addr: str, subject: str, html_body: str, text_body: str) -> bool:
    user = os.environ.get("SMTP_USER", "").strip()
    pw = os.environ.get("SMTP_PASS", "").strip()
    if not (to_addr and user and pw):
        return False
    domain = user.split("@")[-1].lower() if "@" in user else ""
    default_host, default_port, mode = SMTP_PROVIDERS.get(domain, ("smtp.gmail.com", 465, "ssl"))
    host = os.environ.get("SMTP_HOST", "").strip() or default_host
    port = int(os.environ.get("SMTP_PORT", "").strip() or default_port)

    msg = email.message.EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"Stock Signal <{user}>"
    msg["To"] = to_addr
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    ctx = ssl.create_default_context()
    if mode == "ssl" or port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=15) as srv:
            srv.login(user, pw)
            srv.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=15) as srv:
            srv.starttls(context=ctx)
            srv.login(user, pw)
            srv.send_message(msg)
    return True


def send_resend(to_addr: str, subject: str, html_body: str, text_body: str) -> bool:
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not (to_addr and api_key):
        return False
    from_addr = os.environ.get("RESEND_FROM", "").strip() or "Stock Signal <onboarding@resend.dev>"
    payload = json.dumps({
        "from": from_addr,
        "to": [a.strip() for a in to_addr.split(",") if a.strip()],
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return 200 <= resp.status < 300


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-send", action="store_true")
    args = parser.parse_args()

    if not STOCKS_JSON.exists():
        return
    data = json.loads(STOCKS_JSON.read_text(encoding="utf-8"))
    stocks: list[dict[str, Any]] = data.get("stocks", [])
    prev_state = load_prev()
    prev_map: dict[str, dict[str, Any]] = prev_state.get("prevSignals") or {}
    is_seed = not prev_map

    new_prime_dips: list[dict[str, Any]] = []
    new_strong_buys: list[dict[str, Any]] = []
    new_shocks: list[dict[str, Any]] = []

    next_map: dict[str, dict[str, Any]] = {}
    for st in stocks:
        sym = st["symbol"]
        old = prev_map.get(sym) or {}
        if not is_seed:
            if st.get("dipKey") == "prime_dip" and old.get("dipKey") != "prime_dip":
                new_prime_dips.append(st)
            elif st.get("verdictKey") == "strong_buy" and old.get("verdictKey") != "strong_buy":
                new_strong_buys.append(st)
            if st.get("isFallingKnife") and not old.get("isFallingKnife"):
                new_shocks.append(st)

        next_map[sym] = {
            "price": st.get("price"),
            "score": st.get("score"),
            "verdictKey": st.get("verdictKey"),
            "dipKey": st.get("dipKey"),
            "valuationZone": st.get("valuationZone"),
            "isFallingKnife": bool(st.get("isFallingKnife")),
        }

    now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ALERT_STATE_JSON.write_text(
        json.dumps({"updatedAt": now_iso, "prevSignals": next_map}, indent=2),
        encoding="utf-8",
    )

    force_send = os.environ.get("FORCE_SEND_EMAIL", "").lower() == "true"
    has_changes = bool(new_prime_dips or new_strong_buys or new_shocks or force_send)
    if args.no_send or not has_changes:
        print(f"[alerts] Synced {len(next_map)} stock states (send={not args.no_send}, changes={has_changes}).")
        return

    to_addr = os.environ.get("ALERT_EMAIL_TO", "").strip()
    if not to_addr:
        return

    lines = [f"Stock Signal Daily Alert ({now_iso[:10]})", f"Live table: {LIVE_SITE_URL}", ""]
    html_rows = []
    for label, group in [
        ("⚡ New Prime Dip", new_prime_dips),
        ("🟢 New Strong Buy", new_strong_buys),
        ("🚨 Falling Knife / Shock", new_shocks),
    ]:
        for s in group:
            cur = s.get("currencySymbol", "$")
            rng = f"Cheap <= {cur}{s.get('cheapPrice', 0):,.0f} | Fair ~{cur}{s.get('fairPrice', 0):,.0f} | Exp >= {cur}{s.get('expensivePrice', 0):,.0f}"
            lines.append(f"{label}: {s['symbol']} ({cur}{s['price']}) — Score {s['score']} ({s['verdict']}) | {rng}")
            html_rows.append(
                f"<tr><td style='padding:8px;border-bottom:1px solid #e5e7eb;'><b>{html.escape(label)}</b></td>"
                f"<td style='padding:8px;border-bottom:1px solid #e5e7eb;'><b>{html.escape(s['symbol'])}</b> ({cur}{s['price']})</td>"
                f"<td style='padding:8px;border-bottom:1px solid #e5e7eb;'>{s['score']} · {html.escape(s['verdict'])}</td>"
                f"<td style='padding:8px;border-bottom:1px solid #e5e7eb;'>{html.escape(rng)}</td></tr>"
            )

    subject = f"Stock Signal Alert — {len(new_prime_dips) + len(new_strong_buys)} Buy / {len(new_shocks)} Shock Changes"
    html_body = (
        f"<div style='font-family:Inter,sans-serif;max-width:680px;'>"
        f"<h3>Stock Signal Update</h3>"
        f"<table style='width:100%;border-collapse:collapse;font-size:13px;'>{''.join(html_rows)}</table>"
        f"<p style='margin-top:14px;'><a href='{LIVE_SITE_URL}'>Open Stock Signal Table &rarr;</a></p></div>"
    )
    text_body = "\n".join(lines)

    try:
        if not send_smtp(to_addr, subject, html_body, text_body):
            send_resend(to_addr, subject, html_body, text_body)
    except Exception as exc:
        print(f"[alerts] Email delivery warning: {exc}")


if __name__ == "__main__":
    main()
