"""A bet explained the way a person decides: why it wins, how it loses, is the price fair.

The recommendation card already carries every number. This turns them into the three
questions someone actually asks before placing a bet, using only facts the engine computed —
nothing here is a new estimate.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.core.types import MarketType, Side, Signal
from app.data import teams
from app.strategies.base import GameContext
from app.strategies.recommendation import Recommendation


def _room(points: float) -> str:
    if points >= 0:
        return f"{points:.1f} points of room."
    return (f"it needs the game to beat the projection by {-points:.1f} points — this is a "
            "bet on the model being wrong in your favour.")


def _team_margin(ctx: GameContext, team: str) -> float:
    m = ctx.projection.expected_margin
    return m if team == ctx.game.home else -m


def _cushion(signal: Signal, ctx: GameContext) -> Optional[Dict[str, Any]]:
    """How far the projection sits on the winning side of this bet's line, in points."""
    proj = ctx.projection
    side_no = signal.quote is not None and signal.quote.side is Side.NO
    line, team = signal.line, signal.team
    mt = signal.market_type

    if mt is MarketType.MONEYLINE and team:
        x = _team_margin(ctx, team)
        return {"points": x, "text": (
            f"Model has {teams.display(team)} winning by {x:.1f}." if x > 0 else
            f"Model has {teams.display(team)} losing by {-x:.1f} — this is not a likely bet.")}
    if mt is MarketType.SPREAD and team and line is not None:
        x = _team_margin(ctx, team)
        name = teams.display(team)
        lead = (f"Model has {name} winning by {x:.1f}" if x >= 0
                else f"Model has {name} losing by {-x:.1f}")
        if side_no:
            return {"points": line - x, "text": (
                f"{lead}. This bet only loses if {name} wins by {int(line) + 1} or more, "
                f"{_room(line - x)}")}
        return {"points": x - line, "text": (
            f"{lead}. This bet needs {name} by more than {line:g}, "
            f"{_room(x - line)}")}
    if mt is MarketType.TOTAL and line is not None:
        e = proj.expected_total
        if side_no:
            return {"points": line - e, "text": (
                f"Model projects {e:.1f} total points; the under at {line:g}: {_room(line - e)}")}
        return {"points": e - line, "text": (
            f"Model projects {e:.1f} total points; the over at {line:g}: {_room(e - line)}")}
    if mt is MarketType.TEAM_TOTAL and team and line is not None:
        s = proj.home_score if team == ctx.game.home else proj.away_score
        name = teams.display(team)
        if side_no:
            return {"points": line - s, "text": (
                f"Model has {name} scoring {s:.1f}; under {line:g}: {_room(line - s)}")}
        return {"points": s - line, "text": (
            f"Model has {name} scoring {s:.1f}; over {line:g}: {_room(s - line)}")}
    return None


def _backed_team(signal: Signal, ctx: GameContext) -> Optional[str]:
    if signal.market_type not in (MarketType.MONEYLINE, MarketType.SPREAD) or not signal.team:
        return None
    if signal.quote is not None and signal.quote.side is Side.NO:
        return ctx.game.away if signal.team == ctx.game.home else ctx.game.home
    return signal.team


def breakdown(card: Recommendation, signal: Signal,
              ctx: Optional[GameContext]) -> Dict[str, Any]:
    p = card.final_prob
    lose_one_in = (1.0 / (1.0 - p)) if p < 1 else None

    why: List[str] = []
    risks: List[str] = []

    if ctx is not None:
        proj = ctx.projection
        home, away = teams.display(ctx.game.home), teams.display(ctx.game.away)
        why.append(f"Projected score: {away} {proj.away_score:.0f}, "
                   f"{home} {proj.home_score:.0f}.")
        cushion = _cushion(signal, ctx)
        if cushion:
            why.append(cushion["text"])
            if 0 <= cushion["points"] < 3:
                risks.append("Less than a field goal of room — one late score flips it.")
        backed = _backed_team(signal, ctx)
        if backed:
            opponent = ctx.game.away if backed == ctx.game.home else ctx.game.home
            for d in proj.drivers:
                if d.get("edge_to") == backed:
                    why.append(f"{d['label']}: {teams.display(backed)} ahead of "
                               f"{teams.display(opponent)}.")
                    if len(why) >= 5:
                        break
        if ctx.game.div_game:
            risks.append("Divisional game — these run closer than ratings suggest.")
        unknown = (ctx.adjustment.detail or {}).get("unknown_teams") if ctx.adjustment else None
        if unknown:
            risks.append("This week's injury report isn't out yet (it's filed Wed–Fri). "
                         "Re-check before betting — a surprise inactive can move this.")
        if ctx.weather is None and ctx.game.roof not in ("dome", "closed"):
            risks.append("No kickoff weather forecast yet.")

    if card.market_prob is not None:
        if p >= 0.6 and card.market_prob >= 0.55:
            why.append(f"The market agrees it's likely: priced at {card.market_prob:.0%}.")
        elif card.market_prob < p:
            why.append(f"The market prices it at {card.market_prob:.0%}; we have it at "
                       f"{p:.0%}. That gap is the whole case for the bet.")
    disagreement = signal.features.get("disagreement")
    if disagreement and disagreement > 0.06:
        risks.append(f"The strategies disagree by {disagreement:.0%} on this one.")
    if not card.robust:
        risks.append("If the model is 3 points too optimistic, this stops being worth the "
                     "price. Keep the stake small.")
    if p >= 0.5 and lose_one_in:
        risks.insert(0, f"It still loses about 1 time in {lose_one_in:.0f}.")
    elif p >= 0.3:
        risks.insert(0, f"It wins about {p:.0%} of the time — close to a coin flip, so a few "
                        "losses in a row are normal.")
    elif p > 0:
        risks.insert(0, f"It wins only about 1 time in {1.0 / p:.0f}. Expect long losing "
                        "streaks — the value only shows up over many bets.")

    breakeven = card.breakeven_prob
    price_check = (
        f"Costs {card.cost * 100:.0f}¢ to win $1. After Kalshi's fee it needs to hit "
        f"{breakeven:.1%} to break even; we put it at {p:.1%}."
        if breakeven is not None else f"Costs {card.cost * 100:.0f}¢ to win $1.")
    profit = (1.0 - card.cost) / card.cost if card.cost else None
    if profit is not None:
        price_check += f" A win returns {profit:.0%} on the stake."

    return {
        "win_probability": round(p, 4),
        "loses_one_in": round(lose_one_in, 1) if lose_one_in and p >= 0.5 else None,
        "why_it_wins": why,
        "how_it_loses": risks,
        "price_check": price_check,
        "verdict": _verdict(p, card.robust),
    }


def _verdict(p: float, robust: bool) -> str:
    if p >= 0.6:
        return ("Solid: likely, fairly priced, and the edge holds up to model error."
                if robust else
                "Likely and fairly priced, but little margin for error in the price. "
                "A small stake, not a big one.")
    if p >= 0.4:
        return ("Close to a coin flip at a good price. Worth it over many bets, not a lock."
                if robust else
                "Close to a coin flip with a thin edge. Small stake or skip.")
    return ("A longshot that pays well. Most of these lose; only bet money you're fine "
            "losing, and keep the stake tiny." if not robust else
            "A longshot with a real price edge. Most still lose — tiny stakes, many bets.")
