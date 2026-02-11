from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any

from .eval_stats_bootstrap import (
    HAS_NUMPY,
    _bootstrap_delta_pct_distribution,
    _bootstrap_delta_pct_distribution_numpy,
    _percentile,
)
from .eval_stats_errors import EvalStatsError

# Optional matplotlib import for plotting
try:
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from matplotlib.figure import Figure

    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False
    plt = None  # type: ignore
    Figure = None  # type: ignore


#
# Display-name normalization (keep consistent with `impl/scripts/analyze_localization_reports.py`)
#
# In k6 eval reports, per-span keys are short "headlines" like: search/top_region/top_user.
# In localization aggregation, we attribute those to endpoint templates and then shorten them
# for plots using ENDPOINT_TO_NAME.
#
SPAN_NAME_TO_ENDPOINT: dict[str, str] = {
    "search": "/search",
    "top_region": "/top/region/:region/:count",
    "top_user": "/top/user/:userId/:count",
}

ENDPOINT_TO_NAME: dict[str, str] = {
    "/search": "U1 (E7)",
    "/top/region/:region/:count": "U2 (E8)",
    "/top/user/:userId/:count": "U3 (E9)",
}

#
# Preferred use-case order for multi-panel figures.
# (Fixes cases where we previously ordered spans by sample count, which can put U3 first.)
#
PREFERRED_SPAN_HEADLINES: list[str] = ["search", "top_region", "top_user"]
PREFERRED_ENDPOINTS: list[str] = [
    "/search",
    "/top/region/:region/:count",
    "/top/user/:userId/:count",
]


def _display_span_name(span_name: str) -> str:
    """
    Map a per-span headline (e.g. 'top_user') to the short endpoint name used in
    localization plots (e.g. 'U3 (E9)').
    """
    name = (span_name or "").strip()
    if not name:
        return span_name
    if name.startswith("/"):
        return ENDPOINT_TO_NAME.get(name, name)
    endpoint = SPAN_NAME_TO_ENDPOINT.get(name)
    if endpoint:
        return ENDPOINT_TO_NAME.get(endpoint, name)
    return name


def _preferred_span_rank(span_name: str) -> int | None:
    """
    Return the preferred U-order rank for a span name, or None if unknown.

    Handles:
    - k6 span headlines: search/top_region/top_user
    - endpoint templates: /search, /top/region/..., /top/user/...
    - already-mapped display labels: U1 (E7), ...
    """
    s = (span_name or "").strip()
    if not s:
        return None

    if s in PREFERRED_SPAN_HEADLINES:
        return PREFERRED_SPAN_HEADLINES.index(s)

    # Map headline -> endpoint, then rank.
    ep = SPAN_NAME_TO_ENDPOINT.get(s)
    if ep in PREFERRED_ENDPOINTS:
        return PREFERRED_ENDPOINTS.index(ep)

    if s in PREFERRED_ENDPOINTS:
        return PREFERRED_ENDPOINTS.index(s)

    # Display label -> endpoint rank
    for i, ep2 in enumerate(PREFERRED_ENDPOINTS):
        if ENDPOINT_TO_NAME.get(ep2) == s:
            return i

    # Loose fallback if caller passed "U1"/"U2"/"U3" prefixes.
    if s.startswith("U1"):
        return 0
    if s.startswith("U2"):
        return 1
    if s.startswith("U3"):
        return 2

    return None


def _sort_spans_for_display(
    spans: set[str],
    *,
    old_samples_by: dict[str, list[float]],
    new_samples_by: dict[str, list[float]],
    max_spans: int,
) -> list[str]:
    """
    Deterministic ordering for multi-span plots:
    - Prefer known use-cases U1→U2→U3
    - Then fall back to sample count (desc), then name (asc)
    """

    def _n(s: str) -> int:
        return len(old_samples_by.get(s, [])) + len(new_samples_by.get(s, []))

    def _key(s: str) -> tuple[int, int, int, str]:
        r = _preferred_span_rank(s)
        if r is not None:
            return (0, r, 0, s)
        return (1, 999, -_n(s), s)

    return sorted(spans, key=_key)[:max_spans]


def _ensure_matplotlib() -> None:
    """Raise if matplotlib is not installed and configure scientific style."""
    if not HAS_MATPLOTLIB:
        raise EvalStatsError(
            "matplotlib is required for plotting. Install with: pip install matplotlib"
        )

    # Configure scientific paper style
    plt.rcParams.update({
        "font.family": "serif",
        "font.serif": ["Times New Roman", "DejaVu Serif", "Liberation Serif", "serif"],
        "font.size": 10,
        "axes.labelsize": 10,
        "axes.titlesize": 12,
        "xtick.labelsize": 9,
        "ytick.labelsize": 9,
        "legend.fontsize": 9,
        "figure.titlesize": 14,
        "axes.grid": True,
        "grid.alpha": 0.3,
        "grid.linestyle": "--",
        "savefig.dpi": 300,
        "savefig.transparent": False,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "figure.autolayout": True,
    })


def _save_figure_pdf(fig: "Figure", save_path: Path | str) -> Path:
    """Save figure as PDF, forcing .pdf suffix."""
    path = Path(save_path)
    if path.suffix.lower() != ".pdf":
        path = path.with_suffix(".pdf")
    fig.savefig(str(path), bbox_inches="tight", format="pdf")
    return path


def _get_color_for_change(value: float, threshold: float = 0.0) -> str:
    """Return color based on whether value indicates improvement or regression."""
    if value > threshold:
        return "#4D4D4D"  # Dark gray (regression)
    if value < -threshold:
        return "#B3B3B3"  # Light gray (improvement)
    return "#7F7F7F"  # Mid gray (negligible)


def _get_effect_color(interpretation: str) -> str:
    """Return color based on effect size interpretation."""
    colors = {
        # Grayscale ramp (paper / printer friendly)
        "negligible": "#B3B3B3",  # Light gray
        "small": "#8F8F8F",       # Mid gray
        "medium": "#666666",      # Darker gray
        "large": "#3A3A3A",       # Dark gray
    }
    return colors.get(interpretation, "#7F7F7F")


def _bin_timeseries_mean(
    points: list[tuple[int, float]],
    *,
    bin_size_ms: int = 1000,
) -> tuple[list[float], list[float]]:
    """
    Bin a (ts_ms, value) series into fixed time buckets and compute mean per bucket.

    Returns:
        (x_seconds_from_start, y_values)
    """
    if not points:
        return ([], [])

    t0 = points[0][0]
    buckets: dict[int, list[float]] = {}
    for ts, v in points:
        b = int((ts - t0) // bin_size_ms)
        buckets.setdefault(b, []).append(v)

    xs: list[float] = []
    ys: list[float] = []
    for b in sorted(buckets.keys()):
        vals = buckets[b]
        if not vals:
            continue
        xs.append((b * bin_size_ms) / 1000.0)
        ys.append(statistics.fmean(vals))
    return xs, ys


def plot_cpu_steal_over_time(
    old_series: list[tuple[int, float]],
    new_series: list[tuple[int, float]],
    *,
    title: str = "CPU Steal Time Over Experiment (binned mean)",
    figsize: tuple[float, float] = (10, 4),
    bin_size_ms: int = 1000,
    save_path: Path | str | None = None,
) -> "Figure":
    """
    Plot CPU steal ratio over time for base vs head.

    - Uses `cpu_steal_pct` (ratio in [0,1]) written by `worker.js`.
    - Bins points into `bin_size_ms` buckets and plots mean per bucket.
    """
    _ensure_matplotlib()

    fig, ax = plt.subplots(figsize=figsize)

    x_old, y_old = _bin_timeseries_mean(old_series, bin_size_ms=bin_size_ms)
    x_new, y_new = _bin_timeseries_mean(new_series, bin_size_ms=bin_size_ms)

    if x_old:
        ax.plot(x_old, [v * 100.0 for v in y_old], color="#4D4D4D", linewidth=1.5, label="Base")
    if x_new:
        ax.plot(x_new, [v * 100.0 for v in y_new], color="#9A9A9A", linewidth=1.5, label="Head")

    ax.set_title(title)
    ax.set_xlabel("Time since start (s)")
    ax.set_ylabel("CPU steal (%)")
    ax.legend(loc="best", frameon=False)

    if save_path:
        _save_figure_pdf(fig, save_path)
    return fig


def plot_cpu_utilization_over_time(
    old_series: list[tuple[int, float]],
    new_series: list[tuple[int, float]],
    *,
    title: str = "CPU Utilization Over Experiment (binned mean)",
    figsize: tuple[float, float] = (10, 4),
    bin_size_ms: int = 1000,
    save_path: Path | str | None = None,
) -> "Figure":
    """
    Plot CPU utilization ratio over time for base vs head.

    - Uses `cpu_utilization_pct` (ratio in [0,1]) written by `worker.js`.
    - Bins points into `bin_size_ms` buckets and plots mean per bucket.
    """
    _ensure_matplotlib()

    fig, ax = plt.subplots(figsize=figsize)

    x_old, y_old = _bin_timeseries_mean(old_series, bin_size_ms=bin_size_ms)
    x_new, y_new = _bin_timeseries_mean(new_series, bin_size_ms=bin_size_ms)

    if x_old:
        ax.plot(x_old, [v * 100.0 for v in y_old], color="#4D4D4D", linewidth=1.5, label="Base")
    if x_new:
        ax.plot(x_new, [v * 100.0 for v in y_new], color="#9A9A9A", linewidth=1.5, label="Head")

    ax.set_title(title)
    ax.set_xlabel("Time since start (s)")
    ax.set_ylabel("CPU utilization (%)")
    ax.legend(loc="best", frameon=False)

    if save_path:
        _save_figure_pdf(fig, save_path)
    return fig


def plot_cpu_time_median_comparison(
    old_samples: list[float],
    new_samples: list[float],
    *,
    title: str = "CPU Time Median Comparison (per-span user+system)",
    figsize: tuple[float, float] = (6, 4),
    save_path: Path | str | None = None,
) -> "Figure":
    """
    Compare base vs head median CPU time per span (user+system, ms).
    """
    _ensure_matplotlib()

    fig, ax = plt.subplots(figsize=figsize)

    # Boxplot emphasizes distribution; median is central to the comparison.
    bp = ax.boxplot(
        [old_samples, new_samples],
        labels=["Base", "Head"],
        patch_artist=True,
        showfliers=False,
        widths=0.55,
    )
    bp["boxes"][0].set(facecolor="#4D4D4D", alpha=0.25, edgecolor="#4D4D4D")
    bp["boxes"][1].set(facecolor="#9A9A9A", alpha=0.25, edgecolor="#9A9A9A")
    for median in bp["medians"]:
        median.set(color="#333333", linewidth=1.8)

    # Median annotation + relative delta.
    old_med = statistics.median(old_samples) if old_samples else 0.0
    new_med = statistics.median(new_samples) if new_samples else 0.0
    delta_pct = ((new_med - old_med) / old_med * 100.0) if old_med != 0 else 0.0

    ax.set_title(title)
    ax.set_ylabel("CPU time (ms)")
    ax.grid(True, axis="y", alpha=0.3, linestyle="--")

    ax.text(
        0.98,
        0.02,
        f"Median Δ: {delta_pct:+.1f}%",
        transform=ax.transAxes,
        ha="right",
        va="bottom",
        fontsize=9,
        color="#333333",
    )

    if save_path:
        _save_figure_pdf(fig, save_path)
    return fig


def plot_mean_deltas_with_ci(
    report: dict[str, Any],
    *,
    title: str = "Mean Latency Change (%)",
    figsize: tuple[float, float] = (10, 6),
    save_path: Path | str | None = None,
) -> "Figure":
    """
    Create a bar chart showing mean latency deltas as percentages with confidence intervals.

    Suitable for scientific papers: uses clean styling, error bars, and significance markers.

    Args:
        report: Evaluation report dict (from compare_samples or per-span report)
        title: Plot title
        figsize: Figure size (width, height)
        save_path: If provided, save figure to this path
    """
    _ensure_matplotlib()

    # Extract per-span data
    if "per_span" in report:
        per_span = report["per_span"]
    else:
        per_span = {"overall": report}

    span_data = []
    for span_name, data in per_span.items():
        deltas = data.get("deltas", {}).get("mean", {})
        delta_pct = deltas.get("pct", 0)
        ci = deltas.get("ci") or {}  # Handle None from skip_ci mode
        ci_lo = ci.get("lo", delta_pct) if ci else delta_pct
        ci_hi = ci.get("hi", delta_pct) if ci else delta_pct

        err_lo = delta_pct - ci_lo
        err_hi = ci_hi - delta_pct

        tests = data.get("statistical_tests", {})
        is_sig = tests.get("welch_t", {}).get("is_significant", False)

        span_data.append({
            "name": span_name,
            "delta": delta_pct,
            "err_lo": err_lo,
            "err_hi": err_hi,
            "is_significant": is_sig,
        })

    span_data.sort(key=lambda x: x["delta"], reverse=True)

    fig, ax = plt.subplots(figsize=figsize)

    y_pos = list(range(len(span_data)))
    names = [_display_span_name(d["name"]) for d in span_data]
    deltas = [d["delta"] for d in span_data]
    errors = [[d["err_lo"] for d in span_data], [d["err_hi"] for d in span_data]]
    colors = [_get_color_for_change(d["delta"]) for d in span_data]

    # Create horizontal bar chart
    ax.barh(y_pos, deltas, color=colors, alpha=0.85, edgecolor="none")
    ax.errorbar(
        deltas, y_pos, xerr=errors, fmt="none", color="#333333", capsize=3, capthick=1, linewidth=1
    )

    # Add significance markers
    for i, d in enumerate(span_data):
        if d["is_significant"]:
            # Position * at the end of the error bar
            x_pos = d["delta"] + (errors[1][i] if d["delta"] >= 0 else -errors[0][i])
            offset = 0.5 if d["delta"] >= 0 else -0.5
            ax.annotate("*", (x_pos + offset, i), fontsize=12, fontweight="bold",
                        ha="left" if d["delta"] >= 0 else "right", va="center")

    ax.axvline(x=0, color="#333333", linewidth=1)

    ax.set_yticks(y_pos)
    ax.set_yticklabels(names)
    ax.set_xlabel("Relative Change in Mean Latency (%)")
    ax.set_title(title)
    ax.invert_yaxis()

    # Format x-axis as percentages
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:+.0f}%"))

    # Scientific legends often don't have borders
    legend_patches = [
        mpatches.Patch(color="#B3B3B3", alpha=0.85, label="Improvement"),
        mpatches.Patch(color="#4D4D4D", alpha=0.85, label="Regression"),
    ]
    ax.legend(handles=legend_patches, loc="best", frameon=False)

    # Significance note
    ax.text(
        1.0, -0.1, "* p < 0.05 (Welch's t-test)",
        transform=ax.transAxes, fontsize=8, ha="right", color="#555555"
    )

    if save_path:
        _save_figure_pdf(fig, save_path)

    return fig


def plot_effect_sizes(
    report: dict[str, Any],
    *,
    title: str = "Effect Sizes (Cohen's d)",
    figsize: tuple[float, float] = (10, 6),
    save_path: Path | str | None = None,
) -> "Figure":
    """
    Create a bar chart showing Cohen's d effect sizes with interpretation bands.

    Standardized effect size visualization suitable for academic reporting.
    """
    _ensure_matplotlib()

    if "per_span" in report:
        per_span = report["per_span"]
    else:
        per_span = {"overall": report}

    span_data = []
    for span_name, data in per_span.items():
        cohens = data.get("statistical_tests", {}).get("cohens_d", {})
        deltas = data.get("deltas", {}).get("mean", {})
        d_value = cohens.get("value", 0)
        interpretation = cohens.get("interpretation", "negligible")
        delta_pct = deltas.get("pct", 0)
        span_data.append({
            "name": span_name,
            "d": d_value,
            "interpretation": interpretation,
            "delta_pct": delta_pct,
        })

    span_data.sort(key=lambda x: x["d"], reverse=True)

    fig, ax = plt.subplots(figsize=figsize)

    y_pos = list(range(len(span_data)))
    names = [_display_span_name(d["name"]) for d in span_data]
    d_values = [d["d"] for d in span_data]
    colors = [_get_effect_color(d["interpretation"]) for d in span_data]

    ax.barh(y_pos, d_values, color=colors, alpha=0.85, edgecolor="none")

    # Add percentage annotations next to bars
    for i, d in enumerate(span_data):
        x_pos = d["d"]
        pct_label = f"{d['delta_pct']:+.1f}%"
        ha = "left" if d["d"] >= 0 else "right"
        offset = 0.05 if d["d"] >= 0 else -0.05
        ax.annotate(
            pct_label, (x_pos + offset, i),
            fontsize=8, ha=ha, va="center", color="#333333"
        )

    # Add interpretation threshold lines
    thresholds = [(0.2, "Small"), (0.5, "Medium"), (0.8, "Large")]
    for threshold, label in thresholds:
        ax.axvline(x=threshold, color="#999999", linewidth=0.8, linestyle="--")
        ax.axvline(x=-threshold, color="#999999", linewidth=0.8, linestyle="--")
        # Add labels for thresholds at the top
        ax.text(threshold, -0.7, label, ha="center", va="bottom", fontsize=7, color="#777777")
        ax.text(-threshold, -0.7, label, ha="center", va="bottom", fontsize=7, color="#777777")

    ax.axvline(x=0, color="#333333", linewidth=1)

    ax.set_yticks(y_pos)
    ax.set_yticklabels(names)
    ax.set_xlabel("Cohen's $d$ (Standardized Effect Size)")
    ax.set_title(title)
    ax.invert_yaxis()

    # Legend
    legend_patches = [
        mpatches.Patch(color="#B3B3B3", alpha=0.85, label="Negligible"),
        mpatches.Patch(color="#8F8F8F", alpha=0.85, label="Small"),
        mpatches.Patch(color="#666666", alpha=0.85, label="Medium"),
        mpatches.Patch(color="#3A3A3A", alpha=0.85, label="Large"),
    ]
    ax.legend(handles=legend_patches, loc="best", frameon=False, title="Effect Magnitude")

    if save_path:
        _save_figure_pdf(fig, save_path)

    return fig


def plot_percentile_comparison(
    report: dict[str, Any],
    *,
    percentile: str = "p95",
    title: str | None = None,
    x_label: str | None = None,
    figsize: tuple[float, float] = (10, 6),
    save_path: Path | str | None = None,
) -> "Figure":
    """
    Create a bar chart showing percentile latency changes as percentages.

    Academic style with clear labeling and optional confidence intervals.
    """
    _ensure_matplotlib()

    if title is None:
        title = f"{percentile.upper()} Latency Change (%)"

    if "per_span" in report:
        per_span = report["per_span"]
    else:
        per_span = {"overall": report}

    span_data = []
    for span_name, data in per_span.items():
        if percentile in {"p95", "p50"} and "deltas" in data:
            key = "p95" if percentile == "p95" else "p50"
            deltas = data.get("deltas", {}).get(key, {})
            delta_pct = deltas.get("pct", 0)
            ci = deltas.get("ci") or {}  # Handle None from skip_ci mode
            ci_lo = ci.get("lo", delta_pct) if ci else delta_pct
            ci_hi = ci.get("hi", delta_pct) if ci else delta_pct
        else:
            old_val = data.get("old", {}).get(percentile, 0)
            new_val = data.get("new", {}).get(percentile, 0)
            delta_pct = ((new_val - old_val) / old_val * 100.0) if old_val != 0 else 0.0
            ci_lo = delta_pct
            ci_hi = delta_pct

        span_data.append({
            "name": span_name,
            "delta_pct": delta_pct,
            "ci_lo": ci_lo,
            "ci_hi": ci_hi,
        })

    span_data.sort(key=lambda x: x["delta_pct"], reverse=True)

    fig, ax = plt.subplots(figsize=figsize)

    y_pos = list(range(len(span_data)))
    names = [_display_span_name(d["name"]) for d in span_data]
    deltas = [d["delta_pct"] for d in span_data]
    colors = [_get_color_for_change(d["delta_pct"]) for d in span_data]

    ax.barh(y_pos, deltas, color=colors, alpha=0.85, edgecolor="none")

    if percentile in {"p95", "p50"}:
        errors_lo = [d["delta_pct"] - d["ci_lo"] for d in span_data]
        errors_hi = [d["ci_hi"] - d["delta_pct"] for d in span_data]
        ax.errorbar(
            deltas, y_pos, xerr=[errors_lo, errors_hi], fmt="none", color="#333333", capsize=2, linewidth=1
        )
        # CI-based significance marker: CI excludes 0
        for i, d in enumerate(span_data):
            if d["ci_lo"] > 0 or d["ci_hi"] < 0:
                # Position * at end of error bar
                x_pos = d["delta_pct"] + (errors_hi[i] if d["delta_pct"] >= 0 else -errors_lo[i])
                offset = 0.5 if d["delta_pct"] >= 0 else -0.5
                ax.annotate(
                    "*",
                    (x_pos + offset, i),
                    fontsize=12,
                    fontweight="bold",
                    ha="left" if d["delta_pct"] >= 0 else "right",
                    va="center",
                )

    ax.axvline(x=0, color="#333333", linewidth=1)

    ax.set_yticks(y_pos)
    ax.set_yticklabels(names)
    ax.set_xlabel(x_label or f"Relative Change in {percentile.upper()} Latency (%)")
    ax.set_title(title)
    ax.invert_yaxis()

    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:+.0f}%"))

    legend_patches = [
        mpatches.Patch(color="#B3B3B3", alpha=0.85, label="Improvement"),
        mpatches.Patch(color="#4D4D4D", alpha=0.85, label="Regression"),
    ]
    ax.legend(handles=legend_patches, loc="best", frameon=False)

    if percentile in {"p95", "p50"}:
        ax.text(
            1.0,
            -0.1,
            "* CI excludes 0 (bootstrap)",
            transform=ax.transAxes,
            fontsize=8,
            ha="right",
            color="#555555",
        )

    if save_path:
        _save_figure_pdf(fig, save_path)

    return fig


def plot_sample_sizes(
    report: dict[str, Any],
    *,
    title: str = "Sample Sizes per Span",
    figsize: tuple[float, float] = (10, 6),
    save_path: Path | str | None = None,
) -> "Figure":
    """
    Create a grouped bar chart showing sample sizes for old and new data.
    """
    _ensure_matplotlib()

    if "per_span" in report:
        per_span = report["per_span"]
    else:
        per_span = {"overall": report}

    span_data = []
    for span_name, data in per_span.items():
        n_old = data.get("old", {}).get("n", 0)
        n_new = data.get("new", {}).get("n", 0)
        span_data.append({
            "name": span_name,
            "n_old": n_old,
            "n_new": n_new,
        })

    span_data.sort(key=lambda x: x["n_old"] + x["n_new"], reverse=True)

    fig, ax = plt.subplots(figsize=figsize)

    y_pos = list(range(len(span_data)))
    bar_height = 0.35

    n_old = [d["n_old"] for d in span_data]
    n_new = [d["n_new"] for d in span_data]
    names = [_display_span_name(d["name"]) for d in span_data]

    y_old = [y - bar_height / 2 for y in y_pos]
    y_new = [y + bar_height / 2 for y in y_pos]

    ax.barh(y_old, n_old, bar_height, label="Base", color="#4D4D4D", alpha=0.8, edgecolor="none")
    ax.barh(y_new, n_new, bar_height, label="Head", color="#9A9A9A", alpha=0.8, edgecolor="none")

    ax.set_yticks(y_pos)
    ax.set_yticklabels(names)
    ax.set_xlabel("Number of Samples ($n$)")
    ax.set_title(title)
    ax.legend(loc="best", frameon=False)
    ax.invert_yaxis()

    if save_path:
        _save_figure_pdf(fig, save_path)

    return fig


def plot_span_bootstrap_confidence_intervals(
    report: dict[str, Any],
    *,
    metric: str = "p50",  # "mean" | "p50" | "p95"
    relative: bool = False,
    title: str | None = None,
    figsize: tuple[float, float] = (10, 6),
    save_path: Path | str | None = None,
) -> "Figure":
    """
    Plot bootstrapped confidence intervals for each span (base vs head or relative change).
    """
    _ensure_matplotlib()

    if "per_span" in report:
        per_span = report["per_span"]
    else:
        per_span = {"overall": report}

    if metric not in {"mean", "p50", "p95"}:
        raise EvalStatsError(f"Unsupported metric for CI plot: {metric}")

    if title is None:
        if metric == "mean":
            metric_label = "Mean"
        elif metric == "p50":
            metric_label = "Median"
        else:
            metric_label = "P95"
        if relative:
            title = f"{metric_label} Relative Change with Bootstrap CIs (per span)"
        else:
            title = f"{metric_label} Latency with Bootstrap CIs (per span)"

    span_data = []
    for span_name, data in per_span.items():
        if relative:
            deltas = data.get("deltas", {}).get(metric, {})
            delta_pct = deltas.get("pct", 0)
            ci = deltas.get("ci") or {}
            span_data.append({
                "name": span_name,
                "delta_pct": delta_pct,
                "ci_lo": ci.get("lo", delta_pct) if ci else delta_pct,
                "ci_hi": ci.get("hi", delta_pct) if ci else delta_pct,
            })
        else:
            old = data.get("old", {})
            new = data.get("new", {})

            if metric == "mean":
                old_val = old.get("mean", 0)
                new_val = new.get("mean", 0)
                old_ci = old.get("mean_ci") or {}
                new_ci = new.get("mean_ci") or {}
            elif metric == "p50":
                old_val = old.get("p50", 0)
                new_val = new.get("p50", 0)
                old_ci = old.get("p50_ci") or {}
                new_ci = new.get("p50_ci") or {}
            else:
                old_val = old.get("p95", 0)
                new_val = new.get("p95", 0)
                old_ci = old.get("p95_ci") or {}
                new_ci = new.get("p95_ci") or {}

            span_data.append({
                "name": span_name,
                "old_val": old_val,
                "new_val": new_val,
                "old_ci_lo": old_ci.get("lo", old_val),
                "old_ci_hi": old_ci.get("hi", old_val),
                "new_ci_lo": new_ci.get("lo", new_val),
                "new_ci_hi": new_ci.get("hi", new_val),
            })

    if relative:
        span_data.sort(key=lambda x: x["delta_pct"], reverse=True)

        fig, ax = plt.subplots(figsize=figsize)
        y_pos = list(range(len(span_data)))

        deltas = [d["delta_pct"] for d in span_data]
        colors = [_get_color_for_change(d) for d in deltas]
        errors_lo = [d["delta_pct"] - d["ci_lo"] for d in span_data]
        errors_hi = [d["ci_hi"] - d["delta_pct"] for d in span_data]

        ax.barh(y_pos, deltas, color=colors, alpha=0.85, edgecolor="none")
        ax.errorbar(
            deltas,
            y_pos,
            xerr=[errors_lo, errors_hi],
            fmt="none",
            color="#333333",
            capsize=2,
            linewidth=1,
        )

        for i, d in enumerate(span_data):
            if d["ci_lo"] > 0 or d["ci_hi"] < 0:
                x_pos = d["delta_pct"] + (errors_hi[i] if d["delta_pct"] >= 0 else -errors_lo[i])
                offset = 0.5 if d["delta_pct"] >= 0 else -0.5
                ax.annotate(
                    "*",
                    (x_pos + offset, i),
                    fontsize=12,
                    fontweight="bold",
                    ha="left" if d["delta_pct"] >= 0 else "right",
                    va="center",
                )

        ax.axvline(x=0, color="#333333", linewidth=1)

        ax.set_yticks(y_pos)
        ax.set_yticklabels([_display_span_name(d["name"]) for d in span_data])
        ax.set_xlabel("Relative Change (%)")
        ax.set_title(title)
        ax.invert_yaxis()

        ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:+.0f}%"))

        legend_patches = [
            mpatches.Patch(color="#B3B3B3", alpha=0.85, label="Improvement"),
            mpatches.Patch(color="#4D4D4D", alpha=0.85, label="Regression"),
        ]
        ax.legend(handles=legend_patches, loc="best", frameon=False)

        ax.text(
            1.0,
            -0.1,
            "* CI excludes 0 (bootstrap)",
            transform=ax.transAxes,
            fontsize=8,
            ha="right",
            color="#555555",
        )
    else:
        span_data.sort(key=lambda x: x["old_val"] + x["new_val"], reverse=True)

        fig, ax = plt.subplots(figsize=figsize)
        y_pos = list(range(len(span_data)))
        offset = 0.15

        old_vals = [d["old_val"] for d in span_data]
        new_vals = [d["new_val"] for d in span_data]
        old_err = [
            [d["old_val"] - d["old_ci_lo"] for d in span_data],
            [d["old_ci_hi"] - d["old_val"] for d in span_data],
        ]
        new_err = [
            [d["new_val"] - d["new_ci_lo"] for d in span_data],
            [d["new_ci_hi"] - d["new_val"] for d in span_data],
        ]

        ax.errorbar(
            old_vals,
            [y - offset for y in y_pos],
            xerr=old_err,
            fmt="o",
            color="#4D4D4D",
            capsize=3,
            label="Base",
        )
        ax.errorbar(
            new_vals,
            [y + offset for y in y_pos],
            xerr=new_err,
            fmt="o",
            color="#9A9A9A",
            capsize=3,
            label="Head",
        )

        ax.set_yticks(y_pos)
        ax.set_yticklabels([_display_span_name(d["name"]) for d in span_data])
        ax.set_xlabel("Latency (ms)")
        ax.set_title(title)
        ax.legend(loc="best", frameon=False)
        ax.invert_yaxis()

    if save_path:
        _save_figure_pdf(fig, save_path)

    return fig


def plot_relative_change_distribution(
    old_samples: list[float],
    new_samples: list[float],
    *,
    stat: str = "p50",  # "mean" | "p50" | "p95"
    bootstrap: int = 2000,
    alpha: float = 0.01,
    seed: int = 0,
    title: str = "Wall Time Relative Change Distribution (Bootstrap)",
    figsize: tuple[float, float] = (8, 4),
    bins: int = 40,
    save_path: Path | str | None = None,
) -> "Figure":
    """
    Plot bootstrap distribution of relative change (%) for wall time.
    """
    _ensure_matplotlib()

    if not old_samples or not new_samples:
        raise EvalStatsError("Cannot plot relative change distribution with empty samples")

    if HAS_NUMPY:
        dist = _bootstrap_delta_pct_distribution_numpy(
            old_samples,
            new_samples,
            stat=stat,
            n_resamples=bootstrap,
            seed=seed,
        )
        dist_sorted = sorted(dist)
        lo = _percentile(dist_sorted, 100.0 * (alpha / 2.0))
        hi = _percentile(dist_sorted, 100.0 * (1.0 - alpha / 2.0))
    else:
        dist = _bootstrap_delta_pct_distribution(
            old_samples,
            new_samples,
            stat=stat,
            n_resamples=bootstrap,
            seed=seed,
        )
        dist_sorted = sorted(dist)
        lo = _percentile(dist_sorted, 100.0 * (alpha / 2.0))
        hi = _percentile(dist_sorted, 100.0 * (1.0 - alpha / 2.0))

    def stat_value(values: list[float]) -> float:
        if stat == "mean":
            return statistics.fmean(values)
        if stat == "p50":
            return _percentile(sorted(values), 50.0)
        if stat == "p95":
            return _percentile(sorted(values), 95.0)
        raise EvalStatsError(f"Unknown stat: {stat}")

    old_stat = stat_value(old_samples)
    new_stat = stat_value(new_samples)
    observed = ((new_stat - old_stat) / old_stat * 100.0) if old_stat != 0 else 0.0

    fig, ax = plt.subplots(figsize=figsize)
    ax.hist(dist, bins=bins, color="#8C8C8C", alpha=0.7, edgecolor="white")
    ax.axvline(0, color="#333333", linewidth=1, linestyle="--")
    ax.axvline(observed, color="#1F1F1F", linewidth=1.2, label=f"Observed: {observed:+.1f}%")
    ax.axvspan(lo, hi, color="#D9D9D9", alpha=0.6, label=f"{int((1-alpha)*100)}% CI")

    ax.set_xlabel("Relative Change (%)")
    ax.set_ylabel("Bootstrap frequency")
    ax.set_title(title)
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:+.0f}%"))
    ax.legend(loc="best", frameon=False)

    if save_path:
        _save_figure_pdf(fig, save_path)

    return fig


def plot_qq_plot(
    old_samples: list[float],
    new_samples: list[float],
    *,
    percentile_step: float = 1.0,
    title: str = "Q-Q Plot (Relative Performance Differences)",
    figsize: tuple[float, float] = (6, 6),
    save_path: Path | str | None = None,
) -> "Figure":
    """
    Plot quantile-quantile comparison between traditional and instrumented samples.
    """
    _ensure_matplotlib()

    if not old_samples or not new_samples:
        raise EvalStatsError("Cannot plot Q-Q with empty samples")
    if percentile_step <= 0:
        raise EvalStatsError("percentile_step must be > 0")

    old_sorted = sorted(old_samples)
    new_sorted = sorted(new_samples)

    percentiles: list[float] = []
    p = percentile_step
    while p <= 100.0:
        percentiles.append(p)
        p += percentile_step
    if not percentiles:
        raise EvalStatsError("No percentiles generated for Q-Q plot")

    old_q = [_percentile(old_sorted, pct) for pct in percentiles]
    new_q = [_percentile(new_sorted, pct) for pct in percentiles]

    fig, ax = plt.subplots(figsize=figsize)
    ax.scatter(old_q, new_q, s=12, alpha=0.75, color="#5C5C5C", edgecolor="none")

    q_min = min(min(old_q), min(new_q))
    q_max = max(max(old_q), max(new_q))
    q_pad = (q_max - q_min) * 0.03 if q_max > q_min else 1.0
    lo = q_min - q_pad
    hi = q_max + q_pad
    ax.plot([lo, hi], [lo, hi], color="#1F1F1F", linewidth=1, linestyle="--")

    ax.set_xlabel("Traditional Quantile (ms)")
    ax.set_ylabel("Instrumented Quantile (ms)")
    ax.set_title(title)
    ax.set_xlim(lo, hi)
    ax.set_ylim(lo, hi)
    ax.set_aspect("equal", adjustable="box")

    old_med = statistics.median(old_samples) if old_samples else 0.0
    new_med = statistics.median(new_samples) if new_samples else 0.0
    med_delta = ((new_med - old_med) / old_med * 100.0) if old_med != 0 else 0.0
    ax.text(
        0.98,
        0.02,
        f"Median Δ: {med_delta:+.1f}%",
        transform=ax.transAxes,
        ha="right",
        va="bottom",
        fontsize=9,
        color="#333333",
    )

    if save_path:
        _save_figure_pdf(fig, save_path)

    return fig


def plot_span_qq_plots(
    old_samples_by: dict[str, list[float]],
    new_samples_by: dict[str, list[float]],
    *,
    percentile_step: float = 1.0,
    title: str | None = None,
    figsize: tuple[float, float] = (12, 4),
    span_names: list[str] | None = None,
    max_spans: int = 6,
    save_path: Path | str | None = None,
) -> "Figure":
    """
    Plot quantile-quantile comparisons for multiple spans.
    """
    _ensure_matplotlib()
    if percentile_step <= 0:
        raise EvalStatsError("percentile_step must be > 0")

    common_spans = set(old_samples_by.keys()) & set(new_samples_by.keys())
    if span_names:
        spans_to_plot = [s for s in span_names if s in common_spans]
    else:
        spans_to_plot = _sort_spans_for_display(
            common_spans,
            old_samples_by=old_samples_by,
            new_samples_by=new_samples_by,
            max_spans=max_spans,
        )

    if not spans_to_plot:
        raise EvalStatsError("No common spans found to plot")

    n_spans = len(spans_to_plot)
    n_cols = min(3, n_spans)
    n_rows = (n_spans + n_cols - 1) // n_cols

    fig, axes = plt.subplots(n_rows, n_cols, figsize=figsize, squeeze=False)
    axes = axes.flatten()

    percentiles: list[float] = []
    p = percentile_step
    while p < 100.0:
        percentiles.append(p)
        p += percentile_step
    if not percentiles:
        raise EvalStatsError("No percentiles generated for Q-Q plot")

    for i, span_name in enumerate(spans_to_plot):
        ax = axes[i]
        old_samples = old_samples_by[span_name]
        new_samples = new_samples_by[span_name]
        if not old_samples or not new_samples:
            ax.set_visible(False)
            continue

        old_sorted = sorted(old_samples)
        new_sorted = sorted(new_samples)

        old_q = [_percentile(old_sorted, pct) for pct in percentiles]
        new_q = [_percentile(new_sorted, pct) for pct in percentiles]

        ax.scatter(old_q, new_q, s=12, alpha=0.75, color="#5C5C5C", edgecolor="none")

        q_min = min(min(old_q), min(new_q))
        q_max = max(max(old_q), max(new_q))
        q_pad = (q_max - q_min) * 0.03 if q_max > q_min else 1.0
        lo = q_min - q_pad
        hi = q_max + q_pad
        ax.plot([lo, hi], [lo, hi], color="#1F1F1F", linewidth=1, linestyle="--")

        ax.set_title(_display_span_name(span_name), fontsize=9)
        ax.set_xlabel("Traditional Quantile (ms)")
        ax.set_ylabel("Instrumented Quantile (ms)")
        ax.set_xlim(lo, hi)
        ax.set_ylim(lo, hi)
        ax.set_aspect("equal", adjustable="box")

        old_med = statistics.median(old_samples) if old_samples else 0.0
        new_med = statistics.median(new_samples) if new_samples else 0.0
        med_delta = ((new_med - old_med) / old_med * 100.0) if old_med != 0 else 0.0
        ax.text(
            0.98,
            0.02,
            f"Median Δ: {med_delta:+.1f}%",
            transform=ax.transAxes,
            ha="right",
            va="bottom",
            fontsize=8,
            color="#333333",
        )

    for j in range(n_spans, len(axes)):
        axes[j].set_visible(False)

    if title:
        fig.suptitle(title, fontsize=12, y=1.0)
    plt.tight_layout()

    if save_path:
        _save_figure_pdf(fig, save_path)

    return fig


def plot_span_relative_change_distributions(
    old_samples_by: dict[str, list[float]],
    new_samples_by: dict[str, list[float]],
    *,
    stat: str = "p50",  # "mean" | "p50" | "p95"
    bootstrap: int = 2000,
    alpha: float = 0.01,
    seed: int = 0,
    title: str = "Per-Span Relative Change Distributions (Bootstrap)",
    figsize: tuple[float, float] = (12, 8),
    bins: int = 40,
    max_spans: int = 12,
    span_names: list[str] | None = None,
    save_path: Path | str | None = None,
) -> "Figure":
    """
    Plot bootstrap distributions of relative change (%) for multiple spans.
    """
    _ensure_matplotlib()

    common_spans = set(old_samples_by.keys()) & set(new_samples_by.keys())
    if span_names:
        spans_to_plot = [s for s in span_names if s in common_spans]
    else:
        spans_to_plot = _sort_spans_for_display(
            common_spans,
            old_samples_by=old_samples_by,
            new_samples_by=new_samples_by,
            max_spans=max_spans,
        )

    if not spans_to_plot:
        raise EvalStatsError("No common spans found to plot")

    if stat not in {"mean", "p50", "p95"}:
        raise EvalStatsError(f"Unknown stat: {stat}")

    def stat_value(values: list[float]) -> float:
        if stat == "mean":
            return statistics.fmean(values)
        if stat == "p50":
            return _percentile(sorted(values), 50.0)
        if stat == "p95":
            return _percentile(sorted(values), 95.0)
        raise EvalStatsError(f"Unknown stat: {stat}")

    n_spans = len(spans_to_plot)
    n_cols = 2 if n_spans > 1 else 1
    n_rows = (n_spans + n_cols - 1) // n_cols

    fig, axes = plt.subplots(n_rows, n_cols, figsize=figsize, squeeze=False)
    axes = axes.flatten()

    for i, span_name in enumerate(spans_to_plot):
        ax = axes[i]
        old_samples = old_samples_by[span_name]
        new_samples = new_samples_by[span_name]

        if not old_samples or not new_samples:
            ax.set_visible(False)
            continue

        if HAS_NUMPY:
            dist = _bootstrap_delta_pct_distribution_numpy(
                old_samples,
                new_samples,
                stat=stat,
                n_resamples=bootstrap,
                seed=seed,
            )
        else:
            dist = _bootstrap_delta_pct_distribution(
                old_samples,
                new_samples,
                stat=stat,
                n_resamples=bootstrap,
                seed=seed,
            )

        dist_sorted = sorted(dist)
        lo = _percentile(dist_sorted, 100.0 * (alpha / 2.0))
        hi = _percentile(dist_sorted, 100.0 * (1.0 - alpha / 2.0))

        old_stat = stat_value(old_samples)
        new_stat = stat_value(new_samples)
        observed = ((new_stat - old_stat) / old_stat * 100.0) if old_stat != 0 else 0.0

        ax.hist(dist, bins=bins, color="#8C8C8C", alpha=0.7, edgecolor="white")
        ax.axvline(0, color="#333333", linewidth=1, linestyle="--")
        ax.axvline(observed, color="#1F1F1F", linewidth=1.2)
        ax.axvspan(lo, hi, color="#D9D9D9", alpha=0.6)

        ax.set_title(f"{_display_span_name(span_name)} ({observed:+.1f}%)", fontsize=9)
        ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:+.0f}%"))

    for j in range(n_spans, len(axes)):
        axes[j].set_visible(False)

    fig.suptitle(title, fontsize=12, y=1.0)
    plt.tight_layout()

    if save_path:
        _save_figure_pdf(fig, save_path)

    return fig


def plot_mde_analysis(
    report: dict[str, Any],
    *,
    title: str = "Minimum Detectable Effect (MDE) Analysis — Median (P50)",
    figsize: tuple[float, float] = (12, 6),
    save_path: Path | str | None = None,
) -> "Figure":
    """
    Create a visualization showing observed median (p50) effect vs MDE threshold.
    """
    _ensure_matplotlib()

    if "per_span" in report:
        per_span = report["per_span"]
    else:
        per_span = {"overall": report}

    span_data = []
    for span_name, data in per_span.items():
        mde = data.get("mde", {})
        # Prefer median delta if available; fall back to mean for older reports.
        deltas = data.get("deltas", {}).get("p50") or data.get("deltas", {}).get("mean", {})
        observed_pct = abs(deltas.get("pct", 0))
        mde_pct = mde.get("pct", 0)
        exceeds = mde.get("observed_exceeds_mde", False)

        span_data.append({
            "name": span_name,
            "observed_pct": observed_pct,
            "mde_pct": mde_pct,
            "exceeds": exceeds,
        })

    span_data.sort(key=lambda x: x["mde_pct"], reverse=True)

    fig, ax = plt.subplots(figsize=figsize)

    y_pos = list(range(len(span_data)))
    names = [_display_span_name(d["name"]) for d in span_data]
    observed = [d["observed_pct"] for d in span_data]
    mde_vals = [d["mde_pct"] for d in span_data]
    colors = ["#4D4D4D" if d["exceeds"] else "#B3B3B3" for d in span_data]

    ax.barh(y_pos, observed, color=colors, alpha=0.7, edgecolor="none", label="Observed Absolute Median Change")
    ax.scatter(mde_vals, y_pos, color="black", marker="|", s=150, linewidth=2, label="MDE Threshold", zorder=5)

    ax.set_yticks(y_pos)
    ax.set_yticklabels(names)
    ax.set_xlabel("Effect Magnitude (%)")
    ax.set_title(title)
    ax.invert_yaxis()

    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:.0f}%"))

    legend_elements = [
        mpatches.Patch(color="#4D4D4D", alpha=0.7, label="Detectable Change"),
        mpatches.Patch(color="#B3B3B3", alpha=0.7, label="Below MDE"),
        plt.Line2D([0], [0], color="black", marker="|", linestyle="none", markersize=10, markeredgewidth=2, label="MDE Threshold"),
    ]
    ax.legend(handles=legend_elements, loc="best", frameon=False)

    if save_path:
        _save_figure_pdf(fig, save_path)

    return fig


def plot_summary_dashboard(
    report: dict[str, Any],
    *,
    title: str = "Performance Comparison Summary",
    figsize: tuple[float, float] = (14, 10),
    save_path: Path | str | None = None,
) -> "Figure":
    """
    Create a multi-panel dashboard for evaluation reporting in papers.
    """
    _ensure_matplotlib()

    fig = plt.figure(figsize=figsize)
    gs = fig.add_gridspec(2, 2, hspace=0.35, wspace=0.3)

    if "per_span" in report:
        per_span = report["per_span"]
    else:
        per_span = {"overall": report}

    span_data = []
    for span_name, data in per_span.items():
        deltas = data.get("deltas", {}).get("mean", {})
        p95_deltas = data.get("deltas", {}).get("p95", {})
        p50_deltas = data.get("deltas", {}).get("p50", {})
        cohens = data.get("statistical_tests", {}).get("cohens_d", {})
        tests = data.get("statistical_tests", {}).get("welch_t", {})
        mde = data.get("mde", {})

        # Handle None CIs from skip_ci mode
        mean_ci = deltas.get("ci") or {}
        p95_ci = p95_deltas.get("ci") or {}
        p50_ci = p50_deltas.get("ci") or {}

        span_data.append({
            "name": span_name,
            "mean_delta_pct": deltas.get("pct", 0),
            "mean_ci_lo": mean_ci.get("lo", 0) if mean_ci else 0,
            "mean_ci_hi": mean_ci.get("hi", 0) if mean_ci else 0,
            "p50_delta_pct": p50_deltas.get("pct", 0),
            "p50_ci_lo": p50_ci.get("lo", 0) if p50_ci else 0,
            "p50_ci_hi": p50_ci.get("hi", 0) if p50_ci else 0,
            "d": cohens.get("value", 0),
            "d_interp": cohens.get("interpretation", "negligible"),
            "p_value": tests.get("p_value", 1),
            "is_sig": tests.get("is_significant", False),
            "n_old": data.get("old", {}).get("n", 0),
            "n_new": data.get("new", {}).get("n", 0),
        })

    span_data.sort(key=lambda x: x["mean_delta_pct"], reverse=True)
    y_pos = list(range(len(span_data)))
    names = [_display_span_name(d["name"]) for d in span_data]

    # 1. Mean Delta
    ax1 = fig.add_subplot(gs[0, 0])
    deltas = [d["mean_delta_pct"] for d in span_data]
    colors = [_get_color_for_change(d) for d in deltas]
    ax1.barh(y_pos, deltas, color=colors, alpha=0.8)
    ax1.axvline(0, color="#333", linewidth=0.8)
    ax1.set_yticks(y_pos)
    ax1.set_yticklabels(names, fontsize=8)
    ax1.set_title("Mean Latency Change (%)", fontsize=10)
    ax1.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:+.0f}%"))
    ax1.invert_yaxis()

    # 2. Cohen's d
    ax2 = fig.add_subplot(gs[0, 1])
    d_vals = [d["d"] for d in span_data]
    d_colors = [_get_effect_color(d["d_interp"]) for d in span_data]
    ax2.barh(y_pos, d_vals, color=d_colors, alpha=0.8)
    ax2.axvline(0, color="#333", linewidth=0.8)
    ax2.set_yticks(y_pos)
    ax2.set_yticklabels(names, fontsize=8)
    ax2.set_title("Effect Size (Cohen's $d$)", fontsize=10)
    ax2.invert_yaxis()

    # 3. Median (P50) Delta
    ax3 = fig.add_subplot(gs[1, 0])
    p50_deltas = [d["p50_delta_pct"] for d in span_data]
    ax3.barh(y_pos, p50_deltas, color=[_get_color_for_change(d) for d in p50_deltas], alpha=0.8)
    ax3.axvline(0, color="#333", linewidth=0.8)
    ax3.set_yticks(y_pos)
    ax3.set_yticklabels(names, fontsize=8)
    ax3.set_title("Median Latency Change (%)", fontsize=10)
    ax3.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:+.0f}%"))
    ax3.invert_yaxis()

    # 4. Table
    ax4 = fig.add_subplot(gs[1, 1])
    ax4.axis("off")
    table_data = []
    for d in span_data[:8]:
        display_name = _display_span_name(d["name"])
        table_data.append([
            display_name[:15] + ".." if len(display_name) > 15 else display_name,
            f"{d['mean_delta_pct']:+.1f}%",
            f"{d['d']:.2f}",
            f"{d['p_value']:.3f}",
            "*" if d["is_sig"] else ""
        ])

    table = ax4.table(
        cellText=table_data,
        colLabels=["Span", r"$\Delta$Mean", r"$d$", r"$p$-val", "Sig"],
        loc="center", cellLoc="center",
        colColours=["#f2f2f2"] * 5
    )
    table.auto_set_font_size(False)
    table.set_fontsize(8)
    table.scale(1, 1.4)
    ax4.set_title("Detailed Statistics", fontsize=10)

    fig.suptitle(title, fontsize=12, y=0.98)

    if save_path:
        _save_figure_pdf(fig, save_path)

    return fig


def plot_latency_distributions(
    old_samples: dict[str, list[float]],
    new_samples: dict[str, list[float]],
    *,
    span_names: list[str] | None = None,
    max_spans: int = 6,
    title: str = "Latency Distributions Comparison",
    figsize: tuple[float, float] = (12, 8),
    save_path: Path | str | None = None,
) -> "Figure":
    """
    Create scientific box plots for latency distribution comparisons.
    """
    _ensure_matplotlib()

    common_spans = set(old_samples.keys()) & set(new_samples.keys())
    if span_names:
        spans_to_plot = [s for s in span_names if s in common_spans]
    else:
        spans_to_plot = _sort_spans_for_display(
            common_spans,
            old_samples_by=old_samples,
            new_samples_by=new_samples,
            max_spans=max_spans,
        )

    if not spans_to_plot:
        raise EvalStatsError("No common spans found to plot")

    n_spans = len(spans_to_plot)
    n_cols = 2 if n_spans > 1 else 1
    n_rows = (n_spans + n_cols - 1) // n_cols

    fig, axes = plt.subplots(n_rows, n_cols, figsize=figsize, squeeze=False)
    axes = axes.flatten()

    for i, span_name in enumerate(spans_to_plot):
        ax = axes[i]
        old = old_samples[span_name]
        new = new_samples[span_name]

        old_mean = statistics.fmean(old) if old else 0
        new_mean = statistics.fmean(new) if new else 0
        pct_change = ((new_mean - old_mean) / old_mean * 100) if old_mean != 0 else 0

        # Use simpler boxplot without outliers for scientific clarity if desired,
        # or standard boxplot.
        bp = ax.boxplot(
            [old, new],
            labels=["Base", "Head"],
            patch_artist=True,
            showfliers=False,  # Often preferred in papers to see the distribution clearly
            widths=0.5
        )

        bp["boxes"][0].set(facecolor="#4D4D4D", alpha=0.35, edgecolor="#4D4D4D")
        bp["boxes"][1].set(facecolor="#9A9A9A", alpha=0.35, edgecolor="#9A9A9A")

        for median in bp["medians"]:
            median.set(color="#333", linewidth=1.5)

        ax.set_ylabel("Latency (ms)")
        ax.set_title(f"{_display_span_name(span_name)} ({pct_change:+.1f}%)", fontsize=10)

    for j in range(n_spans, len(axes)):
        axes[j].set_visible(False)

    fig.suptitle(title, fontsize=12, y=1.0)
    plt.tight_layout()

    if save_path:
        _save_figure_pdf(fig, save_path)

    return fig


def generate_all_plots(
    report: dict[str, Any],
    output_dir: Path | str,
    *,
    prefix: str = "eval",
    format: str = "pdf",  # Default to PDF for scientific quality
) -> list[Path]:
    """
    Generate all standard plots in high quality.
    """
    _ensure_matplotlib()

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Update default DPI for non-vector formats
    plt.rcParams["savefig.dpi"] = 300

    generated: list[Path] = []

    # Plot definitions
    plot_funcs = [
        ("mean_deltas", plot_mean_deltas_with_ci),
        ("effect_sizes", plot_effect_sizes),
        ("median_comparison", lambda r, **kwargs: plot_percentile_comparison(r, percentile="p50", title="Median Latency Change (%)", **kwargs)),
        ("sample_sizes", plot_sample_sizes),
        ("span_bootstrap_cis", lambda r, **kwargs: plot_span_bootstrap_confidence_intervals(r, metric="p50", relative=True, **kwargs)),
        ("mde_analysis", plot_mde_analysis),
        ("dashboard", plot_summary_dashboard),
    ]

    for name, func in plot_funcs:
        path = output_dir / f"{prefix}_{name}.pdf"
        func(report, save_path=path)
        plt.close()
        generated.append(path)

    return generated
