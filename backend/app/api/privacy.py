"""Which API routes may be reached without a session.

Allowlist, not denylist: the terminal is a private, single-user tool, so every /api/ path
is private unless it is named here exactly. A route added later is private by default.
"""
from __future__ import annotations

PUBLIC_API_PATHS: frozenset[str] = frozenset(
    {"/api/health", "/api/login", "/api/logout", "/api/me"})

# Machine routes: no session, but NOT public. Each one checks its own bearer token and answers
# 404 when that token is not configured. Kept separate from PUBLIC_API_PATHS on purpose so the
# "exactly four public paths" guarantee stays literally true.
MACHINE_API_PATHS: frozenset[str] = frozenset({"/api/pnl/push"})

UNAUTHORIZED_MESSAGE = "Log in to use the terminal."


def is_public(path: str) -> bool:
    return path in PUBLIC_API_PATHS


def skips_session(path: str) -> bool:
    """Reachable without a session: public paths plus token-guarded machine routes."""
    return path in PUBLIC_API_PATHS or path in MACHINE_API_PATHS
