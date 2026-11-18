#!/usr/bin/env python3
"""
Stock Signal — Live Web Server & Automated Daily Updater.

Serves the Stock Signal web app on port 8080 and runs a background daily
refresh loop (every trading day at 21:30 UTC after US market close, plus
automatic catch-up if cached data is >20 hours old).
"""

from __future__ import annotations

import datetime as dt
import json
import mimetypes
import os
import sys
import threading
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = ROOT_DIR / "public"
sys.path.insert(0, str(ROOT_DIR / "scripts"))

import update_data as engine  # noqa: E402

STATE_LOCK = threading.Lock()


def load_cached_payload() -> dict:
    if engine.STOCKS_JSON_FILE.exists():
        try:
            return json.loads(engine.STOCKS_JSON_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return engine.update_all()


def is_stale(payload: dict, max_hours: float = 20.0) -> bool:
    ts = payload.get("updatedAt")
    if not ts:
        return True
    try:
        updated = dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        age_hours = (dt.datetime.now(dt.timezone.utc) - updated).total_seconds() / 3600.0
        return age_hours >= max_hours
    except Exception:
        return True


def daily_scheduler_loop() -> None:
    """Background daemon that refreshes stock valuations daily after US market close."""
    last_scheduled_date = ""
    while True:
        time.sleep(600)  # Check every 10 minutes
        try:
            now = dt.datetime.now(dt.timezone.utc)
            today_str = now.strftime("%Y-%m-%d")
            # Run on weekdays at/after 21:30 UTC (~4:30 PM ET market close) once per day
            should_run_market_close = (
                now.weekday() < 5
                and (now.hour > 21 or (now.hour == 21 and now.minute >= 30))
                and last_scheduled_date != today_str
            )
            payload = load_cached_payload()
            if should_run_market_close or is_stale(payload, max_hours=20.0):
                with STATE_LOCK:
                    engine.update_all()
                    last_scheduled_date = today_str
        except Exception as exc:
            print(f"[scheduler] daily refresh error: {exc}", file=sys.stderr)


class StockRadarHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC_DIR), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        sys.stdout.write(f"[http] {self.address_string()} - {fmt % args}\n")
        sys.stdout.flush()

    def _send_json(self, status: int, data: dict) -> None:
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/stocks":
            payload = load_cached_payload()
            if is_stale(payload, max_hours=20.0):
                with STATE_LOCK:
                    payload = engine.update_all()
            self._send_json(HTTPStatus.OK, payload)
            return
        if parsed.path == "/api/health":
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            body = self._read_json_body()
        except Exception:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body."})
            return

        if parsed.path == "/api/refresh":
            try:
                with STATE_LOCK:
                    payload = engine.update_all()
                self._send_json(HTTPStatus.OK, payload)
            except Exception as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return

        if parsed.path == "/api/add":
            ticker = str(body.get("ticker") or "").strip().upper().replace(".", "-")
            if not ticker or len(ticker) > 12:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Please enter a valid stock ticker (e.g., PLTR, DIS, SHOP)."})
                return
            try:
                with STATE_LOCK:
                    client = engine.YahooClient()
                    new_stock = engine.fetch_stock(client, ticker)
                    watchlist = engine.load_watchlist()
                    if new_stock["symbol"] not in watchlist:
                        watchlist.append(new_stock["symbol"])
                        engine.save_watchlist(watchlist)
                    current = load_cached_payload().get("stocks", [])
                    filtered = [s for s in current if s.get("symbol") != new_stock["symbol"]]
                    filtered.append(new_stock)
                    payload = engine.write_payload(filtered)
                self._send_json(HTTPStatus.OK, {"added": new_stock, "payload": payload})
            except Exception as exc:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": f"Could not find or load '{ticker}'. Check the ticker symbol and try again. ({exc})"},
                )
            return

        if parsed.path == "/api/remove":
            ticker = str(body.get("ticker") or "").strip().upper().replace(".", "-")
            with STATE_LOCK:
                watchlist = [t for t in engine.load_watchlist() if t != ticker]
                engine.save_watchlist(watchlist)
                current = [s for s in load_cached_payload().get("stocks", []) if s.get("symbol") != ticker]
                payload = engine.write_payload(current)
            self._send_json(HTTPStatus.OK, {"removed": ticker, "payload": payload})
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown API endpoint."})


def main() -> None:
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    port = int(os.environ.get("PORT", "8080"))

    if not engine.STOCKS_JSON_FILE.exists():
        print("[init] Fetching initial daily stock snapshot...")
        engine.update_all()

    sched = threading.Thread(target=daily_scheduler_loop, daemon=True)
    sched.start()

    server = ThreadingHTTPServer(("0.0.0.0", port), StockRadarHandler)
    print(f"Stock Signal server listening on http://localhost:{port}")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
