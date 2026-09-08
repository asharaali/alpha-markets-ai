"""Minimal linear algebra: weighted ridge regression on sparse design rows.

The team-rating fits have a very particular shape — every observation touches only three
or four parameters (intercept, one offense, one defense, home-field) out of ~66 — so the
normal equations are built by walking non-zeros instead of materialising a dense design
matrix. That turns each fit from millions of operations into thousands, which is what lets
the backtester refit ratings for every week of every season without a numeric dependency.

Implemented in pure Python on purpose: it removes numpy/scipy from the deployment, and at
this problem size (66 parameters) the cost is microseconds either way.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

# A sparse observation: (list of (param_index, coefficient), target, weight)
SparseRow = Tuple[Sequence[Tuple[int, float]], float, float]


class SingularSystem(Exception):
    """The normal equations could not be solved even with regularisation."""


def solve_symmetric(matrix: List[List[float]], rhs: List[float]) -> List[float]:
    """Gaussian elimination with partial pivoting. Mutates copies, not the inputs."""
    n = len(rhs)
    a = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            raise SingularSystem(f"pivot {col} vanished; system is under-determined")
        if pivot != col:
            a[col], a[pivot] = a[pivot], a[col]
        inv = 1.0 / a[col][col]
        for r in range(col + 1, n):
            factor = a[r][col] * inv
            if factor == 0.0:
                continue
            row_r, row_c = a[r], a[col]
            for c in range(col, n + 1):
                row_r[c] -= factor * row_c[c]
    out = [0.0] * n
    for r in range(n - 1, -1, -1):
        total = a[r][n] - sum(a[r][c] * out[c] for c in range(r + 1, n))
        out[r] = total / a[r][r]
    return out


def ridge_fit(rows: Sequence[SparseRow], n_params: int, penalty: float,
              *, unpenalized: Optional[Sequence[int]] = None) -> List[float]:
    """Weighted ridge: minimise sum_r w_r (y_r - x_r.b)^2 + penalty * sum_{j penalised} b_j^2.

    `unpenalized` names the parameters that must NOT be shrunk (the global intercept and
    home-field advantage — shrinking those toward zero would bias every prediction, not
    just regularise a noisy team).

    The penalty is what makes a two-game sample usable: an unshrunk fit on week 3 would
    hand a team that has played one blowout an absurd rating.
    """
    free = set(unpenalized or ())
    size = n_params
    xtx = [[0.0] * size for _ in range(size)]
    xty = [0.0] * size

    for terms, y, w in rows:
        if w <= 0:
            continue
        for i, (pi, ci) in enumerate(terms):
            wc = w * ci
            xty[pi] += wc * y
            row = xtx[pi]
            for pj, cj in terms[i:]:
                contrib = wc * cj
                row[pj] += contrib
                if pj != pi:
                    xtx[pj][pi] += contrib

    for j in range(size):
        if j not in free:
            xtx[j][j] += penalty
        elif xtx[j][j] == 0.0:
            # An unpenalised parameter no observation touched: pin it at zero rather than
            # letting the solve blow up.
            xtx[j][j] += 1e-9

    return solve_symmetric(xtx, xty)


def least_squares(design: Sequence[Sequence[float]], targets: Sequence[float],
                  *, penalty: float = 1e-6,
                  weights: Optional[Sequence[float]] = None) -> List[float]:
    """Dense weighted least squares with a tiny ridge for numerical stability.

    Used for the small second-stage fits (turning rating differentials into points), where
    the design is a handful of columns rather than one column per team.
    """
    if not design:
        raise SingularSystem("no observations")
    k = len(design[0])
    rows: List[SparseRow] = []
    for i, x in enumerate(design):
        w = weights[i] if weights is not None else 1.0
        rows.append(([(j, x[j]) for j in range(k)], targets[i], w))
    return ridge_fit(rows, k, penalty, unpenalized=range(k))


def r_squared(actual: Sequence[float], predicted: Sequence[float]) -> float:
    n = len(actual)
    if n == 0:
        return 0.0
    mean = sum(actual) / n
    ss_tot = sum((a - mean) ** 2 for a in actual)
    ss_res = sum((a - p) ** 2 for a, p in zip(actual, predicted))
    return 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0


def rmse(actual: Sequence[float], predicted: Sequence[float]) -> float:
    n = len(actual)
    if n == 0:
        return 0.0
    return (sum((a - p) ** 2 for a, p in zip(actual, predicted)) / n) ** 0.5


def describe(params: Sequence[float], names: Sequence[str]) -> Dict[str, float]:
    return {name: round(params[i], 5) for i, name in enumerate(names)}


def invert_symmetric(matrix: List[List[float]]) -> List[List[float]]:
    """Explicit inverse via Gauss-Jordan. Only used on the tiny second-stage systems."""
    n = len(matrix)
    a = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            raise SingularSystem(f"pivot {col} vanished while inverting")
        a[col], a[pivot] = a[pivot], a[col]
        inv = 1.0 / a[col][col]
        for c in range(2 * n):
            a[col][c] *= inv
        for r in range(n):
            if r == col:
                continue
            factor = a[r][col]
            if factor == 0.0:
                continue
            for c in range(2 * n):
                a[r][c] -= factor * a[col][c]
    return [row[n:] for row in a]


@dataclass
class FitResult:
    """Coefficients plus the uncertainty around them.

    Standard errors are the difference between "travel is worth a point" and "travel might
    be worth a point, or nothing, and we cannot tell from 1,000 games" — which is exactly
    the distinction a betting model has to respect.
    """

    coefficients: List[float]
    std_errors: List[float]
    residual_sigma: float
    r2: float
    rmse: float
    n: int

    def t_stats(self) -> List[float]:
        return [(c / se) if se > 1e-12 else 0.0
                for c, se in zip(self.coefficients, self.std_errors)]

    def shrunk(self, protect: Sequence[int] = ()) -> List[float]:
        """Shrink each coefficient toward zero by how well the data supports it.

        The factor t^2/(1+t^2) leaves a strongly-supported coefficient essentially intact
        (|t|=4 keeps 94%) while collapsing an unsupported one (|t|=0.5 keeps 20%). Indices
        in `protect` — the intercept and the core rating terms — are never shrunk; they are
        the model, not an adjustment to it.
        """
        keep = set(protect)
        out = []
        for i, (c, t) in enumerate(zip(self.coefficients, self.t_stats())):
            if i in keep:
                out.append(c)
            else:
                out.append(c * (t * t) / (1.0 + t * t))
        return out


def least_squares_stats(design: Sequence[Sequence[float]], targets: Sequence[float],
                        *, penalty: float = 1e-6) -> FitResult:
    """Dense least squares returning coefficients AND their standard errors."""
    n = len(design)
    if n == 0:
        raise SingularSystem("no observations")
    k = len(design[0])
    if n <= k:
        raise SingularSystem(f"{n} observations cannot support {k} parameters")

    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k
    for x, y in zip(design, targets):
        for i in range(k):
            xty[i] += x[i] * y
            for j in range(i, k):
                v = x[i] * x[j]
                xtx[i][j] += v
                if i != j:
                    xtx[j][i] += v
    for i in range(k):
        xtx[i][i] += penalty

    beta = solve_symmetric([row[:] for row in xtx], xty[:])
    predicted = [sum(b * v for b, v in zip(beta, x)) for x in design]
    rss = sum((a - p) ** 2 for a, p in zip(targets, predicted))
    sigma2 = rss / (n - k)
    cov = invert_symmetric(xtx)
    std_errors = [math.sqrt(max(sigma2 * cov[i][i], 0.0)) for i in range(k)]
    return FitResult(
        coefficients=beta,
        std_errors=std_errors,
        residual_sigma=math.sqrt(max(sigma2, 0.0)),
        r2=r_squared(targets, predicted),
        rmse=rmse(targets, predicted),
        n=n,
    )
