#!/usr/bin/env python3
"""Demonstrate bootstrap-based performance change detection for paired runs.

A "measurement" is one paired benchmark observation:
    (x_i^(A), x_i^(B))
where i indexes the paired run number (0..n-1).
"""

from __future__ import annotations

import argparse
import math
import pathlib
import random
import statistics
from typing import Iterable, List, Sequence, Tuple


def relative_changes_percent(a: Sequence[float], b: Sequence[float]) -> List[float]:
    """Compute per-pair relative changes in percent.

    Delta_i = ((x_i^(B) / x_i^(A)) - 1) * 100
    """
    if len(a) != len(b):
        raise ValueError("A and B must have the same number of paired measurements.")
    if not a:
        raise ValueError("Need at least one paired measurement.")

    deltas: List[float] = []
    for idx, (x_a, x_b) in enumerate(zip(a, b)):
        if x_a == 0:
            raise ValueError(f"Measurement {idx} in A is zero; cannot divide by zero.")
        deltas.append(((x_b / x_a) - 1.0) * 100.0)
    return deltas


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile for q in [0, 1]."""
    if not sorted_values:
        raise ValueError("Cannot compute percentile of empty data.")
    if len(sorted_values) == 1:
        return sorted_values[0]

    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    weight = pos - lo
    return sorted_values[lo] * (1.0 - weight) + sorted_values[hi] * weight


def bootstrap_median_ci(
    values: Sequence[float],
    n_bootstrap: int = 10_000,
    alpha: float = 0.05,
    seed: int = 42,
) -> Tuple[float, float]:
    """Percentile bootstrap CI for the median."""
    if not values:
        raise ValueError("Need at least one value for bootstrapping.")
    if n_bootstrap < 100:
        raise ValueError("Use at least 100 bootstrap samples.")
    if not (0.0 < alpha < 1.0):
        raise ValueError("alpha must be in (0, 1).")

    rng = random.Random(seed)
    n = len(values)
    medians: List[float] = []
    for _ in range(n_bootstrap):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        medians.append(statistics.median(sample))
    medians.sort()

    lower = percentile(medians, alpha / 2.0)
    upper = percentile(medians, 1.0 - alpha / 2.0)
    return lower, upper


def bootstrap_medians(
    values: Sequence[float], n_bootstrap: int = 10_000, seed: int = 42
) -> List[float]:
    """Generate bootstrap medians from resampled values."""
    if not values:
        raise ValueError("Need at least one value for bootstrapping.")
    if n_bootstrap < 100:
        raise ValueError("Use at least 100 bootstrap samples.")

    rng = random.Random(seed)
    n = len(values)
    medians: List[float] = []
    for _ in range(n_bootstrap):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        medians.append(statistics.median(sample))
    medians.sort()
    return medians


def make_figure(
    out_path: pathlib.Path,
) -> None:
    """Create a one-column-friendly conceptual figure with two CI examples."""
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError(
            "matplotlib is required for --figure-out. Install it with: pip install matplotlib"
        ) from exc

    # Conceptual examples for ~Delta (%):
    # Left CI includes 0% -> no significant performance change.
    # Right CI excludes 0% -> significant performance change.
    mean_no_change, sd_no_change = 0.2, 1.0
    mean_change, sd_change = 2.8, 0.7
    z99 = 2.576
    ci_no_change = (mean_no_change - z99 * sd_no_change, mean_no_change + z99 * sd_no_change)
    ci_change = (mean_change - z99 * sd_change, mean_change + z99 * sd_change)

    x_min, x_max = -4.5, 5.5
    x_values = [x_min + i * (x_max - x_min) / 800.0 for i in range(801)]

    def normal_pdf(x: float, mean: float, sd: float) -> float:
        scale = sd * math.sqrt(2.0 * math.pi)
        return math.exp(-0.5 * ((x - mean) / sd) ** 2) / scale

    y_no_change = [normal_pdf(x, mean_no_change, sd_no_change) for x in x_values]
    y_change = [normal_pdf(x, mean_change, sd_change) for x in x_values]

    plt.rcParams.update(
        {
            "font.size": 13,
            "axes.titlesize": 14,
            "axes.labelsize": 13,
            "xtick.labelsize": 12,
            "ytick.labelsize": 12,
        }
    )
    # Stacked panels are more legible when placed in a single-column paper layout.
    fig, (ax_top, ax_bottom) = plt.subplots(2, 1, figsize=(6.4, 7.8), sharex=True)

    def draw_ci_panel(
        ax,
        x_vals: Sequence[float],
        y_vals: Sequence[float],
        ci: Tuple[float, float],
        fill_gray: str,
        dist_linestyle: str,
        ci_linestyle: str,
        title: str,
        decision_label: str,
    ) -> None:
        max_y = max(y_vals)
        ci_y = max_y * 0.12

        # Main distribution in grayscale.
        ax.plot(
            x_vals,
            y_vals,
            linewidth=2.0,
            color="black",
            linestyle=dist_linestyle,
        )
        ax.fill_between(x_vals, y_vals, alpha=0.30, color=fill_gray)
        ax.axvline(0.0, linestyle="--", linewidth=1.5, color="black")

        # Explicit CI band and boundaries for readability.
        ax.axvspan(ci[0], ci[1], color="0.8", alpha=0.35)
        ax.hlines(
            y=ci_y,
            xmin=ci[0],
            xmax=ci[1],
            linewidth=4,
            color="black",
            linestyle=ci_linestyle,
            label="99% CI of $\\tilde{\\Delta}$",
        )
        ax.vlines(
            x=[ci[0], ci[1]],
            ymin=0.0,
            ymax=max_y * 0.23,
            linewidth=2.0,
            color="black",
        )

        ci_center_x = (ci[0] + ci[1]) / 2.0
        ax.annotate(
            f"99% CI: [{ci[0]:+.2f}%, {ci[1]:+.2f}%]",
            xy=(ci_center_x, ci_y),
            xytext=(ci_center_x, ci_y - max_y * 0.06),
            textcoords="data",
            ha="center",
            va="top",
            fontsize=12,
            bbox={"facecolor": "white", "edgecolor": "0.85", "pad": 0.2},
        )

        ax.set_title(title)
        ax.set_ylabel("Density")
        ax.set_xlim(x_min, x_max)
        ax.set_ylim(0.0, max_y * 1.12)
        ax.text(
            0.03,
            0.93,
            decision_label,
            transform=ax.transAxes,
            va="top",
            fontsize=12,
            bbox={"facecolor": "white", "edgecolor": "0.8", "boxstyle": "round"},
        )
        ax.grid(True, alpha=0.25)

    draw_ci_panel(
        ax=ax_top,
        x_vals=x_values,
        y_vals=y_no_change,
        ci=ci_no_change,
        fill_gray="0.75",
        dist_linestyle="-",
        ci_linestyle="-",
        title="Case A: 99% CI includes 0%",
        decision_label="Decision:\nNo significant change",
    )
    draw_ci_panel(
        ax=ax_bottom,
        x_vals=x_values,
        y_vals=y_change,
        ci=ci_change,
        fill_gray="0.6",
        dist_linestyle="-",
        ci_linestyle="-",
        title="Case B: 99% CI excludes 0%",
        decision_label="Decision:\nSignificant change",
    )
    ax_bottom.set_xlabel("Relative median change $\\tilde{\\Delta}$ (%)")

    fig.suptitle(
        "Performance Change Detection using $\\tilde{\\Delta}$ and Bootstrap CIs",
        fontsize=15,
    )
    fig.tight_layout(rect=[0.0, 0.0, 1.0, 0.97])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    plt.close(fig)


def print_table(pairs: Iterable[Tuple[int, float, float, float]]) -> None:
    print("Paired measurements and per-pair relative change")
    print("i\tA (ms)\tB (ms)\tDelta_i (%)")
    for idx, x_a, x_b, delta_i in pairs:
        print(f"{idx}\t{x_a:.2f}\t{x_b:.2f}\t{delta_i:+.2f}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Show bootstrap-based significance decision for performance changes "
            "using paired benchmark measurements."
        )
    )
    parser.add_argument(
        "--bootstrap-samples",
        type=int,
        default=10_000,
        help="Number of bootstrap resamples (default: 10000).",
    )
    parser.add_argument(
        "--alpha",
        type=float,
        default=0.01,
        help="Significance level alpha (default: 0.01 gives 99%% CI).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducibility.",
    )
    parser.add_argument(
        "--figure-out",
        type=str,
        default="",
        help=(
            "Optional output path for a figure (e.g., "
            "paper/graphs/perf_change_detection_demo.pdf)."
        ),
    )
    args = parser.parse_args()

    # Example paired benchmark measurements in milliseconds.
    # Each row is one synchronized pair i from the same experimental condition.
    measurements_a = [101.0, 98.0, 103.0, 100.0, 99.0, 102.0, 97.0, 101.0]
    measurements_b = [106.0, 103.0, 108.0, 105.0, 104.0, 106.0, 102.0, 105.0]

    deltas = relative_changes_percent(measurements_a, measurements_b)
    relative_median = statistics.median(deltas)
    bootstrap_dist = bootstrap_medians(
        deltas, n_bootstrap=args.bootstrap_samples, seed=args.seed
    )
    ci_low = percentile(bootstrap_dist, args.alpha / 2.0)
    ci_high = percentile(bootstrap_dist, 1.0 - args.alpha / 2.0)

    pairs = [
        (idx, x_a, x_b, delta_i)
        for idx, (x_a, x_b, delta_i) in enumerate(zip(measurements_a, measurements_b, deltas))
    ]
    print_table(pairs)

    conf = 100.0 * (1.0 - args.alpha)
    print(f"Relative median change (~Delta): {relative_median:+.2f}%")
    print(f"{conf:.1f}% bootstrap CI for ~Delta: [{ci_low:+.2f}%, {ci_high:+.2f}%]")
    print()

    if ci_low <= 0.0 <= ci_high:
        print("Decision: no significant performance change (CI includes 0).")
    else:
        print("Decision: significant performance change (CI excludes 0).")

    print()
    print("Interpretation notes:")
    print("- i is the paired-run index.")
    print("- A and B are the two service versions compared on the same run index i.")
    print("- *100 converts a relative ratio into percentage points for readability.")

    if args.figure_out:
        out_path = pathlib.Path(args.figure_out)
        make_figure(out_path=out_path)
        print(f"- Figure written to: {out_path}")


if __name__ == "__main__":
    main()
