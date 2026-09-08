"""SQLite persistence for predictions, market snapshots, positions and parlays.

The single most important property of this module: a prediction is written the moment it is
made, with the price that was available at that moment, and its outcome column starts NULL.
Grading later fills the outcome in. Nothing ever rewrites a stored probability.

That is what makes the performance page mean something. A track record you can edit after
the fact is marketing, not evidence — and it is the specific failure of the JSON bet log
this replaces, where a prediction only existed once you chose to log it.

SQLite via the standard library: no ORM, no migration framework, no server. The schema is
created on demand and evolved additively.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from app.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id              TEXT PRIMARY KEY,
    created_at      REAL NOT NULL,
    season          INTEGER NOT NULL,
    week            INTEGER NOT NULL,
    game_id         TEXT NOT NULL,
    kickoff         TEXT,
    strategy        TEXT NOT NULL,
    market_type     TEXT NOT NULL,
    ticker          TEXT,
    selection       TEXT NOT NULL,
    label           TEXT,
    team            TEXT,
    player          TEXT,
    line            REAL,
    model_prob      REAL NOT NULL,
    fair_prob       REAL,
    market_prob     REAL,
    edge            REAL,
    ev_per_dollar   REAL,
    confidence      TEXT,
    cost            REAL,
    depth_usd       REAL,
    reasoning       TEXT,
    -- Outcome columns start NULL and are only ever filled in by grading.
    status          TEXT NOT NULL DEFAULT 'pending',
    settled_at      REAL,
    outcome         INTEGER,
    closing_prob    REAL,
    clv             REAL,
    UNIQUE(game_id, strategy, ticker, selection, created_at)
);
CREATE INDEX IF NOT EXISTS idx_pred_game ON predictions(game_id);
CREATE INDEX IF NOT EXISTS idx_pred_status ON predictions(status);
CREATE INDEX IF NOT EXISTS idx_pred_strategy ON predictions(strategy);
CREATE INDEX IF NOT EXISTS idx_pred_created ON predictions(created_at);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at   REAL NOT NULL,
    ticker        TEXT NOT NULL,
    game_id       TEXT,
    market_type   TEXT,
    label         TEXT,
    yes_bid       REAL,
    yes_ask       REAL,
    mid           REAL,
    depth_usd     REAL
);
CREATE INDEX IF NOT EXISTS idx_snap_ticker ON market_snapshots(ticker, captured_at);
CREATE INDEX IF NOT EXISTS idx_snap_game ON market_snapshots(game_id, captured_at);

CREATE TABLE IF NOT EXISTS positions (
    id            TEXT PRIMARY KEY,
    user          TEXT NOT NULL,
    created_at    REAL NOT NULL,
    mode          TEXT NOT NULL,
    parlay_id     TEXT,
    game_id       TEXT,
    ticker        TEXT NOT NULL,
    side          TEXT NOT NULL,
    label         TEXT,
    market_type   TEXT,
    contracts     INTEGER NOT NULL,
    entry_price   REAL NOT NULL,
    stake         REAL NOT NULL,
    model_prob    REAL,
    status        TEXT NOT NULL DEFAULT 'open',
    closed_at     REAL,
    exit_price    REAL,
    pnl           REAL,
    note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_pos_user ON positions(user, status);
CREATE INDEX IF NOT EXISTS idx_pos_parlay ON positions(parlay_id);

CREATE TABLE IF NOT EXISTS parlays (
    id             TEXT PRIMARY KEY,
    user           TEXT NOT NULL,
    created_at     REAL NOT NULL,
    mode           TEXT NOT NULL,
    category       TEXT NOT NULL,
    legs           TEXT NOT NULL,
    leg_count      INTEGER NOT NULL,
    naive_prob     REAL,
    combined_prob  REAL,
    combined_odds  REAL,
    ev_per_dollar  REAL,
    risk_rating    TEXT,
    stake          REAL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'open',
    settled_at     REAL,
    payout         REAL
);
CREATE INDEX IF NOT EXISTS idx_parlay_user ON parlays(user, status);

CREATE TABLE IF NOT EXISTS bankroll (
    user       TEXT PRIMARY KEY,
    starting   REAL NOT NULL,
    current    REAL NOT NULL,
    updated_at REAL NOT NULL,
    settings   TEXT
);

CREATE TABLE IF NOT EXISTS users (
    username    TEXT PRIMARY KEY,
    salt        TEXT NOT NULL,
    hash        TEXT NOT NULL,
    created_at  REAL NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    path = Path(settings.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15.0,
                           detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    # WAL keeps the background snapshot job from blocking a page load.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def connection() -> sqlite3.Connection:
    """One connection per thread. SQLite connections are not thread-safe to share."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    conn = connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


_initialised = False


def init() -> None:
    global _initialised
    if _initialised:
        return
    with transaction() as conn:
        conn.executescript(SCHEMA)
    _initialised = True
    log.info("database ready at %s", settings.DB_PATH)


def _rows(cursor: sqlite3.Cursor) -> List[Dict[str, Any]]:
    return [dict(r) for r in cursor.fetchall()]


# ----------------------------------------------------------------- predictions

def record_predictions(entries: Sequence[Dict[str, Any]]) -> int:
    """Persist predictions before their outcomes exist. Returns the number written.

    Duplicate suppression is by (game_id, strategy, ticker, selection) within the same
    hour: the snapshot job runs repeatedly and we want a trail of how a view evolved, not
    one row per poll.
    """
    if not entries:
        return 0
    init()
    written = 0
    now = time.time()
    with transaction() as conn:
        for entry in entries:
            recent = conn.execute(
                "SELECT 1 FROM predictions WHERE game_id=? AND strategy=? "
                "AND IFNULL(ticker,'')=? AND selection=? AND created_at > ? LIMIT 1",
                (entry["game_id"], entry["strategy"], entry.get("ticker") or "",
                 entry["selection"], now - 3600),
            ).fetchone()
            if recent:
                continue
            conn.execute(
                """INSERT OR IGNORE INTO predictions
                   (id, created_at, season, week, game_id, kickoff, strategy, market_type,
                    ticker, selection, label, team, player, line, model_prob, fair_prob,
                    market_prob, edge, ev_per_dollar, confidence, cost, depth_usd,
                    reasoning)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex[:16], now, entry["season"], entry["week"],
                 entry["game_id"], entry.get("kickoff"), entry["strategy"],
                 entry["market_type"], entry.get("ticker"), entry["selection"],
                 entry.get("label"), entry.get("team"), entry.get("player"),
                 entry.get("line"), entry["model_prob"], entry.get("fair_prob"),
                 entry.get("market_prob"), entry.get("edge"),
                 entry.get("ev_per_dollar"), entry.get("confidence"),
                 entry.get("cost"), entry.get("depth_usd"),
                 json.dumps(entry.get("reasoning") or [])),
            )
            written += 1
    if written:
        log.info("recorded %d predictions", written)
    return written


def pending_predictions(before_kickoff: Optional[str] = None) -> List[Dict[str, Any]]:
    init()
    conn = connection()
    if before_kickoff:
        cur = conn.execute("SELECT * FROM predictions WHERE status='pending' "
                           "AND kickoff IS NOT NULL AND kickoff < ?", (before_kickoff,))
    else:
        cur = conn.execute("SELECT * FROM predictions WHERE status='pending'")
    return _rows(cur)


def settle_prediction(prediction_id: str, *, outcome: bool,
                      closing_prob: Optional[float] = None) -> None:
    """Record how a prediction actually resolved. Probabilities are never touched.

    Closing-line value is stored alongside: the gap between the price we predicted at and
    the price the market settled on. Positive CLV is the strongest available evidence that
    a model is finding real information rather than variance.
    """
    init()
    with transaction() as conn:
        row = conn.execute("SELECT market_prob FROM predictions WHERE id=?",
                           (prediction_id,)).fetchone()
        clv = None
        if row is not None and closing_prob is not None and row["market_prob"] is not None:
            clv = closing_prob - row["market_prob"]
        conn.execute(
            "UPDATE predictions SET status=?, settled_at=?, outcome=?, closing_prob=?, "
            "clv=? WHERE id=?",
            ("settled", time.time(), 1 if outcome else 0, closing_prob, clv,
             prediction_id))


def void_prediction(prediction_id: str, reason: str = "") -> None:
    init()
    with transaction() as conn:
        conn.execute("UPDATE predictions SET status='void', settled_at=? WHERE id=?",
                     (time.time(), prediction_id))


def settled_predictions(*, strategy: Optional[str] = None,
                        market_type: Optional[str] = None,
                        since: Optional[float] = None) -> List[Dict[str, Any]]:
    init()
    clauses = ["status='settled'", "outcome IS NOT NULL"]
    params: List[Any] = []
    if strategy:
        clauses.append("strategy=?")
        params.append(strategy)
    if market_type:
        clauses.append("market_type=?")
        params.append(market_type)
    if since:
        clauses.append("created_at >= ?")
        params.append(since)
    sql = f"SELECT * FROM predictions WHERE {' AND '.join(clauses)} ORDER BY created_at"
    return _rows(connection().execute(sql, params))


def predictions_for_game(game_id: str, limit: int = 500) -> List[Dict[str, Any]]:
    init()
    return _rows(connection().execute(
        "SELECT * FROM predictions WHERE game_id=? ORDER BY created_at DESC LIMIT ?",
        (game_id, limit)))


# ----------------------------------------------------------------- snapshots

def record_snapshots(quotes: Iterable[Any]) -> int:
    """Append the current book for each quote. This is the raw material for line movement."""
    init()
    now = time.time()
    count = 0
    with transaction() as conn:
        for q in quotes:
            conn.execute(
                """INSERT INTO market_snapshots
                   (captured_at, ticker, game_id, market_type, label, yes_bid, yes_ask,
                    mid, depth_usd)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (now, q.ticker, q.game_id,
                 q.market_type.value if hasattr(q.market_type, "value") else q.market_type,
                 q.label, q.yes_bid, q.yes_ask, q.mid, q.depth_usd))
            count += 1
    return count


def snapshot_history(ticker: str, limit: int = 200) -> List[Dict[str, Any]]:
    init()
    return _rows(connection().execute(
        "SELECT captured_at, yes_bid, yes_ask, mid, depth_usd FROM market_snapshots "
        "WHERE ticker=? ORDER BY captured_at DESC LIMIT ?", (ticker, limit)))[::-1]


def game_snapshot_history(game_id: str, limit: int = 2000) -> List[Dict[str, Any]]:
    init()
    return _rows(connection().execute(
        "SELECT captured_at, ticker, label, market_type, mid, depth_usd "
        "FROM market_snapshots WHERE game_id=? ORDER BY captured_at DESC LIMIT ?",
        (game_id, limit)))[::-1]


def latest_snapshot_before(ticker: str, cutoff: float) -> Optional[Dict[str, Any]]:
    init()
    row = connection().execute(
        "SELECT * FROM market_snapshots WHERE ticker=? AND captured_at <= ? "
        "ORDER BY captured_at DESC LIMIT 1", (ticker, cutoff)).fetchone()
    return dict(row) if row else None


def prune_snapshots(older_than_days: float = 45.0) -> int:
    """Keep the snapshot table from growing without bound on a small persistent disk."""
    init()
    cutoff = time.time() - older_than_days * 86400
    with transaction() as conn:
        cur = conn.execute("DELETE FROM market_snapshots WHERE captured_at < ?", (cutoff,))
        return cur.rowcount


# ----------------------------------------------------------------- positions & parlays

def open_position(**kwargs: Any) -> str:
    init()
    position_id = uuid.uuid4().hex[:16]
    with transaction() as conn:
        conn.execute(
            """INSERT INTO positions
               (id, user, created_at, mode, parlay_id, game_id, ticker, side, label,
                market_type, contracts, entry_price, stake, model_prob, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (position_id, kwargs["user"], time.time(), kwargs["mode"],
             kwargs.get("parlay_id"), kwargs.get("game_id"), kwargs["ticker"],
             kwargs["side"], kwargs.get("label"), kwargs.get("market_type"),
             kwargs["contracts"], kwargs["entry_price"], kwargs["stake"],
             kwargs.get("model_prob"), kwargs.get("note")))
    return position_id


def close_position(position_id: str, *, exit_price: float, note: str = "") -> Optional[Dict[str, Any]]:
    init()
    with transaction() as conn:
        row = conn.execute("SELECT * FROM positions WHERE id=? AND status='open'",
                           (position_id,)).fetchone()
        if row is None:
            return None
        pnl = (exit_price - row["entry_price"]) * row["contracts"]
        conn.execute(
            "UPDATE positions SET status='closed', closed_at=?, exit_price=?, pnl=?, "
            "note=? WHERE id=?",
            (time.time(), exit_price, pnl, note or row["note"], position_id))
        updated = dict(row)
        updated.update({"status": "closed", "exit_price": exit_price, "pnl": pnl})
        return updated


def positions_for(user: str, *, status: Optional[str] = None) -> List[Dict[str, Any]]:
    init()
    if status:
        return _rows(connection().execute(
            "SELECT * FROM positions WHERE user=? AND status=? ORDER BY created_at DESC",
            (user, status)))
    return _rows(connection().execute(
        "SELECT * FROM positions WHERE user=? ORDER BY created_at DESC", (user,)))


def save_parlay(**kwargs: Any) -> str:
    init()
    parlay_id = uuid.uuid4().hex[:16]
    with transaction() as conn:
        conn.execute(
            """INSERT INTO parlays
               (id, user, created_at, mode, category, legs, leg_count, naive_prob,
                combined_prob, combined_odds, ev_per_dollar, risk_rating, stake)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (parlay_id, kwargs["user"], time.time(), kwargs["mode"], kwargs["category"],
             json.dumps(kwargs["legs"]), kwargs["leg_count"], kwargs.get("naive_prob"),
             kwargs.get("combined_prob"), kwargs.get("combined_odds"),
             kwargs.get("ev_per_dollar"), kwargs.get("risk_rating"),
             kwargs.get("stake", 0.0)))
    return parlay_id


def parlays_for(user: str, *, status: Optional[str] = None) -> List[Dict[str, Any]]:
    init()
    sql = "SELECT * FROM parlays WHERE user=?"
    params: List[Any] = [user]
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY created_at DESC"
    rows = _rows(connection().execute(sql, params))
    for row in rows:
        row["legs"] = json.loads(row["legs"])
    return rows


def settle_parlay(parlay_id: str, *, status: str, payout: float = 0.0) -> None:
    init()
    with transaction() as conn:
        conn.execute("UPDATE parlays SET status=?, settled_at=?, payout=? WHERE id=?",
                     (status, time.time(), payout, parlay_id))


# ----------------------------------------------------------------- bankroll

def get_bankroll(user: str) -> Dict[str, Any]:
    init()
    row = connection().execute("SELECT * FROM bankroll WHERE user=?", (user,)).fetchone()
    if row is None:
        return {"user": user, "starting": settings.DEFAULT_BANKROLL,
                "current": settings.DEFAULT_BANKROLL, "updated_at": time.time(),
                "settings": {}}
    data = dict(row)
    data["settings"] = json.loads(data.get("settings") or "{}")
    return data


def set_bankroll(user: str, *, starting: Optional[float] = None,
                 current: Optional[float] = None,
                 config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    init()
    existing = get_bankroll(user)
    new_starting = starting if starting is not None else existing["starting"]
    new_current = current if current is not None else existing["current"]
    new_config = {**existing.get("settings", {}), **(config or {})}
    with transaction() as conn:
        conn.execute(
            "INSERT INTO bankroll (user, starting, current, updated_at, settings) "
            "VALUES (?,?,?,?,?) ON CONFLICT(user) DO UPDATE SET starting=excluded.starting,"
            " current=excluded.current, updated_at=excluded.updated_at, "
            "settings=excluded.settings",
            (user, new_starting, new_current, time.time(), json.dumps(new_config)))
    return get_bankroll(user)


def stats_snapshot() -> Dict[str, Any]:
    """Row counts, for the health endpoint and the empty-state copy."""
    init()
    conn = connection()
    def count(table: str) -> int:
        return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    return {
        "predictions": count("predictions"),
        "settled_predictions": conn.execute(
            "SELECT COUNT(*) AS n FROM predictions WHERE status='settled'").fetchone()["n"],
        "market_snapshots": count("market_snapshots"),
        "positions": count("positions"),
        "parlays": count("parlays"),
    }
