from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any, Iterable

from .eval_stats_errors import EvalStatsError


def _to_float_list(values: Iterable[Any]) -> list[float]:
    out: list[float] = []
    for v in values:
        if v is None:
            continue
        if isinstance(v, bool):
            continue
        try:
            out.append(float(v))
        except Exception:
            continue
    return out


def _samples_from_json_obj(obj: Any, *, path_for_errors: str) -> list[float]:
    """
    Extract numeric samples from a JSON object.

    Supported shapes:
    - [1,2,3]
    - {"samples":[...]} (also: "values", "durations_ms")
    """
    if isinstance(obj, list):
        samples = _to_float_list(obj)
    elif isinstance(obj, dict):
        for key in ("samples", "values", "durations_ms"):
            if key in obj:
                samples = _to_float_list(obj.get(key) or [])
                break
        else:
            raise EvalStatsError(
                f"Unsupported JSON shape in {path_for_errors}. Expected list or dict with keys "
                f"one of: samples|values|durations_ms."
            )
    else:
        raise EvalStatsError(f"Unsupported JSON top-level type in {path_for_errors}: {type(obj).__name__}")

    if not samples:
        raise EvalStatsError(f"No numeric samples found in {path_for_errors}")
    return samples


def load_samples(path: Path) -> list[float]:
    """
    Load a numeric sample list from JSON or CSV.

    Supported JSON shapes:
    - [1.0, 2.0, ...]
    - {"samples": [...]}  (also: "values", "durations_ms")
    """

    if not path.exists():
        raise EvalStatsError(f"Input not found: {path}")

    suffix = path.suffix.lower()
    raw = path.read_text(encoding="utf-8")

    if suffix in {".json", ".jsonl"}:
        try:
            data = json.loads(raw)
        except Exception as e:
            raise EvalStatsError(f"Invalid JSON: {path}") from e

        return _samples_from_json_obj(data, path_for_errors=str(path))

    if suffix in {".csv", ".tsv"}:
        delim = "," if suffix == ".csv" else "\t"
        reader = csv.reader(raw.splitlines(), delimiter=delim)
        values: list[Any] = []
        for row in reader:
            if not row:
                continue
            # take first cell; ignore headers if not numeric
            values.append(row[0])
        samples = _to_float_list(values)
        if not samples:
            raise EvalStatsError(f"No numeric samples found in {path}")
        return samples

    raise EvalStatsError(f"Unsupported file type for {path} (use .json or .csv)")


def load_sample_pair(path: Path) -> tuple[list[float], list[float]]:
    """
    Load old/new samples from a single JSON file.

    Supported shapes:
    - {"old": [...], "new": [...]}
    - {"old": {"samples":[...]} , "new": {"samples":[...]}}
    - {"old": {"values":[...]}  , "new": {"durations_ms":[...]}}
    """
    if not path.exists():
        raise EvalStatsError(f"Input not found: {path}")
    if path.suffix.lower() not in {".json", ".jsonl"}:
        raise EvalStatsError(f"Unsupported file type for {path} (use .json/.jsonl)")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise EvalStatsError(f"Invalid JSON: {path}") from e

    if not isinstance(data, dict):
        raise EvalStatsError(f"Expected JSON object with 'old' and 'new' keys in {path}")
    if "old" not in data or "new" not in data:
        raise EvalStatsError(f"Missing 'old'/'new' keys in {path}")

    old = _samples_from_json_obj(data.get("old"), path_for_errors=f"{path} (old)")
    new = _samples_from_json_obj(data.get("new"), path_for_errors=f"{path} (new)")
    return old, new


def _iter_sqlite_files(path: Path) -> list[Path]:
    """Find SQLite database files (.db) in a path."""
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted([p for p in path.rglob("*.db") if p.is_file()])
    raise EvalStatsError(f"Input not found: {path}")


def is_sqlite_input(path: Path) -> bool:
    """
    True if `path` is a .db file or a directory containing .db files.
    """
    try:
        if not path.exists():
            return False
        if path.is_file():
            return path.suffix.lower() == ".db"
        if path.is_dir():
            return any(path.rglob("*.db"))
    except Exception:
        return False
    return False


def load_span_durations_by_name(
    path: Path,
    *,
    include_warmup: bool = False,
    span_name: str | None = None,
) -> dict[str, list[float]]:
    """
    Load duration samples (ms) from SQLite span event databases, grouped by span_name.
    """
    import sqlite3

    files = _iter_sqlite_files(path)
    if not files:
        raise EvalStatsError(f"No SQLite database files (.db) found under: {path}")

    out: dict[str, list[float]] = {}
    skipped: list[str] = []

    for f in files:
        conn = sqlite3.connect(f.as_posix())
        try:
            # Be tolerant: when a directory is provided we may encounter
            # empty/corrupt DBs or unrelated SQLite files.
            if not _sqlite_has_column(conn, table="spans", column="duration_ms"):
                skipped.append(f.as_posix())
                continue
            if not _sqlite_has_column(conn, table="spans", column="span_name"):
                skipped.append(f.as_posix())
                continue

            cursor = conn.cursor()

            query = "SELECT span_name, duration_ms FROM spans WHERE 1=1"
            params: list[Any] = []

            if span_name is not None:
                query += " AND span_name = ?"
                params.append(span_name)

            if not include_warmup:
                query += " AND (warmup IS NULL OR warmup = 0)"

            cursor.execute(query, params)

            for row in cursor.fetchall():
                try:
                    name = row[0]
                    if not name:
                        continue
                    v = float(row[1])
                except (TypeError, ValueError, IndexError):
                    continue
                out.setdefault(str(name), []).append(v)
        finally:
            conn.close()

    if not out:
        if span_name:
            msg = f"No samples found in {path} for span_name={span_name!r}"
        else:
            msg = f"No samples found in {path}"
        if skipped:
            msg += f" (skipped {len(skipped)} .db files without required spans schema)"
        raise EvalStatsError(msg)

    return out


def _sqlite_has_column(conn: Any, *, table: str, column: str) -> bool:
    """Check whether a SQLite table contains a given column."""
    try:
        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({table})")
        cols = [row[1] for row in cur.fetchall() if len(row) >= 2]
        return column in set(str(c) for c in cols)
    except Exception:
        return False


def load_cpu_steal_pct_timeseries(
    path: Path,
    *,
    include_warmup: bool = False,
    span_name: str | None = None,
) -> list[tuple[int, float]]:
    """
    Load CPU steal ratio time series from SQLite span DBs.

    Requires `worker.js` schema with `cpu_steal_pct` column.

    Returns:
        List of (ts_start_ms, cpu_steal_pct) tuples sorted by time.
        `cpu_steal_pct` is a ratio in [0, 1].
    """
    import sqlite3

    files = _iter_sqlite_files(path)
    if not files:
        raise EvalStatsError(f"No SQLite database files (.db) found under: {path}")

    out: list[tuple[int, float]] = []

    for f in files:
        conn = sqlite3.connect(f.as_posix())
        try:
            if not _sqlite_has_column(conn, table="spans", column="cpu_steal_pct"):
                # Older DB schema: skip silently.
                continue

            query = "SELECT ts_start_ms, cpu_steal_pct FROM spans WHERE cpu_steal_pct IS NOT NULL"
            params: list[Any] = []

            if span_name is not None:
                query += " AND span_name = ?"
                params.append(span_name)

            if not include_warmup:
                query += " AND (warmup IS NULL OR warmup = 0)"

            query += " ORDER BY ts_start_ms ASC"

            cur = conn.cursor()
            cur.execute(query, params)
            for row in cur.fetchall():
                try:
                    ts = int(row[0])
                    v = float(row[1])
                except (TypeError, ValueError, IndexError):
                    continue

                if not math.isfinite(v):
                    continue
                # Keep plots readable even if inputs are slightly off.
                v = min(1.0, max(0.0, v))
                out.append((ts, v))
        finally:
            conn.close()

    out.sort(key=lambda x: x[0])
    return out


def load_cpu_utilization_pct_timeseries(
    path: Path,
    *,
    include_warmup: bool = False,
    span_name: str | None = None,
) -> list[tuple[int, float]]:
    """
    Load CPU utilization ratio time series from SQLite span DBs.

    Requires `worker.js` schema with `cpu_utilization_pct` column.

    Returns:
        List of (ts_start_ms, cpu_utilization_pct) tuples sorted by time.
        `cpu_utilization_pct` is a ratio in [0, 1].
    """
    import sqlite3

    files = _iter_sqlite_files(path)
    if not files:
        raise EvalStatsError(f"No SQLite database files (.db) found under: {path}")

    out: list[tuple[int, float]] = []

    for f in files:
        conn = sqlite3.connect(f.as_posix())
        try:
            if not _sqlite_has_column(conn, table="spans", column="cpu_utilization_pct"):
                # Older DB schema: skip silently.
                continue

            query = "SELECT ts_start_ms, cpu_utilization_pct FROM spans WHERE cpu_utilization_pct IS NOT NULL"
            params: list[Any] = []

            if span_name is not None:
                query += " AND span_name = ?"
                params.append(span_name)

            if not include_warmup:
                query += " AND (warmup IS NULL OR warmup = 0)"

            query += " ORDER BY ts_start_ms ASC"

            cur = conn.cursor()
            cur.execute(query, params)
            for row in cur.fetchall():
                try:
                    ts = int(row[0])
                    v = float(row[1])
                except (TypeError, ValueError, IndexError):
                    continue

                if not math.isfinite(v):
                    continue
                # Keep plots readable even if inputs are slightly off.
                v = min(1.0, max(0.0, v))
                out.append((ts, v))
        finally:
            conn.close()

    out.sort(key=lambda x: x[0])
    return out


def load_cpu_time_total_ms_samples(
    path: Path,
    *,
    include_warmup: bool = False,
    span_name: str | None = None,
) -> list[float]:
    """
    Load per-span CPU time samples (user + system, ms) from SQLite span DBs.

    Requires `worker.js` schema with `cpu_time_user_ms` and `cpu_time_system_ms`.
    """
    import sqlite3

    files = _iter_sqlite_files(path)
    if not files:
        raise EvalStatsError(f"No SQLite database files (.db) found under: {path}")

    out: list[float] = []

    for f in files:
        conn = sqlite3.connect(f.as_posix())
        try:
            if not _sqlite_has_column(conn, table="spans", column="cpu_time_user_ms"):
                continue
            if not _sqlite_has_column(conn, table="spans", column="cpu_time_system_ms"):
                continue

            query = """
                SELECT cpu_time_user_ms, cpu_time_system_ms
                FROM spans
                WHERE cpu_time_user_ms IS NOT NULL AND cpu_time_system_ms IS NOT NULL
            """.strip()
            params: list[Any] = []

            if span_name is not None:
                query += " AND span_name = ?"
                params.append(span_name)

            if not include_warmup:
                query += " AND (warmup IS NULL OR warmup = 0)"

            cur = conn.cursor()
            cur.execute(query, params)
            for row in cur.fetchall():
                try:
                    u = float(row[0])
                    s = float(row[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if not (math.isfinite(u) and math.isfinite(s)):
                    continue
                out.append(u + s)
        finally:
            conn.close()

    return out


def load_cpu_time_total_ms_by_name(
    path: Path,
    *,
    include_warmup: bool = False,
    span_name: str | None = None,
) -> dict[str, list[float]]:
    """
    Load per-span CPU time samples (user + system, ms) from SQLite span DBs,
    grouped by span_name.
    """
    import sqlite3

    files = _iter_sqlite_files(path)
    if not files:
        raise EvalStatsError(f"No SQLite database files (.db) found under: {path}")

    out: dict[str, list[float]] = {}

    for f in files:
        conn = sqlite3.connect(f.as_posix())
        try:
            if not _sqlite_has_column(conn, table="spans", column="cpu_time_user_ms"):
                continue
            if not _sqlite_has_column(conn, table="spans", column="cpu_time_system_ms"):
                continue

            query = """
                SELECT span_name, cpu_time_user_ms, cpu_time_system_ms
                FROM spans
                WHERE cpu_time_user_ms IS NOT NULL AND cpu_time_system_ms IS NOT NULL
            """.strip()
            params: list[Any] = []

            if span_name is not None:
                query += " AND span_name = ?"
                params.append(span_name)

            if not include_warmup:
                query += " AND (warmup IS NULL OR warmup = 0)"

            cur = conn.cursor()
            cur.execute(query, params)
            for row in cur.fetchall():
                try:
                    name = row[0]
                    if not name:
                        continue
                    u = float(row[1])
                    s = float(row[2])
                except (TypeError, ValueError, IndexError):
                    continue

                if not (math.isfinite(u) and math.isfinite(s)):
                    continue
                out.setdefault(str(name), []).append(u + s)
        finally:
            conn.close()

    return out


def load_span_durations_ms(
    path: Path,
    *,
    span_name: str | None = None,
    include_warmup: bool = False,
) -> list[float]:
    """
    Load duration samples (ms) from SQLite span event databases.

    Expected SQLite schema (written by worker.js):
    - duration_ms (REAL)
    - span_name (TEXT)
    - warmup (INTEGER, 0 or 1)
    """
    import sqlite3

    files = _iter_sqlite_files(path)
    if not files:
        raise EvalStatsError(f"No SQLite database files (.db) found under: {path}")

    out: list[float] = []
    skipped: list[str] = []

    for f in files:
        conn = sqlite3.connect(f.as_posix())
        try:
            # Be tolerant: when a directory is provided we may encounter
            # empty/corrupt DBs or unrelated SQLite files.
            if not _sqlite_has_column(conn, table="spans", column="duration_ms"):
                skipped.append(f.as_posix())
                continue

            cursor = conn.cursor()

            # Build query with optional filters
            query = "SELECT duration_ms FROM spans WHERE 1=1"
            params: list[Any] = []

            if span_name is not None:
                query += " AND span_name = ?"
                params.append(span_name)

            if not include_warmup:
                query += " AND (warmup IS NULL OR warmup = 0)"

            cursor.execute(query, params)

            for row in cursor.fetchall():
                try:
                    v = float(row[0])
                    out.append(v)
                except (TypeError, ValueError):
                    continue
        finally:
            conn.close()

    if not out:
        if span_name:
            msg = f"No samples found in {path} for span_name={span_name!r}"
        else:
            msg = f"No samples found in {path}"
        if skipped:
            msg += f" (skipped {len(skipped)} .db files without required spans schema)"
        raise EvalStatsError(msg)
    return out


def load_k6_durations_ms(
    path: Path,
    *,
    metric_name: str = "http_req_duration",
) -> list[float]:
    """
    Load duration samples (ms) from k6 JSON output.

    k6 JSON output format (line-delimited JSON):
    {"type":"Point","metric":"http_req_duration","data":{"value":123.45,...},...}

    Args:
        path: Path to k6 JSON output file
        metric_name: Metric to extract (default: http_req_duration)

    Returns:
        List of duration values in milliseconds
    """
    if not path.exists():
        raise EvalStatsError(f"k6 JSON file not found: {path}")

    out: list[float] = []

    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("type") == "Point" and entry.get("metric") == metric_name:
                    value = entry.get("data", {}).get("value")
                    if value is not None:
                        out.append(float(value))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue

    if not out:
        raise EvalStatsError(f"No {metric_name} samples found in k6 JSON: {path}")

    return out


def is_k6_json(path: Path) -> bool:
    """Check if a file looks like k6 JSON output."""
    if not path.exists() or not path.is_file():
        return False
    if path.suffix.lower() != ".json":
        return False

    # Check first few lines for k6 format
    try:
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= 10:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    # k6 JSON has "type" and "metric" fields
                    if "type" in entry and "metric" in entry:
                        return True
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return False


def load_durations_ms(
    path: Path,
    *,
    span_name: str | None = None,
    include_warmup: bool = False,
    k6_metric: str = "http_req_duration",
) -> list[float]:
    """
    Generic loader for duration samples in milliseconds.

    Supported formats:
    - .db (file or dir): SQLite span events from worker.js (duration_ms column)
    - k6 JSON: Line-delimited JSON from k6 (http_req_duration metric)
    - .json/.csv: Generic sample lists

    Args:
        path: Path to file or directory
        span_name: Filter by span name (SQLite only)
        include_warmup: Include warmup samples (SQLite only)
        k6_metric: k6 metric name to extract (default: http_req_duration)
    """
    # SQLite database
    if path.suffix.lower() == ".db" or (path.is_dir() and any(path.rglob("*.db"))):
        return load_span_durations_ms(path, span_name=span_name, include_warmup=include_warmup)

    # k6 JSON output
    if is_k6_json(path):
        return load_k6_durations_ms(path, metric_name=k6_metric)

    # Generic JSON/CSV samples
    return load_samples(path)
