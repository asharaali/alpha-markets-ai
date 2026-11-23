"""Trade Board cards: a recommendation re-stated with a calibrated hit %, the after-fee edge
that hit % implies, and the evidence state that decides whether it may be sized at all."""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from app.research.calibration import Calibrator
from app.research.evidence import Evidence
from app.risk import fees
from app.strategies.recommendation import Recommendation

BOARD_STRATEGY = "ensemble"     # board cards come from the ensemble's signals
_NO_HISTORY = "No settled history for this kind of bet yet."


def _evidence_dict(e: Optional[Evidence]) -> Dict[str, Any]:
    if e is None:
        return {"state": "WATCHING", "calls": 0, "clusters": 0, "reason": _NO_HISTORY,
                "model_log_loss": None, "market_log_loss": None, "roi_after_fees": None}
    return {"state": e.state, "calls": e.calls, "clusters": e.clusters, "reason": e.reason,
            "model_log_loss": e.model_log_loss, "market_log_loss": e.market_log_loss,
            "roi_after_fees": e.roi_after_fees}


def to_board_card(card: Recommendation, *, evidence: Optional[Evidence],
                  calibrator: Optional[Calibrator], live_allowed: bool) -> Dict[str, Any]:
    price = float(card.cost)
    hit = (calibrator or Calibrator()).apply(card.final_prob, card.market_prob)
    contracts = max(int(card.contracts or 0), 1)
    fee_each = fees.trading_fee(contracts=contracts, price=price) / contracts
    ev = _evidence_dict(evidence)
    series = (card.ticker or "").split("-")[0]
    return {
        "id": card.recommendation_id, "venue": "kalshi", "ticker": card.ticker, "series": series,
        "game_id": card.game_id, "matchup": card.matchup, "kickoff": card.kickoff,
        "selection": card.selection, "side": card.side, "market_type": card.market_type,
        "settlement": card.settlement,
        "price": round(price, 4), "hit": round(hit, 4), "raw_hit": round(card.final_prob, 4),
        "market_prob": card.market_prob,
        "edge_cents": round((hit - price) * 100 - fee_each * 100, 1),
        "max_entry_price": card.max_entry_price, "robust": card.robust,
        "reasons": list(card.supporting), "missing": list(card.missing), "warnings": list(card.warnings),
        "evidence": ev,
        "stake": card.recommended_stake if ev["state"] == "PROVEN" else 0,
        "contracts": card.contracts, "model_version": card.model_version,
        "trade": {"url": f"https://kalshi.com/markets/{series.lower()}", "in_app": bool(live_allowed)},
    }


def assemble(cards: Iterable[Recommendation], *, evidence: Dict, calibrators: Dict,
             live_allowed: bool) -> List[Dict[str, Any]]:
    """Board cards, one per ticker+side (best edge kept), positive edge only, best first."""
    best: Dict[str, Dict[str, Any]] = {}
    for c in cards:
        fam = (BOARD_STRATEGY, c.market_type)
        b = to_board_card(c, evidence=evidence.get(fam), calibrator=calibrators.get(fam),
                          live_allowed=live_allowed)
        key = f"{b['ticker']}|{b['side']}"
        if key not in best or b["edge_cents"] > best[key]["edge_cents"]:
            best[key] = b
    return sorted((b for b in best.values() if b["edge_cents"] > 0),
                  key=lambda b: -b["edge_cents"])
