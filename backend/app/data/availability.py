"""Whether we actually know something, and how old that knowledge is.

The distinction this module exists to enforce: ABSENT DATA IS NOT GOOD NEWS.

`injuries.get(team, [])` returns an empty list both when a team has filed a clean injury
report and when the feed failed, the file has not been published yet, or the team's name
did not match. Those are opposite situations and the model treated them identically — a
team whose data was missing was projected as fully healthy, with full confidence, and the
recommendation card said nothing about it.

The same applies to depth charts, weather and line history. Every one of them has a
"nothing came back" case that silently became "nothing to worry about".

So a source is one of three states, never two:

    FRESH     data arrived, recently enough to trust
    STALE     data arrived, but long enough ago that it may have moved
    UNKNOWN   nothing arrived; the model must widen rather than assume

UNKNOWN adds uncertainty instead of removing it, and surfaces on the card as missing
information rather than being invisible.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

FRESH = "fresh"
STALE = "stale"
UNKNOWN = "unknown"

# How old each source may be before it stops being trustworthy. Injury reports move daily
# in the week before a game and hourly on game day; depth charts move slowly; weather
# forecasts converge as kickoff approaches.
MAX_AGE_SECONDS: Dict[str, float] = {
    "injuries": 12 * 3600,
    "depth_chart": 3 * 24 * 3600,
    "weather": 6 * 3600,
    "line_history": 2 * 3600,
    "book_consensus": 30 * 60,
}


@dataclass
class SourceState:
    """One data source for one game: did it arrive, when, and can it be relied on."""

    name: str
    status: str
    fetched_at: Optional[float] = None
    rows: int = 0
    detail: str = ""

    @property
    def age_seconds(self) -> Optional[float]:
        return None if self.fetched_at is None else time.time() - self.fetched_at

    @property
    def usable(self) -> bool:
        return self.status in (FRESH, STALE)

    def to_dict(self) -> Dict[str, Any]:
        age = self.age_seconds
        return {
            "source": self.name,
            "status": self.status,
            "rows": self.rows,
            "age_seconds": round(age, 1) if age is not None else None,
            "age_text": _age_text(age) if age is not None else "never fetched",
            "detail": self.detail,
        }


def _age_text(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    if seconds < 172800:
        return f"{seconds / 3600:.0f}h ago"
    return f"{seconds / 86400:.0f}d ago"


def classify(name: str, *, present: bool, fetched_at: Optional[float] = None,
             rows: int = 0, detail: str = "") -> SourceState:
    """Turn a fetch result into one of the three states.

    `present` must mean "the fetch succeeded", NOT "the fetch returned rows". A team with
    no injuries files an empty report, which is real information; a failed fetch also
    returns nothing, which is not. Only the caller knows which happened, so the caller has
    to say.
    """
    if not present:
        return SourceState(name=name, status=UNKNOWN, rows=0,
                           detail=detail or "no data returned for this game")
    if fetched_at is None:
        return SourceState(name=name, status=STALE, rows=rows,
                           detail=detail or "age unknown")
    age = time.time() - fetched_at
    limit = MAX_AGE_SECONDS.get(name, 24 * 3600)
    return SourceState(name=name, status=FRESH if age <= limit else STALE,
                       fetched_at=fetched_at, rows=rows, detail=detail)


@dataclass
class DataHealth:
    """Every source behind one game's projection."""

    sources: Dict[str, SourceState] = field(default_factory=dict)

    def add(self, state: SourceState) -> None:
        self.sources[state.name] = state

    def status_of(self, name: str) -> str:
        state = self.sources.get(name)
        return state.status if state else UNKNOWN

    def is_known(self, name: str) -> bool:
        return self.status_of(name) != UNKNOWN

    def missing(self) -> List[str]:
        return sorted(n for n, s in self.sources.items() if s.status == UNKNOWN)

    def stale(self) -> List[str]:
        return sorted(n for n, s in self.sources.items() if s.status == STALE)

    def missing_notes(self) -> List[str]:
        """What the model was NOT told, phrased for the recommendation card."""
        notes: List[str] = []
        for name in self.missing():
            notes.append(MISSING_TEXT.get(
                name, f"No {name.replace('_', ' ')} data for this game."))
        for name in self.stale():
            state = self.sources[name]
            notes.append(
                f"{name.replace('_', ' ').capitalize()} data is {state.to_dict()['age_text']} "
                "and may have moved since.")
        return notes

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sources": [s.to_dict() for s in self.sources.values()],
            "missing": self.missing(),
            "stale": self.stale(),
            "all_fresh": not self.missing() and not self.stale(),
            "notes": self.missing_notes(),
        }


MISSING_TEXT = {
    "injuries": ("No injury report was available for this game. The projection assumes "
                 "nothing about availability — it is not assuming both teams are "
                 "healthy — and carries extra uncertainty to reflect that."),
    "depth_chart": ("No depth chart was available, so any injury on the report could not "
                    "be checked against whether that player starts."),
    "weather": ("No kickoff forecast was available. The total is projected without "
                "wind or temperature effects."),
    "line_history": "No line-movement history yet, so no steam or reverse-move signal.",
    "book_consensus": "No sportsbook consensus available to cross-check the Kalshi price.",
}

# Extra margin uncertainty, in points, added when a source is UNKNOWN. Not a guess about
# the world — a statement that the model is less sure than it would otherwise claim, so it
# stops reporting confident edges on games it is under-informed about.
UNKNOWN_SIGMA_POINTS = {
    "injuries": 2.0,
    "depth_chart": 0.5,
    "weather": 0.8,
}


def uncertainty_for(health: DataHealth) -> float:
    """Points of margin sigma to add because of what we do not know.

    Added in quadrature: two unknowns do not make a game twice as unknowable.
    """
    total = 0.0
    for name in health.missing():
        total += UNKNOWN_SIGMA_POINTS.get(name, 0.0) ** 2
    return total ** 0.5
