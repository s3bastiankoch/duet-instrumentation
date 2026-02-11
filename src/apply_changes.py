from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .mock_git import MockGitChanges


class ApplyChangesError(RuntimeError):
    """Raised when applying changes fails."""


@dataclass(frozen=True)
class PreparedVersions:
    """Paths to the prepared base and head versions."""
    base_dir: Path
    head_dir: Path
    changes_applied: int
    changes_skipped: int


def _parse_unified_diff(diff_text: str) -> list[tuple[int, int, list[str], list[str]]]:
    """
    Parse a unified diff into a list of hunks.
    Each hunk is (old_start, old_count, old_lines, new_lines).
    """
    hunks: list[tuple[int, int, list[str], list[str]]] = []
    
    lines = diff_text.split("\n")
    i = 0
    
    while i < len(lines):
        line = lines[i]
        
        # Look for hunk header: @@ -old_start,old_count +new_start,new_count @@
        hunk_match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if hunk_match:
            old_start = int(hunk_match.group(1))
            old_count = int(hunk_match.group(2)) if hunk_match.group(2) else 1
            
            old_lines: list[str] = []
            new_lines: list[str] = []
            
            i += 1
            while i < len(lines):
                if lines[i].startswith("@@") or lines[i].startswith("diff ") or lines[i].startswith("---") or lines[i].startswith("+++"):
                    break
                
                if lines[i].startswith("-"):
                    old_lines.append(lines[i][1:])
                elif lines[i].startswith("+"):
                    new_lines.append(lines[i][1:])
                elif lines[i].startswith(" "):
                    old_lines.append(lines[i][1:])
                    new_lines.append(lines[i][1:])
                elif lines[i] == "":
                    # Empty line in diff context
                    pass
                else:
                    # Could be context line without space prefix
                    old_lines.append(lines[i])
                    new_lines.append(lines[i])
                
                i += 1
            
            hunks.append((old_start, old_count, old_lines, new_lines))
        else:
            i += 1
    
    return hunks


def _apply_hunks(original: str, hunks: list[tuple[int, int, list[str], list[str]]]) -> str:
    """
    Apply parsed hunks to the original content.
    Uses fuzzy matching to find the best location for each hunk.
    """
    if not hunks:
        return original
    
    lines = original.split("\n")
    
    # Sort hunks by original line number, in reverse to apply from bottom to top
    sorted_hunks = sorted(hunks, key=lambda h: h[0], reverse=True)
    
    for old_start, old_count, old_lines, new_lines in sorted_hunks:
        # Try to find the matching location
        best_match = -1
        best_score = 0
        
        # Search around the expected location
        search_start = max(0, old_start - 50)
        search_end = min(len(lines), old_start + 50)
        
        for start in range(search_start, search_end):
            if start + len(old_lines) > len(lines):
                continue
            
            # Calculate match score
            score = 0
            for j, old_line in enumerate(old_lines):
                if start + j < len(lines) and lines[start + j].strip() == old_line.strip():
                    score += 1
            
            if score > best_score:
                best_score = score
                best_match = start
        
        # Apply the hunk if we found a reasonable match
        if best_match >= 0 and best_score >= len(old_lines) * 0.5:
            # Replace old_lines with new_lines
            lines = lines[:best_match] + new_lines + lines[best_match + len(old_lines):]
    
    return "\n".join(lines)


def apply_diff_to_file(original_content: str, diff_unified: str) -> str:
    """
    Apply a unified diff to file content.
    Returns the modified content.
    """
    hunks = _parse_unified_diff(diff_unified)
    return _apply_hunks(original_content, hunks)


def prepare_versions(
    *,
    app_folder: Path,
    changes: "MockGitChanges",
    working_dir: Path,
    clean: bool = True,
) -> PreparedVersions:
    """
    Prepare base and head versions of an application.
    
    Args:
        app_folder: Path to the source application (becomes base version)
        changes: MockGitChanges with file changes and diffs
        working_dir: Where to write the versions (.otel-agent/)
        clean: If True, remove existing working_dir before writing
    
    Returns:
        PreparedVersions with paths to base_dir and head_dir
    """
    if not app_folder.exists():
        raise ApplyChangesError(f"Application folder not found: {app_folder}")
    if not app_folder.is_dir():
        raise ApplyChangesError(f"Not a directory: {app_folder}")
    
    base_dir = working_dir / "base"
    head_dir = working_dir / "head"
    
    # Clean up existing directories if requested
    if clean:
        if base_dir.exists():
            shutil.rmtree(base_dir)
        if head_dir.exists():
            shutil.rmtree(head_dir)
    
    # Create base version: copy the entire app folder
    def _ignore_patterns(directory: str, files: list[str]) -> list[str]:
        """Ignore node_modules and other large/generated directories."""
        ignored = []
        for f in files:
            if f in ("node_modules", ".git", "__pycache__", ".next", "dist", "build"):
                ignored.append(f)
        return ignored
    
    shutil.copytree(app_folder, base_dir, ignore=_ignore_patterns)
    
    # Create head version: start with a copy, then apply changes
    shutil.copytree(app_folder, head_dir, ignore=_ignore_patterns)
    
    changes_applied = 0
    changes_skipped = 0
    
    for file_change in changes.files:
        # Determine the relative path within the app
        # The path in changes might be absolute or relative to some root
        rel_path = file_change.path
        
        # Try to strip any common prefix with the app_folder
        # This handles cases like "examples/movie/src/app.ts" when app_folder is "examples/movie"
        app_folder_str = app_folder.as_posix()
        if rel_path.startswith(app_folder_str):
            rel_path = rel_path[len(app_folder_str):].lstrip("/")
        
        # Also try matching just the relative part after a known prefix
        parts = rel_path.split("/")
        for i in range(len(parts)):
            candidate = "/".join(parts[i:])
            if (head_dir / candidate).exists() or (base_dir / candidate).exists():
                rel_path = candidate
                break
        
        target_file = head_dir / rel_path
        
        if file_change.change_type == "deleted":
            if target_file.exists():
                target_file.unlink()
                changes_applied += 1
            else:
                changes_skipped += 1
            continue
        
        if file_change.change_type == "added":
            # For added files, we need the full new content
            # If we have diff_unified, try to extract it
            if file_change.diff_unified:
                # Extract just the added lines from the diff
                new_content_lines: list[str] = []
                for line in file_change.diff_unified.split("\n"):
                    if line.startswith("+") and not line.startswith("+++"):
                        new_content_lines.append(line[1:])
                
                target_file.parent.mkdir(parents=True, exist_ok=True)
                target_file.write_text("\n".join(new_content_lines), encoding="utf-8")
                changes_applied += 1
            else:
                changes_skipped += 1
            continue
        
        if file_change.change_type == "modified":
            if not target_file.exists():
                changes_skipped += 1
                continue
            
            if not file_change.diff_unified:
                changes_skipped += 1
                continue
            
            try:
                original = target_file.read_text(encoding="utf-8")
                modified = apply_diff_to_file(original, file_change.diff_unified)
                target_file.write_text(modified, encoding="utf-8")
                changes_applied += 1
            except Exception:
                changes_skipped += 1
            continue
        
        # Unknown change type
        changes_skipped += 1
    
    return PreparedVersions(
        base_dir=base_dir,
        head_dir=head_dir,
        changes_applied=changes_applied,
        changes_skipped=changes_skipped,
    )

