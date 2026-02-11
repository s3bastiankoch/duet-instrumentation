from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.eval_stats import (
    EvalStatsError,
    compare_samples,
    is_sqlite_input,
    load_durations_ms,
    load_sample_pair,
    load_samples,
    load_span_durations_by_name,
    summarize_samples,
)


def test_summarize_basic_stats() -> None:
    s = summarize_samples([1.0, 2.0, 3.0, 4.0], bootstrap=500, seed=0)
    assert s.n == 4
    assert s.min == 1.0
    assert s.max == 4.0
    assert 2.4 < s.mean < 2.6
    assert s.p50 == 2.5
    assert s.p95 > s.p90
    assert s.mean_ci is not None
    assert s.mean_ci["lo"] <= s.mean_ci["hi"]


def test_compare_has_mean_and_p95_deltas() -> None:
    """Test that compare_samples returns relative percentage deltas."""
    rep = compare_samples([10, 10, 10, 10], [20, 20, 20, 20], bootstrap=500, seed=1)
    # Deltas are now relative percentages: (new - old) / old * 100
    # (20 - 10) / 10 * 100 = 100% increase
    assert rep["deltas"]["mean"]["pct"] == 100.0
    assert rep["deltas"]["p95"]["pct"] == 100.0
    # CI should also be in percentages
    assert "ci" in rep["deltas"]["mean"]
    assert rep["deltas"]["mean"]["ci"]["lo"] <= rep["deltas"]["mean"]["ci"]["hi"]


def test_load_samples_json_list(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert load_samples(p) == [1.0, 2.0, 3.0]


def test_load_samples_json_dict_key(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"durations_ms": [1, 2, "3"]}), encoding="utf-8")
    assert load_samples(p) == [1.0, 2.0, 3.0]


def test_load_sample_pair_json(tmp_path: Path) -> None:
    p = tmp_path / "pair.json"
    p.write_text(json.dumps({"old": [1, 2, 3], "new": {"samples": [2, 3, 4]}}), encoding="utf-8")
    old, new = load_sample_pair(p)
    assert old == [1.0, 2.0, 3.0]
    assert new == [2.0, 3.0, 4.0]


def test_load_samples_csv_first_column(tmp_path: Path) -> None:
    p = tmp_path / "x.csv"
    p.write_text("value\n1\n2\n3\n", encoding="utf-8")
    assert load_samples(p) == [1.0, 2.0, 3.0]


def test_load_samples_rejects_empty(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    p.write_text(json.dumps([]), encoding="utf-8")
    with pytest.raises(EvalStatsError):
        load_samples(p)


def test_load_durations_ms_delegates_to_load_samples_for_json(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert load_durations_ms(p) == [1.0, 2.0, 3.0]


def test_load_durations_ms_from_sqlite(tmp_path: Path) -> None:
    """Test loading span durations from SQLite database."""
    import sqlite3
    
    db_path = tmp_path / "spans.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE spans (
            id INTEGER PRIMARY KEY,
            duration_ms REAL,
            span_name TEXT,
            warmup INTEGER
        )
    """)
    # Insert test data
    conn.execute("INSERT INTO spans (duration_ms, span_name, warmup) VALUES (10.5, 'test-span', 0)")
    conn.execute("INSERT INTO spans (duration_ms, span_name, warmup) VALUES (20.5, 'test-span', 0)")
    conn.execute("INSERT INTO spans (duration_ms, span_name, warmup) VALUES (30.5, 'other-span', 0)")
    conn.execute("INSERT INTO spans (duration_ms, span_name, warmup) VALUES (100.0, 'test-span', 1)")  # warmup
    conn.commit()
    conn.close()
    
    # Load all spans (excluding warmup)
    durations = load_durations_ms(db_path)
    assert sorted(durations) == [10.5, 20.5, 30.5]
    
    # Load filtered by span_name
    durations_filtered = load_durations_ms(db_path, span_name="test-span")
    assert sorted(durations_filtered) == [10.5, 20.5]
    
    # Load including warmup
    durations_with_warmup = load_durations_ms(db_path, span_name="test-span", include_warmup=True)
    assert sorted(durations_with_warmup) == [10.5, 20.5, 100.0]


def test_load_durations_ms_from_sqlite_directory(tmp_path: Path) -> None:
    """Test loading span durations from a directory containing SQLite databases."""
    import sqlite3
    
    # Create two database files
    for i, version in enumerate(["v1", "v2"]):
        db_path = tmp_path / f"spans-{version}.db"
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE spans (
                id INTEGER PRIMARY KEY,
                duration_ms REAL,
                span_name TEXT,
                warmup INTEGER
            )
        """)
        conn.execute(f"INSERT INTO spans (duration_ms, span_name, warmup) VALUES ({10.0 + i * 10}, 'test-span', 0)")
        conn.commit()
        conn.close()
    
    # Load from directory
    durations = load_durations_ms(tmp_path)
    assert sorted(durations) == [10.0, 20.0]


def test_load_span_durations_by_name_from_sqlite(tmp_path: Path) -> None:
    import sqlite3

    db_path = tmp_path / "spans.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE spans (
            id INTEGER PRIMARY KEY,
            duration_ms REAL,
            span_name TEXT,
            warmup INTEGER
        )
    """)
    conn.execute("INSERT INTO spans (duration_ms, span_name, warmup) VALUES (10.5, 'test-span', 0)")
    conn.execute("INSERT INTO spans (duration_ms, span_name, warmup) VALUES (20.5, 'test-span', 0)")
    conn.execute("INSERT INTO spans (duration_ms, span_name, warmup) VALUES (30.5, 'other-span', 0)")
    conn.execute("INSERT INTO spans (duration_ms, span_name, warmup) VALUES (100.0, 'test-span', 1)")  # warmup
    conn.commit()
    conn.close()

    grouped = load_span_durations_by_name(db_path)
    assert sorted(grouped.keys()) == ["other-span", "test-span"]
    assert sorted(grouped["test-span"]) == [10.5, 20.5]
    assert sorted(grouped["other-span"]) == [30.5]

    grouped_with_warmup = load_span_durations_by_name(db_path, include_warmup=True, span_name="test-span")
    assert list(grouped_with_warmup.keys()) == ["test-span"]
    assert sorted(grouped_with_warmup["test-span"]) == [10.5, 20.5, 100.0]

    assert is_sqlite_input(db_path) is True
    assert is_sqlite_input(tmp_path) is True

