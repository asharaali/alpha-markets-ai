"""Score distributions with NFL key numbers.

Football margins are not smooth. Because scores move in 3s and 7s, the margin distribution
has hard spikes at 3, 7, 10 and 14 — roughly 1 game in 7 lands on a 3-point margin. A plain
Normal model prices a -2.5 and a -3.5 almost identically, which is precisely the mistake
that makes a spread model lose money against people who know better.

So the discrete margin (and total) distribution here is a Normal *shaped by an empirically
fitted key-number profile*: for each integer outcome we hold a multiplier estimated from
thousands of historical games against their closing lines, saying how much more or less
often that exact number lands than a smooth model expects. The profile is fitted in
app.models.calibration from nflverse data; nothing in it is hand-tuned.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

SQRT2 = math.sqrt(2.0)
SQRT2PI = math.sqrt(2.0 * math.pi)


def normal_cdf(x: float, mu: float = 0.0, sigma: float = 1.0) -> float:
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * SQRT2)))


def normal_pdf(x: float, mu: float = 0.0, sigma: float = 1.0) -> float:
    if sigma <= 0:
        return 0.0
    z = (x - mu) / sigma
    return math.exp(-0.5 * z * z) / (sigma * SQRT2PI)


def normal_pmf(k: int, mu: float, sigma: float) -> float:
    """Probability that a Normal rounds to the integer k (continuity-corrected)."""
    return normal_cdf(k + 0.5, mu, sigma) - normal_cdf(k - 0.5, mu, sigma)


@dataclass
class DiscreteDistribution:
    """A probability mass function over consecutive integers."""

    low: int
    mass: List[float]

    @property
    def high(self) -> int:
        return self.low + len(self.mass) - 1

    def pmf(self, k: int) -> float:
        i = k - self.low
        return self.mass[i] if 0 <= i < len(self.mass) else 0.0

    def cdf(self, k: int) -> float:
        """P(X <= k)."""
        if k < self.low:
            return 0.0
        if k >= self.high:
            return 1.0
        return sum(self.mass[: k - self.low + 1])

    def prob_over(self, line: float) -> float:
        """P(X > line), strictly greater — so a whole-number line pushes rather than wins.

        P(X > 3) and P(X > 3.5) are both 1 - CDF(3) on integer outcomes; the difference
        between them lives in prob_push, which is where a key-number line actually costs
        you if you ignore it.
        """
        return max(0.0, 1.0 - self.cdf(int(math.floor(line))))

    def prob_under(self, line: float) -> float:
        floor = math.floor(line)
        if abs(line - floor) < 1e-9:
            return self.cdf(int(floor) - 1)
        return self.cdf(int(floor))

    def prob_push(self, line: float) -> float:
        floor = math.floor(line)
        if abs(line - floor) < 1e-9:
            return self.pmf(int(floor))
        return 0.0

    def mean(self) -> float:
        return sum((self.low + i) * p for i, p in enumerate(self.mass))

    def stdev(self) -> float:
        mu = self.mean()
        var = sum(((self.low + i) - mu) ** 2 * p for i, p in enumerate(self.mass))
        return math.sqrt(max(var, 0.0))

    def top_outcomes(self, n: int = 8) -> List[Tuple[int, float]]:
        pairs = [(self.low + i, p) for i, p in enumerate(self.mass)]
        pairs.sort(key=lambda kv: kv[1], reverse=True)
        return pairs[:n]

    def to_dict(self) -> Dict[str, object]:
        return {"low": self.low, "mean": round(self.mean(), 2),
                "stdev": round(self.stdev(), 2),
                "mass": [round(p, 6) for p in self.mass]}


# A key-number profile is {outcome: multiplier}. Anything not listed multiplies by 1.
KeyProfile = Dict[int, float]


def build_distribution(mu: float, sigma: float, *,
                       profile: Optional[KeyProfile] = None,
                       span: float = 5.0,
                       low: Optional[int] = None,
                       high: Optional[int] = None) -> DiscreteDistribution:
    """Discretised Normal(mu, sigma), reweighted by an empirical key-number profile.

    `span` is how many standard deviations of support to carry; 5 puts the truncated tail
    mass below 1e-6, which is far smaller than any modelling error here.
    """
    lo = int(math.floor(mu - span * sigma)) if low is None else low
    hi = int(math.ceil(mu + span * sigma)) if high is None else high
    if hi < lo:
        lo, hi = hi, lo
    mass: List[float] = []
    for k in range(lo, hi + 1):
        p = normal_pmf(k, mu, sigma)
        if profile:
            p *= profile.get(k, 1.0)
        mass.append(p)
    total = sum(mass)
    if total <= 0:
        # Degenerate inputs: fall back to a point mass rather than emitting NaNs.
        return DiscreteDistribution(low=int(round(mu)), mass=[1.0])
    mass = [p / total for p in mass]
    return DiscreteDistribution(low=lo, mass=mass)


def margin_distribution(expected_margin: float, sigma: float,
                        profile: Optional[KeyProfile] = None) -> DiscreteDistribution:
    """Home-team margin (home score minus away score)."""
    return build_distribution(expected_margin, sigma, profile=profile)


def total_distribution(expected_total: float, sigma: float,
                       profile: Optional[KeyProfile] = None) -> DiscreteDistribution:
    """Combined points. Truncated at zero — a negative total is not a thing."""
    dist = build_distribution(expected_total, sigma, profile=profile, low=0)
    return dist


def win_probability(dist: DiscreteDistribution) -> Tuple[float, float, float]:
    """(home win, tie, away win) from a margin distribution.

    Ties are rare (about 1 game in 400) but real, and folding them into either side would
    quietly bias every moneyline. Kalshi's NFL moneyline resolves a tie by refunding, so
    keeping the tie mass separate is also the correct treatment for that market.
    """
    tie = dist.pmf(0)
    home = sum(p for i, p in enumerate(dist.mass) if dist.low + i > 0)
    away = sum(p for i, p in enumerate(dist.mass) if dist.low + i < 0)
    return home, tie, away


def cover_probability(dist: DiscreteDistribution, spread: float,
                      *, team_is_home: bool = True) -> Tuple[float, float]:
    """(cover, push) for a team laying or taking `spread` points.

    `spread` follows the betting convention from THAT team's point of view: -3.5 means the
    team is favoured by 3.5, +3.5 means it is getting 3.5.

    The distribution is always the HOME margin, so the two sides resolve against different
    expressions of the same threshold:
      * home at -3.5 covers when the home margin exceeds 3.5  -> prob_over(-spread)
      * away at +3.5 covers when the home margin is under 3.5 -> prob_under(spread)

    Because a home line of -3.5 always pairs with an away line of +3.5, both land on the
    same threshold and the two covers plus the push sum to exactly 1 — which is the
    property the test suite pins, and which the previous sign convention broke.
    """
    threshold = -spread if team_is_home else spread
    if team_is_home:
        cover = dist.prob_over(threshold)
    else:
        cover = dist.prob_under(threshold)
    return cover, dist.prob_push(threshold)


def poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1))


def poisson_at_least_one(lam: float) -> float:
    """P(at least one occurrence) — the shape of every 'anytime' scoring market."""
    return 1.0 - math.exp(-max(lam, 0.0))


def prob_over_continuous(mu: float, sigma: float, line: float) -> float:
    """P(X > line) for a continuous quantity such as passing yards."""
    return 1.0 - normal_cdf(line, mu, sigma)


def clamp_prob(p: float, floor: float = 1e-4, ceil: float = 1.0 - 1e-4) -> float:
    return min(max(p, floor), ceil)


def fit_profile(observed: Dict[int, float], expected: Dict[int, float], *,
                pseudo_count: float = 8.0, window: int = 1,
                clip: Tuple[float, float] = (0.08, 3.0)) -> KeyProfile:
    """Estimate key-number multipliers from observed vs. smooth-model expected counts.

    The estimator is Gamma-Poisson: (observed + a) / (expected + a). That handles both ends
    correctly, which a fixed shrink-toward-one does not — an outcome we expected 110 times
    and saw 11 (exact ties) is measured with real precision and must be allowed to stay
    near 0.1, while an outcome we expected twice is pulled firmly back toward 1.

    A light neighbour blend then removes sampling jitter, weighted so that thinly-observed
    outcomes borrow from their neighbours and well-observed ones keep their own estimate.
    """
    if not expected:
        return {}
    point: Dict[int, float] = {}
    for k, exp in expected.items():
        if exp <= 0.5:
            continue
        point[k] = (observed.get(k, 0.0) + pseudo_count) / (exp + pseudo_count)

    out: KeyProfile = {}
    for k, value in point.items():
        neighbours = [point[k + d] for d in range(-window, window + 1)
                      if d != 0 and (k + d) in point]
        if neighbours:
            neighbour_mean = sum(neighbours) / len(neighbours)
            support = expected.get(k, 0.0)
            own_weight = support / (support + pseudo_count)
            value = own_weight * value + (1.0 - own_weight) * neighbour_mean
        out[k] = min(max(value, clip[0]), clip[1])
    return out
