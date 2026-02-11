from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any

from .eval_stats_stats import compare_samples


def _compare_span_worker(args: tuple) -> tuple[str, dict[str, Any]]:
    """Worker function for parallel span comparison."""
    span_name, old_samples, new_samples, bootstrap, power, skip_ci = args
    result = compare_samples(
        old_samples,
        new_samples,
        bootstrap=bootstrap,
        power=power,
        skip_ci=skip_ci,
    )
    return (span_name, result)


def compare_samples_parallel(
    old_by_span: dict[str, list[float]],
    new_by_span: dict[str, list[float]],
    *,
    bootstrap: int = 2000,
    power: float = 0.80,
    skip_ci: bool = False,
    max_workers: int | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Compare samples for multiple spans in parallel.

    Args:
        old_by_span: Dict mapping span names to old/base samples
        new_by_span: Dict mapping span names to new/head samples
        bootstrap: Number of bootstrap resamples
        power: Statistical power for MDE
        skip_ci: Skip bootstrap CI computation
        max_workers: Max parallel workers (None = CPU count)

    Returns:
        Dict mapping span names to comparison results
    """
    common_spans = sorted(set(old_by_span.keys()) & set(new_by_span.keys()))

    if not common_spans:
        return {}

    # Build work items
    work_items = [
        (span_name, old_by_span[span_name], new_by_span[span_name], bootstrap, power, skip_ci)
        for span_name in common_spans
    ]

    results: dict[str, dict[str, Any]] = {}

    # Use ProcessPoolExecutor for true parallelism (bypasses GIL)
    # For small numbers of spans, sequential may be faster due to overhead
    if len(work_items) <= 2:
        # Sequential for small workloads
        for item in work_items:
            span_name, result = _compare_span_worker(item)
            results[span_name] = result
    else:
        try:
            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(_compare_span_worker, item): item[0] for item in work_items}
                for future in as_completed(futures):
                    span_name, result = future.result()
                    results[span_name] = result
        except (PermissionError, OSError):
            # Some environments (e.g., heavily sandboxed) block process primitives.
            # Fall back to sequential execution.
            for item in work_items:
                span_name, result = _compare_span_worker(item)
                results[span_name] = result

    return results


def filter_spans_by_min_samples(
    old_by_span: dict[str, list[float]],
    new_by_span: dict[str, list[float]],
    *,
    min_samples: int = 10,
) -> tuple[dict[str, list[float]], dict[str, list[float]], list[str]]:
    """
    Filter per-span sample dicts, keeping spans with at least min_samples in both sets.

    Returns:
        (filtered_old, filtered_new, skipped_spans)
    """
    if min_samples <= 0:
        return (old_by_span, new_by_span, [])

    common_spans = sorted(set(old_by_span.keys()) & set(new_by_span.keys()))
    skipped = [
        s
        for s in common_spans
        if (len(old_by_span.get(s, [])) < min_samples or len(new_by_span.get(s, [])) < min_samples)
    ]
    keep = [s for s in common_spans if s not in set(skipped)]

    filtered_old = {s: old_by_span[s] for s in keep}
    filtered_new = {s: new_by_span[s] for s in keep}
    return (filtered_old, filtered_new, skipped)
