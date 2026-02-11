from __future__ import annotations

import argparse
import bisect
import json
import math
from pathlib import Path
from typing import Any, Iterable


def _mpl_rcparams() -> dict[str, Any]:
    """
    Matplotlib styling tuned for paper figures.

    Important: keep figure physical sizes close to the final LaTeX size to avoid
    LaTeX scaling the whole PDF down (which makes tick labels look tiny).
    """
    return {
        "font.family": "serif",
        "font.serif": ["Times New Roman", "TeX Gyre Termes", "DejaVu Serif", "serif"],
        # Bigger text for paper readability (tuned for small PDF figures).
        "font.size": 16,
        "axes.titlesize": 19,
        "axes.labelsize": 18,
        "xtick.labelsize": 16,
        "ytick.labelsize": 16,
        "figure.titlesize": 20,
        "legend.fontsize": 15,
        "axes.titlepad": 6.0,
        "axes.labelpad": 6.0,
        "xtick.major.pad": 4.0,
        "ytick.major.pad": 4.0,
        "axes.edgecolor": "#333333",
        "axes.linewidth": 0.7,
        "grid.color": "#D0D0D0",
        "grid.linestyle": "-",
        "grid.linewidth": 0.4,
        "pdf.fonttype": 42,  # TrueType in PDF for better embedding
        "ps.fonttype": 42,
        "savefig.bbox": "tight",
        "savefig.pad_inches": 0.02,
    }


def _percentile_sorted(values_sorted: list[float], p: float) -> float:
    """
    Percentile for already-sorted values with linear interpolation.

    Args:
        values_sorted: ascending list of floats (non-empty)
        p: percentile in [0, 100]
    """
    if not values_sorted:
        raise ValueError("values_sorted must be non-empty")
    if p <= 0:
        return values_sorted[0]
    if p >= 100:
        return values_sorted[-1]

    # Linear interpolation between closest ranks.
    n = len(values_sorted)
    r = (p / 100.0) * (n - 1)
    lo = int(math.floor(r))
    hi = int(math.ceil(r))
    if lo == hi:
        return values_sorted[lo]
    w = r - lo
    return values_sorted[lo] * (1.0 - w) + values_sorted[hi] * w


def _add_median_iqr_markers(
    ax: Any,
    *,
    x: float,
    values: list[float],
    color: str = "#222222",
) -> None:
    """
    Overlay a compact median marker on a violin.

    The user-facing figure style avoids horizontal/vertical guide lines inside
    violins; so we draw only a short median line (no IQR bar / vertical ticks).
    """
    if not values:
        return
    vs = sorted(float(v) for v in values)
    med = _percentile_sorted(vs, 50.0)

    # Median marker only: short horizontal line.
    ax.hlines(
        med,
        x - 0.14,
        x + 0.14,
        colors=color,
        linewidth=1.6,
        zorder=5,
    )


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        f = float(value)
        return f
    except Exception:
        return None


def _iter_reports(input_dir: Path) -> Iterable[Path]:
    yield from input_dir.rglob("localization_report.json")


#
# Simple constant mapping: file -> endpoint(s)
#
# Keys should match the `per_file` keys in `localization_report.json` (typically repo-relative paths).
#
# Extend this mapping as you add apps/endpoints or want finer-grained attribution.
FILE_TO_ENDPOINTS: dict[str, list[str]] = {
    # movie example
    "examples/movie/src/search.ts": ["/search"],
    "examples/movie/src/filtering/als.ts": ["/top/user/:userId/:count"],
    "examples/movie/src/filtering/random-walk.ts": ["/top/region/:region/:count"],
    # Some shared/support files may contribute to multiple endpoints.
    "examples/movie/src/model.ts": [
        "/search",
        "/top/user/:userId/:count",
        "/top/region/:region/:count",
    ],
    "examples/movie/src/rate-limits.ts": [
        "/search",
        "/top/user/:userId/:count",
        "/top/region/:region/:count",
    ],
    "examples/movie/src/app.ts": [
        "/search",
        "/top/user/:userId/:count",
        "/top/region/:region/:count",
    ],
    "examples/movie/src/config.ts": [
        "/search",
    ],
}

# Short endpoint names for plots.
ENDPOINT_TO_NAME: dict[str, str] = {
    "/search": "U1 (E7)",
    "/top/region/:region/:count": "U2 (E8)",
    "/top/user/:userId/:count": "U3 (E9)",
}


def _endpoints_for_file(file_path: str) -> list[str]:
    """
    Best-effort mapping of a report file path to endpoint(s) using FILE_TO_ENDPOINTS.

    - Prefer exact match.
    - Fallback to suffix match (useful if some reports include extra leading components).
    """

    p = (file_path or "").strip().lstrip("./")
    if not p:
        return []
    p = Path(p).as_posix()

    exact = FILE_TO_ENDPOINTS.get(p)
    if exact:
        return list(exact)

    # Suffix match: pick the longest matching key (most specific).
    best_key: str | None = None
    for k in FILE_TO_ENDPOINTS.keys():
        if p.endswith(k) and (best_key is None or len(k) > len(best_key)):
            best_key = k
    if best_key is not None:
        return list(FILE_TO_ENDPOINTS.get(best_key, []))
    return []


def _endpoint_names(endpoints: list[str]) -> list[str]:
    """
    Convert endpoint paths to short names (e.g. E7/E8/E9) for plotting.
    Unknown endpoints are returned as-is.
    """

    out: list[str] = []
    for e in endpoints:
        if not isinstance(e, str):
            continue
        e = e.strip()
        if not e:
            continue
        out.append(ENDPOINT_TO_NAME.get(e, e))
    return out


def _endpoint_label(endpoints: list[str]) -> str:
    endpoints = _endpoint_names(endpoints)
    endpoints = sorted(dict.fromkeys(e.strip() for e in endpoints if e.strip()).keys())
    if not endpoints:
        return "unknown"
    if len(endpoints) == 1:
        return endpoints[0]
    # Multi-endpoint: use newlines to keep x-axis labels readable.
    return "\n".join(endpoints)


def _compute_plan_validity(per_file: dict[str, Any]) -> float | None:
    total = 0
    valid = 0
    for _, m in per_file.items():
        total += int(m.get("planned_span_count") or 0)
        for loc in m.get("located_spans") or []:
            if loc.get("start_found") is True:
                valid += 1
    if total == 0:
        return None
    return valid / float(total)


def _compute_within_k_from_per_file(
    per_file: dict[str, Any],
) -> tuple[float | None, float | None, float | None, int | None]:
    tp_plans = 0
    total_plans = 0
    perf_files = 0
    perf_files_hit = 0
    non_perf_files = 0
    non_perf_files_hit = 0
    k: int | None = None
    for _, m in per_file.items():
        has_perf = bool(m.get("has_perf_relevant_changes"))
        within_k = m.get("within_k_lines")
        if not isinstance(within_k, dict):
            continue
        k_val = within_k.get("k")
        if isinstance(k_val, int):
            k = k if k is not None else k_val
        tp_plans += int(
            within_k.get("predicted_span_hits", within_k.get("per_plan_hits")) or 0
        )
        total_plans += int(
            within_k.get("predicted_span_total", within_k.get("per_plan_total")) or 0
        )
        if has_perf:
            perf_files += 1
            if within_k.get("file_hit") is True:
                perf_files_hit += 1
        else:
            non_perf_files += 1
            if within_k.get("file_hit") is True:
                non_perf_files_hit += 1
    precision = None if total_plans == 0 else tp_plans / float(total_plans)
    recall = None if perf_files == 0 else perf_files_hit / float(perf_files)
    specificity = (
        None
        if non_perf_files == 0
        else (non_perf_files - non_perf_files_hit) / float(non_perf_files)
    )
    return precision, recall, specificity, k


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    values = sorted(values)
    mid = len(values) // 2
    if len(values) % 2 == 1:
        return float(values[mid])
    return float((values[mid - 1] + values[mid]) / 2.0)


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _find_detection_gain(k6_report: dict[str, Any]) -> float | None:
    overall = k6_report.get("overall", {})
    summary = overall.get("summary") if isinstance(overall, dict) else None
    if not isinstance(summary, dict):
        summary = k6_report.get("summary") if isinstance(k6_report.get("summary"), dict) else None
    if not isinstance(summary, dict):
        return None
    deltas = summary.get("deltas", {}).get("p50") or summary.get("deltas", {}).get("mean", {})
    mde = summary.get("mde", {})
    try:
        observed_pct = abs(float(deltas.get("pct", 0)))
        mde_pct = float(mde.get("pct", 0))
    except Exception:
        return None
    if mde_pct <= 0:
        return None
    return observed_pct / mde_pct


def _plot_scatter(
    points: list[tuple[float, float, str]],
    *,
    title: str,
    xlabel: str,
    ylabel: str,
    out_path: Path,
) -> None:
    import matplotlib.pyplot as plt  # type: ignore

    fig, ax = plt.subplots(figsize=(5.2, 3.4), dpi=200)
    if points:
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        ax.scatter(xs, ys, color="#4D4D4D", edgecolor="#2B2B2B", linewidth=0.6, s=36)
        ax.set_title(title)
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)
        ax.grid(axis="both", alpha=0.4)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)

        if len(points) <= 20:
            for x, y, label in points:
                ax.annotate(
                    label,
                    (x, y),
                    textcoords="offset points",
                    xytext=(4, 4),
                    fontsize=7,
                    color="#444444",
                )
    else:
        ax.set_title(title)
        ax.axis("off")
        ax.text(
            0.5,
            0.5,
            "No paired k6_eval_report.json found",
            ha="center",
            va="center",
            fontsize=9,
            color="#555555",
            transform=ax.transAxes,
        )

    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def _plot_precision_recall_scatter(
    points: list[tuple[float, float, str]],
    *,
    title: str,
    out_path: Path,
    annotation: str | None = None,
) -> None:
    import matplotlib.pyplot as plt  # type: ignore

    fig, ax = plt.subplots(figsize=(4.2, 3.4), dpi=200)
    ax.set_title(title)
    ax.set_xlabel("Precision@k")
    ax.set_ylabel("Recall@k")
    ax.set_xlim(-0.02, 1.02)
    ax.set_ylim(-0.02, 1.02)
    ax.grid(axis="both", alpha=0.4)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    if points:
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        ax.scatter(xs, ys, color="#4D4D4D", edgecolor="#2B2B2B", linewidth=0.6, s=46)
        if len(points) <= 20:
            for x, y, label in points:
                ax.annotate(
                    label,
                    (x, y),
                    textcoords="offset points",
                    xytext=(4, 4),
                    fontsize=8,
                    color="#444444",
                )
    else:
        ax.text(
            0.5,
            0.5,
            "No within-k precision/recall data found",
            ha="center",
            va="center",
            fontsize=9,
            color="#555555",
            transform=ax.transAxes,
        )

    if annotation:
        ax.text(
            0.98,
            0.02,
            annotation,
            transform=ax.transAxes,
            ha="right",
            va="bottom",
            fontsize=8,
            color="#333333",
        )

    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def _plot_precision_recall_violin_combined(
    precision_by_label: dict[str, list[float]],
    recall_by_label: dict[str, list[float]],
    specificity_by_label: dict[str, list[float]],
    *,
    title: str,
    out_path: Path,
    annotation: str | None = None,
) -> None:
    import matplotlib.pyplot as plt  # type: ignore

    # This figure tends to get crowded; keep fonts smaller than the global default.
    rc = dict(_mpl_rcparams())
    rc.update(
        {
            # Slightly bigger text for this multi-panel figure.
            "font.size": 15,
            "axes.titlesize": 18,
            "axes.labelsize": 17,
            "xtick.labelsize": 14,
            "ytick.labelsize": 14,
            "figure.titlesize": 19,
            "legend.fontsize": 14,
        }
    )
    plt.rcParams.update(rc)

    labels = sorted(
        set(precision_by_label.keys())
        | set(recall_by_label.keys())
        | set(specificity_by_label.keys())
    )
    labels = [
        label
        for label in labels
        if precision_by_label.get(label)
        or recall_by_label.get(label)
        or specificity_by_label.get(label)
    ]
    if not labels:
        return
    short_labels = _unique_short_labels(labels)

    precision_data = [precision_by_label.get(label, []) for label in labels]
    recall_data = [recall_by_label.get(label, []) for label in labels]
    specificity_data = [specificity_by_label.get(label, []) for label in labels]

    # Target final size for a two-column figure (acmart `\textwidth` is ~7in).
    # Use a vertical stack to improve readability.
    fig, (ax1, ax2, ax3) = plt.subplots(
        3,
        1,
        figsize=(7.0, 5.6),
        dpi=300,
        sharex=True,
        sharey=True,
    )
    # Bring the headline closer to the plot area.
    fig.suptitle(title, y=0.972)

    def _style_axis(
        ax,
        data: list[list[float]],
        subtitle: str,
        *,
        fill: str,
        show_xticklabels: bool,
    ) -> None:
        non_empty_idx = [i for i, vals in enumerate(data) if vals]
        if not non_empty_idx:
            ax.set_title(subtitle)
            ax.text(
                0.5,
                0.5,
                "No data",
                ha="center",
                va="center",
                fontsize=8,
                color="#666666",
                transform=ax.transAxes,
            )
            ax.set_xticks([])
            return
        data_non_empty = [data[i] for i in non_empty_idx]
        labels_non_empty = [short_labels[i] for i in non_empty_idx]
        parts = ax.violinplot(
            data_non_empty,
            showmeans=False,
            showmedians=False,
            showextrema=False,
            widths=0.85,
        )
        for pc in parts.get("bodies", []):
            pc.set_facecolor(fill)
            pc.set_edgecolor("#2B2B2B")
            pc.set_linewidth(0.6)
            pc.set_alpha(0.55)

        for j, vals in enumerate(data_non_empty, start=1):
            _add_median_iqr_markers(ax, x=float(j), values=vals)

        ax.set_title(subtitle)
        ax.set_xticks(range(1, len(labels_non_empty) + 1))
        if show_xticklabels:
            ax.set_xticklabels(labels_non_empty, rotation=30, ha="right")
        else:
            ax.set_xticklabels([])
        ax.tick_params(axis="y")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.set_ylim(0.0, 1.0)
        # Keep panels clean without per-axis legend boxes.

    # Grayscale fill (paper-friendly / printer-friendly): keep identical across panels.
    fill_gray = "#7A7A7A"
    _style_axis(
        ax1,
        precision_data,
        "Precision@5",
        fill=fill_gray,
        show_xticklabels=False,
    )
    _style_axis(
        ax2,
        recall_data,
        "Recall@5",
        fill=fill_gray,
        show_xticklabels=False,
    )
    _style_axis(
        ax3,
        specificity_data,
        "Specificity@5",
        fill=fill_gray,
        show_xticklabels=True,
    )
    ax1.set_ylabel("Score")
    ax2.set_ylabel("Score")
    ax3.set_ylabel("Score")

    if annotation:
        fig.text(
            0.98,
            0.02,
            annotation,
            ha="right",
            va="bottom",
            fontsize=8,
            color="#333333",
        )

    # Allow axes to extend higher, reducing whitespace under the suptitle.
    fig.tight_layout(rect=(0, 0.02, 1, 0.958), h_pad=0.7)
    fig.savefig(out_path, bbox_inches="tight", pad_inches=0.02)
    plt.close(fig)


def _plot_localization_quality_violin_figure(
    precision_by_label: dict[str, list[float]],
    recall_by_label: dict[str, list[float]],
    error_mean_by_label: dict[str, list[float]],
    *,
    title: str,
    out_path: Path,
    annotation: str | None = None,
) -> None:
    """
    One paper figure:
      - Top row: precision@k and recall@k (shared y in [0,1])
      - Bottom row: average localization error (own y scale)

    Designed to be included as a single `figure*` in LaTeX.
    """
    import matplotlib.pyplot as plt  # type: ignore

    plt.rcParams.update(_mpl_rcparams())

    labels = sorted(
        set(precision_by_label.keys())
        | set(recall_by_label.keys())
        | set(error_mean_by_label.keys())
    )
    labels = [
        label
        for label in labels
        if precision_by_label.get(label) or recall_by_label.get(label) or error_mean_by_label.get(label)
    ]
    if not labels:
        return

    short_labels = _unique_short_labels(labels)
    precision_data = [precision_by_label.get(label, []) for label in labels]
    recall_data = [recall_by_label.get(label, []) for label in labels]
    error_data = [error_mean_by_label.get(label, []) for label in labels]

    # Two-column figure width (~7in) with enough height for two rows.
    fig = plt.figure(figsize=(7.0, 5.4), dpi=300)
    gs = fig.add_gridspec(
        2,
        2,
        height_ratios=[1.0, 1.15],
        hspace=0.45,
        wspace=0.25,
    )
    ax_p = fig.add_subplot(gs[0, 0])
    ax_r = fig.add_subplot(gs[0, 1], sharey=ax_p)
    ax_e = fig.add_subplot(gs[1, :])

    fig.suptitle(title, y=0.99)

    def _violin_panel(
        ax: Any,
        data: list[list[float]],
        *,
        subtitle: str,
        fill: str,
        ylabel: str | None = None,
        ylim: tuple[float, float] | None = None,
        show_xticklabels: bool = True,
    ) -> None:
        non_empty_idx = [i for i, vals in enumerate(data) if vals]
        if not non_empty_idx:
            ax.set_title(subtitle)
            ax.text(
                0.5,
                0.5,
                "No data",
                ha="center",
                va="center",
                fontsize=8,
                color="#666666",
                transform=ax.transAxes,
            )
            ax.set_xticks([])
            return

        data_non_empty = [data[i] for i in non_empty_idx]
        labels_non_empty = [short_labels[i] for i in non_empty_idx]

        parts = ax.violinplot(
            data_non_empty,
            showmeans=False,
            showmedians=False,
            showextrema=False,
            widths=0.85,
        )
        for pc in parts.get("bodies", []):
            pc.set_facecolor(fill)
            pc.set_edgecolor("#2B2B2B")
            pc.set_linewidth(0.6)
            pc.set_alpha(0.55)

        for j, vals in enumerate(data_non_empty, start=1):
            _add_median_iqr_markers(ax, x=float(j), values=vals)

        ax.set_title(subtitle)
        ax.set_xticks(range(1, len(labels_non_empty) + 1))
        if show_xticklabels:
            ax.set_xticklabels(labels_non_empty, rotation=30, ha="right")
        else:
            ax.set_xticklabels([])
        if ylabel:
            ax.set_ylabel(ylabel)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        if ylim is not None:
            ax.set_ylim(*ylim)

    _violin_panel(
        ax_p,
        precision_data,
        subtitle="Precision@5",
        fill="#B5B5B5",
        ylabel="Score",
        ylim=(0.0, 1.0),
        show_xticklabels=False,
    )
    _violin_panel(
        ax_r,
        recall_data,
        subtitle="Recall@5",
        fill="#8F8F8F",
        ylim=(0.0, 1.0),
        show_xticklabels=False,
    )
    _violin_panel(
        ax_e,
        error_data,
        subtitle="Average localization error",
        fill="#A3A3A3",
        ylabel="Lines",
        show_xticklabels=True,
    )

    if annotation:
        fig.text(
            0.985,
            0.02,
            annotation,
            ha="right",
            va="bottom",
            fontsize=8,
            color="#333333",
        )

    fig.tight_layout(rect=(0, 0.02, 1, 0.95))
    fig.savefig(out_path, bbox_inches="tight", pad_inches=0.02)
    plt.close(fig)


def _plot_hist(
    values: list[float],
    *,
    title: str,
    xlabel: str,
    out_path: Path,
    bins: int = 20,
    annotation: str | None = None,
) -> None:
    import matplotlib.pyplot as plt  # type: ignore

    if not values:
        return
    plt.rcParams.update(_mpl_rcparams())
    fig, ax = plt.subplots(figsize=(5.2, 3.2), dpi=300)
    ax.hist(values, bins=bins, color="#8C8C8C", edgecolor="#2B2B2B", linewidth=0.6)
    ax.set_title(title)
    ax.set_xlabel(xlabel)
    ax.set_ylabel("Count")
    ax.grid(axis="y", alpha=0.6)
    ax.set_axisbelow(True)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    if annotation:
        ax.text(
            0.98,
            0.95,
            annotation,
            transform=ax.transAxes,
            ha="right",
            va="top",
            fontsize=8,
            color="#333333",
        )
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight", pad_inches=0.02)
    plt.close(fig)


def _plot_hist_01(
    values: list[float],
    *,
    title: str,
    xlabel: str,
    out_path: Path,
    annotation: str | None = None,
) -> None:
    _plot_hist(
        values,
        title=title,
        xlabel=xlabel,
        out_path=out_path,
        bins=20,
        annotation=annotation,
    )


def _plot_violin(
    values: list[float],
    *,
    title: str,
    ylabel: str,
    out_path: Path,
    annotation: str | None = None,
) -> None:
    import matplotlib.pyplot as plt  # type: ignore

    if not values:
        return
    plt.rcParams.update(_mpl_rcparams())
    fig, ax = plt.subplots(figsize=(3.2, 2.8), dpi=300)
    parts = ax.violinplot(values, showmeans=False, showmedians=False, showextrema=False, widths=0.9)
    for pc in parts.get("bodies", []):
        pc.set_facecolor("#8C8C8C")
        pc.set_edgecolor("#2B2B2B")
        pc.set_linewidth(0.6)
        pc.set_alpha(0.55)
    _add_median_iqr_markers(ax, x=1.0, values=values)
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.set_xticks([])
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    if annotation:
        ax.text(
            0.98,
            0.95,
            annotation,
            transform=ax.transAxes,
            ha="right",
            va="top",
            fontsize=8,
            color="#333333",
        )
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight", pad_inches=0.02)
    plt.close(fig)


def _unique_short_labels(labels: list[str]) -> list[str]:
    counts: dict[str, int] = {}
    out: list[str] = []
    for label in labels:
        # If it's an HTTP endpoint label (starts with "/"), keep it as-is.
        stripped = label.strip()
        if stripped.startswith("/"):
            base = label
        else:
            base = Path(label).name or label
        n = counts.get(base, 0) + 1
        counts[base] = n
        out.append(f"{base} ({n})" if n > 1 else base)
    return out


def _plot_violin_by_label(
    values_by_label: dict[str, list[float]],
    *,
    title: str,
    ylabel: str,
    out_path: Path,
    annotation: str | None = None,
) -> None:
    import matplotlib.pyplot as plt  # type: ignore

    if not values_by_label:
        return
    plt.rcParams.update(_mpl_rcparams())
    labels = sorted(values_by_label.keys())
    data = [values_by_label[label] for label in labels if values_by_label[label]]
    labels = [label for label in labels if values_by_label[label]]
    if not data:
        return
    short_labels = _unique_short_labels(labels)

    # Target final size for a single-column figure (acmart column width is ~3.3in).
    fig, ax = plt.subplots(figsize=(3.3, 2.7), dpi=300)
    parts = ax.violinplot(data, showmeans=False, showmedians=False, showextrema=False, widths=0.85)
    for pc in parts.get("bodies", []):
        pc.set_facecolor("#A3A3A3")
        pc.set_edgecolor("#2B2B2B")
        pc.set_linewidth(0.6)
        pc.set_alpha(0.55)

    for i, vals in enumerate(data, start=1):
        _add_median_iqr_markers(ax, x=float(i), values=vals)

    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.set_xticks(range(1, len(short_labels) + 1))
    ax.set_xticklabels(short_labels, rotation=30, ha="right")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    if annotation:
        ax.text(
            0.02,
            0.95,
            annotation,
            transform=ax.transAxes,
            ha="left",
            va="top",
            fontsize=9,
            color="#333333",
            bbox={
                "boxstyle": "round,pad=0.2",
                "facecolor": "#FFFFFF",
                "edgecolor": "#C9C9C9",
                "alpha": 0.85,
            },
        )
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight", pad_inches=0.02)
    plt.close(fig)


def _plot_location_error_distribution_bars_per_file(
    errors_by_label: dict[str, list[float]],
    inf_counts_by_label: dict[str, int],
    *,
    title: str,
    out_path: Path,
    preferred_labels: list[str] | None = None,
    top_n_labels: int = 3,
) -> None:
    """
    Plot localization error distributions as bar charts in one plot for a few labels.

    The plot shows *three* (by default) per-label distributions in the same axes using
    grouped bars over coarse error buckets.
    """
    import matplotlib.pyplot as plt  # type: ignore

    if top_n_labels <= 0:
        return

    all_labels = sorted(set(errors_by_label.keys()) | set(inf_counts_by_label.keys()))
    # Only include labels with at least one observation (finite or infinite).
    all_labels = [
        lab
        for lab in all_labels
        if (errors_by_label.get(lab) and len(errors_by_label[lab]) > 0)
        or (inf_counts_by_label.get(lab, 0) or 0) > 0
    ]
    if not all_labels:
        return

    # Coarse buckets in lines.
    # Lower bounds; bucket i is [bounds[i], bounds[i+1]) except last which is [bounds[-1], inf).
    bounds: list[float] = [0.0, 1.0, 6.0, 11.0, 21.0, 51.0, 101.0]
    bucket_labels: list[str] = ["0", "1–5", "6–10", "11–20", "21–50", "51–100", "≥101"]
    assert len(bounds) == len(bucket_labels)

    def _n_obs(lab: str) -> int:
        return len(errors_by_label.get(lab, [])) + int(inf_counts_by_label.get(lab, 0) or 0)

    labels: list[str] = []
    if preferred_labels:
        for lab in preferred_labels:
            if lab in all_labels and lab not in labels:
                labels.append(lab)
    if len(labels) < top_n_labels:
        rest = [lab for lab in all_labels if lab not in labels]
        rest = sorted(rest, key=lambda lab: (-_n_obs(lab), lab))
        labels.extend(rest[: max(0, top_n_labels - len(labels))])

    if not labels:
        return

    # counts[label_idx][bucket_idx]
    counts: list[list[int]] = [[0 for _ in bucket_labels] for _ in labels]
    inf_counts: list[int] = [0 for _ in labels]

    def _add_value(file_idx: int, v: float) -> None:
        # Choose the rightmost lower bound <= v.
        bi = bisect.bisect_right(bounds, v) - 1
        if bi < 0:
            bi = 0
        if bi >= len(bucket_labels):
            bi = len(bucket_labels) - 1
        counts[file_idx][bi] += 1

    for i, lab in enumerate(labels):
        for v in errors_by_label.get(lab, []):
            if isinstance(v, (int, float)) and math.isfinite(float(v)):
                _add_value(i, float(v))
        inf_counts[i] = int(inf_counts_by_label.get(lab, 0) or 0)

    plt.rcParams.update(_mpl_rcparams())

    # Keep physical size stable for LaTeX; rotate labels for readability.
    fig, ax = plt.subplots(figsize=(7.0, 3.6), dpi=300)
    xs = list(range(len(bucket_labels)))

    # Use compact labels in the legend (keep "U1"/"U2"/"U3" readable).
    legend_labels = [lab.split()[0] if lab.startswith("U") else lab for lab in labels]

    # Grayscale palette (print-friendly, visually distinct without hatching).
    series_colors = ["#D0D0D0", "#A6A6A6", "#707070"]
    if len(labels) > len(series_colors):
        series_colors = series_colors + ["#8A8A8A"] * (len(labels) - len(series_colors))

    # Grouped bars.
    group_width = 0.78
    bar_w = group_width / float(len(labels))
    for i, _lab in enumerate(labels):
        offsets = [(x - group_width / 2.0 + (i + 0.5) * bar_w) for x in xs]
        ax.bar(
            offsets,
            counts[i],
            width=bar_w * 0.95,
            label=legend_labels[i],
            color=series_colors[i],
            edgecolor="#2B2B2B",
            linewidth=0.7,
            alpha=1.0,
        )

    # Add "∞" as an extra bucket at the far right if present.
    if any(c > 0 for c in inf_counts):
        inf_x = len(bucket_labels)
        xs_inf = [inf_x]
        offsets_base = [x for x in xs_inf]
        for i, _lab in enumerate(labels):
            offsets = [
                (x - group_width / 2.0 + (i + 0.5) * bar_w) for x in offsets_base
            ]
            ax.bar(
                offsets,
                [inf_counts[i]],
                width=bar_w * 0.95,
                color=series_colors[i],
                edgecolor="#2B2B2B",
                linewidth=0.7,
                alpha=1.0,
            )
        x_ticks = xs + [inf_x]
        x_tick_labels = bucket_labels + ["∞"]
    else:
        x_ticks = xs
        x_tick_labels = bucket_labels

    ax.set_title(title)
    ax.set_ylabel("Count")
    ax.set_xticks(x_ticks)
    ax.set_xticklabels(x_tick_labels)
    ax.set_xlabel("Localization error (lines)")
    ax.grid(axis="y", alpha=0.6)
    ax.set_axisbelow(True)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    ax.legend(title="Endpoint", frameon=False, ncol=1)

    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight", pad_inches=0.02)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Aggregate localization reports and plot metric distributions."
    )
    parser.add_argument("input_dir", type=Path, help="Directory to scan for localization_report.json files")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("localization_report_plots"),
        help="Output directory for plots and summary JSON",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Display plots interactively (in addition to saving)",
    )
    args = parser.parse_args()

    input_dir = args.input_dir.expanduser().resolve()
    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale_name in (
        "precision_recall_within_k_scatter.png",
        "precision_recall_within_k_scatter.pdf",
    ):
        stale_path = out_dir / stale_name
        if stale_path.exists():
            stale_path.unlink()

    report_paths = list(_iter_reports(input_dir))
    if not report_paths:
        raise SystemExit(f"No localization_report.json found under {input_dir}")

    precision_vals: list[float] = []
    sensitivity_vals: list[float] = []
    specificity_vals: list[float] = []
    plan_validity_vals: list[float] = []
    error_mean_vals: list[float] = []
    error_all_vals: list[float] = []
    error_inf_total = 0
    precision_k_vals: list[float] = []
    recall_k_vals: list[float] = []
    specificity_k_vals: list[float] = []
    k_vals: list[int] = []
    loc_vs_gain_points: list[tuple[float, float, str]] = []
    per_endpoint_precision: dict[str, list[float]] = {}
    per_endpoint_sensitivity: dict[str, list[float]] = {}
    per_endpoint_specificity: dict[str, list[float]] = {}
    per_endpoint_plan_validity: dict[str, list[float]] = {}
    per_endpoint_error_mean: dict[str, list[float]] = {}
    per_endpoint_error_all: dict[str, list[float]] = {}
    per_endpoint_error_inf_counts: dict[str, int] = {}
    per_endpoint_precision_k: dict[str, list[float]] = {}
    per_endpoint_recall_k: dict[str, list[float]] = {}
    per_endpoint_specificity_k: dict[str, list[float]] = {}

    # Per source-file aggregation across reports.
    file_precision_vals: dict[str, list[float]] = {}
    file_sensitivity_vals: dict[str, list[float]] = {}
    file_specificity_vals: dict[str, list[float]] = {}
    file_precision_k_vals: dict[str, list[float]] = {}
    file_recall_k_vals: dict[str, list[float]] = {}
    file_specificity_k_vals: dict[str, list[float]] = {}
    file_error_vals: dict[str, list[float]] = {}
    file_error_inf_counts: dict[str, int] = {}

    for path in report_paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        per_file = data.get("per_file") or {}
        if not isinstance(per_file, dict):
            continue
        if not any(
            isinstance(m, dict) and isinstance(m.get("within_k_lines"), dict)
            for m in per_file.values()
        ):
            continue
        _, _, _, report_k = _compute_within_k_from_per_file(per_file)
        if isinstance(report_k, int):
            k_vals.append(report_k)

        report_dir = path.parent

        k6_report_path = report_dir / "k6_eval_report.json"
        if k6_report_path.exists():
            try:
                k6_report = json.loads(k6_report_path.read_text(encoding="utf-8"))
            except Exception:
                k6_report = None
            if isinstance(k6_report, dict):
                detection_gain = _find_detection_gain(k6_report)
                if detection_gain is not None:
                    per_plan_errors: list[float] = []
                    for _, m in per_file.items():
                        le = m.get("localization_error") or {}
                        per_file_errs = le.get("per_file") or le.get("per_plan") or []
                        if not isinstance(per_file_errs, list):
                            continue
                        for v in per_file_errs:
                            fv = _safe_float(v)
                            if fv is None or not math.isfinite(fv):
                                continue
                            per_plan_errors.append(fv)
                    loc_mean = _mean(per_plan_errors)
                    if loc_mean is not None:
                        # Prefer endpoint labels (from perf-relevant files) over directory names.
                        perf_eps: set[str] = set()
                        for fp, mm in per_file.items():
                            if not isinstance(mm, dict):
                                continue
                            if mm.get("has_perf_relevant_changes") is True:
                                perf_eps.update(_endpoints_for_file(str(fp)))
                        label = _endpoint_label(sorted(perf_eps)) if perf_eps else report_dir.name
                        loc_vs_gain_points.append((loc_mean, detection_gain, label))

        for file_path, m in per_file.items():
            has_perf = bool(m.get("has_perf_relevant_changes"))
            span_count = int(m.get("planned_span_count") or 0)
            endpoints = _endpoint_names(_endpoints_for_file(str(file_path)))
            # If we can't map a file to an endpoint, bucket it (so plots still render).
            # (Keeping this endpoint-like avoids mixing file paths into endpoint plots.)
            endpoint_labels = endpoints if endpoints else ["unmapped"]

            # Note: if a file maps to multiple endpoints, it contributes to each endpoint bucket.
            if span_count > 0:
                prec_val = 1.0 if has_perf else 0.0
                precision_vals.append(prec_val)
                file_precision_vals.setdefault(file_path, []).append(prec_val)
                for ep in endpoint_labels:
                    per_endpoint_precision.setdefault(ep, []).append(prec_val)
            if has_perf:
                sens_val = 1.0 if span_count > 0 else 0.0
                sensitivity_vals.append(sens_val)
                file_sensitivity_vals.setdefault(file_path, []).append(sens_val)
                for ep in endpoint_labels:
                    per_endpoint_sensitivity.setdefault(ep, []).append(sens_val)
            else:
                spec_val = 1.0 if span_count == 0 else 0.0
                specificity_vals.append(spec_val)
                file_specificity_vals.setdefault(file_path, []).append(spec_val)
                for ep in endpoint_labels:
                    per_endpoint_specificity.setdefault(ep, []).append(spec_val)

            located_spans = m.get("located_spans") or []
            if span_count > 0 and isinstance(located_spans, list):
                valid = sum(1 for ls in located_spans if ls.get("start_found") is True)
                validity = valid / float(span_count)
                plan_validity_vals.append(validity)
                for ep in endpoint_labels:
                    per_endpoint_plan_validity.setdefault(ep, []).append(validity)

            le = m.get("localization_error") or {}
            per_file_errs = le.get("per_file") or le.get("per_plan") or []
            file_errors: list[float] = []
            if isinstance(per_file_errs, list):
                for v in per_file_errs:
                    fv = _safe_float(v)
                    if fv is None:
                        continue
                    if math.isfinite(fv):
                        error_all_vals.append(fv)
                        file_errors.append(fv)
                        for ep in endpoint_labels:
                            per_endpoint_error_all.setdefault(ep, []).append(fv)
                    else:
                        error_inf_total += 1
                        file_error_inf_counts[file_path] = (
                            file_error_inf_counts.get(file_path, 0) + 1
                        )
                        for ep in endpoint_labels:
                            per_endpoint_error_inf_counts[ep] = (
                                per_endpoint_error_inf_counts.get(ep, 0) + 1
                            )
            if file_errors:
                file_error_vals.setdefault(file_path, []).extend(file_errors)
            mean = _mean(file_errors)
            if mean is not None:
                error_mean_vals.append(mean)
                for ep in endpoint_labels:
                    per_endpoint_error_mean.setdefault(ep, []).append(mean)

            within_k = m.get("within_k_lines")
            if isinstance(within_k, dict):
                k_val = within_k.get("k")
                if isinstance(k_val, int):
                    k_vals.append(k_val)
                predicted_span_precision = _safe_float(
                    within_k.get("predicted_span_precision", within_k.get("per_plan_precision"))
                )
                if predicted_span_precision is not None:
                    precision_k_vals.append(predicted_span_precision)
                    file_precision_k_vals.setdefault(file_path, []).append(predicted_span_precision)
                    for ep in endpoint_labels:
                        per_endpoint_precision_k.setdefault(ep, []).append(predicted_span_precision)
                file_hit = within_k.get("file_hit") is True
                if has_perf:
                    recall_k_val = 1.0 if file_hit else 0.0
                    recall_k_vals.append(recall_k_val)
                    file_recall_k_vals.setdefault(file_path, []).append(recall_k_val)
                    for ep in endpoint_labels:
                        per_endpoint_recall_k.setdefault(ep, []).append(recall_k_val)
                else:
                    spec_k_val = 1.0 if not file_hit else 0.0
                    specificity_k_vals.append(spec_k_val)
                    file_specificity_k_vals.setdefault(file_path, []).append(spec_k_val)
                    for ep in endpoint_labels:
                        per_endpoint_specificity_k.setdefault(ep, []).append(spec_k_val)

    k_label = None
    unique_k = sorted(set(k_vals))
    first_k = k_vals[0] if k_vals else None
    if len(unique_k) == 1:
        k_label = f"@k={unique_k[0]}"
    elif first_k is not None:
        k_label = f"@k={first_k}"

    _plot_hist_01(
        plan_validity_vals,
        title="Plan Validity Rate Distribution",
        xlabel="Plan Validity Rate",
        out_path=out_dir / "plan_validity_distribution.pdf",
    )
    _plot_violin_by_label(
        per_endpoint_plan_validity,
        title="Plan Validity Rate per Endpoint",
        ylabel="Plan Validity Rate",
        out_path=out_dir / "plan_validity_violin.pdf",
    )
    if k_label:
        _plot_hist_01(
            precision_k_vals,
            title=f"Precision {k_label} Distribution",
            xlabel=f"Precision {k_label}",
            annotation=f"k={unique_k[0]}" if len(unique_k) == 1 else None,
            out_path=out_dir / "precision_within_k_distribution.pdf",
        )
        _plot_violin_by_label(
            per_endpoint_precision_k,
            title=f"Precision {k_label} per Endpoint",
            ylabel=f"Precision {k_label}",
            annotation=(f"k={unique_k[0]}" if len(unique_k) == 1 else None),
            out_path=out_dir / "precision_within_k_violin.pdf",
        )
        _plot_hist_01(
            recall_k_vals,
            title=f"Recall {k_label} Distribution",
            xlabel=f"Recall {k_label}",
            annotation=f"k={unique_k[0]}" if len(unique_k) == 1 else None,
            out_path=out_dir / "recall_within_k_distribution.pdf",
        )
        _plot_violin_by_label(
            per_endpoint_recall_k,
            title=f"Recall {k_label} per Endpoint",
            ylabel=f"Recall {k_label}",
            annotation=(f"k={unique_k[0]}" if len(unique_k) == 1 else None),
            out_path=out_dir / "recall_within_k_violin.pdf",
        )
        _plot_precision_recall_violin_combined(
            per_endpoint_precision_k,
            per_endpoint_recall_k,
            per_endpoint_specificity_k,
            title=f"Localization Quality{(' ' + k_label) if k_label else ''}",
            annotation=None,
            out_path=out_dir / "precision_recall_within_k_violin_combined.pdf",
        )
        _plot_localization_quality_violin_figure(
            per_endpoint_precision_k,
            per_endpoint_recall_k,
            per_endpoint_error_mean,
            title=f"Localization Quality{(' ' + k_label) if k_label else ''}",
            annotation=(f"k={unique_k[0]}" if len(unique_k) == 1 else None),
            out_path=out_dir / "localization_quality_violin_figure.pdf",
        )
        _plot_hist_01(
            specificity_k_vals,
            title=f"Specificity {k_label} Distribution",
            xlabel=f"Specificity {k_label}",
            annotation=f"k={unique_k[0]}" if len(unique_k) == 1 else None,
            out_path=out_dir / "specificity_within_k_distribution.pdf",
        )
        _plot_violin_by_label(
            per_endpoint_specificity_k,
            title=f"Specificity {k_label} per Endpoint",
            ylabel=f"Specificity {k_label}",
            annotation=(f"k={unique_k[0]}" if len(unique_k) == 1 else None),
            out_path=out_dir / "specificity_within_k_violin.pdf",
        )

    _plot_scatter(
        loc_vs_gain_points,
        title="Localization Error vs. Detection Gain",
        xlabel="Average localization error (lines)",
        ylabel="Detection gain (observed / MDE)",
        out_path=out_dir / "localization_vs_detection_gain_scatter.pdf",
    )
    _plot_hist(
        error_mean_vals,
        title="Average Localization Error Distribution",
        xlabel="Average localization error (lines)",
        out_path=out_dir / "localization_error_mean_distribution.pdf",
        bins=25,
    )
    _plot_violin_by_label(
        per_endpoint_error_mean,
        title="Average Localization Error per Endpoint",
        ylabel="Average localization error (lines)",
        out_path=out_dir / "localization_error_mean_violin.pdf",
    )
    _plot_hist(
        error_all_vals,
        title="Localization Error Distribution (All Plans)",
        xlabel="Localization error (lines)",
        out_path=out_dir / "localization_error_distribution.pdf",
        bins=30,
    )
    _plot_violin(
        error_all_vals,
        title="Localization Error (All Plans)",
        ylabel="Localization error (lines)",
        out_path=out_dir / "localization_error_violin.pdf",
    )
    _plot_location_error_distribution_bars_per_file(
        per_endpoint_error_all,
        per_endpoint_error_inf_counts,
        title="Localization Error Distribution (U1/U2/U3)",
        out_path=out_dir / "localization_error_per_file_bars.pdf",
        preferred_labels=["U1 (E7)", "U2 (E8)", "U3 (E9)", "U1", "U2", "U3"],
        top_n_labels=3,
    )

    if args.show:
        import matplotlib.pyplot as plt  # type: ignore

        plt.show()

    # Build per-file averages across reports.
    per_file_summary: dict[str, dict[str, Any]] = {}
    all_file_paths = sorted(
        set(file_precision_vals) | set(file_sensitivity_vals) | set(file_specificity_vals)
        | set(file_precision_k_vals) | set(file_recall_k_vals) | set(file_specificity_k_vals)
    )
    for fp in all_file_paths:
        entry: dict[str, Any] = {}
        if fp in file_precision_vals:
            entry["precision_mean"] = _mean(file_precision_vals[fp])
            entry["precision_n"] = len(file_precision_vals[fp])
        if fp in file_sensitivity_vals:
            entry["sensitivity_mean"] = _mean(file_sensitivity_vals[fp])
            entry["sensitivity_n"] = len(file_sensitivity_vals[fp])
        if fp in file_specificity_vals:
            entry["specificity_mean"] = _mean(file_specificity_vals[fp])
            entry["specificity_n"] = len(file_specificity_vals[fp])
        if fp in file_precision_k_vals:
            entry["precision_at_k_mean"] = _mean(file_precision_k_vals[fp])
            entry["precision_at_k_n"] = len(file_precision_k_vals[fp])
        if fp in file_recall_k_vals:
            entry["recall_at_k_mean"] = _mean(file_recall_k_vals[fp])
            entry["recall_at_k_n"] = len(file_recall_k_vals[fp])
        if fp in file_specificity_k_vals:
            entry["specificity_at_k_mean"] = _mean(file_specificity_k_vals[fp])
            entry["specificity_at_k_n"] = len(file_specificity_k_vals[fp])
        per_file_summary[fp] = entry

    # Build per-endpoint averages across reports.
    per_endpoint_summary: dict[str, dict[str, Any]] = {}
    all_endpoints = sorted(
        set(per_endpoint_precision) | set(per_endpoint_sensitivity) | set(per_endpoint_specificity)
        | set(per_endpoint_precision_k) | set(per_endpoint_recall_k) | set(per_endpoint_specificity_k)
    )
    for ep in all_endpoints:
        entry_ep: dict[str, Any] = {}
        if ep in per_endpoint_precision:
            entry_ep["precision_mean"] = _mean(per_endpoint_precision[ep])
            entry_ep["precision_n"] = len(per_endpoint_precision[ep])
        if ep in per_endpoint_sensitivity:
            entry_ep["sensitivity_mean"] = _mean(per_endpoint_sensitivity[ep])
            entry_ep["sensitivity_n"] = len(per_endpoint_sensitivity[ep])
        if ep in per_endpoint_specificity:
            entry_ep["specificity_mean"] = _mean(per_endpoint_specificity[ep])
            entry_ep["specificity_n"] = len(per_endpoint_specificity[ep])
        if ep in per_endpoint_precision_k:
            entry_ep["precision_at_k_mean"] = _mean(per_endpoint_precision_k[ep])
            entry_ep["precision_at_k_n"] = len(per_endpoint_precision_k[ep])
        if ep in per_endpoint_recall_k:
            entry_ep["recall_at_k_mean"] = _mean(per_endpoint_recall_k[ep])
            entry_ep["recall_at_k_n"] = len(per_endpoint_recall_k[ep])
        if ep in per_endpoint_specificity_k:
            entry_ep["specificity_at_k_mean"] = _mean(per_endpoint_specificity_k[ep])
            entry_ep["specificity_at_k_n"] = len(per_endpoint_specificity_k[ep])
        per_endpoint_summary[ep] = entry_ep

    summary_out: dict[str, Any] = {
        "reports_found": len(report_paths),
        "precision_count": len(precision_vals),
        "sensitivity_count": len(sensitivity_vals),
        "specificity_count": len(specificity_vals),
        "plan_validity_count": len(plan_validity_vals),
        "error_mean_count": len(error_mean_vals),
        "error_finite_count": len(error_all_vals),
        "error_infinite_count": error_inf_total,
        "precision_median": _median(precision_vals),
        "sensitivity_median": _median(sensitivity_vals),
        "specificity_median": _median(specificity_vals),
        "precision_within_k_median": _median(precision_k_vals),
        "recall_within_k_median": _median(recall_k_vals),
        "specificity_within_k_median": _median(specificity_k_vals),
        "line_tolerance": unique_k[0] if len(unique_k) == 1 else None,
        "plan_validity_median": _median(plan_validity_vals),
        "error_mean_of_means": _mean(error_mean_vals),
        "per_file": per_file_summary,
        "per_endpoint": per_endpoint_summary,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary_out, indent=2), encoding="utf-8")

    print(f"Processed {len(report_paths)} reports.")
    print(f"Wrote plots and summary to {out_dir}")


if __name__ == "__main__":
    main()
