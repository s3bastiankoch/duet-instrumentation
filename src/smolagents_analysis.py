from __future__ import annotations

import ast
import json
from dataclasses import dataclass
import os
from typing import Any
from smolagents import REMOVE_PARAMETER

class SmolagentsAnalysisError(RuntimeError):
    """Raised when we cannot analyze changes via smolagents."""


@dataclass(frozen=True)
class PerfMarkerPlan:
    """
    A plan for inserting a perf marker into a TypeScript file.

    We use a textual anchor instead of line numbers so it stays stable across edits.
    """

    path: str
    # Anchor from the HEAD/new version for startSpan.
    insert_after_new: str
    # Metadata (must not follow defaulted fields in a dataclass).
    label: str
    reason: str

    occurrence_new: int = 1  # 1-based occurrence of the anchor in the file

    # Optional anchor from the BASE/old version (preferred for old_version instrumentation).
    insert_after_old: str | None = None
    occurrence_old: int | None = None

    severity: str = "medium"  # low/medium/high

    # Anchors for endSpan placement (insert BEFORE these lines).
    end_before_new: str | None = None
    end_occurrence_new: int = 1
    end_before_old: str | None = None
    end_occurrence_old: int | None = None

    # Extended context for richer span metadata.
    function_name: str | None = None  # Name of the containing function/method
    operation_type: str | None = None  # e.g., "database", "http", "computation", "file-io", "cache"
    attributes: dict[str, str] | None = None  # Additional key-value pairs for the span

    # Return value propagation analysis.
    propagation_risk: str = "none"  # none/low/medium/high - risk of affecting callers
    return_value_change: str | None = None  # Description of what changed in return value


def analyze_changes_for_perf_markers(
    *,
    model_id: str,
    changes: list[dict[str, Any]],
    repo_context_hint: str,
    sensitivity: str = "medium",
) -> list[PerfMarkerPlan]:
    """
    Ask a smolagents CodeAgent to propose where to add span markers (`startSpan` / `endSpan`).

    The returned plans should be implementable by searching for `insert_after` in the *HEAD* file.
    """

    try:
        from smolagents import CodeAgent, OpenAIModel
    except Exception as e:  # pragma: no cover
        raise SmolagentsAnalysisError(
            "smolagents is required. Install dependencies (e.g. `uv sync`)."
        ) from e

    sensitivity_norm = str(sensitivity or "medium").strip().lower()
    if sensitivity_norm not in ("low", "medium", "high"):
        sensitivity_norm = "medium"

    # We keep the prompt strict: return JSON only.
    prompt = (
        "You are a performance-focused code reviewer.\n"
        "We have changes between two application versions. Your job:\n"
        "- Identify change points likely to have *significant* performance impact.\n"
        "- Propose where to insert `startSpan(name)` and `endSpan(name)` to measure latency.\n"
        "\n"
        f"Sensitivity: {sensitivity_norm}\n"
        "Interpretation (how aggressive to be):\n"
        "- low: focus on obviously expensive work (db, network, heavy loops, parsing/serialization); treat logging/console as NOT perf-critical unless inside tight loops or clearly hot paths.\n"
        "- medium: include logging/console when newly added in request/handler paths, in loops, or when it formats/serializes large objects.\n"
        "- high: treat any new logging/console (e.g., console.log/debug/error) as potentially perf-sensitive; prefer proposing a marker if it is in a likely hot path.\n"
        "\n"
        "Rules:\n"
        "- Return a Python dict ONLY (no markdown, no commentary, no JSON).\n"
        "- Only propose markers inside changed files.\n"
        "- Provide BOTH start and end anchors:\n"
        "  - `insert_after_new`: snippet AFTER which to insert `startSpan` (new/head version)\n"
        "  - `insert_after_old`: same for the old/base version (or null if file is new)\n"
        "  - `end_before_new`: snippet BEFORE which to insert `endSpan` (new/head version)\n"
        "  - `end_before_old`: same for the old/base version (or null if file is new)\n"
        "- For end positions, consider:\n"
        "  - Return statements that exit the measured block\n"
        "  - The closing brace of the function/block being measured\n"
        "  - Early returns or throw statements within the measured scope\n"
        "  - If multiple exit points exist, pick the primary one (we'll handle others separately)\n"
        "- Anchors must be copied exactly from the provided excerpts.\n"
        "  - Do NOT reformat, re-indent, wrap, or summarize.\n"
        "  - Keep whitespace exactly as-is.\n"
        "  - The anchor must be a contiguous substring that appears verbatim in the excerpt.\n"
        "  - If you are not confident you can copy verbatim, set the anchor to null.\n"
        "- Prefer anchors like function signatures, loop starts, new expensive calls, or newly introduced logic.\n"
        "- Keep anchors short but unique (one line or a short snippet).\n"
        "- If you propose multiple markers in a file, ensure each has a different anchor or set occurrences.\n"
        "- Provide rich context for each span to aid in performance analysis.\n"
        "\n"
        "## RETURN VALUE PROPAGATION ANALYSIS\n"
        "\n"
        "For each change, assess whether the return value changed in ways that could affect callers:\n"
        "- `propagation_risk`: none|low|medium|high\n"
        "  - `none`: Change is self-contained, no downstream impact\n"
        "  - `low`: Minor return value change, callers unlikely affected\n"
        "  - `medium`: Return value changed in ways that may affect some callers\n"
        "  - `high`: Significant return value change that likely affects many callers\n"
        "- `return_value_change`: Description of what changed (if propagation_risk > none)\n"
        "\n"
        "High-risk examples:\n"
        "- Function now returns Promise<T> instead of T (callers need await)\n"
        "- Function returns larger data structure (memory/serialization overhead)\n"
        "- Return type changed (e.g., single item → array, primitive → object)\n"
        "- Async function became sync (or vice versa)\n"
        "\n"
        f"Repository context: {repo_context_hint}\n"
        "\n"
        "Input JSON (changed files):\n"
        f"{json.dumps({'files': changes}, ensure_ascii=False)}\n"
        "\n"
        "Output schema:\n"
        "{\n"
        '  "markers": [\n'
        "    {\n"
        '      "path": "src/file.ts",\n'
        '      "insert_after_new": "exact snippet after which to insert startSpan",\n'
        '      "occurrence_new": 1,\n'
        '      "insert_after_old": "exact snippet from old code (or null)",\n'
        '      "occurrence_old": 1,\n'
        '      "end_before_new": "exact snippet before which to insert endSpan",\n'
        '      "end_occurrence_new": 1,\n'
        '      "end_before_old": "exact snippet from old code (or null)",\n'
        '      "end_occurrence_old": 1,\n'
        '      "label": "short label",\n'
        '      "severity": "low|medium|high",\n'
        '      "reason": "why this change is perf sensitive",\n'
        '      "function_name": "name of the containing function/method",\n'
        '      "operation_type": "database|http|computation|file-io|cache|memory|network|other",\n'
        '      "attributes": {"key": "value", "...": "additional span attributes for filtering/grouping"},\n'
        '      "propagation_risk": "none|low|medium|high",\n'
        '      "return_value_change": "description of return value change (or null if propagation_risk is none)"\n'
        "    }\n"
        "  ]\n"
        "}\n"
    )

    client_kwargs: dict[str, Any] = {}
    timeout_env = os.getenv("OPENAI_TIMEOUT")
    try:
        client_kwargs["timeout"] = float(timeout_env) if timeout_env else 300.0
    except ValueError:
        client_kwargs["timeout"] = 300.0

    model = OpenAIModel(
        model_id=model_id,
        reasoning_effort="high",
        stop=REMOVE_PARAMETER,
        client_kwargs=client_kwargs or None,
    )
    agent = CodeAgent(tools=[], model=model)
    try:
        raw = agent.run(prompt)
    except Exception as e:
        message = str(e).lower()
        if "connection error" in message or "api connection error" in message or "timeout" in message:
            raise SmolagentsAnalysisError(
                "OpenAI connection error while analyzing changes. "
                "Check network connectivity and OPENAI_API_KEY or OPENAI_BASE_URL, then retry."
            ) from e
        raise SmolagentsAnalysisError(
            f"smolagents failed to analyze changes: {e}"
        ) from e

    # Handle different return types from agent.run()
    if isinstance(raw, dict):
        # Already a dict, use it directly
        data = raw
    elif isinstance(raw, str):
        # Try to parse as Python dict syntax first (single quotes, None, True, False, etc.)
        try:
            data = ast.literal_eval(raw)
            if not isinstance(data, dict):
                raise ValueError("Parsed value is not a dict")
        except (ValueError, SyntaxError):
            # Fallback: try parsing as JSON in case agent returned JSON anyway
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                raise SmolagentsAnalysisError(
                    f"smolagents did not return a valid Python dict. Output was:\n{raw}"
                ) from e
    else:
        raise SmolagentsAnalysisError(
            f"smolagents returned unexpected type {type(raw).__name__}. Output was:\n{raw}"
        )

    markers = data.get("markers")
    if not isinstance(markers, list):
        raise SmolagentsAnalysisError("Invalid output: missing 'markers' list")

    out: list[PerfMarkerPlan] = []
    for m in markers:
        if not isinstance(m, dict):
            continue
        # Backwards compatibility with older single-anchor schema.
        insert_after_new = str(m.get("insert_after_new") or m.get("insert_after") or "").strip()
        occurrence_new = int(m.get("occurrence_new") or m.get("occurrence") or 1)
        insert_after_old_val = m.get("insert_after_old")
        insert_after_old = None
        if isinstance(insert_after_old_val, str):
            insert_after_old = insert_after_old_val.strip() or None
        occurrence_old_val = m.get("occurrence_old")
        occurrence_old = int(occurrence_old_val) if isinstance(occurrence_old_val, int) else None

        # Parse end span anchors.
        end_before_new_val = m.get("end_before_new")
        end_before_new = None
        if isinstance(end_before_new_val, str):
            end_before_new = end_before_new_val.strip() or None
        end_occurrence_new = int(m.get("end_occurrence_new") or 1)

        end_before_old_val = m.get("end_before_old")
        end_before_old = None
        if isinstance(end_before_old_val, str):
            end_before_old = end_before_old_val.strip() or None
        end_occurrence_old_val = m.get("end_occurrence_old")
        end_occurrence_old = int(end_occurrence_old_val) if isinstance(end_occurrence_old_val, int) else None

        # Parse extended context fields.
        function_name_val = m.get("function_name")
        function_name = str(function_name_val).strip() if isinstance(function_name_val, str) else None

        operation_type_val = m.get("operation_type")
        operation_type = str(operation_type_val).strip() if isinstance(operation_type_val, str) else None

        attributes_val = m.get("attributes")
        attributes = None
        if isinstance(attributes_val, dict):
            # Ensure all keys and values are strings.
            attributes = {str(k): str(v) for k, v in attributes_val.items()}

        # Parse propagation risk fields.
        propagation_risk = str(m.get("propagation_risk", "none")).strip().lower()
        if propagation_risk not in ("none", "low", "medium", "high"):
            propagation_risk = "none"

        return_value_change_val = m.get("return_value_change")
        return_value_change = None
        if isinstance(return_value_change_val, str):
            return_value_change = return_value_change_val.strip() or None

        out.append(
            PerfMarkerPlan(
                path=str(m.get("path", "")).strip(),
                insert_after_new=insert_after_new,
                occurrence_new=occurrence_new,
                insert_after_old=insert_after_old,
                occurrence_old=occurrence_old,
                label=str(m.get("label", "")).strip() or "perf-marker",
                severity=str(m.get("severity", "medium")).strip() or "medium",
                reason=str(m.get("reason", "")).strip() or "",
                end_before_new=end_before_new,
                end_occurrence_new=end_occurrence_new,
                end_before_old=end_before_old,
                end_occurrence_old=end_occurrence_old,
                function_name=function_name,
                operation_type=operation_type,
                attributes=attributes,
                propagation_risk=propagation_risk,
                return_value_change=return_value_change,
            )
        )

    # Filter obviously broken suggestions.
    out = [p for p in out if p.path and p.insert_after_new]
    return out

