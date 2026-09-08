"""Matchup model — unit-versus-unit edges the overall rating averages away.

The core model compares two teams on aggregate efficiency. That is the right default, but
it hides the thing coaches actually plan around: a team whose entire offence runs through
the pass meeting a defence that cannot cover, or a front that generates pressure against a
line that cannot hold it.

So this strategy rebuilds the expected margin from PHASE-WEIGHTED matchups — each team's
passing and rushing efficiency against the specific defence it faces, weighted by how much
that team actually leans on each phase in neutral game script — plus a pass-rush term. It
then prices the same markets from that alternative margin.

It is a genuinely independent read, which is the only reason it is worth ensembling. Where
it agrees with the core model, the ensemble gains little; where it disagrees, that
disagreement is real information about whether an edge is robust or an artefact of
averaging.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from app.core.types import Confidence, MarketType, Signal, StrategyMeta
from app.data import teams
from app.models import distributions as dist
from app.models.calibration import GameModelArtifact
from app.strategies import pricing
from app.strategies.base import GameContext, confidence_from, liquidity_ok

META = StrategyMeta(
    key="matchup",
    name="Matchup",
    market_types=[MarketType.MONEYLINE, MarketType.SPREAD],
    methodology=(
        "Rebuilds expected margin from phase-specific matchups — each offence's passing and "
        "rushing efficiency against that particular defence, weighted by the offence's "
        "neutral-script pass rate — plus a pass-rush versus pass-protection term. Converted "
        "to points using the same fitted EPA coefficient as the core model, then blended "
        "with it so the matchup view tilts rather than replaces the projection."
    ),
    inputs=["opponent-adjusted passing and rushing EPA ratings",
            "neutral-script pass rate", "pressure and sack rates allowed and generated"],
    limitations=(
        "Public play-by-play carries no coverage scheme, personnel groupings or charted "
        "pressures, so 'matchup' here means phase-level efficiency, not the film. It cannot "
        "see that a specific cornerback travels with a specific receiver."
    ),
)

# How much the matchup view is allowed to move the projection. The phase split is real
# signal but a noisier estimate than the aggregate, so it tilts rather than overrides.
MATCHUP_BLEND = 0.45
# Points of margin per unit of pass-rush mismatch (pressure generated vs allowed).
PRESSURE_POINTS = 6.0


def _phase_edge(ctx: GameContext, team: str, opponent: str, home: bool) -> Dict[str, float]:
    """Expected EPA/play for `team` against `opponent`, split by phase."""
    rs = ctx.ratings
    pass_epa = rs.expected("pass_epa_per_dropback", team, opponent, home=home)
    rush_epa = rs.expected("rush_epa_per_carry", team, opponent, home=home)
    pass_rate = rs.expected("neutral_pass_rate", team, opponent, home=home)
    pass_rate = min(max(pass_rate, 0.35), 0.80)
    blended = pass_rate * pass_epa + (1.0 - pass_rate) * rush_epa
    return {"pass_epa": pass_epa, "rush_epa": rush_epa,
            "pass_rate": pass_rate, "blended_epa": blended}


def _pressure_edge(ctx: GameContext, team: str, opponent: str) -> float:
    """Positive when `team`'s pass protection outclasses `opponent`'s rush.

    Both ratings are oriented so a lower value is better for the offence, hence the
    subtraction: the offence's own pressure-allowed rating against the defence's ability to
    generate it.
    """
    rs = ctx.ratings
    allowed = rs.get(team).offense.get("pressure_rate", 0.0)
    generated = rs.get(opponent).defense.get("pressure_rate", 0.0)
    return -(allowed - generated)


def matchup_margin(ctx: GameContext, artifact: GameModelArtifact) -> Dict[str, object]:
    """The matchup model's own expected margin, and the reasoning behind it."""
    game = ctx.game
    home = _phase_edge(ctx, game.home, game.away, home=True)
    away = _phase_edge(ctx, game.away, game.home, home=False)
    epa_gap = home["blended_epa"] - away["blended_epa"]

    epa_coefficient = artifact.margin_coefficients[1] if len(artifact.margin_coefficients) > 1 else 15.0
    margin = epa_coefficient * epa_gap + artifact.margin_coefficients[0]

    pressure_gap = (_pressure_edge(ctx, game.home, game.away)
                    - _pressure_edge(ctx, game.away, game.home))
    margin += PRESSURE_POINTS * pressure_gap

    reasons: List[str] = []
    pass_gap = home["pass_epa"] - away["pass_epa"]
    rush_gap = home["rush_epa"] - away["rush_epa"]
    for label, gap, unit in (("Passing matchup", pass_gap, "EPA/dropback"),
                             ("Rushing matchup", rush_gap, "EPA/carry"),
                             ("Pass-rush matchup", pressure_gap, "pressure rate")):
        side = game.home if gap > 0 else game.away
        reasons.append(f"{label} favours {teams.display(side)} by {abs(gap):.3f} {unit}")
    reasons.append(
        f"{teams.display(game.home)} passes on {home['pass_rate']:.0%} of neutral downs vs "
        f"{teams.display(game.away)} at {away['pass_rate']:.0%} — the phase weighting "
        "follows how each offence actually plays")

    return {"margin": margin, "epa_gap": epa_gap, "pressure_gap": pressure_gap,
            "home": home, "away": away, "reasons": reasons}


def signals(ctx: GameContext, artifact: GameModelArtifact) -> List[Signal]:
    view = matchup_margin(ctx, artifact)
    proj = ctx.projection
    # Tilt the core projection toward the matchup view rather than replacing it.
    blended_margin = ((1.0 - MATCHUP_BLEND) * proj.expected_margin
                      + MATCHUP_BLEND * float(view["margin"]))
    margin_dist = dist.margin_distribution(blended_margin, proj.margin_sigma,
                                           artifact.margin_profile())
    home_win, tie, away_win = dist.win_probability(margin_dist)
    decisive = home_win + away_win
    if decisive <= 0:
        return []

    reasons = [f"Matchup-weighted margin {blended_margin:+.1f} vs core model "
               f"{proj.expected_margin:+.1f}"] + list(view["reasons"])[:3]
    out: List[Signal] = []

    probs = {ctx.game.home: home_win / decisive, ctx.game.away: away_win / decisive}
    ml_quotes = ctx.quotes_of(MarketType.MONEYLINE)
    vigfree = pricing.vig_free(ml_quotes)
    for quote in ml_quotes:
        if quote.team not in probs:
            continue
        signal = Signal(
            strategy=META.key, game_id=ctx.game.game_id,
            market_type=MarketType.MONEYLINE, label=quote.label,
            selection=f"{teams.display(quote.team)} ML",
            model_prob=probs[quote.team], confidence=Confidence.LOW,
            team=quote.team, reasoning=reasons,
        )
        pricing.price_signal(signal, quote, vigfree=vigfree,
                             sample_confidence=proj.confidence * 0.9)
        signal.confidence = _grade(signal, proj.confidence, quote)
        out.append(signal)

    for quote in ctx.quotes_of(MarketType.SPREAD):
        if quote.team is None or quote.line is None:
            continue
        if quote.team == ctx.game.home:
            prob = margin_dist.prob_over(quote.line)
        else:
            prob = margin_dist.prob_under(-quote.line)
        signal = Signal(
            strategy=META.key, game_id=ctx.game.game_id,
            market_type=MarketType.SPREAD, label=quote.label,
            selection=quote.label, model_prob=prob, confidence=Confidence.LOW,
            team=quote.team, line=quote.line, reasoning=reasons,
        )
        pricing.price_signal(signal, quote, sample_confidence=proj.confidence * 0.9)
        signal.confidence = _grade(signal, proj.confidence, quote)
        out.append(signal)
    return out


def _grade(signal: Signal, sample_confidence: float, quote) -> Confidence:
    if signal.market_prob is None:
        return Confidence.LOW
    agreement = 1.0 - min(abs(signal.model_prob - signal.market_prob) / 0.25, 1.0)
    # The matchup view is a noisier estimator than the aggregate model, so it is never
    # graded higher than MEDIUM on its own.
    graded = confidence_from(sample_confidence=sample_confidence * 0.85,
                             market_agreement=agreement,
                             liquidity_ok=liquidity_ok(quote))
    return Confidence.MEDIUM if graded is Confidence.HIGH else graded
