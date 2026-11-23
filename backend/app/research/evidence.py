"""The evidence gate.

A signal family (strategy x market type) earns PROVEN only by beating the market price, on
log loss, over enough independent games that the difference cannot be luck, and by making
money after fees doing it. FAILING is the mirror image: significantly worse than the price.
Everything else is WATCHING, which the board shows but never sizes.

The numbers are deliberately strict and live in config so the Research page can print them.
They are never lowered to put something on the board: "nothing is proven" is an answer.
"""
from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple

from app.config import settings
from app.evaluation import protocol
from app.risk import fees

Family = Tuple[str, str]
State = Literal["PROVEN", "WATCHING", "FAILING"]
_EPS = 1e-4


def family_key(row: Dict[str, Any]) -> Family:
    return (str(row.get("strategy") or ""), str(row.get("market_type") or ""))


def _log_loss(p: float, outcome: int) -> float:
    p = min(max(float(p), _EPS), 1 - _EPS)
    return -math.log(p if outcome else 1 - p)


def _prob(row: Dict[str, Any]) -> Optional[float]:
    p = row.get("fair_prob")
    return float(p) if p is not None else (float(row["model_prob"]) if row.get("model_prob") is not None else None)


@dataclass
class Evidence:
    family: Family
    state: State
    calls: int
    clusters: int
    model_log_loss: Optional[float]
    market_log_loss: Optional[float]
    difference: Optional[float]
    ci95: Optional[Tuple[float, float]]
    roi_after_fees: Optional[float]
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["strategy"], d["market_type"] = self.family
        d["family"] = list(self.family)
        return d


def _roi_after_fees(rows: List[Dict[str, Any]]) -> Optional[float]:
    """Flat $1 on every call the family said had value, net of the taker fee."""
    staked = returned = 0.0
    for r in rows:
        cost, ev = r.get("cost"), r.get("ev_per_dollar")
        if cost is None or ev is None or not (0 < float(cost) < 1) or float(ev) <= 0:
            continue
        cost = float(cost)
        staked += 1.0
        returned -= fees.fee_per_contract(cost) / cost      # $1 buys 1/cost contracts
        if r.get("outcome"):
            returned += 1.0 / cost
    return None if staked == 0 else round((returned - staked) / staked, 4)


def assess(rows: Iterable[Dict[str, Any]]) -> Evidence:
    """One family's settled rows → its evidence state. Refreshes are collapsed first."""
    rows = list(rows)
    family = family_key(rows[0]) if rows else ("", "")
    deduped = [r for r in protocol.deduplicate(rows)
               if r.get("outcome") is not None and _prob(r) is not None
               and r.get("market_prob") is not None]
    for r in deduped:
        r["_ll_model"] = _log_loss(_prob(r), int(r["outcome"]))
        r["_ll_market"] = _log_loss(float(r["market_prob"]), int(r["outcome"]))
    calls = len(deduped)
    n_clusters = len({protocol.cluster_key(r) for r in deduped})
    paired = protocol.paired_difference(deduped, "_ll_model", "_ll_market") if deduped else None
    ci = tuple(paired["ci95"]) if paired and "ci95" in paired else None
    roi = _roi_after_fees(deduped)

    def mean(col):
        return round(sum(r[col] for r in deduped) / calls, 4) if calls else None

    min_calls, min_clusters = settings.EVIDENCE_MIN_CALLS, settings.EVIDENCE_MIN_CLUSTERS
    if ci and ci[0] > 0:
        state, reason = "FAILING", "Significantly worse than the market price."
    elif n_clusters < min_clusters:
        state, reason = "WATCHING", f"Only {n_clusters} games settled (needs {min_clusters})."
    elif calls < min_calls:
        state, reason = "WATCHING", f"Only {calls} calls (needs {min_calls})."
    elif not ci or ci[1] >= 0:
        state, reason = "WATCHING", "Not distinguishable from the market yet."
    elif roi is None or roi <= 0:
        state, reason = "WATCHING", "Beats the market but loses money after fees."
    else:
        state, reason = "PROVEN", f"Beats the market on {n_clusters} games."
    return Evidence(family=family, state=state, calls=calls, clusters=n_clusters,
                    model_log_loss=mean("_ll_model"), market_log_loss=mean("_ll_market"),
                    difference=round(paired["difference"], 5) if paired else None,
                    ci95=(round(ci[0], 5), round(ci[1], 5)) if ci else None,
                    roi_after_fees=roi, reason=reason)


def group_settled() -> Dict[Family, List[Dict[str, Any]]]:
    from app.tracking import store
    groups: Dict[Family, List[Dict[str, Any]]] = defaultdict(list)
    for row in store.iter_settled_predictions():
        groups[family_key(row)].append(row)
    return groups


_CACHE: Dict[str, Any] = {"at": 0.0, "value": None}
CACHE_SECONDS = 1800


def assess_all(*, force: bool = False) -> Dict[Family, Evidence]:
    if not force and _CACHE["value"] is not None and time.time() - _CACHE["at"] < CACHE_SECONDS:
        return _CACHE["value"]
    value = {fam: assess(rows) for fam, rows in group_settled().items()}
    _CACHE.update(at=time.time(), value=value)
    return value


def thresholds() -> Dict[str, int]:
    return {"min_calls": settings.EVIDENCE_MIN_CALLS, "min_clusters": settings.EVIDENCE_MIN_CLUSTERS}
