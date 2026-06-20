"""
Multi-user accounts + login. Each user gets isolated data (own bet log + own ntfy topic).

Passwords are salted + PBKDF2-hashed (never stored in plaintext). Sessions are stateless
signed cookies (HMAC of the username with SITE_SECRET) — tamper-proof, no server session store.
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

from app.config import settings

_USERS = Path(settings.DATA_DIR) / "users.json"
_USERS.parent.mkdir(parents=True, exist_ok=True)
_SECRET = settings.SITE_SECRET.encode()


def _load() -> dict:
    if _USERS.exists():
        try:
            return json.loads(_USERS.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save(users: dict) -> None:
    _USERS.write_text(json.dumps(users, indent=2))


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 100_000).hex()


def create_user(username: str, password: str):
    username = (username or "").strip().lower()
    if not username.isalnum() or len(username) < 3:
        return None, "username must be 3+ letters/numbers, no spaces"
    if len(password or "") < 6:
        return None, "password must be at least 6 characters"
    users = _load()
    if username in users:
        return None, "that username is taken"
    salt = secrets.token_hex(16)
    users[username] = {
        "salt": salt,
        "hash": _hash(password, salt),
        "ntfy_topic": f"alpha-{username}-{secrets.token_hex(3)}",
        "created": time.time(),
    }
    _save(users)
    return username, None


def verify_user(username: str, password: str) -> bool:
    username = (username or "").strip().lower()
    u = _load().get(username)
    if not u:
        return False
    return hmac.compare_digest(u["hash"], _hash(password, u["salt"]))


def get_user(username: str):
    return _load().get((username or "").strip().lower())


def all_usernames():
    return list(_load().keys())


# ---- session tokens ----
def make_token(username: str) -> str:
    msg = base64.urlsafe_b64encode(username.encode()).decode()
    sig = hmac.new(_SECRET, msg.encode(), hashlib.sha256).hexdigest()
    return f"{msg}.{sig}"


def read_token(token: str):
    try:
        msg, sig = token.split(".", 1)
        good = hmac.new(_SECRET, msg.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return None
        return base64.urlsafe_b64decode(msg).decode()
    except Exception:
        return None
