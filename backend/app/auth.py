"""Accounts and sessions.

Passwords are salted and PBKDF2-hashed; sessions are stateless signed cookies (HMAC of the
username with a per-install secret), so there is no server-side session store to keep in
sync or lose on a restart.

Accounts live in the same SQLite database as everything else. Any pre-existing users.json
from the previous build is migrated once, on first start, so nobody loses their login.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import List, Optional, Tuple

from app.config import settings
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.tracking import store

log = get_logger(__name__)

_SECRET = settings.SITE_SECRET.encode()
PBKDF2_ROUNDS = 200_000
COOKIE_NAME = "alpha_session"


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt),
                               PBKDF2_ROUNDS).hex()


def migrate_legacy_users() -> int:
    """Import accounts from the previous JSON store exactly once."""
    legacy = Path(settings.DATA_DIR) / "users.json"
    if not legacy.exists():
        return 0
    try:
        data = json.loads(legacy.read_text())
    except (json.JSONDecodeError, OSError):
        return 0
    store.init()
    imported = 0
    with store.transaction() as conn:
        for username, record in (data or {}).items():
            if not (record.get("salt") and record.get("hash")):
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO users (username, salt, hash, created_at) "
                "VALUES (?,?,?,?)",
                (username, record["salt"], record["hash"],
                 record.get("created", time.time())))
            imported += cur.rowcount
    if imported:
        legacy.rename(legacy.with_suffix(".json.migrated"))
        log.info("migrated %d account(s) from the previous users.json", imported)
    return imported


def create_user(username: str, password: str) -> Tuple[Optional[str], Optional[str]]:
    name = (username or "").strip().lower()
    if not name.isalnum() or len(name) < 3:
        return None, "username must be at least 3 letters or numbers, with no spaces"
    if len(password or "") < 8:
        return None, "password must be at least 8 characters"
    store.init()
    if get_user(name):
        return None, "that username is taken"
    salt = secrets.token_hex(16)
    with store.transaction() as conn:
        conn.execute("INSERT INTO users (username, salt, hash, created_at) VALUES (?,?,?,?)",
                     (name, salt, _hash(password, salt), time.time()))
    store.set_bankroll(name, starting=settings.DEFAULT_BANKROLL,
                       current=settings.DEFAULT_BANKROLL)
    log.info("created account %s", name)
    return name, None


def get_user(username: str) -> Optional[dict]:
    store.init()
    row = store.connection().execute(
        "SELECT username, salt, hash, created_at FROM users WHERE username=?",
        ((username or "").strip().lower(),)).fetchone()
    return dict(row) if row else None


def verify_user(username: str, password: str) -> bool:
    record = get_user(username)
    if not record:
        # Hash anyway so a missing account and a wrong password take the same time.
        _hash(password or "", secrets.token_hex(16))
        return False
    return hmac.compare_digest(record["hash"], _hash(password or "", record["salt"]))


def all_usernames() -> List[str]:
    store.init()
    return [r["username"] for r in
            store.connection().execute("SELECT username FROM users").fetchall()]


def make_token(username: str) -> str:
    payload = base64.urlsafe_b64encode(username.encode()).decode()
    signature = hmac.new(_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def read_token(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    try:
        payload, signature = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        return base64.urlsafe_b64decode(payload).decode()
    except (ValueError, UnicodeDecodeError):
        return None
