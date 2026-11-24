"""Calibrated hit %.

A family's raw probability is replaced by what its calls in the same 10-point band have
actually hit, shrunk toward the market price by SHRINK_K pseudo-observations. With no
history the market price is the honest answer; with a lot of history the family's own
record dominates. The NFL model's live record (2026-11-23) is the reason this exists: its
10-20% calls hit 3-9%.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional

from app.evaluation import protocol

SHRINK_K = 30
BINS = 10


def _prob(row: Dict[str, Any]) -> Optional[float]:
    p = row.get("fair_prob")
    if p is None:
        p = row.get("model_prob")
    return None if p is None else float(p)


def _bin(p: float) -> int:
    return min(int(p * BINS), BINS - 1)


class Calibrator:
    def __init__(self) -> None:
        self.n = [0] * BINS
        self.hits = [0] * BINS
        self.psum = [0.0] * BINS

    def fit(self, rows: Iterable[Dict[str, Any]]) -> "Calibrator":
        for r in protocol.deduplicate(rows):
            p, outcome = _prob(r), r.get("outcome")
            if p is None or outcome is None:
                continue
            b = _bin(p)
            self.n[b] += 1
            self.hits[b] += int(outcome)
            self.psum[b] += p
        return self

    def apply(self, p: float, market_prob: Optional[float]) -> float:
        b = _bin(p)
        anchor = market_prob if market_prob is not None else p
        n = self.n[b]
        observed = self.hits[b] / n if n else 0.0
        return (n * observed + SHRINK_K * anchor) / (n + SHRINK_K)

    def to_state(self) -> Dict[str, List]:
        return {"n": self.n, "hits": self.hits, "psum": self.psum}

    @classmethod
    def from_state(cls, state: Dict[str, List]) -> "Calibrator":
        c = cls()
        c.n, c.hits, c.psum = list(state["n"]), list(state["hits"]), [float(x) for x in state["psum"]]
        return c

    def table(self) -> List[Dict[str, Any]]:
        return [{"bucket": f"{b * 10}-{b * 10 + 10}%", "n": self.n[b],
                 "predicted": round(self.psum[b] / self.n[b], 4),
                 "observed": round(self.hits[b] / self.n[b], 4)}
                for b in range(BINS) if self.n[b]]


_CACHE: Dict[str, Any] = {"at": 0.0, "value": None}
CACHE_SECONDS = 1800


def calibrators(*, force: bool = False):
    """One Calibrator per family, from the same grouping the evidence gate uses."""
    from app.research import evidence
    if not force and _CACHE["value"] is not None and time.time() - _CACHE["at"] < CACHE_SECONDS:
        return _CACHE["value"]
    value = {fam: Calibrator().fit(rows) for fam, rows in evidence.group_settled(force=force).items()}
    _CACHE.update(at=time.time(), value=value)
    return value
