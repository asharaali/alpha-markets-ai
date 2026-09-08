"""Typed error taxonomy.

Callers need to tell "the upstream feed is down" apart from "you asked for a game that
doesn't exist" apart from "our own math broke" — the old code raised bare Exception and
swallowed all three identically, which is how an empty board and a crashed model looked
the same in the UI.
"""
from __future__ import annotations


class AlphaError(Exception):
    """Base for every error this application raises deliberately."""

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, detail: str | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        out = {"error": self.code, "message": self.message}
        if self.detail:
            out["detail"] = self.detail
        return out


class UpstreamError(AlphaError):
    """A third-party feed (Kalshi, nflverse, Odds API, weather) failed or misbehaved."""

    status_code = 502
    code = "upstream_unavailable"


class RateLimited(UpstreamError):
    """A third-party feed told us to slow down."""

    status_code = 429
    code = "upstream_rate_limited"


class Unauthorized(AlphaError):
    """The caller is not logged in, or is not the account this action belongs to."""

    status_code = 401
    code = "unauthorized"


class NotFound(AlphaError):
    status_code = 404
    code = "not_found"


class ValidationError(AlphaError):
    status_code = 400
    code = "invalid_request"


class ConfigError(AlphaError):
    """A required credential or setting is missing. Surfaced, never guessed around."""

    status_code = 503
    code = "not_configured"


class InsufficientData(AlphaError):
    """We can compute the shape of an answer but not honestly enough to publish it."""

    status_code = 409
    code = "insufficient_data"
