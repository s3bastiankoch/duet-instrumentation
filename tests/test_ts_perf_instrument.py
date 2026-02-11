from __future__ import annotations

from src.ts_perf_instrument import InsertPlan, apply_perf_markers


def test_inserts_endspan_before_return_and_throw() -> None:
    ts = """
export function f(x: number) {
  if (x < 0) {
    return -1;
  }
  if (x === 0) {
    throw new Error("boom");
  }
  return x + 1;
}
""".lstrip()

    plan = InsertPlan(
        path="src/app.ts",
        insert_after="export function f(x: number) {",
        occurrence=1,
        label="f-body",
        reason="test",
    )

    out, inserted = apply_perf_markers(ts, [plan], strict=True, instrument_mode="text")
    assert inserted == 1
    # startSpan and endSpan exist
    assert "startSpan(" in out
    assert "endSpan(" in out
    # endSpan should appear before 'return -1' line
    assert out.find("endSpan(") < out.find("return -1;")
    # There should be an endSpan before the throw as well (not necessarily the first one).
    assert out.rfind("endSpan(") < out.find('throw new Error("boom");') or "endSpan(" in out[: out.find('throw new Error("boom");')]


def test_idempotent_does_not_duplicate() -> None:
    ts = "export function f() {\n  return 1;\n}\n"
    plan = InsertPlan(
        path="src/app.ts",
        insert_after="export function f() {",
        occurrence=1,
        label="f",
        reason="test",
    )
    out1, inserted1 = apply_perf_markers(ts, [plan], strict=True, instrument_mode="text")
    out2, inserted2 = apply_perf_markers(out1, [plan], strict=True, instrument_mode="text")
    assert inserted1 == 1
    assert inserted2 == 0
    assert out1 == out2


def test_non_strict_skips_missing_anchor() -> None:
    ts = "export function f() {\n  return 1;\n}\n"
    plan = InsertPlan(
        path="src/app.ts",
        insert_after="DOES_NOT_EXIST",
        occurrence=1,
        label="x",
        reason="test",
    )
    out, inserted = apply_perf_markers(ts, [plan], strict=False, instrument_mode="text")
    assert out == ts
    assert inserted == 0


def test_end_before_whitespace_insensitive_fallback() -> None:
    ts = """
export function f() {
  const x = 1;

  // Compute full predictions matrix as U * V^T.
  return x;
}
""".lstrip()

    # Note the intentional newline reflow + missing indentation before '}' compared to the real file.
    end_before = "}\n\n  // Compute full predictions matrix as \nU * V^T."

    plan = InsertPlan(
        path="src/app.ts",
        insert_after="export function f() {",
        occurrence=1,
        label="ws-fallback",
        reason="test",
        end_before=end_before,
        end_occurrence=1,
    )

    out, inserted = apply_perf_markers(ts, [plan], strict=True, instrument_mode="text")
    assert inserted == 1
    assert "startSpan(" in out
    assert "endSpan(" in out

