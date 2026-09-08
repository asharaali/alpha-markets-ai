"""Shared request helpers: who is asking, and turning errors into clean JSON."""
from __future__ import annotations

from typing import Optional

from fastapi import Request, Response

from app import auth
from app.config import settings
from app.core.errors import AlphaError


def current_user(request: Request) -> Optional[str]:
    return getattr(request.state, "user", None)


def require_user(request: Request) -> str:
    user = current_user(request)
    if not user:
        from app.core.errors import Unauthorized
        raise Unauthorized("Log in to use portfolio, bankroll and trading features.")
    return user


def set_session(response: Response, username: str, request: Request) -> None:
    """Mark the cookie Secure only over HTTPS, so it also works on a local http dev server."""
    https = (request.headers.get("x-forwarded-proto", "").startswith("https")
             or request.url.scheme == "https")
    response.set_cookie(auth.COOKIE_NAME, auth.make_token(username),
                        max_age=60 * 60 * 24 * settings.SESSION_DAYS,
                        httponly=True, samesite="lax", secure=https, path="/")


def clear_session(response: Response) -> None:
    response.delete_cookie(auth.COOKIE_NAME, path="/")
