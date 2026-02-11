from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .eval_stats_bootstrap import (
    HAS_NUMPY,
    _bootstrap_delta_pct_distribution,
    _bootstrap_delta_pct_distribution_numpy,
    _percentile,
)
from .eval_stats_errors import EvalStatsError
from .eval_stats_plotting import plot_qq_plot
from .eval_stats_plotting import plot_span_qq_plots
from .eval_stats_plotting import plot_relative_change_distribution
from .eval_stats_plotting import plot_span_relative_change_distributions
from .eval_stats_stats import compare_samples


def _stat_value(values: list[float], stat: str) -> float:
    if not values:
        raise EvalStatsError("Cannot compute stat on empty samples")
    if stat == "mean":
        return statistics.fmean(values)
    if stat == "p50":
        return _percentile(sorted(values), 50.0)
    if stat == "p95":
        return _percentile(sorted(values), 95.0)
    raise EvalStatsError(f"Unknown stat: {stat}")


def _relative_pct(new_val: float, old_val: float) -> float:
    if old_val == 0.0:
        return 0.0
    return ((new_val - old_val) / old_val) * 100.0


def _endpoint_name(path: str) -> str:
    path = path.split("?", 1)[0]
    if path.startswith("/search"):
        return "search"
    if path.startswith("/top/region/"):
        return "top_region"
    if path.startswith("/top/user/"):
        return "top_user"
    if path.startswith("/top/region"):
        return "top_region"
    if path.startswith("/top/user"):
        return "top_user"
    return path.strip("/") or "root"


def _safe_slug(value: str) -> str:
    out = []
    for ch in value.lower():
        if ch.isalnum() or ch in {"-", "_"}:
            out.append(ch)
        else:
            out.append("_")
    return "".join(out).strip("_") or "endpoint"


def _parse_path(url: str | None, fallback_path: str | None) -> str:
    if fallback_path:
        return fallback_path
    if not url:
        return "/"
    parsed = urlparse(url)
    return parsed.path or "/"


def _parse_port(url: str | None) -> int | None:
    if not url:
        return None
    parsed = urlparse(url)
    return parsed.port


def _qq_plot_data(
    base_samples: list[float],
    head_samples: list[float],
    *,
    percentile_step: float = 1.0,
) -> dict[str, list[float]]:
    if not base_samples or not head_samples:
        raise EvalStatsError("Cannot compute Q-Q data with empty samples")
    if percentile_step <= 0:
        raise EvalStatsError("percentile_step must be > 0")
    base_median = statistics.median(base_samples)
    head_median = statistics.median(head_samples)

    def to_relative(values: list[float], baseline: float) -> list[float]:
        if baseline == 0:
            return [0.0 for _ in values]
        return [((v - baseline) / baseline) * 100.0 for v in values]

    base_sorted = sorted(to_relative(base_samples, base_median))
    head_sorted = sorted(to_relative(head_samples, head_median))
    percentiles: list[float] = []
    p = percentile_step
    while p < 100.0:
        percentiles.append(p)
        p += percentile_step
    if not percentiles:
        raise EvalStatsError("No percentiles generated for Q-Q data")
    base_q = [_percentile(base_sorted, pct) for pct in percentiles]
    head_q = [_percentile(head_sorted, pct) for pct in percentiles]
    return {
        "percentiles": percentiles,
        "traditional_quantiles_pct": base_q,
        "instrumented_quantiles_pct": head_q,
    }


def _histogram_probs(
    samples: list[float],
    *,
    bins: int,
    data_min: float,
    data_max: float,
) -> list[float]:
    if bins <= 0:
        raise EvalStatsError("bins must be > 0")
    if not samples:
        return [0.0 for _ in range(bins)]
    if data_max <= data_min:
        return [1.0] + [0.0 for _ in range(bins - 1)]
    width = (data_max - data_min) / bins
    counts = [0 for _ in range(bins)]
    for v in samples:
        if v >= data_max:
            idx = bins - 1
        else:
            idx = int((v - data_min) / width)
            if idx < 0:
                idx = 0
            elif idx >= bins:
                idx = bins - 1
        counts[idx] += 1
    total = sum(counts)
    if total == 0:
        return [0.0 for _ in range(bins)]
    return [c / total for c in counts]


def _hellinger_distance(
    base_samples: list[float],
    head_samples: list[float],
    *,
    bins: int = 50,
) -> float:
    if not base_samples or not head_samples:
        raise EvalStatsError("Cannot compute Hellinger distance with empty samples")
    data_min = min(min(base_samples), min(head_samples))
    data_max = max(max(base_samples), max(head_samples))
    if data_max == data_min:
        return 0.0
    p = _histogram_probs(base_samples, bins=bins, data_min=data_min, data_max=data_max)
    q = _histogram_probs(head_samples, bins=bins, data_min=data_min, data_max=data_max)
    sum_sq = 0.0
    for p_i, q_i in zip(p, q):
        sum_sq += (math.sqrt(p_i) - math.sqrt(q_i)) ** 2
    return (1.0 / math.sqrt(2.0)) * math.sqrt(sum_sq)


def load_k6_jsonl_samples(
    path: Path,
    *,
    base_port: int,
    head_port: int,
    status_ok: int = 200,
) -> tuple[dict[str, list[float]], dict[str, list[float]]]:
    if not path.exists():
        raise EvalStatsError(f"k6 JSONL file not found: {path}")

    base_samples: dict[str, list[float]] = defaultdict(list)
    head_samples: dict[str, list[float]] = defaultdict(list)

    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue

            # Pair-style log: {"endpoint": "...", "path": "...", "base": {...}, "head": {...}}
            if isinstance(entry, dict) and "base" in entry and "head" in entry:
                ep = entry.get("endpoint") or _endpoint_name(_parse_path(None, entry.get("path")))
                for target, bucket in (("base", base_samples), ("head", head_samples)):
                    data = entry.get(target) or {}
                    if not isinstance(data, dict):
                        continue
                    status = data.get("status")
                    if status_ok is not None and status != status_ok:
                        continue
                    value = data.get("time")
                    if value is None:
                        continue
                    try:
                        bucket[ep].append(float(value))
                    except (TypeError, ValueError):
                        continue
                continue

            # Single-request log: {"time": ..., "url": "...", "status": ...}
            if not isinstance(entry, dict):
                continue
            status = entry.get("status")
            if status_ok is not None and status != status_ok:
                continue
            value = entry.get("time")
            if value is None:
                continue
            url = entry.get("url")
            path_str = _parse_path(url, entry.get("path"))
            ep = entry.get("endpoint") or _endpoint_name(path_str)
            port = _parse_port(url)
            try:
                value_f = float(value)
            except (TypeError, ValueError):
                continue
            if port == base_port:
                base_samples[ep].append(value_f)
            elif port == head_port:
                head_samples[ep].append(value_f)

    return base_samples, head_samples


def _bootstrap_distribution(
    old: list[float],
    new: list[float],
    *,
    stat: str,
    bootstrap: int,
    alpha: float,
    seed: int,
) -> dict[str, Any]:
    if HAS_NUMPY:
        dist = _bootstrap_delta_pct_distribution_numpy(
            old,
            new,
            stat=stat,
            n_resamples=bootstrap,
            seed=seed,
        )
    else:
        dist = _bootstrap_delta_pct_distribution(
            old,
            new,
            stat=stat,
            n_resamples=bootstrap,
            seed=seed,
        )
    dist_sorted = sorted(dist)
    lo = _percentile(dist_sorted, 100.0 * (alpha / 2.0))
    hi = _percentile(dist_sorted, 100.0 * (1.0 - alpha / 2.0))

    old_stat = _stat_value(old, stat)
    new_stat = _stat_value(new, stat)
    observed = _relative_pct(new_stat, old_stat)

    return {
        "stat": stat,
        "bootstrap": int(bootstrap),
        "alpha": float(alpha),
        "observed_pct": float(observed),
        "ci": {"lo": float(lo), "hi": float(hi)},
        "distribution": [float(v) for v in dist],
    }


def evaluate_k6_jsonl(
    path: Path,
    *,
    base_port: int,
    head_port: int,
    bootstrap: int,
    alpha: float,
    seed: int,
    stat: str,
    min_samples: int,
) -> dict[str, Any]:
    base_by, head_by = load_k6_jsonl_samples(path, base_port=base_port, head_port=head_port)
    report = evaluate_k6_samples(
        base_by,
        head_by,
        bootstrap=bootstrap,
        alpha=alpha,
        seed=seed,
        stat=stat,
        min_samples=min_samples,
    )
    report["meta"] = {
        "input": str(path),
        "base_port": int(base_port),
        "head_port": int(head_port),
        "bootstrap": int(bootstrap),
        "alpha": float(alpha),
        "seed": int(seed),
        "stat": stat,
        "min_samples": int(min_samples),
    }
    return report


def evaluate_k6_samples(
    base_by: dict[str, list[float]],
    head_by: dict[str, list[float]],
    *,
    bootstrap: int,
    alpha: float,
    seed: int,
    stat: str,
    min_samples: int,
) -> dict[str, Any]:
    per_endpoint: dict[str, Any] = {}
    for endpoint in sorted(set(base_by.keys()) | set(head_by.keys())):
        base_samples = base_by.get(endpoint, [])
        head_samples = head_by.get(endpoint, [])
        if len(base_samples) < min_samples or len(head_samples) < min_samples:
            per_endpoint[endpoint] = {
                "skipped": True,
                "reason": f"Need at least {min_samples} samples per side",
                "base_n": len(base_samples),
                "head_n": len(head_samples),
            }
            continue
        per_endpoint[endpoint] = {
            "skipped": False,
            "base_n": len(base_samples),
            "head_n": len(head_samples),
            "summary": compare_samples(
                base_samples,
                head_samples,
                bootstrap=bootstrap,
                power=0.80,
                seed=seed,
                skip_ci=False,
            ),
            "relative_change_distribution": _bootstrap_distribution(
                base_samples,
                head_samples,
                stat=stat,
                bootstrap=bootstrap,
                alpha=alpha,
                seed=seed + 1,
            ),
            "hellinger_distance": _hellinger_distance(base_samples, head_samples),
            "qq_plot": _qq_plot_data(base_samples, head_samples, percentile_step=1.0),
        }

    overall_base = [v for vs in base_by.values() for v in vs]
    overall_head = [v for vs in head_by.values() for v in vs]
    overall: dict[str, Any] = {
        "base_n": len(overall_base),
        "head_n": len(overall_head),
    }
    if len(overall_base) >= min_samples and len(overall_head) >= min_samples:
        overall["summary"] = compare_samples(
            overall_base,
            overall_head,
            bootstrap=bootstrap,
            power=0.80,
            seed=seed + 10,
            skip_ci=False,
        )
        overall["relative_change_distribution"] = _bootstrap_distribution(
            overall_base,
            overall_head,
            stat=stat,
            bootstrap=bootstrap,
            alpha=alpha,
            seed=seed + 11,
        )
        overall["hellinger_distance"] = _hellinger_distance(overall_base, overall_head)
    else:
        overall["skipped"] = True
        overall["reason"] = f"Need at least {min_samples} samples per side"

    return {
        "overall": overall,
        "per_endpoint": per_endpoint,
    }


def _generate_plots(
    in_path: Path,
    *,
    plot_dir: Path,
    prefix: str,
    plot_format: str,
    base_port: int,
    head_port: int,
    min_samples: int,
    stat: str,
    bootstrap: int,
    alpha: float,
    seed: int,
) -> None:
    base_by, head_by = load_k6_jsonl_samples(
        in_path,
        base_port=base_port,
        head_port=head_port,
    )
    _generate_plots_from_samples(
        base_by,
        head_by,
        plot_dir=plot_dir,
        prefix=prefix,
        plot_format=plot_format,
        min_samples=min_samples,
        stat=stat,
        bootstrap=bootstrap,
        alpha=alpha,
        seed=seed,
    )


def _generate_plots_from_samples(
    base_by: dict[str, list[float]],
    head_by: dict[str, list[float]],
    *,
    plot_dir: Path,
    prefix: str,
    plot_format: str,
    min_samples: int,
    stat: str,
    bootstrap: int,
    alpha: float,
    seed: int,
) -> None:
    plot_dir.mkdir(parents=True, exist_ok=True)
    plot_path = plot_dir / f"{prefix}_relative_change_distributions.{plot_format}"
    # Keep only endpoints that have sufficient samples on both sides.
    base_by = {k: v for k, v in base_by.items() if len(v) >= min_samples}
    head_by = {k: v for k, v in head_by.items() if len(v) >= min_samples}
    common = sorted(set(base_by.keys()) & set(head_by.keys()))
    if common:
        plot_span_relative_change_distributions(
            {k: base_by[k] for k in common},
            {k: head_by[k] for k in common},
            stat=stat,
            bootstrap=bootstrap,
            alpha=alpha,
            seed=seed,
            title="k6 Relative Change Distributions (Bootstrap)",
            save_path=plot_path,
        )
        qq_plot_path = plot_dir / f"{prefix}_qq_plots.{plot_format}"
        plot_span_qq_plots(
            {k: base_by[k] for k in common},
            {k: head_by[k] for k in common},
            percentile_step=1.0,
            title=None,
            save_path=qq_plot_path,
        )
    # Emit one plot per endpoint with CIs.
    for endpoint in common:
        base_samples = base_by.get(endpoint, [])
        head_samples = head_by.get(endpoint, [])
        if len(base_samples) < min_samples or len(head_samples) < min_samples:
            continue
        endpoint_slug = _safe_slug(endpoint)
        endpoint_plot = plot_dir / f"{prefix}_relative_change_distribution_{endpoint_slug}.{plot_format}"
        plot_relative_change_distribution(
            base_samples,
            head_samples,
            stat=stat,
            bootstrap=bootstrap,
            alpha=alpha,
            seed=seed,
            title=f"k6 Relative Change Distribution — {endpoint}",
            save_path=endpoint_plot,
        )
        endpoint_qq_plot = plot_dir / f"{prefix}_qq_{endpoint_slug}.{plot_format}"
        plot_qq_plot(
            base_samples,
            head_samples,
            percentile_step=1.0,
            title=f"{endpoint}",
            save_path=endpoint_qq_plot,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate k6 jsonl logs (base/head) with bootstrap distributions.")
    parser.add_argument("--input", help="Path to k6 jsonl file (console logs).")
    parser.add_argument("--input-dir", help="Directory to scan for duet-k6.jsonl files.")
    parser.add_argument("--out", help="Where to write JSON report (file input).")
    parser.add_argument("--out-dir", help="Output directory for directory input (optional).")
    parser.add_argument(
        "--aggregate",
        action="store_true",
        help="Generate aggregate report/plots across all jsonl files (input-dir mode).",
    )
    parser.add_argument("--base-port", type=int, default=3001, help="Base service port (default: 3001).")
    parser.add_argument("--head-port", type=int, default=3002, help="Head service port (default: 3002).")
    parser.add_argument("--bootstrap", type=int, default=2000, help="Bootstrap resamples.")
    parser.add_argument("--alpha", type=float, default=0.01, help="Significance level for CI.")
    parser.add_argument("--seed", type=int, default=0, help="Random seed.")
    parser.add_argument("--stat", default="p50", choices=["mean", "p50", "p95"], help="Statistic to bootstrap.")
    parser.add_argument("--min-samples", type=int, default=10, help="Min samples per side to include endpoint.")
    parser.add_argument("--plot", action="store_true", help="Generate relative change distribution plot.")
    parser.add_argument("--plot-dir", default=None, help="Directory for plots (default: same as output).")
    parser.add_argument("--plot-format", default="png", help="Plot format: png, pdf, svg.")
    args = parser.parse_args()

    if bool(args.input) == bool(args.input_dir):
        raise SystemExit("Provide exactly one of --input or --input-dir.")

    if args.input:
        if not args.out:
            raise SystemExit("--out is required when using --input.")
        in_path = Path(args.input).expanduser().resolve()
        out_path = Path(args.out).expanduser().resolve()
        report = evaluate_k6_jsonl(
            in_path,
            base_port=args.base_port,
            head_port=args.head_port,
            bootstrap=args.bootstrap,
            alpha=args.alpha,
            seed=args.seed,
            stat=args.stat,
            min_samples=args.min_samples,
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

        if args.plot:
            plot_dir = Path(args.plot_dir).expanduser().resolve() if args.plot_dir else out_path.parent
            _generate_plots(
                in_path,
                plot_dir=plot_dir,
                prefix=out_path.stem,
                plot_format=args.plot_format,
                base_port=args.base_port,
                head_port=args.head_port,
                min_samples=args.min_samples,
                stat=args.stat,
                bootstrap=args.bootstrap,
                alpha=args.alpha,
                seed=args.seed,
            )
    else:
        input_dir = Path(args.input_dir).expanduser().resolve()
        if not input_dir.exists():
            raise SystemExit(f"input dir not found: {input_dir}")
        jsonl_paths = sorted(input_dir.rglob("duet-k6.jsonl"))
        if not jsonl_paths:
            raise SystemExit(f"No duet-k6.jsonl found under {input_dir}")
        out_root = Path(args.out_dir).expanduser().resolve() if args.out_dir else None
        plot_root = Path(args.plot_dir).expanduser().resolve() if args.plot_dir else None
        aggregated_base: dict[str, list[float]] = defaultdict(list)
        aggregated_head: dict[str, list[float]] = defaultdict(list)

        for in_path in jsonl_paths:
            rel_parent = in_path.parent.relative_to(input_dir)
            out_base = (out_root / rel_parent) if out_root else in_path.parent
            out_path = out_base / "k6_eval_report.json"
            report = evaluate_k6_jsonl(
                in_path,
                base_port=args.base_port,
                head_port=args.head_port,
                bootstrap=args.bootstrap,
                alpha=args.alpha,
                seed=args.seed,
                stat=args.stat,
                min_samples=args.min_samples,
            )
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

            if args.plot:
                plot_dir = (plot_root / rel_parent) if plot_root else out_path.parent
                _generate_plots(
                    in_path,
                    plot_dir=plot_dir,
                    prefix=out_path.stem,
                    plot_format=args.plot_format,
                    base_port=args.base_port,
                    head_port=args.head_port,
                    min_samples=args.min_samples,
                    stat=args.stat,
                    bootstrap=args.bootstrap,
                    alpha=args.alpha,
                    seed=args.seed,
                )

            if args.aggregate:
                base_by, head_by = load_k6_jsonl_samples(
                    in_path,
                    base_port=args.base_port,
                    head_port=args.head_port,
                )
                for ep, samples in base_by.items():
                    aggregated_base[ep].extend(samples)
                for ep, samples in head_by.items():
                    aggregated_head[ep].extend(samples)

        if args.aggregate:
            aggregate_out_base = out_root if out_root else input_dir
            aggregate_out_path = aggregate_out_base / "k6_eval_report_aggregate.json"
            aggregate_report = evaluate_k6_samples(
                dict(aggregated_base),
                dict(aggregated_head),
                bootstrap=args.bootstrap,
                alpha=args.alpha,
                seed=args.seed,
                stat=args.stat,
                min_samples=args.min_samples,
            )
            aggregate_report["meta"] = {
                "input_dir": str(input_dir),
                "base_port": int(args.base_port),
                "head_port": int(args.head_port),
                "bootstrap": int(args.bootstrap),
                "alpha": float(args.alpha),
                "seed": int(args.seed),
                "stat": args.stat,
                "min_samples": int(args.min_samples),
                "files_aggregated": len(jsonl_paths),
            }
            aggregate_out_path.parent.mkdir(parents=True, exist_ok=True)
            aggregate_out_path.write_text(
                json.dumps(aggregate_report, indent=2),
                encoding="utf-8",
            )

            if args.plot:
                aggregate_plot_dir = plot_root if plot_root else aggregate_out_path.parent
                _generate_plots_from_samples(
                    dict(aggregated_base),
                    dict(aggregated_head),
                    plot_dir=aggregate_plot_dir,
                    prefix="k6_eval_report_aggregate",
                    plot_format=args.plot_format,
                    min_samples=args.min_samples,
                    stat=args.stat,
                    bootstrap=args.bootstrap,
                    alpha=args.alpha,
                    seed=args.seed,
                )


if __name__ == "__main__":
    main()
