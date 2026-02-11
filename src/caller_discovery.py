"""
Grep-based caller discovery for return value propagation analysis.

This module provides utilities to find all call sites of a function
across a TypeScript/JavaScript codebase using ripgrep (with grep fallback).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class CallSite:
    """A location where a function is called."""

    file_path: str
    line_number: int
    line_content: str
    context_before: list[str] | None = None  # Lines before the match
    context_after: list[str] | None = None  # Lines after the match


@dataclass(frozen=True)
class CallerDiscoveryResult:
    """Result of caller discovery for a function."""

    function_name: str
    source_file: str
    call_sites: list[CallSite]
    tool_used: str  # "ripgrep" or "grep"
    error: str | None = None


def _find_ripgrep() -> str | None:
    """Find ripgrep executable."""
    return shutil.which("rg")


def _find_grep() -> str | None:
    """Find grep executable."""
    return shutil.which("grep")


def _parse_ripgrep_output(output: str, function_name: str) -> list[CallSite]:
    """
    Parse ripgrep output in the format: file:line:content
    """
    results: list[CallSite] = []
    if not output.strip():
        return results

    for line in output.strip().split("\n"):
        if not line:
            continue
        # Format: filepath:linenumber:content
        # Handle Windows paths (C:\...) by splitting from the right
        parts = line.split(":")
        if len(parts) >= 3:
            # Reconstruct filepath (may contain colons on Windows)
            # line_number is second-to-last before content
            try:
                line_num = int(parts[-2])
                content = parts[-1]
                file_path = ":".join(parts[:-2])
            except (ValueError, IndexError):
                # Fallback: assume standard format
                try:
                    file_path = parts[0]
                    line_num = int(parts[1])
                    content = ":".join(parts[2:])
                except (ValueError, IndexError):
                    continue

            results.append(
                CallSite(
                    file_path=file_path,
                    line_number=line_num,
                    line_content=content.strip(),
                )
            )

    return results


def _parse_grep_output(output: str, function_name: str) -> list[CallSite]:
    """
    Parse grep -n output in the format: file:line:content
    """
    # Same format as ripgrep
    return _parse_ripgrep_output(output, function_name)


def find_callers(
    function_name: str,
    search_dir: Path,
    *,
    source_file: str | None = None,
    extensions: Iterable[str] = (".ts", ".tsx", ".js", ".jsx"),
    exclude_patterns: Iterable[str] = ("node_modules", ".git", "dist", "build"),
    context_lines: int = 0,
) -> CallerDiscoveryResult:
    """
    Find all call sites of a function in a directory.

    Uses ripgrep if available, falls back to grep.

    Args:
        function_name: Name of the function to search for
        search_dir: Directory to search in
        source_file: Optional source file to exclude from results
        extensions: File extensions to search
        exclude_patterns: Directory patterns to exclude
        context_lines: Number of context lines before/after (0 = none)

    Returns:
        CallerDiscoveryResult with all found call sites
    """
    search_dir = Path(search_dir).resolve()
    if not search_dir.exists():
        return CallerDiscoveryResult(
            function_name=function_name,
            source_file=source_file or "",
            call_sites=[],
            tool_used="none",
            error=f"Search directory does not exist: {search_dir}",
        )

    # Build pattern: function_name followed by ( with optional whitespace
    # This catches: functionName(, functionName (, await functionName(
    pattern = rf"{re.escape(function_name)}\s*\("

    rg_path = _find_ripgrep()
    grep_path = _find_grep()

    call_sites: list[CallSite] = []
    tool_used = "none"
    error: str | None = None

    if rg_path:
        # Use ripgrep (faster, respects .gitignore)
        tool_used = "ripgrep"
        cmd = [
            rg_path,
            "--line-number",
            "--no-heading",
            "--with-filename",
        ]

        # Add file type filters
        for ext in extensions:
            ext_clean = ext.lstrip(".")
            cmd.extend(["--glob", f"*.{ext_clean}"])

        # Add exclusions
        for excl in exclude_patterns:
            cmd.extend(["--glob", f"!{excl}/**"])

        # Add context if requested
        if context_lines > 0:
            cmd.extend(["-C", str(context_lines)])

        cmd.extend([pattern, str(search_dir)])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
            )
            # ripgrep returns 1 if no matches, 0 if matches, 2+ on error
            if result.returncode <= 1:
                call_sites = _parse_ripgrep_output(result.stdout, function_name)
            else:
                error = f"ripgrep error: {result.stderr}"
        except subprocess.TimeoutExpired:
            error = "ripgrep timed out after 60 seconds"
        except Exception as e:
            error = f"ripgrep failed: {e}"

    elif grep_path:
        # Fallback to grep
        tool_used = "grep"

        # Build file pattern for grep
        ext_pattern = "|".join(ext.lstrip(".") for ext in extensions)
        include_args = []
        for ext in extensions:
            include_args.extend(["--include", f"*{ext}"])

        exclude_args = []
        for excl in exclude_patterns:
            exclude_args.extend(["--exclude-dir", excl])

        cmd = [
            grep_path,
            "-r",  # recursive
            "-n",  # line numbers
            "-E",  # extended regex
            *include_args,
            *exclude_args,
            pattern,
            str(search_dir),
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
            # grep returns 1 if no matches, 0 if matches
            if result.returncode <= 1:
                call_sites = _parse_grep_output(result.stdout, function_name)
            else:
                error = f"grep error: {result.stderr}"
        except subprocess.TimeoutExpired:
            error = "grep timed out after 120 seconds"
        except Exception as e:
            error = f"grep failed: {e}"

    else:
        error = "Neither ripgrep (rg) nor grep found in PATH"

    # Filter out the source file if specified
    if source_file:
        source_file_normalized = source_file.replace("\\", "/")
        call_sites = [
            cs
            for cs in call_sites
            if not cs.file_path.replace("\\", "/").endswith(source_file_normalized)
        ]

    return CallerDiscoveryResult(
        function_name=function_name,
        source_file=source_file or "",
        call_sites=call_sites,
        tool_used=tool_used,
        error=error,
    )


def find_callers_for_plans(
    plans: list,  # list[PerfMarkerPlan] - avoid circular import
    search_dir: Path,
    *,
    min_propagation_risk: str = "medium",
) -> dict[str, CallerDiscoveryResult]:
    """
    Find callers for all plans with propagation risk >= min_propagation_risk.

    Args:
        plans: List of PerfMarkerPlan objects
        search_dir: Directory to search for callers
        min_propagation_risk: Minimum risk level to trigger caller discovery

    Returns:
        Dict mapping function_name to CallerDiscoveryResult
    """
    risk_levels = {"none": 0, "low": 1, "medium": 2, "high": 3}
    min_level = risk_levels.get(min_propagation_risk, 2)

    results: dict[str, CallerDiscoveryResult] = {}

    for plan in plans:
        # Check propagation risk
        plan_risk = getattr(plan, "propagation_risk", "none")
        if risk_levels.get(plan_risk, 0) < min_level:
            continue

        # Get function name
        func_name = getattr(plan, "function_name", None)
        if not func_name:
            # Try to extract from label
            func_name = getattr(plan, "label", None)
        if not func_name:
            continue

        # Skip if already discovered
        if func_name in results:
            continue

        # Find callers
        result = find_callers(
            function_name=func_name,
            search_dir=search_dir,
            source_file=getattr(plan, "path", None),
        )
        results[func_name] = result

    return results


def format_caller_report(results: dict[str, CallerDiscoveryResult]) -> str:
    """
    Format caller discovery results as a human-readable report.
    """
    if not results:
        return "No functions with propagation risk found.\n"

    lines = [
        "=" * 60,
        "CALLER DISCOVERY REPORT",
        "=" * 60,
        "",
    ]

    total_callers = sum(len(r.call_sites) for r in results.values())
    lines.append(f"Found {total_callers} call sites across {len(results)} function(s):")
    lines.append("")

    for func_name, result in results.items():
        lines.append(f"📍 {func_name}()")
        if result.error:
            lines.append(f"   ⚠️  Error: {result.error}")
        elif not result.call_sites:
            lines.append("   No call sites found (may be exported/external)")
        else:
            lines.append(f"   Found {len(result.call_sites)} call site(s):")
            for cs in result.call_sites[:10]:  # Limit to first 10
                lines.append(f"     {cs.file_path}:{cs.line_number}")
                lines.append(f"       {cs.line_content[:80]}...")
            if len(result.call_sites) > 10:
                lines.append(f"     ... and {len(result.call_sites) - 10} more")
        lines.append("")

    return "\n".join(lines)

