"""Bankroll and exposure management.

Sizing is where a good model goes to die. A 6% edge is real and worth having; betting a
quarter of your bankroll on it is how you go broke holding a winning hand, because the
edge is an estimate and the variance is not.

So every recommendation here is deliberately conservative:

  * FRACTIONAL KELLY, not full Kelly. Full Kelly is optimal only if your probability is
    exactly right, and it never is. A quarter-Kelly stake gives up a little growth for a
    large reduction in the chance of a deep drawdown, and — more importantly — it is
    forgiving of the model being somewhat wrong, which it always is.
  * HARD CAPS the recommendation cannot exceed, regardless of what Kelly says.
  * EXPOSURE LIMITS per game, per team and per day, because five bets on the same game are
    one bet with extra steps.
  * CORRELATION WARNINGS when new exposure lines up with what is already open.

The dashboard says plainly that a high model edge does not guarantee a winning outcome. It
is not a disclaimer bolted on; it is the single most important thing on the page.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from app.config import settings
from app.core.types import Signal
from app.data import teams
from app.strategies import pricing
from app.tracking import store

SIZING_MODES = ("flat", "percentage", "kelly")


@dataclass
class RiskSettings:
    mode: str = "kelly"
    bankroll: float = 0.0
    flat_stake: float = 10.0
    percentage: float = 0.01
    kelly_fraction: float = settings.KELLY_FRACTION
    max_stake_pct: float = settings.MAX_STAKE_PCT
    max_per_game_pct: float = settings.MAX_EXPOSURE_PER_GAME_PCT
    max_per_team_pct: float = settings.MAX_EXPOSURE_PER_TEAM_PCT
    max_daily_pct: float = settings.MAX_DAILY_EXPOSURE_PCT

    @classmethod
    def load(cls, user: str) -> "RiskSettings":
        record = store.get_bankroll(user)
        config = record.get("settings") or {}
        return cls(
            mode=config.get("mode", "kelly"),
            bankroll=float(record.get("current") or settings.DEFAULT_BANKROLL),
            flat_stake=float(config.get("flat_stake", 10.0)),
            percentage=float(config.get("percentage", 0.01)),
            kelly_fraction=float(config.get("kelly_fraction", settings.KELLY_FRACTION)),
            max_stake_pct=float(config.get("max_stake_pct", settings.MAX_STAKE_PCT)),
            max_per_game_pct=float(config.get("max_per_game_pct",
                                             settings.MAX_EXPOSURE_PER_GAME_PCT)),
            max_per_team_pct=float(config.get("max_per_team_pct",
                                             settings.MAX_EXPOSURE_PER_TEAM_PCT)),
            max_daily_pct=float(config.get("max_daily_pct",
                                           settings.MAX_DAILY_EXPOSURE_PCT)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode, "bankroll": round(self.bankroll, 2),
            "flat_stake": self.flat_stake, "percentage": self.percentage,
            "kelly_fraction": self.kelly_fraction,
            "max_stake_pct": self.max_stake_pct,
            "max_per_game_pct": self.max_per_game_pct,
            "max_per_team_pct": self.max_per_team_pct,
            "max_daily_pct": self.max_daily_pct,
            "sizing_modes": list(SIZING_MODES),
        }


@dataclass
class Exposure:
    total: float = 0.0
    by_game: Dict[str, float] = field(default_factory=dict)
    by_team: Dict[str, float] = field(default_factory=dict)
    today: float = 0.0
    open_positions: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"total": round(self.total, 2),
                "today": round(self.today, 2),
                "open_positions": self.open_positions,
                "by_game": {k: round(v, 2) for k, v in sorted(
                    self.by_game.items(), key=lambda kv: -kv[1])},
                "by_team": {k: round(v, 2) for k, v in sorted(
                    self.by_team.items(), key=lambda kv: -kv[1])}}


def current_exposure(user: str) -> Exposure:
    """What is already at risk, from open paper and live positions."""
    exposure = Exposure()
    start_of_day = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    for row in store.positions_for(user, status="open"):
        stake = float(row.get("stake") or 0.0)
        exposure.total += stake
        exposure.open_positions += 1
        game_id = row.get("game_id")
        if game_id:
            exposure.by_game[game_id] = exposure.by_game.get(game_id, 0.0) + stake
            for abbr in _teams_in(game_id):
                exposure.by_team[abbr] = exposure.by_team.get(abbr, 0.0) + stake
        if float(row.get("created_at") or 0) >= start_of_day:
            exposure.today += stake
    return exposure


def _teams_in(game_id: str) -> List[str]:
    """nflverse game ids are SEASON_WEEK_AWAY_HOME."""
    parts = (game_id or "").split("_")
    if len(parts) < 4:
        return []
    return [t for t in (teams.resolve(parts[2]), teams.resolve(parts[3])) if t]


def size_bet(signal: Signal, risk: RiskSettings, exposure: Exposure) -> Dict[str, Any]:
    """A recommended stake, the caps that shaped it, and the warnings that go with it."""
    cost = signal.features.get("cost")
    probability = pricing.published_prob(signal)
    bankroll = max(risk.bankroll, 0.0)

    if not cost or bankroll <= 0:
        return {"stake": 0.0, "reason": "no live price or no bankroll set",
                "capped_by": "unpriced", "warnings": []}

    full_kelly = pricing.kelly_fraction(probability, float(cost))
    if risk.mode == "flat":
        raw = min(risk.flat_stake, bankroll)
        basis = f"flat ${risk.flat_stake:.2f} per bet"
    elif risk.mode == "percentage":
        raw = bankroll * risk.percentage
        basis = f"{risk.percentage:.1%} of bankroll"
    else:
        raw = bankroll * full_kelly * risk.kelly_fraction
        basis = (f"{risk.kelly_fraction:.0%} Kelly (full Kelly would be "
                 f"{full_kelly:.1%} of bankroll)")

    caps: List[tuple] = [("per-bet cap", bankroll * risk.max_stake_pct)]
    game_used = exposure.by_game.get(signal.game_id, 0.0)
    caps.append((f"per-game cap ({signal.game_id})",
                 max(bankroll * risk.max_per_game_pct - game_used, 0.0)))
    for abbr in _teams_in(signal.game_id):
        used = exposure.by_team.get(abbr, 0.0)
        caps.append((f"per-team cap ({teams.display(abbr)})",
                     max(bankroll * risk.max_per_team_pct - used, 0.0)))
    caps.append(("daily cap",
                 max(bankroll * risk.max_daily_pct - exposure.today, 0.0)))

    stake = raw
    capped_by = None
    for name, limit in caps:
        if limit < stake:
            stake = limit
            capped_by = name

    warnings = correlation_warnings(signal, exposure)
    if full_kelly <= 0:
        warnings.append("Kelly says not to bet this at all — the price does not compensate "
                        "for the risk at the model's own probability.")
    if stake <= 0:
        warnings.append("Exposure limits leave no room for this bet right now.")

    # Contracts are indivisible, and on a small bankroll that matters more than the
    # arithmetic suggests. A quarter-Kelly stake of $0.27 against a 30c contract floors to
    # zero contracts and reads as "do not bet", when in fact ONE contract is within a
    # rounding error of the right size. Report the exact figure, the tradeable figure, and
    # how far apart they are, rather than silently truncating to nothing.
    exact_contracts = (stake / float(cost)) if cost else 0.0
    tradeable = int(exact_contracts)
    rounding_note = None
    if tradeable == 0 and exact_contracts > 0:
        if exact_contracts >= 0.5:
            tradeable = 1
            overshoot = (float(cost) / stake) if stake > 0 else float("inf")
            rounding_note = (
                f"One contract costs ${float(cost):.2f}, which is {overshoot:.1f}x the "
                f"${stake:.2f} this edge justifies at your bankroll. A single contract is "
                "the smallest bet available and is close enough to correct; anything more "
                "is over-betting the edge.")
        else:
            rounding_note = (
                f"This edge justifies ${stake:.2f}, and one contract costs "
                f"${float(cost):.2f} — more than twice the right size. The disciplined "
                "answer at this bankroll is to skip it.")
            warnings.append("Smallest tradeable size is more than twice the stake this "
                            "edge justifies.")

    return {
        "stake": round(max(stake, 0.0), 2),
        "uncapped_stake": round(raw, 2),
        "full_kelly_pct": round(full_kelly * 100, 2),
        "recommended_pct_of_bankroll": round((stake / bankroll) * 100, 2) if bankroll else 0.0,
        "exact_contracts": round(exact_contracts, 3),
        "contracts": tradeable,
        "tradeable_cost": round(tradeable * float(cost), 2) if cost else 0.0,
        "rounding_note": rounding_note,
        "basis": basis,
        "capped_by": capped_by,
        "caps": [{"name": n, "limit": round(v, 2)} for n, v in caps],
        "warnings": warnings,
    }


def correlation_warnings(signal: Signal, exposure: Exposure) -> List[str]:
    """Flag new exposure that duplicates risk already on the book."""
    out: List[str] = []
    existing_game = exposure.by_game.get(signal.game_id, 0.0)
    if existing_game > 0:
        out.append(
            f"You already have ${existing_game:.2f} at risk on this game. Additional "
            "positions here are correlated — they tend to win and lose together, so this "
            "is closer to increasing one bet than adding a second.")
    for abbr in _teams_in(signal.game_id):
        if signal.team and abbr != signal.team:
            continue
        used = exposure.by_team.get(abbr, 0.0)
        if used > 0 and used > existing_game:
            out.append(
                f"${used:.2f} of open exposure already depends on {teams.display(abbr)}.")
    return out


def drawdown_report(user: str) -> Dict[str, Any]:
    """Realised performance and worst drawdown across settled positions."""
    positions = [p for p in store.positions_for(user) if p.get("status") == "closed"]
    positions.sort(key=lambda p: p.get("closed_at") or 0)
    record = store.get_bankroll(user)
    starting = float(record.get("starting") or settings.DEFAULT_BANKROLL)

    equity = starting
    curve: List[Dict[str, Any]] = []
    peak = starting
    max_dd = 0.0
    for position in positions:
        equity += float(position.get("pnl") or 0.0)
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
        curve.append({"at": position.get("closed_at"), "equity": round(equity, 2)})

    return {
        "starting_bankroll": round(starting, 2),
        "current_bankroll": round(float(record.get("current") or starting), 2),
        "realised_pnl": round(equity - starting, 2),
        "closed_positions": len(positions),
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd / peak, 4) if peak else 0.0,
        "equity_curve": curve,
    }


def portfolio(user: str) -> Dict[str, Any]:
    """The full risk picture for the portfolio page."""
    risk = RiskSettings.load(user)
    exposure = current_exposure(user)
    bankroll = max(risk.bankroll, 1e-9)
    return {
        "settings": risk.to_dict(),
        "exposure": exposure.to_dict(),
        "limits": {
            "per_bet": round(bankroll * risk.max_stake_pct, 2),
            "per_game": round(bankroll * risk.max_per_game_pct, 2),
            "per_team": round(bankroll * risk.max_per_team_pct, 2),
            "daily_remaining": round(max(bankroll * risk.max_daily_pct - exposure.today, 0), 2),
        },
        "utilisation": {
            "total_pct": round(exposure.total / bankroll, 4),
            "daily_pct": round(exposure.today / bankroll, 4),
        },
        "drawdown": drawdown_report(user),
        "disclaimer": (
            "A high model edge does not guarantee a winning bet. These probabilities are "
            "estimates from a model that is wrong some of the time, and every position "
            "here can lose. Sizing is deliberately conservative for exactly that reason."
        ),
    }
