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
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

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


# Additive column migrations, applied in order and idempotently. Every existing row keeps
# its data: SQLite fills new columns with NULL and the readers below treat NULL as "this
# position predates the column", never as zero. Nothing here drops or rewrites a column,
# because the performance page's credibility rests on stored predictions being immutable.
MIGRATIONS: List[Tuple[str, str, str]] = [
    # (table, column, DDL type) — settlement metadata the resolver needs.
    ("positions", "team", "TEXT"),
    ("positions", "line", "REAL"),
    ("positions", "selection", "TEXT"),
    # Order lifecycle and venue reconciliation.
    ("positions", "client_order_id", "TEXT"),
    ("positions", "venue_order_id", "TEXT"),
    ("positions", "requested_contracts", "INTEGER"),
    ("positions", "filled_contracts", "INTEGER"),
    ("positions", "remaining_contracts", "INTEGER"),
    ("positions", "avg_fill_price", "REAL"),
    ("positions", "entry_fees", "REAL"),
    ("positions", "exit_fees", "REAL"),
    ("positions", "closed_contracts", "INTEGER"),
    ("positions", "last_reconciled_at", "REAL"),
    # Provenance: which model said what, and when.
    ("positions", "model_version", "TEXT"),
    ("positions", "predicted_at", "REAL"),
    ("positions", "recommendation_id", "TEXT"),
    ("positions", "max_entry_price", "REAL"),
    ("positions", "settled_at", "REAL"),
    # Predictions gain a stable per-game forecast cluster so repeated hourly refreshes of
    # the same view can be collapsed when scoring.
    ("predictions", "forecast_key", "TEXT"),
    ("predictions", "kickoff_ts", "REAL"),
    ("predictions", "horizon_hours", "REAL"),
    ("predictions", "model_version", "TEXT"),
    ("predictions", "closing_at", "REAL"),
    # Parlays record which product was actually bought.
    ("parlays", "product", "TEXT"),
    ("parlays", "executable", "INTEGER"),
    ("parlays", "pricing_basis", "TEXT"),
    ("parlays", "max_payout", "REAL"),
    ("parlays", "total_fees", "REAL"),
    ("parlays", "standard_error", "REAL"),
]


def _existing_columns(conn: sqlite3.Connection, table: str) -> set:
    try:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _migrate(conn: sqlite3.Connection) -> int:
    applied = 0
    by_table: Dict[str, set] = {}
    for table, column, ddl in MIGRATIONS:
        if table not in by_table:
            by_table[table] = _existing_columns(conn, table)
        if not by_table[table] or column in by_table[table]:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        by_table[table].add(column)
        applied += 1
    return applied


def init() -> None:
    global _initialised
    if _initialised:
        return
    with transaction() as conn:
        conn.executescript(SCHEMA)
        applied = _migrate(conn)
    _initialised = True
    if applied:
        log.info("applied %d column migration(s)", applied)
    log.info("database ready at %s", settings.DB_PATH)


def _rows(cursor: sqlite3.Cursor) -> List[Dict[str, Any]]:
    return [dict(r) for r in cursor.fetchall()]


# ----------------------------------------------------------------- predictions

# A refresh is only worth storing if the view actually moved. Below this, the new row is
# the same opinion at the same price and adds nothing but sample-size inflation.
MATERIAL_PROB_CHANGE = 0.01
MATERIAL_COST_CHANGE = 0.01


def _is_redundant(previous: Any, entry: Dict[str, Any], now: float) -> bool:
    """Would storing this row add evidence, or only add rows?

    Redundant means both: inside the hourly window, OR materially unchanged. An opinion
    that has not moved and a price that has not moved describe the same forecast already on
    file.
    """
    if now - float(previous["created_at"]) < 3600:
        return True

    def moved(a: Any, b: Any, threshold: float) -> bool:
        if a is None or b is None:
            return a is not b
        return abs(float(a) - float(b)) >= threshold

    if moved(previous["fair_prob"], entry.get("fair_prob"), MATERIAL_PROB_CHANGE):
        return False
    if moved(previous["model_prob"], entry.get("model_prob"), MATERIAL_PROB_CHANGE):
        return False
    if moved(previous["cost"], entry.get("cost"), MATERIAL_COST_CHANGE):
        return False
    return True


def _kickoff_ts(kickoff: Any) -> Optional[float]:
    """Kickoff as a POSIX timestamp. Stored so closing-line value has a cutoff to use."""
    if kickoff is None:
        return None
    if isinstance(kickoff, (int, float)):
        return float(kickoff)
    try:
        from datetime import datetime, timezone
        parsed = datetime.fromisoformat(str(kickoff).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, TypeError):
        return None


def _horizon_hours(kickoff: Any, now: float) -> Optional[float]:
    """Hours from now until kickoff. Counts DOWN, so bigger means earlier."""
    if kickoff is None:
        return None
    if isinstance(kickoff, (int, float)):
        return (float(kickoff) - now) / 3600.0
    try:
        from datetime import datetime, timezone
        parsed = datetime.fromisoformat(str(kickoff).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (parsed.timestamp() - now) / 3600.0
    except (ValueError, TypeError):
        return None


def record_predictions(entries: Sequence[Dict[str, Any]]) -> int:
    """Persist predictions before their outcomes exist. Returns the number written.

    Two suppression rules, because the hourly rule alone was not enough. The snapshot job
    runs every hour for the whole week before a game, so one view on one contract produced
    dozens of rows; across a slate that reached 155,058 rows covering 16 games, and the
    performance page counted 134,560 of them as independent settled predictions.

    So a refresh is now written only if it is BOTH outside the hourly window AND materially
    different from the last stored row — a probability or price move of at least a cent.
    An unchanged opinion at an unchanged price is not new evidence and no longer pretends
    to be.

    The forecast's distance from kickoff is stored alongside, so evaluation can ask what
    the model believed 24 hours out rather than averaging every horizon together.
    """
    if not entries:
        return 0
    init()
    written = 0
    now = time.time()
    with transaction() as conn:
        for entry in entries:
            previous = conn.execute(
                "SELECT created_at, fair_prob, model_prob, cost FROM predictions "
                "WHERE game_id=? AND strategy=? AND IFNULL(ticker,'')=? AND selection=? "
                "ORDER BY created_at DESC LIMIT 1",
                (entry["game_id"], entry["strategy"], entry.get("ticker") or "",
                 entry["selection"]),
            ).fetchone()
            if previous is not None and _is_redundant(previous, entry, now):
                continue
            conn.execute(
                """INSERT OR IGNORE INTO predictions
                   (id, created_at, season, week, game_id, kickoff, strategy, market_type,
                    ticker, selection, label, team, player, line, model_prob, fair_prob,
                    market_prob, edge, ev_per_dollar, confidence, cost, depth_usd,
                    reasoning, forecast_key, kickoff_ts, horizon_hours, model_version)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex[:16], now, entry["season"], entry["week"],
                 entry["game_id"], entry.get("kickoff"), entry["strategy"],
                 entry["market_type"], entry.get("ticker"), entry["selection"],
                 entry.get("label"), entry.get("team"), entry.get("player"),
                 entry.get("line"), entry["model_prob"], entry.get("fair_prob"),
                 entry.get("market_prob"), entry.get("edge"),
                 entry.get("ev_per_dollar"), entry.get("confidence"),
                 entry.get("cost"), entry.get("depth_usd"),
                 json.dumps(entry.get("reasoning") or []),
                 "|".join(str(entry.get(p) or "") for p in
                          ("game_id", "strategy", "market_type", "selection")),
                 _kickoff_ts(entry.get("kickoff")),
                 _horizon_hours(entry.get("kickoff"), now),
                 entry.get("model_version")),
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

POSITION_COLUMNS = (
    "id", "user", "created_at", "mode", "parlay_id", "game_id", "ticker", "side",
    "label", "market_type", "contracts", "entry_price", "stake", "model_prob", "note",
    "status", "team", "line", "selection", "client_order_id", "venue_order_id",
    "requested_contracts", "filled_contracts", "remaining_contracts", "avg_fill_price",
    "entry_fees", "model_version", "predicted_at", "recommendation_id",
    "max_entry_price",
)


def open_position(**kwargs: Any) -> str:
    """Record a position from the fills behind it, with the metadata to settle it later.

    `contracts` is always the FILLED count. Requested and remaining are stored alongside
    so a partial fill is visible as a partial fill rather than quietly looking like the
    order the user asked for.
    """
    init()
    position_id = uuid.uuid4().hex[:16]
    values = {
        "id": position_id,
        "created_at": time.time(),
        "status": kwargs.get("status") or "open",
        **{k: kwargs.get(k) for k in POSITION_COLUMNS
           if k not in ("id", "created_at", "status")},
    }
    columns = ", ".join(POSITION_COLUMNS)
    marks = ", ".join("?" for _ in POSITION_COLUMNS)
    with transaction() as conn:
        conn.execute(f"INSERT INTO positions ({columns}) VALUES ({marks})",
                     tuple(values[c] for c in POSITION_COLUMNS))
    return position_id


def staked_since(user: str, *, since: float, mode: Optional[str] = None) -> float:
    """Total staked by this user since `since`, optionally restricted to one mode.

    Read inside the same connection the limit check runs in. Paper and live are counted
    separately by default because a simulated stake is not real exposure and must not
    consume a real-money cap.
    """
    init()
    if mode:
        row = connection().execute(
            "SELECT COALESCE(SUM(stake), 0) FROM positions "
            "WHERE user=? AND mode=? AND created_at > ? AND status != 'rejected'",
            (user, mode, since)).fetchone()
    else:
        row = connection().execute(
            "SELECT COALESCE(SUM(stake), 0) FROM positions "
            "WHERE user=? AND created_at > ? AND status != 'rejected'",
            (user, since)).fetchone()
    return float(row[0] or 0.0)


def mark_reconciled(position_id: str) -> None:
    init()
    with transaction() as conn:
        conn.execute("UPDATE positions SET last_reconciled_at=? WHERE id=?",
                     (time.time(), position_id))


def _append_note(previous: Optional[str], addition: str) -> str:
    """Notes accumulate; they never overwrite.

    The note column is the audit trail. Replacing it on settlement destroyed the record of
    a metadata correction made minutes earlier, which is exactly the history an audit trail
    exists to keep.
    """
    previous = (previous or "").strip()
    addition = (addition or "").strip()
    if not addition:
        return previous
    if not previous:
        return addition
    if addition in previous:
        return previous
    return f"{previous} | {addition}"


def _open_contracts(row: Any) -> int:
    """How many contracts are still held: filled minus already closed."""
    filled = row["filled_contracts"] if row["filled_contracts"] is not None else row["contracts"]
    closed = row["closed_contracts"] or 0
    return max(0, int(filled) - int(closed))


def close_position(position_id: str, *, exit_price: float, note: str = "",
                   contracts: Optional[int] = None,
                   exit_fee: float = 0.0) -> Optional[Dict[str, Any]]:
    """Sell some or all of a position at `exit_price`, net of fees.

    Partial closes are first-class. `contracts=None` means "everything still open"; a
    smaller number leaves the remainder held and marks the row `partially_closed`, so the
    residual keeps its entry price and continues to appear in exposure. The old version
    always closed the whole row, which silently destroyed the remainder.

    P&L is computed from the price actually traded and the fees actually paid, on both
    legs. Fees are never netted out of the entry price, because the entry price has to
    keep matching the fill it came from.
    """
    init()
    with transaction() as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE id=? AND status IN "
            "('open','partially_closed','filled','partially_filled')",
            (position_id,)).fetchone()
        if row is None:
            return None

        available = _open_contracts(row)
        if available <= 0:
            return None
        qty = available if contracts is None else max(0, min(int(contracts), available))
        if qty <= 0:
            return None

        entry = float(row["entry_price"])
        realised = (exit_price - entry) * qty - float(exit_fee or 0.0)
        # The entry fee is charged once, at open, so it is attributed to the first close
        # in proportion to the share of the position being retired.
        entry_fee_total = float(row["entry_fees"] or 0.0)
        filled = row["filled_contracts"] if row["filled_contracts"] is not None else row["contracts"]
        entry_fee_share = entry_fee_total * (qty / filled) if filled else 0.0
        realised -= entry_fee_share

        already_closed = int(row["closed_contracts"] or 0)
        now_closed = already_closed + qty
        remaining = int(filled) - now_closed
        status = "closed" if remaining <= 0 else "partially_closed"
        pnl = float(row["pnl"] or 0.0) + realised

        conn.execute(
            "UPDATE positions SET status=?, closed_at=?, exit_price=?, pnl=?, note=?, "
            "closed_contracts=?, exit_fees=? WHERE id=?",
            (status, time.time(), exit_price, pnl,
             _append_note(row["note"], note), now_closed,
             float(row["exit_fees"] or 0.0) + float(exit_fee or 0.0), position_id))

        updated = dict(row)
        updated.update({"status": status, "exit_price": exit_price, "pnl": pnl,
                        "closed_contracts": now_closed, "remaining_open": remaining,
                        "realised_this_close": realised, "contracts_closed": qty})
        return updated


def settle_position(position_id: str, *, won: bool, note: str = "") -> Optional[Dict[str, Any]]:
    """Settle a position at expiry: $1 per winning contract, $0 per loser.

    Kalshi charges no fee at settlement, so only the entry fee reduces the result. Marked
    `settled` rather than `closed` so the interface can tell "the game finished" apart
    from "I sold out early", which are different events with different lessons.
    """
    init()
    with transaction() as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE id=? AND status IN "
            "('open','partially_closed','filled','partially_filled')",
            (position_id,)).fetchone()
        if row is None:
            return None
        qty = _open_contracts(row)
        if qty <= 0:
            return None

        entry = float(row["entry_price"])
        value = 1.0 if won else 0.0
        filled = row["filled_contracts"] if row["filled_contracts"] is not None else row["contracts"]
        entry_fee_total = float(row["entry_fees"] or 0.0)
        entry_fee_share = entry_fee_total * (qty / filled) if filled else 0.0
        realised = (value - entry) * qty - entry_fee_share
        pnl = float(row["pnl"] or 0.0) + realised

        conn.execute(
            "UPDATE positions SET status='settled', closed_at=?, settled_at=?, "
            "exit_price=?, pnl=?, note=?, closed_contracts=? WHERE id=?",
            (time.time(), time.time(), value, pnl,
             _append_note(row["note"], note), int(filled), position_id))
        updated = dict(row)
        updated.update({"status": "settled", "exit_price": value, "pnl": pnl,
                        "contracts_settled": qty})
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
    """Record a multi-leg position, tagged with WHICH PRODUCT was bought.

    `product` is stored because a basket of singles and a combination contract pay
    differently, and a record that does not say which one this was cannot be reconciled
    later. `pricing_basis` records whether the price came from a real book or a
    calculation.
    """
    init()
    parlay_id = uuid.uuid4().hex[:16]
    with transaction() as conn:
        conn.execute(
            """INSERT INTO parlays
               (id, user, created_at, mode, category, legs, leg_count, naive_prob,
                combined_prob, combined_odds, ev_per_dollar, risk_rating, stake,
                product, executable, pricing_basis, max_payout, total_fees,
                standard_error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (parlay_id, kwargs["user"], time.time(), kwargs["mode"], kwargs["category"],
             json.dumps(kwargs["legs"]), kwargs["leg_count"], kwargs.get("naive_prob"),
             kwargs.get("combined_prob"), kwargs.get("combined_odds"),
             kwargs.get("ev_per_dollar"), kwargs.get("risk_rating"),
             kwargs.get("stake", 0.0),
             kwargs.get("product", "basket_of_singles"),
             1 if kwargs.get("executable", True) else 0,
             kwargs.get("pricing_basis", "live_order_books"),
             kwargs.get("max_payout"), kwargs.get("total_fees"),
             kwargs.get("standard_error")))
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
    """Counts for the health endpoint and the empty-state copy.

    Both the row count AND the game count, because only one of them is evidence. The
    header used to read "155,266 predictions recorded, 134,560 settled", which sounds like
    a large track record and is in fact 16 games observed hourly for a week.
    """
    init()
    conn = connection()

    def count(table: str) -> int:
        return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]

    def scalar(sql: str) -> int:
        return conn.execute(sql).fetchone()["n"]

    return {
        "predictions": count("predictions"),
        "settled_predictions": scalar(
            "SELECT COUNT(*) AS n FROM predictions WHERE status='settled'"),
        # The honest denominators.
        "games_forecast": scalar(
            "SELECT COUNT(DISTINCT game_id) AS n FROM predictions"),
        "games_settled": scalar(
            "SELECT COUNT(DISTINCT game_id) AS n FROM predictions WHERE status='settled'"),
        "distinct_forecasts": scalar(
            "SELECT COUNT(*) AS n FROM (SELECT DISTINCT game_id, strategy, market_type, "
            "selection FROM predictions)"),
        "market_snapshots": count("market_snapshots"),
        "positions": count("positions"),
        "parlays": count("parlays"),
        "counting_note": (
            "Rows are contracts observed repeatedly; games are the independent unit of "
            "evidence. Quote the game count."),
    }
