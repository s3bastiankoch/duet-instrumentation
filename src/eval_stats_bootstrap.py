from __future__ import annotations

import math
import random
import statistics
from typing import Callable

from .eval_stats_errors import EvalStatsError

# NumPy for fast vectorized bootstrap
try:
    import numpy as np

    HAS_NUMPY = True
except ImportError:
    np = None  # type: ignore
    HAS_NUMPY = False


def _percentile(sorted_x: list[float], p: float) -> float:
    """
    Linear-interpolated percentile (like NumPy default).
    p is in [0, 100].
    """
    if not sorted_x:
        raise EvalStatsError("Cannot compute percentile of empty sample set")
    if p <= 0:
        return sorted_x[0]
    if p >= 100:
        return sorted_x[-1]
    if len(sorted_x) == 1:
        return sorted_x[0]
    k = (len(sorted_x) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_x[int(k)]
    d0 = sorted_x[int(f)] * (c - k)
    d1 = sorted_x[int(c)] * (k - f)
    return d0 + d1


def _bootstrap_ci_numpy(
    *,
    samples: list[float],
    stat: str,  # "mean" or "p95"
    n_resamples: int,
    alpha: float,
    seed: int,
) -> dict[str, float]:
    """Fast vectorized bootstrap using NumPy."""
    if not HAS_NUMPY:
        raise EvalStatsError("NumPy is required for fast bootstrap. Install with: pip install numpy")

    arr = np.array(samples, dtype=np.float64)
    n = len(arr)

    rng = np.random.default_rng(seed)
    indices = rng.integers(0, n, size=(n_resamples, n))
    resamples = arr[indices]  # shape: (n_resamples, n)

    if stat == "mean":
        stats = np.mean(resamples, axis=1)
    elif stat == "p50":
        stats = np.percentile(resamples, 50, axis=1)
    elif stat == "p95":
        stats = np.percentile(resamples, 95, axis=1)
    else:
        raise EvalStatsError(f"Unknown stat: {stat}")

    lo = float(np.percentile(stats, 100.0 * (alpha / 2.0)))
    hi = float(np.percentile(stats, 100.0 * (1.0 - alpha / 2.0)))
    return {"lo": lo, "hi": hi}


def _bootstrap_ci(
    *,
    rng: random.Random,
    samples: list[float],
    stat_fn: Callable[[list[float]], float],
    n_resamples: int,
    alpha: float,
) -> dict[str, float]:
    """Pure Python bootstrap (fallback when NumPy unavailable or for custom stat_fn)."""
    if n_resamples <= 0:
        raise EvalStatsError("n_resamples must be > 0")
    if not (0.0 < alpha < 1.0):
        raise EvalStatsError("alpha must be in (0, 1)")
    if not samples:
        raise EvalStatsError("Cannot bootstrap empty samples")

    n = len(samples)
    stats: list[float] = []
    for _ in range(n_resamples):
        resample = [samples[rng.randrange(0, n)] for _ in range(n)]
        stats.append(stat_fn(resample))
    stats.sort()
    lo = _percentile(stats, 100.0 * (alpha / 2.0))
    hi = _percentile(stats, 100.0 * (1.0 - alpha / 2.0))
    return {"lo": float(lo), "hi": float(hi)}


def _bootstrap_delta_pct_numpy(
    old: list[float],
    new: list[float],
    *,
    stat: str,  # "mean" | "p50" | "p95"
    n_resamples: int,
    alpha: float,
    seed: int,
) -> dict[str, float]:
    """Fast vectorized bootstrap for relative percentage delta using NumPy."""
    if not HAS_NUMPY:
        raise EvalStatsError("NumPy is required for fast bootstrap")

    old_arr = np.array(old, dtype=np.float64)
    new_arr = np.array(new, dtype=np.float64)
    n_old, n_new = len(old_arr), len(new_arr)

    rng = np.random.default_rng(seed)
    old_indices = rng.integers(0, n_old, size=(n_resamples, n_old))
    new_indices = rng.integers(0, n_new, size=(n_resamples, n_new))

    old_resamples = old_arr[old_indices]
    new_resamples = new_arr[new_indices]

    if stat == "mean":
        old_stats = np.mean(old_resamples, axis=1)
        new_stats = np.mean(new_resamples, axis=1)
    elif stat == "p50":
        old_stats = np.percentile(old_resamples, 50, axis=1)
        new_stats = np.percentile(new_resamples, 50, axis=1)
    elif stat == "p95":
        old_stats = np.percentile(old_resamples, 95, axis=1)
        new_stats = np.percentile(new_resamples, 95, axis=1)
    else:
        raise EvalStatsError(f"Unknown stat: {stat}")

    # Calculate relative percentage change, handling zeros
    with np.errstate(divide="ignore", invalid="ignore"):
        pct_changes = np.where(old_stats != 0, ((new_stats - old_stats) / old_stats) * 100.0, 0.0)

    lo = float(np.percentile(pct_changes, 100.0 * (alpha / 2.0)))
    hi = float(np.percentile(pct_changes, 100.0 * (1.0 - alpha / 2.0)))
    return {"lo": lo, "hi": hi}


def _bootstrap_delta_pct_distribution_numpy(
    old: list[float],
    new: list[float],
    *,
    stat: str,  # "mean" | "p50" | "p95"
    n_resamples: int,
    seed: int,
) -> list[float]:
    """Fast vectorized bootstrap for relative percentage delta distribution using NumPy."""
    if not HAS_NUMPY:
        raise EvalStatsError("NumPy is required for fast bootstrap")
    if n_resamples <= 0:
        raise EvalStatsError("n_resamples must be > 0")

    old_arr = np.array(old, dtype=np.float64)
    new_arr = np.array(new, dtype=np.float64)
    n_old, n_new = len(old_arr), len(new_arr)

    rng = np.random.default_rng(seed)
    old_indices = rng.integers(0, n_old, size=(n_resamples, n_old))
    new_indices = rng.integers(0, n_new, size=(n_resamples, n_new))

    old_resamples = old_arr[old_indices]
    new_resamples = new_arr[new_indices]

    if stat == "mean":
        old_stats = np.mean(old_resamples, axis=1)
        new_stats = np.mean(new_resamples, axis=1)
    elif stat == "p50":
        old_stats = np.percentile(old_resamples, 50, axis=1)
        new_stats = np.percentile(new_resamples, 50, axis=1)
    elif stat == "p95":
        old_stats = np.percentile(old_resamples, 95, axis=1)
        new_stats = np.percentile(new_resamples, 95, axis=1)
    else:
        raise EvalStatsError(f"Unknown stat: {stat}")

    with np.errstate(divide="ignore", invalid="ignore"):
        pct_changes = np.where(old_stats != 0, ((new_stats - old_stats) / old_stats) * 100.0, 0.0)

    return [float(v) for v in pct_changes]


def _bootstrap_delta_pct_distribution(
    old: list[float],
    new: list[float],
    *,
    stat: str,  # "mean" | "p50" | "p95"
    n_resamples: int,
    seed: int,
) -> list[float]:
    """Pure Python bootstrap for relative percentage delta distribution."""
    if n_resamples <= 0:
        raise EvalStatsError("n_resamples must be > 0")
    if not old or not new:
        raise EvalStatsError("Cannot bootstrap empty samples")

    rng = random.Random(seed)
    n_old = len(old)
    n_new = len(new)

    if stat == "mean":
        stat_fn = lambda s: statistics.fmean(s)
    elif stat == "p50":
        stat_fn = lambda s: _percentile(sorted(s), 50.0)
    elif stat == "p95":
        stat_fn = lambda s: _percentile(sorted(s), 95.0)
    else:
        raise EvalStatsError(f"Unknown stat: {stat}")

    out: list[float] = []
    for _ in range(n_resamples):
        o = [old[rng.randrange(0, n_old)] for _ in range(n_old)]
        n = [new[rng.randrange(0, n_new)] for _ in range(n_new)]
        old_stat = stat_fn(o)
        new_stat = stat_fn(n)
        if old_stat != 0:
            pct_change = ((new_stat - old_stat) / old_stat) * 100.0
        else:
            pct_change = 0.0
        out.append(float(pct_change))
    return out
