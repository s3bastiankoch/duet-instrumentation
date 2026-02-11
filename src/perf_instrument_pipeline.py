from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .git_changes import ChangeSet, FileChange, GitChangesError, open_repo, read_file_at_ref
from .smolagents_analysis import PerfMarkerPlan, SmolagentsAnalysisError, analyze_changes_for_perf_markers
from .ts_perf_instrument import InsertPlan, InstrumentationError, apply_perf_markers


class PerfPipelineError(RuntimeError):
    """Raised when performance instrumentation pipeline fails."""


@dataclass(frozen=True)
class LintResult:
    """Result of linting a TypeScript directory."""
    success: bool
    directory: Path
    error_count: int
    errors: list[str]
    command: str


@dataclass(frozen=True)
class SyntaxCheckResult:
    """Result of running a bun syntax check on a file."""
    success: bool
    available: bool
    errors: list[str]
    command: str


def _find_bun() -> str | None:
    """Find bun executable in PATH."""
    return shutil.which("bun")


def _find_package_root(start_path: Path) -> Path | None:
    """Find nearest package.json directory starting at start_path."""
    current = start_path
    while True:
        if (current / "package.json").exists():
            return current
        if current.parent == current:
            return None
        current = current.parent


def _bun_syntax_check(file_path: Path) -> SyntaxCheckResult:
    """Run a bun syntax check on a single file."""
    bun_path = _find_bun()
    if bun_path is None:
        return SyntaxCheckResult(
            success=True,
            available=False,
            errors=[],
            command="",
        )
    if not file_path.exists():
        return SyntaxCheckResult(
            success=False,
            available=True,
            errors=[f"File not found: {file_path}"],
            command="",
        )

    install_dir = _find_package_root(file_path.parent) or file_path.parent
    install_cmd = [bun_path, "install"]
    install_cmd_str = " ".join(install_cmd)
    try:
        install_result = subprocess.run(
            install_cmd,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=install_dir,
        )
    except subprocess.TimeoutExpired:
        return SyntaxCheckResult(
            success=False,
            available=True,
            errors=["bun install timed out after 120 seconds"],
            command=install_cmd_str,
        )
    except Exception as e:
        return SyntaxCheckResult(
            success=False,
            available=True,
            errors=[f"Failed to run bun install: {e}"],
            command=install_cmd_str,
        )
    if install_result.returncode != 0:
        output = (install_result.stdout + "\n" + install_result.stderr).strip()
        error_lines = [line for line in output.split("\n") if line.strip()]
        return SyntaxCheckResult(
            success=False,
            available=True,
            errors=error_lines[:50],
            command=install_cmd_str,
        )

    cmd = [bun_path, "--check", str(file_path)]
    cmd_str = " ".join(cmd)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return SyntaxCheckResult(
            success=False,
            available=True,
            errors=["bun syntax check timed out after 30 seconds"],
            command=cmd_str,
        )
    except Exception as e:
        return SyntaxCheckResult(
            success=False,
            available=True,
            errors=[f"Failed to run bun syntax check: {e}"],
            command=cmd_str,
        )

    if result.returncode == 0:
        return SyntaxCheckResult(
            success=True,
            available=True,
            errors=[],
            command=cmd_str,
        )

    output = (result.stdout + "\n" + result.stderr).strip()
    error_lines = [line for line in output.split("\n") if line.strip()]
    return SyntaxCheckResult(
        success=False,
        available=True,
        errors=error_lines[:50],
        command=cmd_str,
    )


def _syntax_check_content(content: str, *, target_path: Path) -> SyntaxCheckResult:
    """Run bun syntax check against content without mutating the target file."""
    suffix = target_path.suffix if target_path.suffix else ".ts"
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=suffix,
        dir=target_path.parent,
        delete=False,
        encoding="utf-8",
    ) as tmp:
        tmp.write(content)
        temp_path = Path(tmp.name)
    try:
        return _bun_syntax_check(temp_path)
    finally:
        try:
            temp_path.unlink()
        except Exception:
            pass


def _create_temp_tsconfig(directory: Path) -> Path:
    """Create a temporary tsconfig.json for type-checking instrumented files."""
    tsconfig = {
        "compilerOptions": {
            "target": "ES2022",
            "module": "NodeNext",
            "moduleResolution": "NodeNext",
            "strict": True,
            "esModuleInterop": True,
            "skipLibCheck": True,
            "noEmit": True,
            "allowImportingTsExtensions": True,
            # Be lenient for instrumented code - allow unused parameters, etc.
            "noUnusedLocals": False,
            "noUnusedParameters": False,
        },
        "include": ["**/*.ts", "**/*.tsx"],
        "exclude": ["node_modules"],
    }
    tsconfig_path = directory / "tsconfig.lint.json"
    tsconfig_path.write_text(json.dumps(tsconfig, indent=2), encoding="utf-8")
    return tsconfig_path


def lint_typescript_directory(directory: Path, *, use_existing_tsconfig: bool = False) -> LintResult:
    """
    Lint TypeScript files in a directory using bun.
    
    Uses `bun --bun tsc --noEmit` to type-check without emitting output.
    
    Args:
        directory: Path to directory containing TypeScript files
        use_existing_tsconfig: If True, use existing tsconfig.json; otherwise create temporary one
    
    Returns:
        LintResult with success status and any errors found
    """
    bun_path = _find_bun()
    if bun_path is None:
        return LintResult(
            success=False,
            directory=directory,
            error_count=1,
            errors=["bun executable not found in PATH"],
            command="",
        )
    
    if not directory.exists():
        return LintResult(
            success=False,
            directory=directory,
            error_count=1,
            errors=[f"Directory not found: {directory}"],
            command="",
        )
    
    # Check if there are any TypeScript files to lint
    ts_files = list(directory.rglob("*.ts")) + list(directory.rglob("*.tsx"))
    if not ts_files:
        return LintResult(
            success=True,
            directory=directory,
            error_count=0,
            errors=[],
            command="(no TypeScript files to lint)",
        )
    
    # Determine which tsconfig to use
    existing_tsconfig = directory / "tsconfig.json"
    temp_tsconfig: Path | None = None
    
    if use_existing_tsconfig and existing_tsconfig.exists():
        tsconfig_path = existing_tsconfig
    else:
        temp_tsconfig = _create_temp_tsconfig(directory)
        tsconfig_path = temp_tsconfig
    
    try:
        # Run bun x tsc to type-check (auto-installs tsc if needed)
        cmd = [bun_path, "x", "tsc", "--project", str(tsconfig_path), "--noEmit"]
        cmd_str = " ".join(cmd)
        
        result = subprocess.run(
            cmd,
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=120,  # 2 minute timeout
        )
        
        if result.returncode == 0:
            return LintResult(
                success=True,
                directory=directory,
                error_count=0,
                errors=[],
                command=cmd_str,
            )
        
        # Parse errors from stderr/stdout
        output = (result.stdout + "\n" + result.stderr).strip()
        error_lines = [line for line in output.split("\n") if line.strip()]
        
        # Count actual errors (lines that look like TypeScript errors)
        error_count = sum(1 for line in error_lines if ": error TS" in line)
        if error_count == 0:
            error_count = len(error_lines)
        
        return LintResult(
            success=False,
            directory=directory,
            error_count=error_count,
            errors=error_lines[:50],  # Limit to first 50 errors
            command=cmd_str,
        )
        
    except subprocess.TimeoutExpired:
        return LintResult(
            success=False,
            directory=directory,
            error_count=1,
            errors=["TypeScript compilation timed out after 120 seconds"],
            command=cmd_str,
        )
    except Exception as e:
        return LintResult(
            success=False,
            directory=directory,
            error_count=1,
            errors=[f"Failed to run TypeScript linting: {e}"],
            command="",
        )
    finally:
        # Clean up temp tsconfig if we created one
        if temp_tsconfig is not None and temp_tsconfig.exists():
            temp_tsconfig.unlink()


def lint_instrumented_versions(
    base_dir: Path,
    head_dir: Path,
) -> tuple[LintResult, LintResult]:
    """
    Lint both base and head directories after instrumentation.
    
    Returns:
        Tuple of (base_result, head_result)
    """
    base_result = lint_typescript_directory(base_dir)
    head_result = lint_typescript_directory(head_dir)
    
    return base_result, head_result

try:
    from diskcache import Cache
except Exception:  # pragma: no cover
    Cache = None


_SMOL_CACHE_SCHEMA_VERSION = "v2"


def _render_measure_template(template: str, *, service_version: str) -> str:
    """
    Replace placeholders in `measure.template.ts` with safe defaults.
    """
    out = template
    out = out.replace("$SERVICE_VERSION", service_version)
    out = out.replace("$EMPIRIS_API_URL", "")
    out = out.replace("$EMPIRIS_API_KEY", "")
    # Keep the template offline by default.
    out = out.replace("$SEND_TO_EMPIRIS", "false")
    return out


def _smol_cache_key(
    *,
    repo_source: str,
    base_ref: str,
    head_ref: str,
    model_id: str,
    changeset: ChangeSet,
    sensitivity: str,
) -> str:
    # Avoid including any secrets (tokens are never part of repo_source).
    file_fingerprint = "|".join(sorted(f"{c.change_type}:{c.path}" for c in changeset.files))
    raw = f"{_SMOL_CACHE_SCHEMA_VERSION}|{repo_source}|{base_ref}|{head_ref}|{model_id}|{sensitivity}|{file_fingerprint}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _cache_dir_from_repo_cache_dir(repo_cache_dir: Path) -> Path:
    # repo_cache_dir typically looks like: <base>/.otel-agent/repos
    # We store LLM cache alongside: <base>/.otel-agent/smolagents_cache
    return repo_cache_dir.parent / "smolagents_cache"


def plan_perf_markers_with_smolagents(
    *,
    model_id: str,
    changeset: ChangeSet,
    repo_context_hint: str,
    repo_source: str | None,
    base_ref: str,
    head_ref: str,
    cache_dir: Path,
    token_env: str | None,
    sensitivity: str = "medium",
    base_dir: Path | None = None,
    head_dir: Path | None = None,
    skip_cache: bool = False,
    chunk_size: int | None = None,
    max_workers: int = 4,
    on_chunk_complete: "Callable[[int, int, list[PerfMarkerPlan]], None] | None" = None,
) -> list[PerfMarkerPlan]:
    """
    Analyze changes and plan performance markers using smolagents.
    
    Can work in two modes:
    1. Git mode: Pass repo_source to read files from git refs
    2. Directory mode: Pass base_dir and head_dir to read from pre-extracted directories
    
    Args:
        chunk_size: If set, process files in chunks of this size. Default 1 (per-file).
        max_workers: Number of parallel workers for analysis. Default 4.
        on_chunk_complete: Optional callback called after each chunk with (chunk_index, total_chunks, plans).
    """
    # Use a stable cache key based on available info
    cache_key = _smol_cache_key(
        repo_source=repo_source or str(head_dir or base_dir or "local"),
        base_ref=base_ref,
        head_ref=head_ref,
        model_id=model_id,
        changeset=changeset,
        sensitivity=str(sensitivity or "medium").strip().lower() or "medium",
    )
    if Cache is not None and not skip_cache:
        llm_cache_dir = _cache_dir_from_repo_cache_dir(cache_dir)
        llm_cache_dir.mkdir(parents=True, exist_ok=True)
        with Cache(llm_cache_dir.as_posix()) as cache:
            cached = cache.get(cache_key)
            if isinstance(cached, list):
                try:
                    return [PerfMarkerPlan(**p) for p in cached if isinstance(p, dict)]
                except Exception:
                    # Cache content incompatible; ignore and recompute.
                    pass

    # Determine how to read files: from git repo or from directories
    use_git = repo_source is not None and base_dir is None and head_dir is None
    repo = None
    
    if use_git:
        try:
            repo, _repo_path = open_repo(repo_source=repo_source, cache_dir=cache_dir, token_env=token_env)
        except GitChangesError as e:
            raise PerfPipelineError(str(e)) from e

    def _read_file_from_dir(dir_path: Path | None, rel_path: str) -> str | None:
        """Read a file from a directory, trying various path resolutions."""
        if dir_path is None:
            return None
        # Try direct path
        target = dir_path / rel_path
        if target.exists():
            return target.read_text(encoding="utf-8")
        # Try stripping common prefixes
        parts = rel_path.split("/")
        for i in range(len(parts)):
            candidate = "/".join(parts[i:])
            target = dir_path / candidate
            if target.exists():
                return target.read_text(encoding="utf-8")
        return None

    # Keep prompts bounded.
    def _clip(s: str, n: int = 8000) -> str:
        if len(s) <= n:
            return s
        return s[: n] + "\n/* ... truncated ... */\n"

    def _enrich_file(f: "FileChange") -> dict[str, Any]:
        """Enrich a single file change with excerpts."""
        item: dict[str, Any] = {
            "path": f.path,
            "change_type": f.change_type,
            "summary": f.summary,
            "diff_unified": f.diff_unified,
            "old_path": f.old_path,
        }

        if f.path.endswith(".ts") or f.path.endswith(".tsx"):
            if use_git and repo is not None:
                try:
                    item["new_excerpt"] = _clip(read_file_at_ref(repo, head_ref, f.path))
                except Exception:
                    item["new_excerpt"] = None
                try:
                    item["old_excerpt"] = _clip(read_file_at_ref(repo, base_ref, f.path))
                except Exception:
                    item["old_excerpt"] = None
            else:
                # Read from pre-extracted directories
                new_content = _read_file_from_dir(head_dir, f.path)
                item["new_excerpt"] = _clip(new_content) if new_content else None
                old_content = _read_file_from_dir(base_dir, f.path)
                item["old_excerpt"] = _clip(old_content) if old_content else None

        return item

    # Only consider TypeScript files for chunking - non-TS files won't produce markers
    ts_files = [f for f in changeset.files if f.path.endswith(".ts") or f.path.endswith(".tsx")]
    other_files = [f for f in changeset.files if not (f.path.endswith(".ts") or f.path.endswith(".tsx"))]

    # Determine chunking strategy
    effective_chunk_size = chunk_size if chunk_size and chunk_size > 0 else len(ts_files)
    if effective_chunk_size <= 0:
        effective_chunk_size = max(1, len(ts_files))
    
    # Create chunks of TypeScript files
    chunks: list[list["FileChange"]] = []
    for i in range(0, len(ts_files), effective_chunk_size):
        chunks.append(ts_files[i : i + effective_chunk_size])
    
    # If no TS files, nothing to analyze
    if not chunks:
        return []

    total_chunks = len(chunks)
    
    # Thread-safe counter for progress reporting
    completed_count = 0
    completed_lock = threading.Lock()
    
    def _analyze_chunk(chunk_idx: int, chunk_files: list[FileChange]) -> tuple[int, list[PerfMarkerPlan]]:
        """Analyze a single chunk and return (chunk_idx, plans)."""
        nonlocal completed_count
        
        # Enrich files in this chunk
        enriched = [_enrich_file(f) for f in chunk_files]
        
        # Include context hint about chunk
        chunk_hint = repo_context_hint
        if total_chunks > 1:
            chunk_hint = f"{repo_context_hint} [File {chunk_idx + 1}/{total_chunks}]"
        
        # Analyze this chunk
        chunk_plans = analyze_changes_for_perf_markers(
            model_id=model_id,
            changes=enriched,
            repo_context_hint=chunk_hint,
            sensitivity=sensitivity,
        )
        
        # Thread-safe progress update
        with completed_lock:
            completed_count += 1
            current_completed = completed_count
        
        # Notify caller of progress (callback should be thread-safe)
        if on_chunk_complete is not None:
            on_chunk_complete(current_completed, total_chunks, chunk_plans)
        
        return chunk_idx, chunk_plans
    
    try:
        all_plans: list[PerfMarkerPlan] = []
        
        # Use parallel processing if max_workers > 1 and there are multiple chunks
        effective_workers = min(max_workers, total_chunks) if max_workers > 1 else 1
        
        if effective_workers > 1:
            # Parallel processing
            results: dict[int, list[PerfMarkerPlan]] = {}
            
            with ThreadPoolExecutor(max_workers=effective_workers) as executor:
                futures = {
                    executor.submit(_analyze_chunk, idx, chunk_files): idx
                    for idx, chunk_files in enumerate(chunks)
                }
                
                for future in as_completed(futures):
                    try:
                        chunk_idx, chunk_plans = future.result()
                        results[chunk_idx] = chunk_plans
                    except SmolagentsAnalysisError as e:
                        raise PerfPipelineError(str(e)) from e
            
            # Combine results in original order
            for idx in range(total_chunks):
                all_plans.extend(results.get(idx, []))
        else:
            # Sequential processing (max_workers=1 or single chunk)
            for chunk_idx, chunk_files in enumerate(chunks):
                _, chunk_plans = _analyze_chunk(chunk_idx, chunk_files)
                all_plans.extend(chunk_plans)
        
        # Cache all plans together
        if Cache is not None:
            llm_cache_dir = _cache_dir_from_repo_cache_dir(cache_dir)
            llm_cache_dir.mkdir(parents=True, exist_ok=True)
            with Cache(llm_cache_dir.as_posix()) as cache:
                cache.set(cache_key, [p.__dict__ for p in all_plans])
        
        return all_plans
    except SmolagentsAnalysisError as e:
        raise PerfPipelineError(str(e)) from e


def _inject_measure_import(root_dir: Path, entrypoint: str) -> bool:
    """
    Inject `import "./measure";` at the top of the entrypoint file.
    
    Args:
        root_dir: Root directory of the app (e.g., base_dir or head_dir)
        entrypoint: Relative path to the entrypoint file (e.g., "src/app.ts")
    
    Returns:
        True if import was added, False otherwise
    """
    entrypoint_path = root_dir / entrypoint
    
    if not entrypoint_path.exists():
        return False
    
    content = entrypoint_path.read_text(encoding="utf-8")
    
    # Determine the relative import path from entrypoint to measure.ts
    # measure.ts is always placed in src/, so calculate the relative path
    entrypoint_dir = entrypoint_path.parent
    measure_path = root_dir / "src" / "measure.ts"
    
    try:
        rel_path = measure_path.relative_to(entrypoint_dir)
        # Convert to import path (use ./ prefix if in same directory)
        import_path = "./" + str(rel_path).replace("\\", "/").replace(".ts", "")
    except ValueError:
        # If measure.ts is not relative to entrypoint dir, use absolute-ish path
        import_path = "./measure"
    
    measure_import = f'import "{import_path}"; // Initialize global startSpan/endSpan functions'
    
    # Check if already imported
    if 'import "./measure"' in content or "import './measure'" in content or f'import "{import_path}"' in content:
        return False
    
    # Add import at the very top of the file
    new_content = measure_import + "\n" + content
    entrypoint_path.write_text(new_content, encoding="utf-8")
    return True


def _inject_express_tracing_middleware(root_dir: Path, entrypoint: str) -> bool:
    """
    Inject `app.use(withTracing)` for Express applications.
    
    This function:
    1. Detects if the entrypoint uses Express
    2. Adds import for withTracing from measure.ts
    3. Adds app.use(withTracing) after the Express app is created
    
    Args:
        root_dir: Root directory of the app (e.g., base_dir or head_dir)
        entrypoint: Relative path to the entrypoint file (e.g., "src/app.ts")
    
    Returns:
        True if middleware was added, False otherwise
    """
    import re
    
    entrypoint_path = root_dir / entrypoint
    
    if not entrypoint_path.exists():
        return False
    
    content = entrypoint_path.read_text(encoding="utf-8")
    
    # Check if this is an Express application
    if 'from "express"' not in content and "from 'express'" not in content:
        return False
    
    # Check if withTracing middleware is already added
    if "app.use(withTracing)" in content:
        return False
    
    # Determine the relative import path from entrypoint to measure.ts
    entrypoint_dir = entrypoint_path.parent
    measure_path = root_dir / "src" / "measure.ts"
    
    try:
        rel_path = measure_path.relative_to(entrypoint_dir)
        import_path = "./" + str(rel_path).replace("\\", "/").replace(".ts", "")
    except ValueError:
        import_path = "./measure"
    
    modified = False
    
    # Step 1: Add or update import to include withTracing
    # Check if there's already an import from measure.ts that we can extend
    measure_import_pattern = re.compile(
        rf'import\s+{{([^}}]+)}}\s+from\s+["\']\.\/measure["\'];?'
    )
    measure_import_match = measure_import_pattern.search(content)
    
    if measure_import_match:
        # Extend existing import if withTracing is not there
        existing_imports = measure_import_match.group(1)
        if "withTracing" not in existing_imports:
            new_imports = existing_imports.strip() + ", withTracing"
            content = content[:measure_import_match.start(1)] + new_imports + content[measure_import_match.end(1):]
            modified = True
    else:
        # Check for side-effect only import and convert it
        side_effect_pattern = re.compile(
            rf'import\s+["\']\.\/measure["\'];?\s*//[^\n]*'
        )
        side_effect_match = side_effect_pattern.search(content)
        
        if side_effect_match:
            # Replace with named import
            new_import = f'import {{ withTracing }} from "{import_path}"; // Initialize global startSpan/endSpan functions and Express tracing'
            content = content[:side_effect_match.start()] + new_import + content[side_effect_match.end():]
            modified = True
        else:
            # No measure import found, add new one at top
            new_import = f'import {{ withTracing }} from "{import_path}"; // Express tracing middleware\n'
            content = new_import + content
            modified = True
    
    # Step 2: Find where to inject app.use(withTracing)
    # Look for patterns like: const app = express() or let app = express()
    # We need to add app.use(withTracing) right after app is created but before other middleware
    
    # Pattern to match Express app creation
    app_creation_pattern = re.compile(
        r'((?:const|let|var)\s+(\w+)\s*=\s*express\s*\(\s*\)\s*;?)',
        re.MULTILINE
    )
    
    app_match = app_creation_pattern.search(content)
    
    if app_match:
        app_var_name = app_match.group(2)  # The variable name (usually "app")
        insert_pos = app_match.end()
        
        # Find the indentation of the app creation line
        line_start = content.rfind('\n', 0, app_match.start()) + 1
        line_content = content[line_start:app_match.start()]
        indent = len(line_content) - len(line_content.lstrip())
        indent_str = ' ' * indent
        
        # Check if there's already an app.use right after (to insert before other middleware)
        # We want withTracing to be the FIRST middleware
        next_content = content[insert_pos:insert_pos + 200]
        
        # Insert the withTracing middleware
        tracing_line = f"\n\n{indent_str}// Enable request tracing for performance measurement\n{indent_str}{app_var_name}.use(withTracing);"
        
        content = content[:insert_pos] + tracing_line + content[insert_pos:]
        modified = True
    
    if modified:
        entrypoint_path.write_text(content, encoding="utf-8")
    
    return modified


def instrument_prepared_versions(
    *,
    base_dir: Path,
    head_dir: Path,
    base_ref: str,
    head_ref: str,
    changeset: ChangeSet,
    marker_plans: list[PerfMarkerPlan],
    working_dir: Path,
    instrument_mode: str = "text",
    entrypoint: str | None = None,
) -> tuple[int, int, dict[str, dict[str, int | bool]]]:
    """
    Instrument files in-place within the base and head directories.
    
    Args:
        base_dir: Directory containing the base version files (will be modified in-place)
        head_dir: Directory containing the head version files (will be modified in-place)
        base_ref: Git ref or version label for base
        head_ref: Git ref or version label for head
        changeset: Set of changed files
        marker_plans: List of instrumentation plans from analysis
        working_dir: Directory to write metadata files (plan, skipped, README)
        instrument_mode: Instrumentation mode ("text" or other)
    
    Returns (base_files_written, head_files_written).
    """
    
    # Group plans by file
    by_path: dict[str, list[PerfMarkerPlan]] = {}
    for p in marker_plans:
        by_path.setdefault(p.path, []).append(p)

    base_written = 0
    head_written = 0
    working_dir.mkdir(parents=True, exist_ok=True)

    # Copy measurement helpers into both trees so span events can be written.
    repo_root = Path(__file__).resolve().parents[1]
    measure_template_path = repo_root / "measure.template.ts"
    worker_template_path = repo_root / "worker.js"
    try:
        measure_template = measure_template_path.read_text(encoding="utf-8")
        worker_template = worker_template_path.read_text(encoding="utf-8")
    except Exception as e:  # pragma: no cover
        raise PerfPipelineError(f"Failed to read measurement templates from repo root: {e}") from e

    for root, version in ((base_dir, base_ref), (head_dir, head_ref)):
        target_measure = root / "src" / "measure.ts"
        target_worker = root / "src" / "worker.js"
        target_measure.parent.mkdir(parents=True, exist_ok=True)
        target_worker.parent.mkdir(parents=True, exist_ok=True)
        target_measure.write_text(_render_measure_template(measure_template, service_version=version), encoding="utf-8")
        target_worker.write_text(worker_template, encoding="utf-8")
        
        # Add import for measure.ts to the entrypoint file
        if entrypoint:
            _inject_measure_import(root, entrypoint)
            # For Express apps, also inject the withTracing middleware
            _inject_express_tracing_middleware(root, entrypoint)

    # Persist the plan for traceability
    plan_path = working_dir / "perf_marker_plan.json"
    plan_path.write_text(
        json.dumps([p.__dict__ for p in marker_plans], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    def _resolve_file_path(dir_path: Path, rel_path: str) -> tuple[Path, str]:
        """Find the actual file path in a directory, trying various path resolutions.
        
        Returns (full_path, resolved_rel_path).
        """
        # Try direct path
        target = dir_path / rel_path
        if target.exists():
            return target, rel_path
        
        # Try stripping common prefixes
        parts = rel_path.split("/")
        for i in range(len(parts)):
            candidate = "/".join(parts[i:])
            target = dir_path / candidate
            if target.exists():
                return target, candidate
        
        raise FileNotFoundError(f"File not found: {rel_path} in {dir_path}")

    skipped_base: list[dict[str, str]] = []
    skipped_all: list[dict[str, str]] = []
    syntax_check_available = _find_bun() is not None
    skip_counts: dict[str, dict[str, int | bool]] = {
        "syntax": {
            "available": syntax_check_available,
            "base": 0,
            "head": 0,
        },
        "anchors": {
            "base": 0,
            "head": 0,
        },
    }
    for f in changeset.files:
        if f.path not in by_path:
            continue
        # Only instrument TS-like files for now
        if not (f.path.endswith(".ts") or f.path.endswith(".tsx")):
            continue

        plans_head = [
            InsertPlan(
                path=p.path,
                insert_after=p.insert_after_new,
                occurrence=p.occurrence_new,
                label=p.label,
                reason=p.reason,
                end_before=p.end_before_new,
                end_occurrence=p.end_occurrence_new,
                function_name=p.function_name,
                operation_type=p.operation_type,
                attributes=p.attributes,
            )
            for p in by_path[f.path]
        ]
        plans_base = [
            InsertPlan(
                path=p.path,
                insert_after=(p.insert_after_old or p.insert_after_new),
                occurrence=(p.occurrence_old or p.occurrence_new),
                label=p.label,
                reason=p.reason,
                end_before=(p.end_before_old or p.end_before_new),
                end_occurrence=(p.end_occurrence_old or p.end_occurrence_new),
                function_name=p.function_name,
                operation_type=p.operation_type,
                attributes=p.attributes,
            )
            for p in by_path[f.path]
        ]

        try:
            # Instrument head version strictly (anchors should come from new code).
            head_file, head_rel = _resolve_file_path(head_dir, f.path)
            head_ts = head_file.read_text(encoding="utf-8")
            instrumented_head, head_inserted = apply_perf_markers(
                head_ts, plans_head, strict=True, instrument_mode=instrument_mode
            )
        except (InstrumentationError, FileNotFoundError) as e:
            raise PerfPipelineError(str(e)) from e

        if head_inserted > 0:
            if syntax_check_available:
                syntax_result = _syntax_check_content(instrumented_head, target_path=head_file)
                if not syntax_result.success:
                    skip_counts["syntax"]["head"] = int(skip_counts["syntax"]["head"]) + 1
                    skipped_all.append(
                        {
                            "path": f.path,
                            "version": "head",
                            "reason": "Syntax check failed after instrumentation.",
                            "details": "\n".join(syntax_result.errors[:10]),
                        }
                    )
                    continue
            # Write instrumented code back to the head directory (in-place)
            head_file.write_text(instrumented_head, encoding="utf-8")
            head_written += 1

        # Instrument base version best-effort (anchors may not exist).
        try:
            base_file, base_rel = _resolve_file_path(base_dir, f.path)
            base_ts = base_file.read_text(encoding="utf-8")
            instrumented_base, base_inserted = apply_perf_markers(
                base_ts, plans_base, strict=False, instrument_mode=instrument_mode
            )
        except Exception:
            base_inserted = 0

        if base_inserted > 0:
            if syntax_check_available:
                syntax_result = _syntax_check_content(instrumented_base, target_path=base_file)
                if not syntax_result.success:
                    skip_counts["syntax"]["base"] = int(skip_counts["syntax"]["base"]) + 1
                    skipped_all.append(
                        {
                            "path": f.path,
                            "version": "base",
                            "reason": "Syntax check failed after instrumentation.",
                            "details": "\n".join(syntax_result.errors[:10]),
                        }
                    )
                    continue
            # Write instrumented code back to the base directory (in-place)
            base_file.write_text(instrumented_base, encoding="utf-8")
            base_written += 1
        else:
            skip_counts["anchors"]["base"] = int(skip_counts["anchors"]["base"]) + 1
            skipped_base.append(
                {
                    "path": f.path,
                    "reason": "No matching anchors in base version (skipped best-effort instrumentation).",
                }
            )
            skipped_all.append(skipped_base[-1] | {"version": "base"})

    # Small breadcrumb for the user
    (working_dir / "README.txt").write_text(
        "\n".join(
            [
                "Instrumented code has been written in-place to the base and head directories.",
                f"Base dir: {base_dir}",
                f"Head dir: {head_dir}",
                f"Range: {base_ref}..{head_ref}",
                "Plan: perf_marker_plan.json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    (working_dir / "skipped_base.json").write_text(
        json.dumps(skipped_base, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (working_dir / "skipped_instrumentation.json").write_text(
        json.dumps(skipped_all, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (working_dir / "instrumentation_summary.json").write_text(
        json.dumps(
            {
                "base_written": base_written,
                "head_written": head_written,
                "skipped": skip_counts,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return base_written, head_written, skip_counts