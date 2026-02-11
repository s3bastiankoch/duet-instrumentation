from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass
from typing import Any, Callable

from .eval_stats_bootstrap import (
    HAS_NUMPY,
    _bootstrap_ci,
    _bootstrap_ci_numpy,
    _bootstrap_delta_pct_numpy,
    _percentile,
)
from .eval_stats_errors import EvalStatsError


def welch_t_test(data1: list[float], data2: list[float]) -> tuple[float, float]:
    """
    Perform Welch's t-test for two independent samples.

    This is more robust than Student's t-test when variances are unequal.

    Returns:
        (t_statistic, p_value)
    """
    n1, n2 = len(data1), len(data2)
    if n1 < 2 or n2 < 2:
        return (0.0, 1.0)

    mean1, mean2 = statistics.mean(data1), statistics.mean(data2)
    var1 = statistics.variance(data1)
    var2 = statistics.variance(data2)

    # Welch's t-statistic
    se = math.sqrt(var1 / n1 + var2 / n2) if (var1 / n1 + var2 / n2) > 0 else 1e-10
    t_stat = (mean1 - mean2) / se

    # Welch-Satterthwaite degrees of freedom
    if var1 == 0 and var2 == 0:
        df = n1 + n2 - 2
    else:
        num = (var1 / n1 + var2 / n2) ** 2
        denom = (var1 / n1) ** 2 / (n1 - 1) + (var2 / n2) ** 2 / (n2 - 1)
        df = num / denom if denom > 0 else 1

    # Approximate p-value using normal distribution for large samples
    try:
        from scipy import stats as scipy_stats

        p_value = 2 * scipy_stats.t.sf(abs(t_stat), df)
    except ImportError:
        # Approximate using normal distribution for large df
        z = abs(t_stat)
        p_value = 2 * (1 - 0.5 * (1 + math.erf(z / math.sqrt(2))))

    return (t_stat, p_value)


def cohens_d(data1: list[float], data2: list[float]) -> float:
    """
    Calculate Cohen's d effect size (data1 - data2).

    Interpretation:
    - |d| < 0.2: negligible
    - 0.2 <= |d| < 0.5: small
    - 0.5 <= |d| < 0.8: medium
    - |d| >= 0.8: large
    """
    n1, n2 = len(data1), len(data2)
    if n1 < 2 or n2 < 2:
        return 0.0

    mean1, mean2 = statistics.mean(data1), statistics.mean(data2)
    var1 = statistics.variance(data1)
    var2 = statistics.variance(data2)

    # Pooled standard deviation
    pooled_std = math.sqrt(((n1 - 1) * var1 + (n2 - 1) * var2) / (n1 + n2 - 2))

    if pooled_std == 0:
        return 0.0

    return (mean1 - mean2) / pooled_std


def effect_size_interpretation(d: float) -> str:
    """Interpret Cohen's d effect size."""
    abs_d = abs(d)
    if abs_d < 0.2:
        return "negligible"
    if abs_d < 0.5:
        return "small"
    if abs_d < 0.8:
        return "medium"
    return "large"


def calculate_mde(
    data1: list[float],
    data2: list[float],
    alpha: float = 0.01,
    power: float = 0.80,
) -> tuple[float, float]:
    """
    Calculate the Minimum Detectable Effect (MDE) for a two-sample comparison.

    MDE is the smallest effect size the experiment can reliably detect given:
    - Sample sizes (n1, n2)
    - Variance in the data
    - Desired significance level (alpha)
    - Desired statistical power (1 - beta)

    Formula: MDE = (z_α + z_β) × σ_pooled × √(1/n1 + 1/n2)

    Returns:
        (mde_absolute, mde_cohens_d)
        - mde_absolute: MDE in the same units as the data (e.g., ms)
        - mde_cohens_d: MDE expressed as Cohen's d
    """
    n1, n2 = len(data1), len(data2)

    if n1 < 2 or n2 < 2:
        return (0.0, 0.0)

    # Calculate pooled standard deviation
    var1 = statistics.variance(data1)
    var2 = statistics.variance(data2)
    pooled_std = math.sqrt(((n1 - 1) * var1 + (n2 - 1) * var2) / (n1 + n2 - 2))

    # Critical values from normal distribution (or t-distribution for small samples)
    try:
        from scipy import stats as scipy_stats

        # Use t-distribution for more accuracy with smaller samples
        df = n1 + n2 - 2
        z_alpha = scipy_stats.t.ppf(1 - alpha / 2, df)  # Two-tailed
        z_beta = scipy_stats.t.ppf(power, df)
    except ImportError:
        # Fall back to normal approximation
        z_alpha = 1.96 if alpha == 0.05 else 2.576 if alpha == 0.01 else 1.96
        z_beta = 0.84 if power == 0.80 else 1.28 if power == 0.90 else 0.84

    # MDE formula for two-sample comparison
    mde_absolute = (z_alpha + z_beta) * pooled_std * math.sqrt(1 / n1 + 1 / n2)

    # Express MDE as Cohen's d
    mde_cohens_d = mde_absolute / pooled_std if pooled_std > 0 else 0

    return (mde_absolute, mde_cohens_d)


@dataclass(frozen=True)
class SummaryStats:
    n: int
    mean: float
    stdev: float
    min: float
    max: float
    p50: float
    p90: float
    p95: float
    p99: float

    mean_ci: dict[str, float] | None = None
    p95_ci: dict[str, float] | None = None
    p50_ci: dict[str, float] | None = None


def summarize_samples(
    samples: list[float],
    *,
    bootstrap: int = 2000,
    alpha: float = 0.01,
    seed: int = 0,
    skip_ci: bool = False,
) -> SummaryStats:
    if not samples:
        raise EvalStatsError("No samples provided")

    x = list(samples)
    x_sorted = sorted(x)
    n = len(x_sorted)
    mean = statistics.fmean(x_sorted)
    stdev = statistics.stdev(x_sorted) if n >= 2 else 0.0

    mean_ci: dict[str, float] | None = None
    p95_ci: dict[str, float] | None = None
    p50_ci: dict[str, float] | None = None

    if not skip_ci:
        if HAS_NUMPY:
            # Fast NumPy vectorized bootstrap
            mean_ci = _bootstrap_ci_numpy(
                samples=x_sorted,
                stat="mean",
                n_resamples=bootstrap,
                alpha=alpha,
                seed=seed,
            )
            p95_ci = _bootstrap_ci_numpy(
                samples=x_sorted,
                stat="p95",
                n_resamples=bootstrap,
                alpha=alpha,
                seed=seed + 1,
            )
            p50_ci = _bootstrap_ci_numpy(
                samples=x_sorted,
                stat="p50",
                n_resamples=bootstrap,
                alpha=alpha,
                seed=seed + 2,
            )
        else:
            # Fallback to pure Python
            rng = random.Random(seed)
            mean_ci = _bootstrap_ci(
                rng=rng,
                samples=x_sorted,
                stat_fn=lambda s: statistics.fmean(s),
                n_resamples=bootstrap,
                alpha=alpha,
            )
            rng_p95 = random.Random(seed + 1)
            p95_ci = _bootstrap_ci(
                rng=rng_p95,
                samples=x_sorted,
                stat_fn=lambda s: _percentile(sorted(s), 95.0),
                n_resamples=bootstrap,
                alpha=alpha,
            )
            rng_p50 = random.Random(seed + 2)
            p50_ci = _bootstrap_ci(
                rng=rng_p50,
                samples=x_sorted,
                stat_fn=lambda s: _percentile(sorted(s), 50.0),
                n_resamples=bootstrap,
                alpha=alpha,
            )

    return SummaryStats(
        n=n,
        mean=float(mean),
        stdev=float(stdev),
        min=float(x_sorted[0]),
        max=float(x_sorted[-1]),
        p50=float(_percentile(x_sorted, 50.0)),
        p90=float(_percentile(x_sorted, 90.0)),
        p95=float(_percentile(x_sorted, 95.0)),
        p99=float(_percentile(x_sorted, 99.0)),
        mean_ci=mean_ci,
        p95_ci=p95_ci,
        p50_ci=p50_ci,
    )


def compare_samples(
    old: list[float],
    new: list[float],
    *,
    bootstrap: int = 10000,
    alpha: float = 0.01,
    seed: int = 0,
    power: float = 0.80,
    skip_ci: bool = False,
) -> dict[str, Any]:
    """
    Compare two independent sample sets (old vs new).

    All delta values are expressed as RELATIVE PERCENTAGES:
    - positive = regression (new is slower than old)
    - negative = improvement (new is faster than old)

    Args:
        old: Baseline samples
        new: New/head samples
        bootstrap: Number of bootstrap resamples for CIs
        alpha: Significance level (default 0.01)
        seed: Random seed for reproducibility
        power: Statistical power for MDE calculation
        skip_ci: If True, skip bootstrap CI computation for speed

    Returns a JSON-serializable dict containing:
    - per-group summaries (still in original units for reference)
    - deltas as relative percentages for mean and p95
    - bootstrap CIs for relative percentage deltas (unless skip_ci=True)
    - statistical tests (Welch's t-test, Cohen's d)
    - Minimum Detectable Effect (MDE) as relative percentage
    """

    old_s = summarize_samples(old, bootstrap=bootstrap, alpha=alpha, seed=seed, skip_ci=skip_ci)
    new_s = summarize_samples(new, bootstrap=bootstrap, alpha=alpha, seed=seed + 10, skip_ci=skip_ci)

    # Calculate relative percentage changes: (new - old) / old * 100
    def relative_pct(new_val: float, old_val: float) -> float:
        if old_val == 0.0:
            return 0.0
        return ((new_val - old_val) / old_val) * 100.0

    mean_delta_pct = relative_pct(new_s.mean, old_s.mean)
    p50_delta_pct = relative_pct(new_s.p50, old_s.p50)
    p95_delta_pct = relative_pct(new_s.p95, old_s.p95)

    mean_delta_ci: dict[str, float] | None = None
    p50_delta_ci: dict[str, float] | None = None
    p95_delta_ci: dict[str, float] | None = None

    if not skip_ci:
        if HAS_NUMPY:
            # Fast NumPy vectorized bootstrap
            mean_delta_ci = _bootstrap_delta_pct_numpy(
                old, new, stat="mean", n_resamples=bootstrap, alpha=alpha, seed=seed + 100
            )
            p50_delta_ci = _bootstrap_delta_pct_numpy(
                old, new, stat="p50", n_resamples=bootstrap, alpha=alpha, seed=seed + 102
            )
            p95_delta_ci = _bootstrap_delta_pct_numpy(
                old, new, stat="p95", n_resamples=bootstrap, alpha=alpha, seed=seed + 101
            )
        else:
            # Fallback to pure Python
            rng = random.Random(seed + 100)

            def boot_delta_pct(stat_fn: Callable[[list[float]], float]) -> dict[str, float]:
                n_old = len(old)
                n_new = len(new)
                stats: list[float] = []
                for _ in range(bootstrap):
                    o = [old[rng.randrange(0, n_old)] for _ in range(n_old)]
                    n = [new[rng.randrange(0, n_new)] for _ in range(n_new)]
                    old_stat = stat_fn(o)
                    new_stat = stat_fn(n)
                    if old_stat != 0:
                        pct_change = ((new_stat - old_stat) / old_stat) * 100.0
                    else:
                        pct_change = 0.0
                    stats.append(pct_change)
                stats.sort()
                lo = _percentile(stats, 100.0 * (alpha / 2.0))
                hi = _percentile(stats, 100.0 * (1.0 - alpha / 2.0))
                return {"lo": float(lo), "hi": float(hi)}

            mean_delta_ci = boot_delta_pct(lambda s: statistics.fmean(s))
            p50_delta_ci = boot_delta_pct(lambda s: _percentile(sorted(s), 50.0))
            p95_delta_ci = boot_delta_pct(lambda s: _percentile(sorted(s), 95.0))

    # Statistical tests
    t_stat, p_value = welch_t_test(old, new)
    d = cohens_d(new, old)  # new - old
    is_significant = p_value < alpha

    # Minimum Detectable Effect - express as relative percentage
    mde_abs, mde_d = calculate_mde(old, new, alpha=alpha, power=power)
    # We report and visualize median (p50) deltas; express MDE% on the same baseline for consistency.
    mde_rel_pct = (mde_abs / old_s.p50 * 100) if old_s.p50 > 0 else 0.0

    # Check if observed relative change (median) exceeds MDE percentage
    observed_pct = abs(p50_delta_pct)
    observed_exceeds_mde = observed_pct >= mde_rel_pct if mde_rel_pct > 0 else False

    return {
        "old": old_s.__dict__,
        "new": new_s.__dict__,
        "deltas": {
            "mean": {
                "pct": float(mean_delta_pct),
                "ci": mean_delta_ci,
            },
            "p50": {
                "pct": float(p50_delta_pct),
                "ci": p50_delta_ci,
            },
            "p95": {
                "pct": float(p95_delta_pct),
                "ci": p95_delta_ci,
            },
        },
        "statistical_tests": {
            "welch_t": {
                "t_statistic": float(t_stat),
                "p_value": float(p_value),
                "is_significant": is_significant,
            },
            "cohens_d": {
                "value": float(d),
                "interpretation": effect_size_interpretation(d),
            },
        },
        "mde": {
            "pct": float(mde_rel_pct),
            "cohens_d": float(mde_d),
            "power": float(power),
            "observed_exceeds_mde": observed_exceeds_mde,
            "baseline": "p50",
        },
        "meta": {
            "bootstrap": int(bootstrap),
            "alpha": float(alpha),
            "seed": int(seed),
            "power": float(power),
            "skip_ci": skip_ci,
            "note": "All deltas are relative percentages: (new-old)/old*100. Positive=regression, negative=improvement.",
        },
    }
