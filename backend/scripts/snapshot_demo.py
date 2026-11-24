#!/usr/bin/env python3
"""Capture a sanitized snapshot of the live terminal for /demo.

    backend/.venv/bin/python backend/scripts/snapshot_demo.py --base https://alpha-markets.onrender.com --user ashar

The password comes from $ALPHA_PW or an interactive prompt, never argv (argv lands in shell
history and `ps`). Every response passes through sanitize() before it touches disk, and
tests/test_demo_snapshot.py re-checks the committed files.
"""
from __future__ import annotations

import argparse
import getpass
import http.cookiejar
import json
import os
import re
import ssl
import sys
import urllib.request
from pathlib import Path
from typing import Any

ENDPOINTS = ("/slate", "/strategies", "/status/jobs", "/health",
             "/board", "/research/evidence", "/markets/browse", "/research/briefs")
OUT = Path(__file__).resolve().parents[2] / "frontend/public/demo"
FORBIDDEN = re.compile(r"(?i)balance|bankroll|position|account|user|email|order_id|client_order|kalshi_key")


def sanitize(obj: Any) -> Any:
    """Drop every key that looks account-shaped, at any depth."""
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items() if not FORBIDDEN.search(k)}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    return obj


def demo_me() -> dict:
    return {"user": "demo", "live_trading": {"allowed": False, "reason": "demo"}}


def demo_board(board: dict) -> dict:
    """The demo can look at cards but never trade them."""
    for card in board.get("cards", []):
        card["trade"] = {**card.get("trade", {}), "in_app": False}
    return board


def finish(path: str, data: Any) -> Any:
    data = sanitize(data)
    return demo_board(data) if path == "/board" else data


def demo_file(path: str) -> str:
    return path.lstrip("/").replace("/", "_") + ".json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", required=True)
    ap.add_argument("--user", help="log in first (needed once the API is private)")
    args = ap.parse_args()
    try:  # python.org builds ship without a CA bundle; certifi comes with httpx
        import certifi
        tls = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        tls = ssl.create_default_context()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
                                         urllib.request.HTTPSHandler(context=tls))
    if args.user:
        pw = os.environ.get("ALPHA_PW") or getpass.getpass(f"password for {args.user}: ")
        req = urllib.request.Request(f"{args.base}/api/login", method="POST",
                                     data=json.dumps({"username": args.user, "password": pw}).encode(),
                                     headers={"Content-Type": "application/json"})
        opener.open(req, timeout=60).read()
    OUT.mkdir(parents=True, exist_ok=True)
    for path in ENDPOINTS:
        with opener.open(f"{args.base}/api{path}", timeout=180) as resp:
            data = finish(path, json.load(resp))
        (OUT / demo_file(path)).write_text(json.dumps(data, separators=(",", ":")))
        print(f"wrote {demo_file(path)}")
    (OUT / "me.json").write_text(json.dumps(demo_me()))
    (OUT / "journal.json").write_text(json.dumps({"entries": [], "summary": {}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
