"""Joint game simulation — the honest way to price a multi-leg bet.

The standard parlay mistake is multiplying leg probabilities. That is correct only for
independent legs, and same-game legs are never independent: "Chiefs to win" and "Chiefs
-3.5" hit together most of the time, so multiplying them understates the parlay's real
chance and makes a bad price look good. Applying a flat "correlation haircut" is better but
still a guess.

Instead we simulate. Each game's margin and total are drawn together from the model's own
fitted distributions, coupled by the correlation measured from history, and every leg on
that game is evaluated against the same simulated scoreline. Correlation then falls out of
the arithmetic exactly as the model implies it — including the awkward cases a coefficient
table would get wrong, such as a team-total over paired with the game under.

Legs in different games are genuinely independent, so those combine by multiplication
across per-game simulated probabilities.
"""
from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from app.core.types import MarketType, Signal
from app.models import distributions as dist
from app.models.game_model import GameProjection

# Enough draws that the Monte Carlo error on a combined probability is well under a point,
# which is far smaller than the model error it is measuring.
DEFAULT_DRAWS = 20000


@dataclass
class LegOutcome:
    """A parlay leg reduced to a predicate over a simulated scoreline."""

    signal: Signal
    game_id: str
    describe: str
    test: Callable[[int, int], bool] = field(repr=False)


def _norm_pair(rng: random.Random, correlation: float) -> Tuple[float, float]:
    """Two standard normals with the given correlation (Box-Muller + Cholesky)."""
    u1 = max(rng.random(), 1e-12)
    u2 = rng.random()
    radius = math.sqrt(-2.0 * math.log(u1))
    z1 = radius * math.cos(2.0 * math.pi * u2)
    z2 = radius * math.sin(2.0 * math.pi * u2)
    return z1, correlation * z1 + math.sqrt(max(1.0 - correlation ** 2, 0.0)) * z2


def _quantile(distribution: dist.DiscreteDistribution, u: float) -> int:
    """Inverse CDF of a discrete distribution — turns a uniform draw into an outcome."""
    cumulative = 0.0
    for i, p in enumerate(distribution.mass):
        cumulative += p
        if u <= cumulative:
            return distribution.low + i
    return distribution.high


def frechet_bounds(marginals: Sequence[float]) -> Tuple[float, float]:
    """(upper, lower) limits on P(all of these) given only the individual probabilities.

    These hold for ANY dependence structure. The upper bound is the smallest marginal —
    an intersection cannot be likelier than its rarest member. The lower bound is
    sum(p) - (n - 1), which bites when the legs are all likely: two 90% legs must land
    together at least 80% of the time, because there is not enough probability left over
    for them to miss separately.
    """
    if not marginals:
        return 1.0, 1.0
    upper = min(marginals)
    lower = max(0.0, sum(marginals) - (len(marginals) - 1))
    return upper, min(lower, upper)


def game_seed(game_id: str) -> int:
    """A per-game seed that is identical in every process.

    `hash()` on a string is salted by PYTHONHASHSEED, so the previous implementation gave
    a different seed after every restart and the same slate priced differently on reload.
    A digest is stable forever, which is what "the recommendation does not move when you
    refresh" actually requires.
    """
    digest = hashlib.blake2b(game_id.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % (2 ** 31)


def _reconcile_parity(rng: random.Random, distribution: dist.DiscreteDistribution,
                      total: int, margin: int) -> Optional[int]:
    """Nudge the total to the parity the margin requires, or give up on this draw.

    A scoreline only exists when `total` and `margin` share a parity: home is
    (total + margin) / 2, and half a point is not a football score. The old code papered
    over this by rounding home and away independently, which silently changed the margin —
    a sampled 3 became a 4 whenever the total was even. Since the margin carries the key
    numbers the whole distribution exists to model, the margin is what must be preserved.

    So the total moves instead, by exactly one point, up or down in proportion to the mass
    the total distribution puts on each neighbour. Over many draws that leaves the total's
    own distribution essentially undisturbed while keeping every margin exactly as drawn.
    """
    if (total + margin) % 2 == 0:
        return total
    down, up = total - 1, total + 1
    p_down = distribution.pmf(down) if down >= max(0, abs(margin)) else 0.0
    p_up = distribution.pmf(up)
    if p_down <= 0.0 and p_up <= 0.0:
        return up if up >= abs(margin) else None
    return down if rng.random() * (p_down + p_up) < p_down else up


def simulate_game(projection: GameProjection, *, correlation: float = 0.0,
                  draws: int = DEFAULT_DRAWS,
                  seed: int = 0) -> List[Tuple[int, int]]:
    """Draw (home_score, away_score) pairs from a game's joint distribution.

    Margin and total are sampled through a Gaussian copula so their historical correlation
    is preserved, then converted to a valid integer scoreline. The seed is fixed per game
    so the same slate produces the same parlay numbers on every refresh — a recommendation
    that changes when you reload is not a recommendation.

    Every requested draw is returned. The earlier version dropped any draw that implied a
    negative score, which both shrank the sample below `draws` and biased what remained;
    an impossible pair is now redrawn instead.
    """
    rng = random.Random(seed)
    out: List[Tuple[int, int]] = []
    attempts = 0
    limit = draws * 20        # generous: rejections are rare outside absurd projections
    while len(out) < draws and attempts < limit:
        attempts += 1
        z_margin, z_total = _norm_pair(rng, correlation)
        margin = _quantile(projection.margin, dist.normal_cdf(z_margin))
        total = _quantile(projection.total, dist.normal_cdf(z_total))
        if total < abs(margin):
            continue          # a margin cannot exceed the points that produced it
        adjusted = _reconcile_parity(rng, projection.total, total, margin)
        if adjusted is None or adjusted < abs(margin):
            continue
        home = (adjusted + margin) // 2
        away = (adjusted - margin) // 2
        if home < 0 or away < 0:
            continue
        out.append((home, away))
    return out


def leg_predicate(signal: Signal, home_team: str) -> Optional[Callable[[int, int], bool]]:
    """Turn a signal into a test against a simulated (home_score, away_score).

    Returns None for markets the simulation cannot resolve — player props depend on who
    touches the ball, not the scoreline, so they are excluded from correlated pricing
    rather than assumed independent.
    """
    market = signal.market_type
    line = signal.line
    team_is_home = signal.team == home_team

    if market is MarketType.MONEYLINE:
        if team_is_home:
            return lambda h, a: h > a
        return lambda h, a: a > h

    if market is MarketType.SPREAD and line is not None:
        if team_is_home:
            return lambda h, a, ln=line: (h - a) > ln
        return lambda h, a, ln=line: (a - h) > ln

    if market is MarketType.TOTAL and line is not None:
        return lambda h, a, ln=line: (h + a) > ln

    if market is MarketType.TEAM_TOTAL and line is not None:
        if team_is_home:
            return lambda h, a, ln=line: h > ln
        return lambda h, a, ln=line: a > ln

    if market is MarketType.WIN_MARGIN and line is not None:
        selection = signal.selection.lower()
        if "tie" in selection:
            return lambda h, a: h == a
        if "1-6" in selection:
            if team_is_home:
                return lambda h, a: 1 <= (h - a) <= 6
            return lambda h, a: 1 <= (a - h) <= 6
        if "7-14" in selection:
            if team_is_home:
                return lambda h, a: 7 <= (h - a) <= 14
            return lambda h, a: 7 <= (a - h) <= 14
        if "15+" in selection:
            if team_is_home:
                return lambda h, a: (h - a) >= 15
            return lambda h, a: (a - h) >= 15
    return None


class SlateSimulator:
    """Per-game simulated scorelines, reused across every parlay evaluated on a slate."""

    def __init__(self, correlation: float = 0.0, draws: int = DEFAULT_DRAWS):
        self.correlation = correlation
        self.draws = draws
        self._sims: Dict[str, List[Tuple[int, int]]] = {}
        self._home: Dict[str, str] = {}
        # Marginals get asked for repeatedly while ranking combinations; recomputing them
        # over 20k draws each time dominated the parlay search.
        self._marginal_cache: Dict[str, Dict[int, float]] = {}

    def register(self, game_id: str, projection: GameProjection, home_team: str) -> None:
        if game_id in self._sims:
            return
        # Deterministic per-game seed: stable across processes, still decorrelated between
        # games. See game_seed() for why builtin hash() cannot be used here.
        self._sims[game_id] = simulate_game(projection, correlation=self.correlation,
                                            draws=self.draws, seed=game_seed(game_id))
        self._home[game_id] = home_team
        self._marginal_cache.pop(game_id, None)

    def home_team(self, game_id: str) -> Optional[str]:
        return self._home.get(game_id)

    def can_simulate(self, signal: Signal) -> bool:
        home = self._home.get(signal.game_id)
        return home is not None and leg_predicate(signal, home) is not None

    def single_probability(self, signal: Signal) -> Optional[float]:
        """A leg's probability under the simulation. Used to sanity-check the analytics."""
        sims = self._sims.get(signal.game_id)
        home = self._home.get(signal.game_id)
        if not sims or home is None:
            return None
        test = leg_predicate(signal, home)
        if test is None:
            return None
        cache = self._marginal_cache.setdefault(signal.game_id, {})
        key = id(signal)
        if key not in cache:
            cache[key] = sum(1 for h, a in sims if test(h, a)) / len(sims)
        return cache[key]

    def joint_probability(self, legs: Sequence[Signal],
                          marginals: Optional[Sequence[float]] = None
                          ) -> Optional[Dict[str, object]]:
        """Correlation-aware probability that EVERY leg lands.

        Legs are grouped by game; within a game they are evaluated against the same drawn
        scoreline (so correlation is exact), and the per-game results multiply because
        separate games really are independent.

        When `marginals` is supplied — the PUBLISHED per-leg probabilities, which are the
        model blended toward the market — the simulation is used only for the correlation
        STRUCTURE, not the levels: we take the ratio of simulated-joint to
        simulated-independent within each game and apply it to the published product.

        That separation matters. The simulation runs on the raw projection, while the leg
        cards show the market-blended number, and without this the parlay would be priced
        off probabilities that differ from the ones displayed beside it. Keeping the
        marginals and borrowing only the correlation gives a combined probability that is
        consistent with every leg shown, and still correct about legs moving together.
        """
        by_game: Dict[str, List[Signal]] = {}
        for leg in legs:
            by_game.setdefault(leg.game_id, []).append(leg)

        published: Dict[int, float] = {}
        if marginals is not None:
            if len(marginals) != len(legs):
                return None
            published = {id(leg): float(p) for leg, p in zip(legs, marginals)}

        combined = 1.0
        naive = 1.0
        per_game: List[Dict[str, object]] = []

        for game_id, group in by_game.items():
            sims = self._sims.get(game_id)
            home = self._home.get(game_id)
            if not sims or home is None:
                return None
            tests = []
            for leg in group:
                test = leg_predicate(leg, home)
                if test is None:
                    return None
                tests.append(test)

            hits = 0
            for h, a in sims:
                if all(test(h, a) for test in tests):
                    hits += 1
            sim_joint = hits / len(sims)
            sim_independent = 1.0
            for leg in group:
                p = self.single_probability(leg)
                if p is None:
                    return None
                sim_independent *= p

            ratio = (sim_joint / sim_independent) if sim_independent > 1e-9 else None
            clamped = False
            if published:
                group_marginals = [published[id(leg)] for leg in group]
                group_naive = 1.0
                for p in group_marginals:
                    group_naive *= p
                if ratio is None:
                    scaled = 0.0 if sim_joint <= 0 else group_naive
                else:
                    scaled = group_naive * ratio
                # The ratio is measured on the RAW model while the marginals are
                # market-blended, so their product is not guaranteed to be a coherent
                # joint probability. Frechet says any joint must sit between
                # max(0, sum(p) - (n-1)) and min(p). Outside that range the number is not
                # merely imprecise, it is impossible, so it is clamped and the clamp is
                # reported rather than hidden.
                upper, lower = frechet_bounds(group_marginals)
                joint = min(max(scaled, lower), upper)
                clamped = abs(joint - scaled) > 1e-9
                independent = group_naive
            else:
                joint = sim_joint
                independent = sim_independent

            combined *= joint
            naive *= independent
            per_game.append({
                "game_id": game_id,
                "legs": len(group),
                "joint_prob": round(joint, 5),
                "independent_prob": round(independent, 5),
                "correlation_ratio": round(ratio, 4) if ratio is not None else None,
                "correlation_effect": round(joint - independent, 5),
                "frechet_clamped": clamped,
            })

        combined = max(0.0, min(combined, 1.0))
        # Monte Carlo error on the tightest per-game estimate, propagated across games.
        # Reported so a 4%-vs-3% comparison between two parlays is not read as meaningful
        # when the simulation itself only resolves to half a point.
        standard_error = math.sqrt(
            max(combined * (1.0 - combined), 1e-12) / max(self.draws, 1))
        return {
            "combined_prob": combined,
            "naive_prob": naive,
            "correlation_effect": combined - naive,
            "per_game": per_game,
            "draws": self.draws,
            "standard_error": standard_error,
            "frechet_clamped": any(g["frechet_clamped"] for g in per_game),
        }
