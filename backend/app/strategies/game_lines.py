"""The core market strategies: moneyline, spread, totals, team totals, winning margin.

Each reads the same adjusted game projection and asks a different question of it. They are
separate strategies rather than one because they earn separate track records — a model can
be genuinely good at totals and useless at spreads, and averaging those into one "accuracy"
number hides exactly the thing you need to know.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from app.core.types import Confidence, MarketQuote, MarketType, Signal, StrategyMeta
from app.data import teams
from app.strategies import pricing
from app.strategies.base import GameContext, confidence_from, liquidity_ok

_COMMON_INPUTS = ["opponent-adjusted EPA and points ratings", "fitted margin/total model",
                  "empirical key-number profile", "live Kalshi order book"]


# --------------------------------------------------------------------------- moneyline

MONEYLINE_META = StrategyMeta(
    key="moneyline",
    name="Moneyline",
    market_types=[MarketType.MONEYLINE],
    methodology=(
        "Win probability is read off the discrete margin distribution rather than a "
        "separate classifier, so the moneyline, the spread and the total are guaranteed to "
        "be mutually consistent. Kalshi's NFL moneyline lists only two contracts and voids "
        "on a tie, so tie probability is removed and the two sides renormalised."
    ),
    inputs=_COMMON_INPUTS,
    limitations=(
        "Moneylines are the most efficiently priced market on the board; genuine edges are "
        "rare and usually small. A large disagreement here is more often a stale model "
        "input than an opportunity."
    ),
)


def moneyline_signals(ctx: GameContext) -> List[Signal]:
    quotes = ctx.quotes_of(MarketType.MONEYLINE)
    if not quotes:
        return []
    proj = ctx.projection
    # A tie voids the market, so the live question is "who wins, given someone does".
    decisive = proj.home_win + proj.away_win
    if decisive <= 0:
        return []
    probs = {ctx.game.home: proj.home_win / decisive,
             ctx.game.away: proj.away_win / decisive}
    vigfree = pricing.vig_free(quotes)

    out: List[Signal] = []
    for quote in quotes:
        team = quote.team
        if team not in probs:
            continue
        opponent = ctx.game.away if team == ctx.game.home else ctx.game.home
        signal = Signal(
            strategy=MONEYLINE_META.key,
            game_id=ctx.game.game_id,
            market_type=MarketType.MONEYLINE,
            label=quote.label,
            selection=f"{teams.display(team)} ML",
            model_prob=probs[team],
            confidence=Confidence.LOW,
            team=team,
            reasoning=[
                f"Projected score {proj.away_score:.1f}-{proj.home_score:.1f} "
                f"({teams.display(ctx.game.home)} home)",
                f"Margin distribution puts {teams.display(team)} ahead "
                f"{probs[team]:.1%} of the time",
            ] + _driver_reasons(ctx, team, opponent),
        )
        pricing.price_signal(signal, quote, vigfree=vigfree,
                             sample_confidence=proj.confidence)
        signal.confidence = _grade(signal, proj.confidence, quote)
        out.append(signal)
    return out


# --------------------------------------------------------------------------- spread

SPREAD_META = StrategyMeta(
    key="spread",
    name="Spread",
    market_types=[MarketType.SPREAD],
    methodology=(
        "Kalshi writes spreads as 'team wins by over N.5 points', which reads directly off "
        "the margin distribution. That distribution is key-number aware, so a 2.5 and a 3.5 "
        "are priced as differently as the historical record says they should be — margins "
        "of exactly 3 occur roughly 2.5x more often than a smooth model implies."
    ),
    inputs=_COMMON_INPUTS,
    limitations=(
        "Accuracy depends on the projected margin being close to right; at 13 points of "
        "residual standard deviation, a one-point projection error moves a near-the-number "
        "spread probability by about three points."
    ),
)


def spread_signals(ctx: GameContext) -> List[Signal]:
    quotes = ctx.quotes_of(MarketType.SPREAD)
    out: List[Signal] = []
    proj = ctx.projection
    for quote in quotes:
        if quote.team is None or quote.line is None:
            continue
        prob = proj.margin_over(quote.line, quote.team)
        signal = Signal(
            strategy=SPREAD_META.key,
            game_id=ctx.game.game_id,
            market_type=MarketType.SPREAD,
            label=quote.label,
            selection=quote.label,
            model_prob=prob,
            confidence=Confidence.LOW,
            line=quote.line,
            team=quote.team,
            reasoning=[
                f"Projected margin {abs(proj.expected_margin):.1f} pts to "
                f"{teams.display(ctx.game.home if proj.expected_margin > 0 else ctx.game.away)}",
                f"P(win by more than {quote.line:g}) = {prob:.1%} "
                f"(sigma {proj.margin_sigma:.1f}, key numbers applied)",
            ],
        )
        pricing.price_signal(signal, quote, sample_confidence=proj.confidence)
        signal.confidence = _grade(signal, proj.confidence, quote)
        out.append(signal)
    return out


# --------------------------------------------------------------------------- totals

TOTAL_META = StrategyMeta(
    key="totals",
    name="Totals",
    market_types=[MarketType.TOTAL, MarketType.TEAM_TOTAL],
    methodology=(
        "Expected combined points come from both teams' opponent-adjusted scoring and "
        "efficiency plus pace, with fitted wind and roof terms. The result is expanded into "
        "a key-number-aware discrete distribution, so any over/under on the ladder is priced "
        "from the same object."
    ),
    inputs=_COMMON_INPUTS + ["Open-Meteo kickoff forecast"],
    limitations=(
        "Totals are the noisiest of the three core markets: the fitted model explains only "
        "about 6% of the variance in game totals, which is close to what the market itself "
        "manages. Edges here should be small and are."
    ),
)


def total_signals(ctx: GameContext) -> List[Signal]:
    proj = ctx.projection
    out: List[Signal] = []

    weather_note = []
    wx = (ctx.adjustment.detail.get("weather") or {}) if ctx.adjustment.detail else {}
    if isinstance(wx, dict) and wx.get("reasons"):
        weather_note = list(wx["reasons"])

    for quote in ctx.quotes_of(MarketType.TOTAL):
        if quote.line is None:
            continue
        prob = proj.total_over(quote.line)
        signal = Signal(
            strategy=TOTAL_META.key,
            game_id=ctx.game.game_id,
            market_type=MarketType.TOTAL,
            label=quote.label,
            selection=f"Over {quote.line:g}",
            model_prob=prob,
            confidence=Confidence.LOW,
            line=quote.line,
            reasoning=[
                f"Projected total {proj.expected_total:.1f} pts "
                f"({proj.away_score:.1f} + {proj.home_score:.1f})",
                f"P(over {quote.line:g}) = {prob:.1%} (sigma {proj.total_sigma:.1f})",
            ] + weather_note,
        )
        pricing.price_signal(signal, quote, sample_confidence=proj.confidence)
        signal.confidence = _grade(signal, proj.confidence, quote)
        out.append(signal)

    for quote in ctx.quotes_of(MarketType.TEAM_TOTAL):
        if quote.line is None or quote.team is None:
            continue
        prob = proj.team_total_over(quote.team, quote.line)
        expected = (proj.home_score if quote.team == ctx.game.home else proj.away_score)
        signal = Signal(
            strategy=TOTAL_META.key,
            game_id=ctx.game.game_id,
            market_type=MarketType.TEAM_TOTAL,
            label=quote.label,
            selection=quote.label,
            model_prob=prob,
            confidence=Confidence.LOW,
            line=quote.line,
            team=quote.team,
            reasoning=[
                f"{teams.display(quote.team)} projected for {expected:.1f} pts",
                f"P(over {quote.line:g}) = {prob:.1%}",
            ] + weather_note,
        )
        pricing.price_signal(signal, quote, sample_confidence=proj.confidence)
        signal.confidence = _grade(signal, proj.confidence, quote)
        out.append(signal)
    return out


# --------------------------------------------------------------------------- win margin

WIN_MARGIN_META = StrategyMeta(
    key="win_margin",
    name="Winning Margin",
    market_types=[MarketType.WIN_MARGIN],
    methodology=(
        "Margin bands (1-6, 7-14, 15+, tie) are summed straight out of the discrete margin "
        "distribution. These bands sit right on top of the key numbers, which is exactly "
        "where a smooth model misprices and where the fitted profile earns its keep."
    ),
    inputs=_COMMON_INPUTS,
    limitations=(
        "Band markets are thin. A tempting edge on an untraded band is usually an absent "
        "counterparty rather than value, which is why the liquidity gate applies here too."
    ),
)

_BAND_RANGES = {"1-6": (1, 6), "7-14": (7, 14), "15+": (15, 200)}


def win_margin_signals(ctx: GameContext) -> List[Signal]:
    quotes = ctx.quotes_of(MarketType.WIN_MARGIN)
    if not quotes:
        return []
    proj = ctx.projection
    vigfree = pricing.vig_free(quotes)
    out: List[Signal] = []

    for quote in quotes:
        prob = _band_probability(ctx, quote)
        if prob is None:
            continue
        signal = Signal(
            strategy=WIN_MARGIN_META.key,
            game_id=ctx.game.game_id,
            market_type=MarketType.WIN_MARGIN,
            label=quote.label,
            selection=quote.label,
            model_prob=prob,
            confidence=Confidence.LOW,
            team=quote.team,
            line=quote.line,
            reasoning=[
                f"Projected margin {proj.expected_margin:+.1f} to the home side",
                f"P({quote.label}) = {prob:.1%} from the key-number-adjusted margin "
                "distribution",
            ],
        )
        pricing.price_signal(signal, quote, vigfree=vigfree,
                             sample_confidence=proj.confidence)
        signal.confidence = _grade(signal, proj.confidence, quote)
        out.append(signal)
    return out


def _band_probability(ctx: GameContext, quote: MarketQuote) -> Optional[float]:
    """P(margin lands in this contract's band), read from the home-margin distribution."""
    dist = ctx.projection.margin
    label = quote.label.lower()
    if "tie" in label:
        return dist.pmf(0)
    if quote.team is None:
        return None
    home_side = quote.team == ctx.game.home
    for key, (lo, hi) in _BAND_RANGES.items():
        if key in quote.selection:
            if home_side:
                return sum(dist.pmf(m) for m in range(lo, hi + 1))
            return sum(dist.pmf(-m) for m in range(lo, hi + 1))
    return None


# --------------------------------------------------------------------------- helpers

def _grade(signal: Signal, sample_confidence: float, quote: MarketQuote) -> Confidence:
    """Confidence for a priced signal, penalising disagreement and illiquidity."""
    market_prob = signal.market_prob
    if market_prob is None:
        return Confidence.LOW
    agreement = 1.0 - min(abs(signal.model_prob - market_prob) / 0.25, 1.0)
    return confidence_from(sample_confidence=sample_confidence,
                           market_agreement=agreement,
                           liquidity_ok=liquidity_ok(quote))


def _driver_reasons(ctx: GameContext, team: str, opponent: str, n: int = 2) -> List[str]:
    """The top rating gaps favouring `team`, phrased for a prediction card."""
    out: List[str] = []
    for driver in ctx.projection.drivers:
        if driver.get("edge_to") != team:
            continue
        out.append(f"{driver['label']}: edge to {teams.display(team)} "
                   f"over {teams.display(opponent)}")
        if len(out) >= n:
            break
    return out
