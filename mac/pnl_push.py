#!/usr/bin/env python3
"""Read Kalshi, Public and Topstep READ-ONLY on this Mac and push one P&L snapshot to the terminal.

Why on the Mac: broker credentials never go on the hosted server (spec, anti-goals). This script
is the only thing that touches them, and it only ever reads:
  - Kalshi  -> the local kalshi-monitor API (http://127.0.0.1:8790/api/state). No Kalshi key here.
  - Public  -> Keychain "public-api-secret" -> 30-min token -> GET portfolio/v2.
  - Topstep -> while KRYVANT's live runner owns the session (weekdays 09:20-16:05 ET) we do NOT
               log in; the runner's morning journal balance is used and labelled. Outside that
               window: Keychain "topstepx-username"/"topstepx-api" -> loginKey -> Account/search
               and Position/searchOpen (both reads).
Nothing that identifies an account (ids, names, usernames) is put in the snapshot.

Each venue fails on its own: one broker down never blanks the others.
Stdlib only, Python 3.9+ (runs on the system python3 from launchd).

  python3 mac/pnl_push.py            # collect + push (token: Keychain "alpha-markets-pnl-push")
  python3 mac/pnl_push.py --dry-run  # collect and print, push nothing
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - zoneinfo is stdlib on 3.9+
    ET = None

KMON_URL = os.getenv("KMON_URL", "http://127.0.0.1:8790")
PUBLIC_BASE = "https://api.public.com"
TOPSTEP_BASE = "https://api.topstepx.com"
PUSH_URL = os.getenv("ALPHA_PNL_URL", "https://alpha-markets.onrender.com") + "/api/pnl/push"
KRYVANT = Path(os.getenv("KRYVANT_DIR", str(Path.home() / "ai-hedge-fund")))
PUBLIC_CONFIG = KRYVANT / "config" / "public_lane.json"
TOPSTEP_JOURNAL = KRYVANT / "journal" / "topstep_practice.jsonl"
COMBINE_START = 50_000.0     # 50K Combine starting balance: lifetime P&L is measured from here
HEARTBEAT = Path.home() / "Library" / "Application Support" / "alpha-markets" / "pnl_push.heartbeat.json"

Http = Callable[..., Any]


def keychain(service: str) -> str:
    return subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                          capture_output=True, text=True, check=True).stdout.strip()


def http(method: str, url: str, body: Any = None, headers: Optional[dict] = None, timeout: float = 20) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "alpha-markets-pnl/1"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        text = r.read().decode()
    return json.loads(text) if text else {}


def _f(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _failed(venue: str, exc: BaseException) -> Dict[str, Any]:
    return {"venue": venue, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}


# ---------------------------------------------------------------- Kalshi (via kalshi-monitor)

def parse_kalshi(state: Dict[str, Any]) -> Dict[str, Any]:
    if not state.get("ready") or state.get("mode") != "live":
        raise RuntimeError(f"kalshi-monitor not live (mode={state.get('mode')})")
    conn = state.get("connection") or {}
    if conn.get("positions_error"):
        raise RuntimeError(f"kalshi-monitor positions error: {conn['positions_error']}")
    cash = _f(conn.get("cash_balance"))
    totals = state.get("totals") or {}
    liq = _f(totals.get("liquidation_value")) or 0.0
    positions = []
    for p in state.get("positions") or []:
        if p.get("source") != "live":
            continue
        m = p.get("metrics") or {}
        positions.append({
            "symbol": p.get("ticker"), "name": p.get("title"), "kind": f"{p.get('kind')}:{p.get('side')}",
            "qty": _f(m.get("qty")), "value": _f(m.get("liquidation_value")), "cost": _f(m.get("cost_basis")),
            "pnl": _f(m.get("exec_pnl")), "day_pnl": None,
            "note": None if m.get("liquidation_complete") else "no full exit in the book; unsellable part valued $0",
        })
    as_of = float(state.get("ts") or time.time()) - float(conn.get("positions_age_s") or 0)
    return {"venue": "kalshi", "ok": True, "as_of": as_of,
            "equity": None if cash is None else round(cash + liq, 4), "cash": cash,
            "day_pnl": None, "unrealized_pnl": _f(totals.get("exec_pnl")),
            "note": "Valued at what the order book would pay now (executable bid), not mid.",
            "positions": positions}


def kalshi(get: Http = http) -> Dict[str, Any]:
    try:
        return parse_kalshi(get("GET", f"{KMON_URL}/api/state", timeout=10))
    except Exception as exc:  # noqa: BLE001 - isolate the venue
        return _failed("kalshi", exc)


# ---------------------------------------------------------------- Public

def parse_public(pf: Dict[str, Any], now: float) -> Dict[str, Any]:
    positions, day, unreal = [], 0.0, 0.0
    for p in pf.get("positions") or []:
        inst = p.get("instrument") or {}
        cb = p.get("costBasis") or {}
        dg = _f((p.get("positionDailyGain") or {}).get("gainValue"))
        g = _f(cb.get("gainValue"))
        day += dg or 0.0
        unreal += g or 0.0
        positions.append({"symbol": inst.get("symbol"), "name": inst.get("name"), "kind": inst.get("type"),
                          "qty": _f(p.get("quantity")), "value": _f(p.get("currentValue")),
                          "cost": _f(cb.get("totalCost")), "pnl": g, "day_pnl": dg})
    return {"venue": "public", "ok": True, "as_of": now,
            "equity": _f(pf.get("totalAccountValue")), "cash": _f(pf.get("cash")),
            "day_pnl": round(day, 2), "unrealized_pnl": round(unreal, 2),
            "note": "Day P&L = sum of today's position gains (excludes positions closed today).",
            "positions": positions}


def public(req: Http = http, secret: Callable[[str], str] = keychain, now: Callable[[], float] = time.time) -> Dict[str, Any]:
    try:
        acct = json.loads(PUBLIC_CONFIG.read_text())["account_id"]
        tok = req("POST", f"{PUBLIC_BASE}/userapiauthservice/personal/access-tokens",
                  {"secret": secret("public-api-secret"), "validityInMinutes": 15})["accessToken"]
        pf = req("GET", f"{PUBLIC_BASE}/userapigateway/trading/{acct}/portfolio/v2",
                 headers={"Authorization": f"Bearer {tok}"})
        if pf.get("accountId") != acct:
            raise RuntimeError("portfolio came back for a different account")
        return parse_public(pf, now())
    except Exception as exc:  # noqa: BLE001
        return _failed("public", exc)


# ---------------------------------------------------------------- Topstep

def runner_window(now: datetime) -> bool:
    """Weekdays 09:20-16:05 ET: KRYVANT's live MNQ runner owns the TopstepX session. Hands off."""
    t = now.astimezone(ET) if ET else now
    return t.weekday() < 5 and (9, 20) <= (t.hour, t.minute) < (16, 5)


def morning_balance(lines: List[str], today: str) -> Optional[Dict[str, Any]]:
    """The runner's 'ready' event for `today` (YYYY-MM-DD, ET): balance at session start."""
    for line in reversed(lines):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("event") == "ready" and str(r.get("at", "")).startswith(today) and r.get("balance") is not None:
            return r
    return None


def last_position(lines: List[str], today: str) -> Optional[float]:
    for line in reversed(lines):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not str(r.get("at", "")).startswith(today):
            return None
        if r.get("event") == "checkpoint" and "have" in r:
            return _f(r["have"])
    return None


def topstep(now: Callable[[], datetime] = lambda: datetime.now(ET), req: Http = http,
            secret: Callable[[str], str] = keychain, journal: Path = TOPSTEP_JOURNAL) -> Dict[str, Any]:
    try:
        t = now()
        today = t.strftime("%Y-%m-%d")
        lines = journal.read_text().splitlines()[-4000:] if journal.exists() else []
        ready = morning_balance(lines, today)
        start_today = _f(ready["balance"]) if ready else None

        if runner_window(t):
            if not ready:
                raise RuntimeError("runner window, and no session-start balance in the journal yet")
            have = last_position(lines, today)
            return {"venue": "topstep", "ok": True,
                    "as_of": datetime.fromisoformat(ready["at"]).timestamp(),
                    "equity": start_today, "cash": None, "day_pnl": None, "unrealized_pnl": None,
                    "start_balance": COMBINE_START,
                    "note": ("Runner is live: balance is the 09:25 session-start figure; intraday P&L "
                             "is in TopstepX. Refreshes after 16:05 ET."),
                    "positions": ([{"symbol": "MNQ", "name": "Micro Nasdaq (runner)", "kind": "FUTURE",
                                    "qty": have, "note": "from the runner's last checkpoint"}] if have else [])}

        auth = req("POST", f"{TOPSTEP_BASE}/api/Auth/loginKey",
                   {"userName": secret("topstepx-username"), "apiKey": secret("topstepx-api")})
        if not auth.get("success"):
            raise RuntimeError(f"TopstepX login failed (errorCode={auth.get('errorCode')})")
        h = {"Authorization": f"Bearer {auth['token']}"}
        accts = req("POST", f"{TOPSTEP_BASE}/api/Account/search", {"onlyActiveAccounts": True}, headers=h)
        accounts = accts.get("accounts") or []
        want = (ready or {}).get("account_id")
        acct = next((a for a in accounts if want and a.get("id") == want), None) or \
            next((a for a in accounts if "50K" in str(a.get("name", "")).upper()), None)
        if not acct:
            raise RuntimeError(f"no 50K Combine among {len(accounts)} active account(s)")
        bal = _f(acct.get("balance"))
        pos = req("POST", f"{TOPSTEP_BASE}/api/Position/searchOpen", {"accountId": acct["id"]}, headers=h)
        positions = [{"symbol": str(p.get("contractId", "")).split(".")[-2] if p.get("contractId") else "?",
                      "name": p.get("contractId"), "kind": "FUTURE",
                      "qty": _f(p.get("size")) * (1 if p.get("type") == 1 else -1) if p.get("size") else None,
                      "cost": _f(p.get("averagePrice")), "note": "cost = average price"}
                     for p in pos.get("positions") or []]
        return {"venue": "topstep", "ok": True, "as_of": time.time(), "equity": bal, "cash": None,
                "day_pnl": round(bal - start_today, 2) if (bal is not None and start_today is not None) else None,
                "unrealized_pnl": None, "start_balance": COMBINE_START,
                "note": "50K Combine. Day P&L = balance now minus the runner's 09:25 balance.",
                "positions": positions}
    except Exception as exc:  # noqa: BLE001
        return _failed("topstep", exc)


# ---------------------------------------------------------------- push

def collect() -> Dict[str, Any]:
    return {"schema": 1, "venues": [kalshi(), public(), topstep()]}


def push(snapshot: Dict[str, Any], token: str, req: Http = http) -> Any:
    return req("POST", PUSH_URL, snapshot, headers={"Authorization": f"Bearer {token}"}, timeout=30)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    snap = collect()
    summary = {v["venue"]: ("ok" if v["ok"] else v.get("error")) for v in snap["venues"]}
    if args.dry_run:
        print(json.dumps(snap, indent=1))
        return 0
    beat: Dict[str, Any] = {"at": datetime.now().isoformat(timespec="seconds"), "venues": summary}
    try:
        beat["push"] = push(snap, keychain("alpha-markets-pnl-push"))
        rc = 0
    except urllib.error.HTTPError as exc:
        beat["push_error"] = f"HTTP {exc.code}"
        rc = 1
    except Exception as exc:  # noqa: BLE001
        beat["push_error"] = f"{type(exc).__name__}: {exc}"[:300]
        rc = 1
    HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
    HEARTBEAT.write_text(json.dumps(beat, indent=1))
    print(json.dumps(beat))
    return rc


if __name__ == "__main__":
    sys.exit(main())
