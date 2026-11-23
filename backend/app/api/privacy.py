"""Which API routes may be reached without a session.

Allowlist, not denylist: the terminal is a private, single-user tool, so every /api/ path
is private unless it is named here exactly. A route added later is private by default.
"""
from __future__ import annotations

PUBLIC_API_PATHS: frozenset[str] = frozenset(
    {"/api/health", "/api/login", "/api/logout", "/api/me"})

UNAUTHORIZED_MESSAGE = "Log in to use the terminal."


def is_public(path: str) -> bool:
    return path in PUBLIC_API_PATHS
