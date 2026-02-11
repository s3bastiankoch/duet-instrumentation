from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol


class LocalizationMetricsError(RuntimeError):
    """Raised when localization metrics cannot be computed."""


class _PlanLike(Protocol):
    """
    Minimal protocol for PerfMarkerPlan-like objects.

    We keep this module independent from the instrumentation pipeline by relying on duck-typing
    instead of importing PerfMarkerPlan directly.
    """

    path: str
    insert_after_new: str
    occurrence_new: int
    end_before_new: str | None
    end_occurrence_new: int


MARKER_COMMENT_PREFIX = "// otel-agent: perf-marker"
_SPAN_CALL_RE = re.compile(r"""\b(?:startSpan|endSpan)\(\s*["']otel-agent:""")
LINE_DISTANCE_TOLERANCE = 5


@dataclass(frozen=True)
class PlanAnchors:
    """
    Minimal, serializable plan representation for localization evaluation.

    This matches the keys written by `perf_marker_plan.json` (from PerfMarkerPlan.__dict__).
    """

    path: str
    insert_after_new: str
    occurrence_new: int = 1
    end_before_new: str | None = None
    end_occurrence_new: int = 1

    @classmethod
    def from_obj(cls, obj: Any) -> "PlanAnchors | None":
        if not isinstance(obj, dict):
            return None
        path = obj.get("path")
        insert_after_new = obj.get("insert_after_new")
        if not isinstance(path, str) or not path.strip():
            return None
        if not isinstance(insert_after_new, str) or not insert_after_new.strip():
            return None
        occ = obj.get("occurrence_new", 1)
        try:
            occurrence_new = int(occ) if occ is not None else 1
        except Exception:
            occurrence_new = 1

        end_before_new = obj.get("end_before_new")
        if not isinstance(end_before_new, str) or not end_before_new.strip():
            end_before_new = None
        end_occ = obj.get("end_occurrence_new", 1)
        try:
            end_occurrence_new = int(end_occ) if end_occ is not None else 1
        except Exception:
            end_occurrence_new = 1

        return cls(
            path=path.strip(),
            insert_after_new=insert_after_new,
            occurrence_new=occurrence_new,
            end_before_new=end_before_new,
            end_occurrence_new=end_occurrence_new,
        )


@dataclass(frozen=True)
class LocatedSpan:
    """
    A best-effort location of a planned span in the (head) source.

    `start_line`/`end_line` are 1-based line numbers in the *de-instrumented* text (marker lines removed).
    """

    path: str
    start_line: int | None
    end_line: int | None
    start_found: bool
    end_found: bool
    error: str | None = None

    @property
    def line_range(self) -> tuple[int, int] | None:
        if self.start_line is None:
            return None
        if self.end_line is None:
            return (self.start_line, self.start_line)
        lo = min(self.start_line, self.end_line)
        hi = max(self.start_line, self.end_line)
        return (lo, hi)


def strip_perf_markers(ts: str) -> str:
    """
    Remove otel-agent perf marker lines from instrumented TypeScript/JavaScript.

    This is used to keep line numbers comparable to unified diff line numbers from the uninstrumented version.
    """

    out_lines: list[str] = []
    for line in ts.splitlines(True):  # keep line endings
        if MARKER_COMMENT_PREFIX in line:
            continue
        if _SPAN_CALL_RE.search(line):
            continue
        out_lines.append(line)
    return "".join(out_lines)


def _resolve_file_path(dir_path: Path, rel_path: str) -> tuple[Path, str]:
    """
    Find the actual file path in a directory, trying prefix-stripping.

    Returns (full_path, resolved_rel_path).
    """

    # Try direct path first
    target = dir_path / rel_path
    if target.exists():
        return target, rel_path

    parts = rel_path.split("/")
    for i in range(len(parts)):
        candidate = "/".join(parts[i:])
        target = dir_path / candidate
        if target.exists():
            return target, candidate

    raise FileNotFoundError(f"File not found: {rel_path} in {dir_path}")


def _find_nth(haystack: str, needle: str, n: int) -> int:
    if n <= 0:
        n = 1
    start = -1
    for _ in range(n):
        start = haystack.find(needle, start + 1)
        if start == -1:
            return -1
    return start


def _find_nth_ws_insensitive(haystack: str, needle: str, n: int) -> int:
    """
    Find the nth occurrence of `needle` in `haystack`, treating whitespace differences as equivalent.
    Returns the start index of the match in the original haystack, or -1.
    """

    if n <= 0:
        n = 1
    tokens = re.split(r"\s+", needle.strip())
    if not tokens or tokens == [""]:
        return -1
    pattern = r"\s+".join(re.escape(t) for t in tokens)
    pattern = r"\s*" + pattern
    matches = list(re.finditer(pattern, haystack, flags=re.MULTILINE | re.DOTALL))
    if len(matches) < n:
        return -1
    return matches[n - 1].start()


def _line_number_at_index(text: str, idx: int) -> int:
    """
    Convert a character index into a 1-based line number.
    """

    if idx < 0:
        raise ValueError("idx must be >= 0")
    return text.count("\n", 0, idx) + 1


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", flags=re.MULTILINE)


def parse_unified_diff_changed_lines_new(
    diff_unified: str,
) -> set[int]:
    """
    Parse a unified diff and return the set of changed line numbers on the NEW/+ side.

    We count only lines that appear as additions ('+'), which corresponds to inserted/modified lines
    in the new version. Deleted-only changes do not contribute new-side line numbers.
    """

    changed: set[int] = set()
    new_line: int | None = None
    old_line: int | None = None

    for raw in diff_unified.splitlines():
        m = _HUNK_RE.match(raw)
        if m:
            old_line = int(m.group(1))
            new_line = int(m.group(3))
            continue

        # Ignore any content before the first hunk.
        if new_line is None or old_line is None:
            continue

        if not raw:
            # Empty context line counts as a context line in diff output only if prefixed by ' '.
            # A truly empty raw line can't be interpreted reliably; ignore.
            continue

        c = raw[0]

        # Skip file header lines.
        if raw.startswith("+++ ") or raw.startswith("--- "):
            continue

        if c == "+":
            changed.add(new_line)
            new_line += 1
        elif c == "-":
            old_line += 1
        elif c == " ":
            old_line += 1
            new_line += 1
        else:
            # "\ No newline at end of file" or other meta lines
            continue

    return changed


def locate_span_in_head(
    plan: _PlanLike,
    *,
    head_dir: Path,
    strip_markers: bool = True,
) -> LocatedSpan:
    """
    Locate planned span anchors in the head source tree and return their (best-effort) line range.
    """

    try:
        file_path, _ = _resolve_file_path(head_dir, plan.path)
    except FileNotFoundError as e:
        return LocatedSpan(
            path=plan.path,
            start_line=None,
            end_line=None,
            start_found=False,
            end_found=False,
            error=str(e),
        )

    try:
        raw = file_path.read_text(encoding="utf-8")
    except Exception as e:
        return LocatedSpan(
            path=plan.path,
            start_line=None,
            end_line=None,
            start_found=False,
            end_found=False,
            error=f"Failed to read file: {e}",
        )

    text = strip_perf_markers(raw) if strip_markers else raw

    # Start anchor: insert_after_new
    start_idx = _find_nth(text, plan.insert_after_new, int(getattr(plan, "occurrence_new", 1) or 1))
    start_found = start_idx != -1
    start_line = _line_number_at_index(text, start_idx) if start_found else None

    # End anchor: end_before_new (insert BEFORE that line)
    end_line: int | None = None
    end_found = False
    end_before = getattr(plan, "end_before_new", None)
    if isinstance(end_before, str) and end_before.strip():
        occ = int(getattr(plan, "end_occurrence_new", 1) or 1)
        end_idx = _find_nth(text, end_before, occ)
        if end_idx == -1:
            end_idx = _find_nth_ws_insensitive(text, end_before, occ)
        end_found = end_idx != -1
        end_line = _line_number_at_index(text, end_idx) if end_found else None

    return LocatedSpan(
        path=plan.path,
        start_line=start_line,
        end_line=end_line,
        start_found=start_found,
        end_found=end_found,
        error=None if start_found else f"Start anchor not found for {plan.path}",
    )


@dataclass(frozen=True)
class LocalizationEval:
    """
    Aggregate localization metrics for a single evaluation unit (e.g., an experiment run).
    """

    localization_accuracy: float  # in [0,1]
    localization_errors: list[float]  # per-file (lines); may contain +inf
    line_tolerance: int
    precision_within_lines: float | None
    recall_within_lines: float | None
    specificity_within_lines: float | None

    tp_predicted_spans_within_lines: int
    fp_predicted_spans_within_lines: int
    tn_files_within_lines: int
    fn_files_within_lines: int

    @property
    def localization_error_mean(self) -> float:
        finite = [x for x in self.localization_errors if math.isfinite(x)]
        if not finite:
            return float("inf")
        return float(sum(finite) / len(finite))

    @property
    def localization_error_median(self) -> float:
        finite = [x for x in self.localization_errors if math.isfinite(x)]
        if not finite:
            return float("inf")
        finite.sort()
        mid = len(finite) // 2
        if len(finite) % 2 == 1:
            return float(finite[mid])
        return float((finite[mid - 1] + finite[mid]) / 2.0)


def _distance_between_ranges(a: tuple[int, int], b: tuple[int, int]) -> float:
    a_lo, a_hi = a
    b_lo, b_hi = b
    if a_lo <= b_hi and b_lo <= a_hi:
        return 0.0
    if a_hi < b_lo:
        return float(b_lo - a_hi)
    return float(a_lo - b_hi)


def _boundary_distance(a: tuple[int, int], b: tuple[int, int]) -> float:
    """
    Absolute boundary distance between two ranges.

    Measures ``|a_lo - b_lo| + |a_hi - b_hi|``, penalising both undershoot
    (planned spans narrower than perfect) and overshoot (planned spans wider
    than perfect).  Returns 0 only when both boundaries match exactly.
    """

    a_lo, a_hi = a
    b_lo, b_hi = b
    return float(abs(a_lo - b_lo) + abs(a_hi - b_hi))


def _min_distance_range_to_lines(rng: tuple[int, int], lines: Iterable[int]) -> float:
    lo, hi = rng
    best: float | None = None
    for ln in lines:
        if lo <= ln <= hi:
            return 0.0
        d = float(min(abs(ln - lo), abs(ln - hi)))
        if best is None or d < best:
            best = d
    return float("inf") if best is None else best


def _hit_within_tolerance(
    *,
    loc: LocatedSpan,
    tolerance: int,
    perfect_span: tuple[int, int] | None,
    changed_lines: set[int],
) -> bool:
    rng = loc.line_range
    if rng is None:
        return False
    if perfect_span is not None:
        return _boundary_distance(rng, perfect_span) <= float(tolerance)
    if not changed_lines:
        return False
    return _min_distance_range_to_lines(rng, changed_lines) <= float(tolerance)


_MERGE_MAX_LINE_SEPARATION = 1  # allow merging when separation is 0 or 1 lines


def _merge_adjacent_ranges(
    ranges: list[tuple[int, int]],
    max_separation: int = _MERGE_MAX_LINE_SEPARATION,
) -> list[tuple[int, int]]:
    """
    Merge sorted ranges when they are non-overlapping and separated by at most
    `max_separation` lines (i.e. 0 or 1 lines by default).

    Returns a list of merged (lo, hi) ranges.
    """

    if not ranges:
        return []
    sorted_ranges = sorted(ranges, key=lambda r: (r[0], r[1]))
    merged: list[tuple[int, int]] = [sorted_ranges[0]]
    for lo, hi in sorted_ranges[1:]:
        prev_lo, prev_hi = merged[-1]
        # Merge only non-overlapping ranges when separation is <= 1 line.
        # Overlapping or containing ranges are intentionally kept separate.
        if lo >= prev_hi and (lo - prev_hi) <= max_separation:
            merged[-1] = (prev_lo, max(prev_hi, hi))
        else:
            merged.append((lo, hi))
    return merged


def _merged_file_hit_within_tolerance(
    *,
    locs: list[LocatedSpan],
    tolerance: int,
    perfect_span: tuple[int, int] | None,
    changed_lines: set[int],
) -> bool:
    """
    Check whether any merged group of spans is within tolerance of the
    perfect span.

    Only non-overlapping spans with 0/1 line separation are merged; overlapping
    or containing spans are kept separate and checked individually. When no
    perfect span is available, falls back to per-group checks against changed
    lines.
    """

    ranges = [loc.line_range for loc in locs if loc.line_range is not None]
    if not ranges:
        return False

    groups = _merge_adjacent_ranges(ranges)

    if perfect_span is not None:
        return any(
            _boundary_distance(g, perfect_span) <= float(tolerance)
            for g in groups
        )

    # No perfect span – fall back to per-group checks against changed lines.
    if not changed_lines:
        return False
    return any(
        _min_distance_range_to_lines(g, changed_lines) <= float(tolerance)
        for g in groups
    )


def _zero_gap_group_hits_within_tolerance(
    *,
    locs: list[LocatedSpan],
    tolerance: int,
    perfect_span: tuple[int, int] | None,
    changed_lines: set[int],
) -> list[bool]:
    """
    Return per-group hit booleans after merging spans with zero line gap.

    This is used for precision calculation so that contiguous/touching spans
    are counted as one predicted span.
    """

    ranges = [loc.line_range for loc in locs if loc.line_range is not None]
    if not ranges:
        return []

    groups = _merge_adjacent_ranges(ranges, max_separation=0)
    if perfect_span is not None:
        return [_boundary_distance(g, perfect_span) <= float(tolerance) for g in groups]
    if not changed_lines:
        return [False for _ in groups]
    return [_min_distance_range_to_lines(g, changed_lines) <= float(tolerance) for g in groups]

def compute_localization_metrics_per_file(
    *,
    plans: list[_PlanLike],
    head_dir: Path,
    diffs_by_path: dict[str, str],
    perf_relevant_by_path: dict[str, bool] | None = None,
    perfect_span_by_path: dict[str, tuple[int, int]] | None = None,
    strip_markers: bool = True,
) -> dict[str, dict[str, Any]]:
    """
    Compute localization metrics *per file*.

    For each file path present in `diffs_by_path` we consider only the planned spans that target that file.

    We compute two kinds of signals:
    - For files with *performance-relevant* changed lines: localization accuracy/error against those lines.
    - For files with *no* performance-relevant changed lines: "no-span" success (did the pipeline avoid
      placing spans in a perf-neutral change?).

    Returns a JSON-serializable mapping keyed by file path.
    """

    by_path: dict[str, list[_PlanLike]] = {}
    for p in plans:
        by_path.setdefault(p.path, []).append(p)

    out: dict[str, dict[str, Any]] = {}

    for path, diff in diffs_by_path.items():
        file_plans = by_path.get(path) or []
        located_for_plans = [
            locate_span_in_head(p, head_dir=head_dir, strip_markers=strip_markers) for p in file_plans
        ]
        perfect_span = None
        if perfect_span_by_path is not None:
            perfect_span = perfect_span_by_path.get(path)
        is_perf_relevant_change = (
            True
            if perf_relevant_by_path is None
            else bool(perf_relevant_by_path.get(path, True))
        )
        perf_changed_lines = (
            parse_unified_diff_changed_lines_new(diff) if (diff and is_perf_relevant_change) else set()
        )
        has_perf_relevant_changes = bool(perf_changed_lines)
        tolerance = LINE_DISTANCE_TOLERANCE
        # Precision uses zero-gap merged span groups so contiguous spans count
        # as one predicted span.
        per_precision_group_hits = _zero_gap_group_hits_within_tolerance(
            locs=located_for_plans,
            tolerance=tolerance,
            perfect_span=perfect_span,
            changed_lines=perf_changed_lines,
        )
        predicted_span_hit_count = sum(1 for hit in per_precision_group_hits if hit)
        predicted_span_total = len(per_precision_group_hits)
        predicted_span_precision = (
            None
            if not per_precision_group_hits
            else predicted_span_hit_count / float(predicted_span_total)
        )
        # file_hit uses merged spans so that multiple spans that jointly cover
        # the perfect span are recognised as a hit even if no single span does.
        file_hit = _merged_file_hit_within_tolerance(
            locs=located_for_plans,
            tolerance=tolerance,
            perfect_span=perfect_span,
            changed_lines=perf_changed_lines,
        )

        # Always emit an entry so we can evaluate "no-span" behavior on perf-neutral diffs.
        base: dict[str, Any] = {
            "plans": [getattr(p, "__dict__", {"path": p.path}) for p in file_plans],
            "planned_span_count": len(file_plans),
            "perf_relevant_changed_line_count": len(perf_changed_lines),
            "has_perf_relevant_changes": has_perf_relevant_changes,
            "change_is_perf_relevant": is_perf_relevant_change,
            "perfect_span": (
                None
                if perfect_span is None
                else {"start_line": perfect_span[0], "end_line": perfect_span[1]}
            ),
            "within_k_lines": {
                "k": tolerance,
                "predicted_span_hits": predicted_span_hit_count,
                "predicted_span_total": predicted_span_total,
                "predicted_span_precision": predicted_span_precision,
                "file_hit": file_hit,
            },
        }

        if not has_perf_relevant_changes:
            # Negative evaluation: success means the pipeline did not propose any spans in this file.
            base.update(
                {
                    "expected_spans": 0,
                    "no_span_success": len(file_plans) == 0,
                    "false_positive_spans_on_perf_neutral_change": len(file_plans) > 0,
                    "localization_accuracy": None,
                    "localization_error": None,
                    "located_spans": [ls.__dict__ for ls in located_for_plans],
                }
            )
            out[path] = base
            continue

        # Perf-relevant file: compute localization metrics if there are plans; otherwise record miss.
        if not file_plans:
            base.update(
                {
                    "expected_spans": ">=1",
                    "no_span_success": None,
                    "false_positive_spans_on_perf_neutral_change": None,
                    "missed_perf_relevant_change": True,
                    "localization_accuracy": 0.0,
                    "localization_error": None,
                    "located_spans": [],
                }
            )
            out[path] = base
            continue

        metrics, located = compute_localization_metrics(
            plans=file_plans,
            head_dir=head_dir,
            diffs_by_path={path: diff},
            perf_relevant_by_path=perf_relevant_by_path,
            perfect_span_by_path=perfect_span_by_path,
            strip_markers=strip_markers,
        )

        base.update(
            {
                "expected_spans": ">=1",
                "no_span_success": None,
                "false_positive_spans_on_perf_neutral_change": None,
                "missed_perf_relevant_change": False,
                # For perf-relevant files, accuracy is whether the file has any planned span.
                "localization_accuracy": 1.0 if len(file_plans) > 0 else 0.0,
                "localization_error": {
                    "per_file": metrics.localization_errors,
                    "mean_finite": metrics.localization_error_mean,
                    "median_finite": metrics.localization_error_median,
                },
                "located_spans": [ls.__dict__ for ls in located],
            }
        )
        out[path] = base

    return out


def compute_localization_metrics(
    *,
    plans: list[_PlanLike],
    head_dir: Path,
    diffs_by_path: dict[str, str],
    perf_relevant_by_path: dict[str, bool] | None = None,
    perfect_span_by_path: dict[str, tuple[int, int]] | None = None,
    strip_markers: bool = True,
) -> tuple[LocalizationEval, list[LocatedSpan]]:
    """
    Compute:
    - Localization Accuracy (file-level): perf-relevant files that have any planned span
    - Localization Error (lines, per-file): all located spans for a file contribute to the
      file error. We first build span groups via `_merge_adjacent_ranges` (non-overlapping
      ranges with <=1 line separation are merged), then sum each group's distance:
      * with a perfect span: boundary distance ``|group_lo - perfect_lo| + |group_hi - perfect_hi|``
      * without a perfect span: minimum line-distance from that group to changed diff lines

    Args:
        plans: Marker plans for the evaluated file(s)
        head_dir: Root of head source tree
        diffs_by_path: Mapping {file_path: unified_diff_text}
        perf_relevant_by_path: Optional mapping {file_path: is_perf_relevant}
        perfect_span_by_path: Optional mapping {file_path: (start_line, end_line)}
        strip_markers: Whether to strip inserted marker lines before locating anchors
    """

    located: list[LocatedSpan] = [
        locate_span_in_head(p, head_dir=head_dir, strip_markers=strip_markers) for p in plans
    ]

    changed_lines_cache: dict[str, set[int]] = {}

    def changed_lines_for(path: str) -> set[int]:
        if path in changed_lines_cache:
            return changed_lines_cache[path]
        if perf_relevant_by_path is not None and (perf_relevant_by_path.get(path, True) is False):
            changed_lines_cache[path] = set()
            return changed_lines_cache[path]
        diff = diffs_by_path.get(path) or ""
        cls = parse_unified_diff_changed_lines_new(diff) if diff else set()
        changed_lines_cache[path] = cls
        return cls

    def perfect_span_for(path: str) -> tuple[int, int] | None:
        if perfect_span_by_path is None:
            return None
        return perfect_span_by_path.get(path)

    # Per-file localization error: sum contributions from all predicted span
    # groups so we do not keep only a single best/smallest error.
    spans_by_file: dict[str, list[LocatedSpan]] = {}
    for loc in located:
        spans_by_file.setdefault(loc.path, []).append(loc)

    errors: list[float] = []
    for path, file_locs in spans_by_file.items():
        ranges = [loc.line_range for loc in file_locs if loc.line_range is not None]
        if not ranges:
            errors.append(float("inf"))
            continue

        groups = _merge_adjacent_ranges(ranges)

        perfect = perfect_span_for(path)
        if perfect is not None:
            errors.append(sum(_boundary_distance(g, perfect) for g in groups))
            continue

        cls = changed_lines_for(path)
        if not cls:
            errors.append(float("inf"))
            continue
        errors.append(sum(_min_distance_range_to_lines(g, cls) for g in groups))

    # Localization Accuracy (binary): any planned span in a perf-relevant file.
    perf_relevant_files: set[str] = set()
    for path, diff in diffs_by_path.items():
        if perf_relevant_by_path is not None and (perf_relevant_by_path.get(path, True) is False):
            continue
        if diff and parse_unified_diff_changed_lines_new(diff):
            perf_relevant_files.add(path)

    has_span_on_perf_file = any(loc.path in perf_relevant_files for loc in located)

    # Thresholded precision/recall within LINE_DISTANCE_TOLERANCE.
    tolerance = LINE_DISTANCE_TOLERANCE
    precision_group_hits: list[bool] = []
    for path, file_locs in spans_by_file.items():
        precision_group_hits.extend(
            _zero_gap_group_hits_within_tolerance(
                locs=file_locs,
                tolerance=tolerance,
                perfect_span=perfect_span_for(path),
                changed_lines=changed_lines_for(path),
            )
        )
    tp_plans = sum(1 for hit in precision_group_hits if hit)
    fp_plans = len(precision_group_hits) - tp_plans
    precision = (
        None if not precision_group_hits else tp_plans / float(len(precision_group_hits))
    )

    # File-level hit uses merged spans (consistent with localization error).
    all_files = set(diffs_by_path.keys())
    hit_by_path: dict[str, bool] = {}
    for fpath in all_files:
        file_locs = spans_by_file.get(fpath, [])
        if not file_locs:
            hit_by_path[fpath] = False
            continue
        hit_by_path[fpath] = _merged_file_hit_within_tolerance(
            locs=file_locs,
            tolerance=tolerance,
            perfect_span=perfect_span_for(fpath),
            changed_lines=changed_lines_for(fpath),
        )

    perf_files_with_hit = {p for p in perf_relevant_files if hit_by_path.get(p, False)}
    fn_files = len(perf_relevant_files - perf_files_with_hit)
    recall = (
        None
        if not perf_relevant_files
        else len(perf_files_with_hit) / float(len(perf_relevant_files))
    )
    non_perf_files = all_files - perf_relevant_files
    fp_files = sum(1 for path in non_perf_files if hit_by_path.get(path) is True)
    tn_files = len(non_perf_files) - fp_files
    specificity = (
        None if not non_perf_files else tn_files / float(len(non_perf_files))
    )

    metrics = LocalizationEval(
        localization_accuracy=1.0 if has_span_on_perf_file else 0.0,
        localization_errors=errors,
        line_tolerance=tolerance,
        precision_within_lines=precision,
        recall_within_lines=recall,
        specificity_within_lines=specificity,
        tp_predicted_spans_within_lines=tp_plans,
        fp_predicted_spans_within_lines=fp_plans,
        tn_files_within_lines=tn_files,
        fn_files_within_lines=fn_files,
    )
    return metrics, located


def diffs_by_path_from_changeset(changeset: Any) -> dict[str, str]:
    """
    Build {path: diff_unified} from a ChangeSet-like object (duck-typed).

    Expects `changeset.files` with elements having `.path` and `.diff_unified`.
    """

    out: dict[str, str] = {}
    files = getattr(changeset, "files", None)
    if not isinstance(files, list):
        return out
    for f in files:
        path = getattr(f, "path", None)
        diff = getattr(f, "diff_unified", None)
        if isinstance(path, str) and isinstance(diff, str) and diff.strip():
            out[path] = diff
    return out

