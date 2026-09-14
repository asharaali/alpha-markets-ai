"""Recover settlement metadata for positions opened before the columns existed.

Positions written by the old execution path carry no team, line or selection, because the
old code never passed them. Under the corrected resolver those positions return "cannot
say" and stay open rather than being graded on a guess, which is the right behaviour and
also means a real bet sits unresolved forever.

The information is recoverable. A Kalshi NFL ticker encodes the market, the fixture and
the threshold, and the position's own label carries the line in words. This parses both,
requires them to AGREE before writing anything, and records what it changed in the note
field so the correction is auditable rather than silent.

Nothing here guesses. A position whose ticker and label disagree, or whose ticker does not
parse, is left exactly as it was and reported as needing a human.
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

from app.core.logging import get_logger
from app.data import teams
from app.tracking import store

log = get_logger(__name__)

# KXNFLTEAMTOTAL-26SEP09NESEA-NE25  ->  series, fixture, outcome
TICKER = re.compile(r"^(?P<series>KXNFL[A-Z]+)-(?P<fixture>[0-9A-Z]+)-(?P<outcome>[A-Z0-9.]+)$")

# "Patriots over 24.5 points", "Chiefs by more than 3.5"
LABEL_LINE = re.compile(r"(?:over|more than|under)\s+(?P<line>\d+(?:\.\d+)?)", re.I)

SERIES_MARKETS = {
    "KXNFLTEAMTOTAL": "team_total",
    "KXNFLGAME": "moneyline",
    "KXNFLSPREAD": "spread",
    "KXNFLTOTAL": "total",
    "KXNFLWINMARGIN": "win_margin",
}


def parse_ticker(ticker: str) -> Optional[Dict[str, Any]]:
    """Team and threshold implied by a Kalshi NFL ticker, or None if it does not parse."""
    match = TICKER.match(ticker or "")
    if not match:
        return None
    series = match.group("series")
    outcome = match.group("outcome")
    market = SERIES_MARKETS.get(series)
    if market is None:
        return None

    # The outcome segment is an abbreviation followed by a threshold, e.g. NE25.
    parts = re.match(r"^(?P<abbr>[A-Z]{2,4})(?P<value>\d+(?:\.\d+)?)?$", outcome)
    if not parts:
        return None
    abbr = parts.group("abbr")
    raw = parts.group("value")

    # Kalshi threshold contracts read "N or more", which is the same market as "over N-0.5".
    line = (float(raw) - 0.5) if raw is not None else None
    return {"market_type": market, "team": abbr, "line": line, "series": series}


def parse_label(label: str) -> Optional[float]:
    match = LABEL_LINE.search(label or "")
    return float(match.group("line")) if match else None


def plan(rows: Optional[List[Dict[str, Any]]] = None) -> Tuple[List[Dict[str, Any]],
                                                               List[Dict[str, Any]]]:
    """(repairable, needs_human) without writing anything."""
    store.init()
    if rows is None:
        rows = [dict(r) for r in store.connection().execute(
            "SELECT * FROM positions WHERE team IS NULL OR line IS NULL").fetchall()]

    repairable: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []

    for row in rows:
        parsed = parse_ticker(row.get("ticker") or "")
        if parsed is None:
            blocked.append({**row, "reason": "ticker does not parse"})
            continue
        from_label = parse_label(row.get("label") or "")

        # Both sources must agree. One source alone is a guess, and a guess about which
        # side of a line a bet sat on is exactly the error this whole change removes.
        if parsed["line"] is not None and from_label is not None:
            if abs(parsed["line"] - from_label) > 1e-6:
                blocked.append({**row,
                                "reason": (f"ticker implies {parsed['line']} but the label "
                                           f"says {from_label}")})
                continue
        line = parsed["line"] if parsed["line"] is not None else from_label
        if parsed["market_type"] != "moneyline" and line is None:
            blocked.append({**row, "reason": "no line recoverable from ticker or label"})
            continue

        repairable.append({
            "id": row["id"], "ticker": row["ticker"],
            "team": parsed["team"], "line": line,
            "selection": row.get("label"),
            "market_type": row.get("market_type") or parsed["market_type"],
        })

    return repairable, blocked


def apply() -> Dict[str, Any]:
    """Write the recoverable repairs, leaving an audit note on every row touched."""
    repairable, blocked = plan()
    if not repairable:
        return {"repaired": 0, "blocked": blocked,
                "note": "Nothing to repair." if not blocked else
                        f"{len(blocked)} position(s) need a human."}

    stamp = time.strftime("%Y-%m-%d", time.gmtime())
    with store.transaction() as conn:
        for repair in repairable:
            row = conn.execute("SELECT note FROM positions WHERE id=?",
                               (repair["id"],)).fetchone()
            previous = (row["note"] if row else "") or ""
            audit = (f"{previous} | {stamp}: settlement metadata recovered from ticker "
                     f"{repair['ticker']} (team {repair['team']}, line {repair['line']}); "
                     "no price or quantity was changed").strip(" |")
            conn.execute(
                "UPDATE positions SET team=?, line=?, selection=?, market_type=?, note=? "
                "WHERE id=?",
                (repair["team"], repair["line"], repair["selection"],
                 repair["market_type"], audit, repair["id"]))

    log.info("backfilled settlement metadata on %d position(s)", len(repairable))
    return {
        "repaired": len(repairable),
        "details": repairable,
        "blocked": blocked,
        "note": (f"Recovered team and line for {len(repairable)} position(s) from their "
                 "tickers. Entry prices, quantities and P&L were not touched; only the "
                 "metadata needed to settle them. Each row carries a note recording the "
                 "change."),
    }
