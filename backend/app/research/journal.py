"""Decision journal: every trade taken from the board, the card exactly as it looked when the
decision was made, and — once the contract settles — whether it won and what it made."""
from __future__ import annotations

import json
import time
import uuid
from collections import defaultdict
from typing import Any, Dict, List, Optional

from app.tracking import store


def record(username: str, card: Dict[str, Any], *, price: float, contracts: int, note: str = "") -> str:
    store.init()
    jid = uuid.uuid4().hex
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO journal (id, created_at, username, ticker, side, venue, price, contracts, card_json, note)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (jid, time.time(), username, str(card.get("ticker") or ""), str(card.get("side") or "yes"),
             str(card.get("venue") or "kalshi"), float(price), int(contracts), json.dumps(card), note or ""))
    return jid


def _yes_outcome(ticker: str, selection: Optional[str]) -> Optional[int]:
    """Latest settled YES outcome for this contract, from the graded predictions."""
    conn = store.connection()
    q = "SELECT outcome FROM predictions WHERE ticker=? AND status='settled' AND outcome IS NOT NULL"
    params: List[Any] = [ticker]
    if selection:
        q += " AND selection=?"
        params.append(selection)
    row = conn.execute(q + " ORDER BY settled_at DESC LIMIT 1", params).fetchone()
    return None if row is None else int(row["outcome"])


def entries(username: str) -> List[Dict[str, Any]]:
    store.init()
    rows = store.connection().execute(
        "SELECT * FROM journal WHERE username=? ORDER BY created_at DESC", (username,)).fetchall()
    out = []
    for r in rows:
        card = json.loads(r["card_json"])
        yes = _yes_outcome(r["ticker"], card.get("selection"))
        if yes is None:
            outcome, pnl = "open", None
        else:
            won = bool(yes) if r["side"] == "yes" else not yes
            outcome = "won" if won else "lost"
            pnl = r["contracts"] * (1 - r["price"]) if won else -r["contracts"] * r["price"]
        out.append({"id": r["id"], "created_at": r["created_at"], "ticker": r["ticker"], "side": r["side"],
                    "venue": r["venue"], "price": r["price"], "contracts": r["contracts"],
                    "note": r["note"], "card": card, "outcome": outcome,
                    "pnl": None if pnl is None else round(pnl, 2)})
    return out


def summary(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per evidence state at the time of the trade: does trusting PROVEN actually pay?"""
    agg: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"trades": 0, "won": 0, "lost": 0, "open": 0, "pnl": 0.0})
    for e in items:
        state = (e["card"].get("evidence") or {}).get("state", "WATCHING")
        a = agg[state]
        a["trades"] += 1
        a[e["outcome"]] += 1
        a["pnl"] = round(a["pnl"] + (e["pnl"] or 0.0), 2)
    return dict(agg)
