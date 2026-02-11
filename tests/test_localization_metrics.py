from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import src.localization_metrics as lm


@dataclass(frozen=True)
class _Plan:
    path: str
    insert_after_new: str
    occurrence_new: int = 1
    end_before_new: str | None = None
    end_occurrence_new: int = 1


def test_localization_error_sums_all_span_group_errors_not_best_span(
    monkeypatch, tmp_path: Path
) -> None:
    path = "examples/movie/src/filtering/als.ts"
    plans = [
        _Plan(path=path, insert_after_new="span-a"),
        _Plan(path=path, insert_after_new="span-b"),
    ]

    located_by_insert = {
        "span-a": lm.LocatedSpan(
            path=path,
            start_line=173,
            end_line=233,
            start_found=True,
            end_found=True,
        ),
        "span-b": lm.LocatedSpan(
            path=path,
            start_line=205,
            end_line=223,
            start_found=True,
            end_found=True,
        ),
    }

    def _fake_locate(plan: _Plan, *, head_dir: Path, strip_markers: bool = True) -> lm.LocatedSpan:
        return located_by_insert[plan.insert_after_new]

    monkeypatch.setattr(lm, "locate_span_in_head", _fake_locate)

    metrics, _ = lm.compute_localization_metrics(
        plans=plans,
        head_dir=tmp_path,
        diffs_by_path={path: "@@ -1 +1 @@\n-a\n+b"},
        perfect_span_by_path={path: (205, 220)},
    )

    # There are two predicted groups:
    #   [173, 233] -> error 45 to [205, 220]
    #   [205, 223] -> error 3 to [205, 220]
    # File-level localization error must sum to 48 (not 3).
    assert metrics.localization_errors == [48.0]
    assert metrics.localization_error_mean == 48.0
    assert metrics.localization_error_median == 48.0
