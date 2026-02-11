from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from src.perf_instrument_pipeline import (
    LintResult,
    lint_typescript_directory,
    lint_instrumented_versions,
)


@pytest.fixture
def bun_available() -> bool:
    """Check if bun is available in PATH."""
    return shutil.which("bun") is not None


def test_lint_result_dataclass() -> None:
    """Test LintResult dataclass creation."""
    result = LintResult(
        success=True,
        directory=Path("/test"),
        error_count=0,
        errors=[],
        command="bun tsc",
    )
    assert result.success is True
    assert result.error_count == 0


def test_lint_missing_directory(tmp_path: Path) -> None:
    """Test linting a non-existent directory."""
    missing_dir = tmp_path / "nonexistent"
    result = lint_typescript_directory(missing_dir)
    
    assert result.success is False
    assert result.error_count == 1
    assert "Directory not found" in result.errors[0]


def test_lint_empty_directory(tmp_path: Path) -> None:
    """Test linting an empty directory (no TypeScript files)."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    
    result = lint_typescript_directory(empty_dir)
    
    # Should succeed because there's nothing to lint
    assert result.success is True
    assert result.error_count == 0
    assert "no TypeScript files" in result.command


def test_lint_valid_typescript(tmp_path: Path, bun_available: bool) -> None:
    """Test linting valid TypeScript code."""
    if not bun_available:
        pytest.skip("bun not available")
    
    ts_dir = tmp_path / "valid_ts"
    ts_dir.mkdir()
    
    # Write valid TypeScript
    (ts_dir / "app.ts").write_text(
        'export const x: number = 1;\nconsole.log(x);\n',
        encoding="utf-8",
    )
    
    result = lint_typescript_directory(ts_dir)
    
    assert result.success is True
    assert result.error_count == 0


def test_lint_invalid_typescript(tmp_path: Path, bun_available: bool) -> None:
    """Test linting invalid TypeScript code."""
    if not bun_available:
        pytest.skip("bun not available")
    
    ts_dir = tmp_path / "invalid_ts"
    ts_dir.mkdir()
    
    # Write TypeScript with syntax error
    (ts_dir / "broken.ts").write_text(
        'const x: number = "not a number";\n'  # Type error
        'x.nonexistent();\n',  # Method doesn't exist
        encoding="utf-8",
    )
    
    result = lint_typescript_directory(ts_dir)
    
    assert result.success is False
    assert result.error_count > 0


def test_lint_instrumented_versions_both_pass(tmp_path: Path, bun_available: bool) -> None:
    """Test linting both versions when they pass."""
    if not bun_available:
        pytest.skip("bun not available")
    
    base_dir = tmp_path / "base"
    head_dir = tmp_path / "head"
    base_dir.mkdir(parents=True)
    head_dir.mkdir(parents=True)
    
    # Write valid TypeScript to both
    (base_dir / "app.ts").write_text('export const x = 1;\n', encoding="utf-8")
    (head_dir / "app.ts").write_text('export const x = 2;\n', encoding="utf-8")
    
    base_result, head_result = lint_instrumented_versions(base_dir, head_dir)
    
    assert base_result.success is True
    assert head_result.success is True


def test_lint_instrumented_versions_one_fails(tmp_path: Path, bun_available: bool) -> None:
    """Test linting when one version has errors."""
    if not bun_available:
        pytest.skip("bun not available")
    
    base_dir = tmp_path / "base"
    head_dir = tmp_path / "head"
    base_dir.mkdir(parents=True)
    head_dir.mkdir(parents=True)
    
    # Base version is valid
    (base_dir / "app.ts").write_text('export const x = 1;\n', encoding="utf-8")
    
    # Head version has type error
    (head_dir / "broken.ts").write_text(
        'const x: number = "string";\n',
        encoding="utf-8",
    )
    
    base_result, head_result = lint_instrumented_versions(base_dir, head_dir)
    
    assert base_result.success is True
    assert head_result.success is False


def test_lint_bun_not_found(tmp_path: Path, monkeypatch) -> None:
    """Test behavior when bun is not in PATH."""
    monkeypatch.setattr("shutil.which", lambda x: None)
    
    ts_dir = tmp_path / "test"
    ts_dir.mkdir()
    (ts_dir / "app.ts").write_text('export const x = 1;\n', encoding="utf-8")
    
    # Need to reimport to get the patched version
    from src.perf_instrument_pipeline import lint_typescript_directory as lint_fn
    result = lint_fn(ts_dir)
    
    assert result.success is False
    assert "bun executable not found" in result.errors[0]

