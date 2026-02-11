from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from . import __version__
from .apply_changes import ApplyChangesError, prepare_versions
from .git_changes import ChangeSet, FileChange, GitChangesError, get_changes, open_repo, read_file_at_ref
from .mock_git import MockGitError, load_mock_changes
from .otel_setup import cli_span, init_otel

APP_NAME = "otel-agent"


theme = Theme(
    {
        "brand": "bold magenta",
        "muted": "dim",
        "ok": "bold green",
        "warn": "bold yellow",
        "err": "bold red",
        "path": "cyan",
        "cmd": "bold",
    }
)
console = Console(theme=theme)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    invoke_without_command=True,
    rich_markup_mode="rich",
    help="[bold magenta]otel-agent[/] — Instrument and benchmark code changes.",
)


@dataclass
class WorkingDir:
    """Paths to prepared base/head versions and metadata."""
    root: Path
    base: Path
    head: Path
    changes: ChangeSet
    source: str  # "git" or "json"


def _resolve(p: Path) -> Path:
    return p if p.is_absolute() else Path.cwd() / p


def _print_changes(changes: ChangeSet) -> None:
    """Print a table of changed files."""
    table = Table(box=box.SIMPLE, show_header=True)
    table.add_column("type", style="dim")
    table.add_column("path", style="cyan")
    table.add_column("summary")
    for f in changes.files:
        table.add_row(f.change_type, f.path, f.summary or "")
    console.print(table)


def _prepare_from_git(
    repo: str,
    base_ref: str,
    head_ref: str,
    working_dir: Path,
) -> WorkingDir:
    """Extract base and head versions from git into working directory."""
    cache_dir = working_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    # Get changes
    try:
        changes = get_changes(
            repo_source=repo,
            base_ref=base_ref,
            head_ref=head_ref,
            cache_dir=cache_dir,
            token_env="GIT_TOKEN",
        )
    except GitChangesError as e:
        raise typer.Exit(2) from console.print(f"[red]Error:[/] {e}")
    
    # Open repo and extract files
    try:
        git_repo, _ = open_repo(repo_source=repo, cache_dir=cache_dir, token_env="GIT_TOKEN")
    except GitChangesError as e:
        raise typer.Exit(2) from console.print(f"[red]Error:[/] {e}")
    
    base_dir = working_dir / "base"
    head_dir = working_dir / "head"
    
    # Clean and create directories
    for d in [base_dir, head_dir]:
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
    
    # Extract all changed files at both refs
    for f in changes.files:
        if f.change_type == "deleted":
            # Only exists in base
            try:
                content = read_file_at_ref(git_repo, base_ref, f.path)
                target = base_dir / f.path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            except Exception:
                pass
        elif f.change_type == "added":
            # Only exists in head
            try:
                content = read_file_at_ref(git_repo, head_ref, f.path)
                target = head_dir / f.path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            except Exception:
                pass
        else:
            # Modified: exists in both
            try:
                base_content = read_file_at_ref(git_repo, base_ref, f.path)
                target = base_dir / f.path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(base_content, encoding="utf-8")
            except Exception:
                pass
            try:
                head_content = read_file_at_ref(git_repo, head_ref, f.path)
                target = head_dir / f.path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(head_content, encoding="utf-8")
            except Exception:
                pass
    
    return WorkingDir(
        root=working_dir,
        base=base_dir,
        head=head_dir,
        changes=changes,
        source="git",
    )


def _prepare_from_json(
    app_folder: Path,
    changes_file: Path,
    working_dir: Path,
) -> WorkingDir:
    """Prepare base and head from app folder + JSON changes."""
    try:
        mock_changes = load_mock_changes(changes_file)
    except MockGitError as e:
        console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(2)
    
    try:
        result = prepare_versions(
            app_folder=app_folder,
            changes=mock_changes,
            working_dir=working_dir,
            clean=True,
        )
    except ApplyChangesError as e:
        console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(2)
    
    # Convert to ChangeSet
    changes = ChangeSet(
        base=mock_changes.base,
        head=mock_changes.head,
        files=[
            FileChange(
                path=f.path,
                change_type=f.change_type,
                summary=f.summary,
                diff_unified=f.diff_unified,
            )
            for f in mock_changes.files
        ],
    )
    
    return WorkingDir(
        root=working_dir,
        base=result.base_dir,
        head=result.head_dir,
        changes=changes,
        source="json",
    )


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", "-V", help="Show version."),
) -> None:
    if version:
        console.print(f"{APP_NAME} v{__version__}")
        raise typer.Exit(0)
    # Initialize OpenTelemetry once per process (guarded by env toggle).
    # Only do this for real commands to avoid trace noise on "help" output.
    if ctx.invoked_subcommand is not None:
        init_otel(service_name=APP_NAME, service_version=__version__)
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())


@app.command()
def run(
    config: Path = typer.Option(None, "-c", "--config", help="Config file path (default: searches for otel-agent.yml or config.yml)."),
    force: bool = typer.Option(False, "-f", "--force", help="Skip cache and force re-analysis."),
    sensitivity: str = typer.Option(
        "medium",
        "--sensitivity",
        help="How aggressively to treat changes as perf-critical: low|medium|high. Example: with high, logging like console.log may be considered perf-critical in hot paths.",
    ),
) -> None:
    """
    Prepare and instrument code changes.
    
    All options are read from config file (otel-agent.yml, config.yml, or specified via --config).
    Some options (like --force, --sensitivity) can be specified via CLI to override analysis behavior.
    """
    with cli_span("cli.run"):
        # Load config file (explicit or default)
        from .config import ConfigError, find_default_config, load_config

        with cli_span("cli.run.load_config", config=str(config) if config is not None else None):
            config_path = _resolve(config) if config is not None else find_default_config()
            if config_path is None:
                console.print("[red]Error:[/] No config file found. Create otel-agent.yml or config.yml, or specify --config.")
                raise typer.Exit(2)

            try:
                cfg = load_config(config_path)
                console.print(f"[dim]Using config: {config_path}[/]")
            except ConfigError as e:
                console.print(f"[red]Error:[/] {e}")
                raise typer.Exit(2)

        # Get all options from config
        working_dir = cfg.working_dir if cfg.working_dir else Path(".otel-agent")
        working_dir = _resolve(working_dir)
        working_dir.mkdir(parents=True, exist_ok=True)

        resolved_entrypoint = cfg.entrypoint
        resolved_model = cfg.model
        resolved_instrument = cfg.instrument
        resolved_repo = cfg.repository_url
        resolved_app = cfg.repository_dir
        resolved_changes = cfg.mock_changes_file
        resolved_base_ref = cfg.base_ref
        resolved_head_ref = cfg.head_ref
        resolved_force = force  # CLI override
        resolved_sensitivity = str(sensitivity or "medium").strip().lower()
        if resolved_sensitivity not in ("low", "medium", "high"):
            console.print("[yellow]Warning:[/] Invalid --sensitivity. Using 'medium'.")
            resolved_sensitivity = "medium"

        # Determine mode
        use_git = resolved_repo is not None
        use_json = resolved_app is not None or resolved_changes is not None

        if use_git and use_json:
            console.print("[red]Error:[/] Config has both repository_url and repository_dir. Pick one mode.")
            raise typer.Exit(2)

        if not use_git and not use_json:
            console.print("[red]Error:[/] Config must specify repository_url (git mode) or repository_dir + mock_changes_file (JSON mode).")
            raise typer.Exit(2)

        if use_json and (resolved_app is None or resolved_changes is None):
            console.print("[red]Error:[/] Both repository_dir and mock_changes_file are required for JSON mode.")
            raise typer.Exit(2)

        # Prepare versions
        console.print(Panel(f"[bold]{APP_NAME}[/] v{__version__}", expand=False))

        with cli_span(
            "cli.run.prepare_versions",
            mode="git" if use_git else "json",
            working_dir=str(working_dir),
            base_ref=resolved_base_ref,
            head_ref=resolved_head_ref,
        ):
            if use_git:
                console.print(f"\n[bold]Git mode[/]: {resolved_repo}")
                console.print(f"  Range: {resolved_base_ref}..{resolved_head_ref}")
                wd = _prepare_from_git(resolved_repo, resolved_base_ref, resolved_head_ref, working_dir)
            else:
                app_path = _resolve(resolved_app)  # type: ignore
                changes_path = _resolve(resolved_changes)  # type: ignore
                console.print(f"\n[bold]JSON mode[/]")
                console.print(f"  App: {app_path}")
                console.print(f"  Changes: {changes_path}")
                wd = _prepare_from_json(app_path, changes_path, working_dir)

        console.print(f"\n[green]✓[/] Prepared {len(wd.changes.files)} file(s)")
        console.print(f"  Base: {wd.base}")
        console.print(f"  Head: {wd.head}")

        _print_changes(wd.changes)

        # Save metadata
        metadata = {
            "source": wd.source,
            "base_dir": str(wd.base),
            "head_dir": str(wd.head),
            "base_ref": wd.changes.base,
            "head_ref": wd.changes.head,
            "files": [{"path": f.path, "type": f.change_type, "summary": f.summary} for f in wd.changes.files],
        }
        (working_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        # Analyze and instrument
        if resolved_instrument:
            from .perf_instrument_pipeline import PerfPipelineError, plan_perf_markers_with_smolagents, instrument_prepared_versions

            cache_dir = working_dir / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)

            # Get chunk size and max workers from config
            resolved_chunk_size = getattr(cfg, "analysis_chunk_size", 1)
            resolved_max_workers = getattr(cfg, "analysis_max_workers", 4)
            
            console.print("\n[bold]Analyzing changes...[/]")
            if resolved_max_workers > 1:
                console.print(f"  [dim]Parallel analysis with {resolved_max_workers} workers[/]")
            
            with cli_span(
                "cli.run.analyze",
                model=resolved_model,
                skip_cache=resolved_force,
                chunk_size=resolved_chunk_size,
                max_workers=resolved_max_workers,
                sensitivity=resolved_sensitivity,
            ):
                try:
                    import threading
                    
                    # Thread-safe progress callback
                    print_lock = threading.Lock()
                    
                    def on_chunk_complete(completed: int, total_chunks: int, chunk_plans: list) -> None:
                        if total_chunks > 1:
                            with print_lock:
                                # Show file path when processing per-file, otherwise show progress
                                if len(chunk_plans) > 0:
                                    paths = sorted(set(p.path for p in chunk_plans))
                                    if len(paths) == 1:
                                        console.print(f"  [dim][{completed}/{total_chunks}] {paths[0]}: {len(chunk_plans)} marker(s)[/]")
                                    else:
                                        console.print(f"  [dim][{completed}/{total_chunks}] {len(chunk_plans)} marker(s)[/]")
                                else:
                                    console.print(f"  [dim][{completed}/{total_chunks}] no markers[/]")
                    
                    with Progress(
                        SpinnerColumn(),
                        TextColumn("[progress.description]{task.description}"),
                        TextColumn(" "),
                        TimeElapsedColumn(),
                        console=console,
                        transient=True,
                    ) as progress:
                        progress.add_task("Running smolagent analysis...", total=None)

                        # Use git mode if we have a repo, otherwise use directory mode
                        if use_git:
                            plans = plan_perf_markers_with_smolagents(
                                model_id=resolved_model,
                                changeset=wd.changes,
                                repo_context_hint=f"{resolved_repo} ({wd.changes.base}..{wd.changes.head})",
                                repo_source=resolved_repo,
                                base_ref=wd.changes.base,
                                head_ref=wd.changes.head,
                                cache_dir=cache_dir,
                                token_env="GIT_TOKEN",
                                sensitivity=resolved_sensitivity,
                                skip_cache=resolved_force,
                                chunk_size=resolved_chunk_size,
                                max_workers=resolved_max_workers,
                                on_chunk_complete=on_chunk_complete,
                            )
                        else:
                            # JSON mode: read from pre-extracted directories
                            plans = plan_perf_markers_with_smolagents(
                                model_id=resolved_model,
                                changeset=wd.changes,
                                repo_context_hint=f"{wd.head} ({wd.changes.base}..{wd.changes.head})",
                                repo_source=None,
                                base_ref=wd.changes.base,
                                head_ref=wd.changes.head,
                                cache_dir=cache_dir,
                                token_env=None,
                                base_dir=wd.base,
                                head_dir=wd.head,
                                sensitivity=resolved_sensitivity,
                                skip_cache=resolved_force,
                                chunk_size=resolved_chunk_size,
                                max_workers=resolved_max_workers,
                                on_chunk_complete=on_chunk_complete,
                            )
                except PerfPipelineError as e:
                    console.print(f"[red]Error:[/] {e}")
                    raise typer.Exit(2)

            if plans:
                console.print(f"[green]✓[/] Found {len(plans)} instrumentation point(s)")
                for p in plans:
                    console.print(f"  • {p.path}: {p.label}")
            else:
                console.print("[yellow]No instrumentation points found.[/]")

            if resolved_instrument and plans:
                console.print("\n[bold]Instrumenting...[/]")
                with cli_span("cli.run.instrument", entrypoint=resolved_entrypoint, plan_count=len(plans)):
                    try:
                        base_count, head_count, skip_counts = instrument_prepared_versions(
                            base_dir=wd.base,
                            head_dir=wd.head,
                            base_ref=wd.changes.base,
                            head_ref=wd.changes.head,
                            changeset=wd.changes,
                            marker_plans=plans,
                            working_dir=working_dir,
                            entrypoint=resolved_entrypoint,
                        )
                        console.print(f"[green]✓[/] Instrumented files in-place")
                        console.print(f"  Base ({wd.base}): {base_count} file(s)")
                        console.print(f"  Head ({wd.head}): {head_count} file(s)")
                        if not bool(skip_counts["syntax"]["available"]):
                            console.print("[yellow]⚠[/] bun not found; skipped syntax checks")
                        else:
                            skipped_syntax = int(skip_counts["syntax"]["base"]) + int(skip_counts["syntax"]["head"])
                            if skipped_syntax > 0:
                                console.print(
                                    f"[yellow]⚠[/] Skipped instrumentation due to syntax errors: {skipped_syntax} "
                                    f"(base: {skip_counts['syntax']['base']}, head: {skip_counts['syntax']['head']})"
                                )
                    except PerfPipelineError as e:
                        console.print(f"[red]Error:[/] {e}")
                        raise typer.Exit(2)

        # Generate duet + experiment compose
        from .duet_prepare import DuetConfig, DuetPrepareError, prepare_duet_compose
        from .experiment_compose import (
            ExperimentComposeError,
            prepare_experiment_compose as _prepare_experiment_compose,
        )

        duet_cfg = DuetConfig(
            cpu_limit=getattr(cfg, "duet_cpu_limit", "2"),
            cpu_reservation=getattr(cfg, "duet_cpu_reservation", "1"),
            memory_limit=getattr(cfg, "duet_memory_limit", "4G"),
            base_port=getattr(cfg, "duet_base_port", 3001),
            head_port=getattr(cfg, "duet_head_port", 3002),
            prefix=getattr(cfg, "duet_prefix", "duet"),
            base_cpuset=getattr(cfg, "duet_base_cpuset", None),
            head_cpuset=getattr(cfg, "duet_head_cpuset", None),
        )

        with cli_span(
            "cli.run.prepare_duet_and_experiment_compose",
            cpu_limit=duet_cfg.cpu_limit,
            memory_limit=duet_cfg.memory_limit,
            base_port=duet_cfg.base_port,
            head_port=duet_cfg.head_port,
        ):
            console.print("\n[bold]Preparing duet benchmark + experiment compose...[/]")
            console.print(f"  Base: {wd.base}")
            console.print(f"  Head: {wd.head}")

            try:
                duet_result = prepare_duet_compose(
                    base_dir=wd.base,
                    head_dir=wd.head,
                    output_dir=working_dir,
                    config=duet_cfg,
                )
            except DuetPrepareError as e:
                console.print(f"[red]Error:[/] {e}")
                raise typer.Exit(2)

            ext_compose = wd.base / "docker-compose.ext.yml"
            out_path = getattr(cfg, "experiment_compose_out", None) or (working_dir / "docker-compose.experiment.yml")
            try:
                exp_result = _prepare_experiment_compose(
                    duet_compose_path=duet_result.compose_path,
                    ext_compose_path=ext_compose if ext_compose.exists() else None,
                    output_path=out_path,
                    mongo_cpuset=getattr(cfg, "duet_mongo_cpuset", None),
                    separate_mongo=getattr(cfg, "duet_separate_mongo", False),
                )
            except ExperimentComposeError as e:
                console.print(f"[red]Error:[/] {e}")
                raise typer.Exit(2)

            console.print(f"\n[green]✓[/] Generated duet compose files:")
            console.print(f"  Compose: [path]{duet_result.compose_path}[/]")
            console.print(f"  Env:     [path]{duet_result.env_path}[/]")

            console.print(f"\n[green]✓[/] Wrote experiment compose: [path]{exp_result.compose_path}[/]")
            console.print("\n[dim]Run locally:[/]")
            console.print(
                "  [cmd]BASE_DIR=$(pwd)/.otel-agent/base HEAD_DIR=$(pwd)/.otel-agent/head docker compose -f "
                f"{exp_result.compose_path} up -d --build[/]"
            )
    
    console.print("\n[dim]Done.[/]")


@app.command()
def eval(
    old: Path = typer.Option(..., "-o", "--old", help="Old/base samples: SQLite (.db), k6 JSON, or sample file."),
    new: Path = typer.Option(..., "-n", "--new", help="New/head samples: SQLite (.db), k6 JSON, or sample file."),
    span: str = typer.Option(None, "-s", "--span", help="Span name filter (SQLite only)."),
    k6_metric: str = typer.Option("http_req_duration", "--k6-metric", help="k6 metric name (for k6 JSON)."),
    out: Path = typer.Option(Path("eval_report.json"), "--out", help="Output report."),
    bootstrap: int = typer.Option(2000, help="Bootstrap resamples."),
    power: float = typer.Option(0.80, help="Statistical power for MDE (default: 0.80)."),
    fast: bool = typer.Option(False, "--fast", "-f", help="Fast mode: skip bootstrap CIs for quick results."),
    parallel: bool = typer.Option(True, "--parallel/--no-parallel", help="Parallelize per-span comparisons."),
    plot: bool = typer.Option(False, "--plot", help="Generate visualization plots."),
    plot_dir: Path = typer.Option(None, "--plot-dir", help="Directory for plots (default: same as output)."),
    plot_format: str = typer.Option("png", "--plot-format", help="Plot format: png, pdf, svg."),
) -> None:
    """
    Compare performance samples between old and new versions.
    
    Supports multiple input formats:
    - SQLite databases (.db) from worker.js instrumentation
    - k6 JSON output files
    - Generic JSON/CSV sample files
    
    With --plot, generates visualization plots:
    - Mean latency deltas with confidence intervals
    - Effect sizes (Cohen's d)
    - P95 latency comparison
    - Sample sizes
    - Per-span bootstrap confidence intervals (mean latency)
    - MDE (Minimum Detectable Effect) analysis
    - Summary dashboard
    - Per-span relative change distributions (bootstrap, SQLite only)
    - CPU steal time over experiment (SQLite inputs only, Linux-collected metric)
    
    Performance options:
    - --fast (-f): Skip bootstrap CIs for quick results (10-100x faster)
    - --no-parallel: Disable parallel span processing
    
    Examples:
        otel-agent eval -o base.db -n head.db
        otel-agent eval -o base.db -n head.db --fast  # Quick results
        otel-agent eval -o base-k6.json -n head-k6.json --plot
        otel-agent eval -o base.db -n head.db --span "search"
        otel-agent eval -o base.db -n head.db --plot --plot-format pdf
    """
    from .eval_stats import (
        EvalStatsError,
        compare_samples,
        compare_samples_parallel,
        filter_spans_by_min_samples,
        is_sqlite_input,
        load_durations_ms,
        load_span_durations_by_name,
    )

    with cli_span("cli.eval", span_filter=span, bootstrap=bootstrap, fast=fast):
        console.print(Panel(f"[bold]{APP_NAME} eval[/]", expand=False))
        
        if fast:
            console.print("[dim]Fast mode: skipping bootstrap CIs[/]")

        try:
            old_path = _resolve(old)
            new_path = _resolve(new)
            span_samples_old: dict[str, list[float]] | None = None
            span_samples_new: dict[str, list[float]] | None = None

            # If both inputs are SQLite and the user didn't request a single span,
            # compute stats per span_name (instead of aggregating across spans).
            if span is None and is_sqlite_input(old_path) and is_sqlite_input(new_path):
                old_by = load_span_durations_by_name(old_path)
                new_by = load_span_durations_by_name(new_path)

                # Filter out spans with too few observations (noise-prone).
                # We require at least N samples in BOTH base and head.
                min_n = 10
                old_by, new_by, too_small = filter_spans_by_min_samples(
                    old_by,
                    new_by,
                    min_samples=min_n,
                )
                if too_small:
                    console.print(f"[dim]Skipping {len(too_small)} spans with < {min_n} samples[/]")

                old_spans = set(old_by.keys())
                new_spans = set(new_by.keys())

                # Use parallel comparison for multiple spans
                if parallel and len(old_spans & new_spans) > 2:
                    console.print(f"[dim]Comparing {len(old_spans & new_spans)} spans in parallel...[/]")
                    per_span = compare_samples_parallel(
                        old_by,
                        new_by,
                        bootstrap=bootstrap,
                        power=power,
                        skip_ci=fast,
                    )
                else:
                    per_span: dict[str, Any] = {}
                    for span_name in sorted(old_spans & new_spans):
                        per_span[span_name] = compare_samples(
                            old_by[span_name],
                            new_by[span_name],
                            bootstrap=bootstrap,
                            power=power,
                            skip_ci=fast,
                        )

                overall_old = [v for vs in old_by.values() for v in vs]
                overall_new = [v for vs in new_by.values() for v in vs]
                wall_time_old = overall_old
                wall_time_new = overall_new
                span_samples_old = old_by
                span_samples_new = new_by

                report = {
                    "per_span": per_span,
                    "missing": {
                        "old_only": sorted(old_spans - new_spans),
                        "new_only": sorted(new_spans - old_spans),
                    },
                    "overall": compare_samples(
                        overall_old,
                        overall_new,
                        bootstrap=bootstrap,
                        power=power,
                        seed=10,
                        skip_ci=fast,
                    ),
                    "meta": {
                        "note": "Per-span statistics (grouped by span_name) for SQLite inputs.",
                        "bootstrap": int(bootstrap),
                        "power": float(power),
                        "fast_mode": fast,
                        "min_samples_per_span": min_n,
                    },
                }
            else:
                old_samples = load_durations_ms(old_path, span_name=span, k6_metric=k6_metric)
                new_samples = load_durations_ms(new_path, span_name=span, k6_metric=k6_metric)
                wall_time_old = old_samples
                wall_time_new = new_samples
                report = compare_samples(old_samples, new_samples, bootstrap=bootstrap, power=power, skip_ci=fast)
        except EvalStatsError as e:
            console.print(f"[red]Error:[/] {e}")
            raise typer.Exit(2)

        out_path = _resolve(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

        console.print(f"[green]✓[/] Wrote report to {out_path}")

        # Single-span / non-SQLite output (backward compatible)
        if "deltas" in report:
            deltas = report.get("deltas", {})
            mean_delta = deltas.get("mean", {}).get("rel")
            p95_delta = deltas.get("p95", {}).get("rel")
            tests = report.get("statistical_tests", {})
            mde = report.get("mde", {})

            console.print()
            console.print(f"  Samples: {report['old']['n']} (old) vs {report['new']['n']} (new)")

            if mean_delta is not None:
                console.print(f"  Δ mean: {mean_delta * 100:+.1f}%")
            if p95_delta is not None:
                console.print(f"  Δ p95:  {p95_delta * 100:+.1f}%")

            welch = tests.get("welch_t", {})
            cohens = tests.get("cohens_d", {})
            if welch:
                sig_text = "[green]Yes[/]" if welch.get("is_significant") else "[yellow]No[/]"
                console.print(f"  Significant: {sig_text} (p={welch.get('p_value', 0):.4f})")
            if cohens:
                console.print(f"  Cohen's d: {cohens.get('value', 0):.3f} ({cohens.get('interpretation', '')})")

            if mde:
                console.print()
                console.print(f"  [dim]MDE @ {int(power*100)}% power:[/]")
                console.print(f"    Absolute: {mde.get('absolute', 0):.3f} ms")
                console.print(f"    Relative: {mde.get('relative_percent', 0):.2f}%")
                if mde.get("observed_exceeds_mde"):
                    console.print("    [green]✓ Observed effect exceeds MDE[/]")
                else:
                    console.print("    [yellow]⚠ Observed effect below MDE[/]")
        else:
            # Per-span SQLite output
            per_span = report.get("per_span", {}) or {}
            missing = report.get("missing", {}) or {}
            console.print()
            console.print(f"  Spans compared: {len(per_span)}")
            if missing.get("old_only") or missing.get("new_only"):
                console.print(
                    f"  Missing: {len(missing.get('old_only', []))} old-only, {len(missing.get('new_only', []))} new-only"
                )

        # Generate plots if requested
        if plot:
            try:
                from .eval_stats import (
                    compare_samples,
                    compare_samples_parallel,
                    filter_spans_by_min_samples,
                    generate_all_plots,
                    is_sqlite_input,
                    load_cpu_steal_pct_timeseries,
                    load_cpu_utilization_pct_timeseries,
                    load_cpu_time_total_ms_samples,
                    load_cpu_time_total_ms_by_name,
                    plot_cpu_steal_over_time,
                    plot_cpu_utilization_over_time,
                    plot_percentile_comparison,
                    plot_relative_change_distribution,
                    plot_span_relative_change_distributions,
                )
                
                # Determine plot directory
                plots_dir = _resolve(plot_dir) if plot_dir else out_path.parent
                plots_dir.mkdir(parents=True, exist_ok=True)
                
                # Use output filename as prefix (without extension)
                prefix = out_path.stem
                
                console.print()
                with console.status("[bold blue]Generating plots...[/]"):
                    plot_paths = generate_all_plots(
                        report,
                        plots_dir,
                        prefix=prefix,
                        format=plot_format,
                    )

                    if not fast and span_samples_old and span_samples_new:
                        span_dist_path = plots_dir / f"{prefix}_span_relative_change_distributions.{plot_format}"
                        fig = plot_span_relative_change_distributions(
                            span_samples_old,
                            span_samples_new,
                            save_path=span_dist_path,
                        )
                        try:
                            import matplotlib.pyplot as plt  # type: ignore

                            plt.close(fig)
                        except Exception:
                            pass
                        plot_paths.append(span_dist_path)

                    # Extra: CPU steal time series plot (only for SQLite inputs).
                    if is_sqlite_input(old_path) and is_sqlite_input(new_path):
                        old_steal = load_cpu_steal_pct_timeseries(old_path)
                        new_steal = load_cpu_steal_pct_timeseries(new_path)
                        if old_steal or new_steal:
                            steal_path = plots_dir / f"{prefix}_cpu_steal_over_time.{plot_format}"
                            fig = plot_cpu_steal_over_time(old_steal, new_steal, save_path=steal_path)
                            try:
                                import matplotlib.pyplot as plt  # type: ignore

                                plt.close(fig)
                            except Exception:
                                pass
                            plot_paths.append(steal_path)

                        old_util = load_cpu_utilization_pct_timeseries(old_path)
                        new_util = load_cpu_utilization_pct_timeseries(new_path)
                        if old_util or new_util:
                            util_path = plots_dir / f"{prefix}_cpu_utilization_over_time.{plot_format}"
                            fig = plot_cpu_utilization_over_time(old_util, new_util, save_path=util_path)
                            try:
                                import matplotlib.pyplot as plt  # type: ignore

                                plt.close(fig)
                            except Exception:
                                pass
                            plot_paths.append(util_path)

                        # Additional: per-span median CPU time change (%) plot (mirrors median_comparison for latency).
                        cpu_old_by = load_cpu_time_total_ms_by_name(old_path)
                        cpu_new_by = load_cpu_time_total_ms_by_name(new_path)
                        min_cpu_n = 10
                        cpu_old_by, cpu_new_by, cpu_too_small = filter_spans_by_min_samples(
                            cpu_old_by,
                            cpu_new_by,
                            min_samples=min_cpu_n,
                        )
                        if cpu_too_small:
                            console.print(
                                f"[dim]Skipping {len(cpu_too_small)} CPU spans with < {min_cpu_n} samples[/]"
                            )
                        common_cpu = sorted(set(cpu_old_by.keys()) & set(cpu_new_by.keys()))
                        if common_cpu:
                            if parallel and len(common_cpu) > 2:
                                cpu_per_span = compare_samples_parallel(
                                    cpu_old_by,
                                    cpu_new_by,
                                    bootstrap=bootstrap,
                                    power=power,
                                    skip_ci=fast,
                                )
                            else:
                                cpu_per_span = {}
                                for span_name in common_cpu:
                                    cpu_per_span[span_name] = compare_samples(
                                        cpu_old_by[span_name],
                                        cpu_new_by[span_name],
                                        bootstrap=bootstrap,
                                        power=power,
                                        skip_ci=fast,
                                    )

                            cpu_report = {"per_span": cpu_per_span}
                            cpu_median_change_path = plots_dir / f"{prefix}_cpu_time_median_comparison_by_span.{plot_format}"
                            fig = plot_percentile_comparison(
                                cpu_report,
                                percentile="p50",
                                title="Median CPU Time Change (%)",
                                x_label="Relative Change in Median CPU Time (%)",
                                save_path=cpu_median_change_path,
                            )
                            try:
                                import matplotlib.pyplot as plt  # type: ignore

                                plt.close(fig)
                            except Exception:
                                pass
                            plot_paths.append(cpu_median_change_path)
                
                console.print(f"[green]✓[/] Generated {len(plot_paths)} plots in {plots_dir}")
                for p in plot_paths:
                    console.print(f"    • {p.name}")
            except EvalStatsError as e:
                console.print(f"[yellow]⚠ Could not generate plots:[/] {e}")


@app.command()
def localize(
    head_dir: Path = typer.Option(..., "--head-dir", help="Path to the HEAD/head source directory (unified diff + plans are evaluated against this tree)."),
    plan: Path = typer.Option(..., "--plan", help="Path to perf_marker_plan.json (written by the instrumentation pipeline)."),
    changes: Path = typer.Option(..., "--changes", help="Path to mock git changes JSON with unified diffs (must include files[].path and files[].diff_unified)."),
    out: Path = typer.Option(Path("localization_report.json"), "-o", "--out", help="Where to write the localization report JSON."),
) -> None:
    """
    Compute semantic localization metrics for planned instrumentation.

    Outputs:
    - Localization Accuracy (perf-relevant files that have any planned span)
    - Localization Error (line distance to changed lines; distribution)
    """

    from .eval_stats import EvalStatsError  # reuse common error type for CLI UX
    from .localization_metrics import PlanAnchors, compute_localization_metrics_per_file

    with cli_span("cli.localize"):
        console.print(Panel(f"[bold]{APP_NAME} localize[/]", expand=False))

        head_dir = _resolve(head_dir)
        plan = _resolve(plan)
        changes = _resolve(changes)
        out = _resolve(out)

        try:
            if not head_dir.exists() or not head_dir.is_dir():
                raise EvalStatsError(f"head_dir does not exist or is not a directory: {head_dir}")
            if not plan.exists():
                raise EvalStatsError(f"plan file not found: {plan}")
            if not changes.exists():
                raise EvalStatsError(f"changes file not found: {changes}")

            # Load plans (ranked order is the JSON order as written by pipeline).
            raw_plans = json.loads(plan.read_text(encoding="utf-8"))
            if not isinstance(raw_plans, list):
                raise EvalStatsError(f"plan must be a JSON list: {plan}")

            plans: list[PlanAnchors] = []
            for obj in raw_plans:
                p = PlanAnchors.from_obj(obj)
                if p is not None:
                    plans.append(p)

            # Load unified diffs.
            raw_changes = json.loads(changes.read_text(encoding="utf-8"))
            files = raw_changes.get("files") if isinstance(raw_changes, dict) else None
            if not isinstance(files, list):
                raise EvalStatsError(f"changes JSON must contain a 'files' list: {changes}")

            diffs_by_path: dict[str, str] = {}
            perf_relevant_by_path: dict[str, bool] = {}
            perfect_span_by_path: dict[str, tuple[int, int]] = {}
            for f in files:
                if not isinstance(f, dict):
                    continue
                pth = f.get("path")
                diff = f.get("diff_unified")
                if isinstance(pth, str) and isinstance(diff, str) and diff.strip():
                    diffs_by_path[pth] = diff
                    # Optional flag in mock changesets. Defaults to True when absent.
                    pr = f.get("perf_relevant")
                    if isinstance(pr, bool):
                        perf_relevant_by_path[pth] = pr
                    ps = f.get("perfect_span")
                    if isinstance(ps, dict):
                        start_line = ps.get("start_line")
                        end_line = ps.get("end_line")
                        if isinstance(start_line, int) and isinstance(end_line, int):
                            perfect_span_by_path[pth] = (start_line, end_line)

            if not diffs_by_path:
                raise EvalStatsError(
                    f"No unified diffs found in {changes}. Expected files[].diff_unified strings."
                )

            # Only evaluate planned spans that belong to files present in the changeset/diffs.
            # (This avoids penalizing the metric for plans outside the changed-file set.)
            diff_paths = set(diffs_by_path.keys())
            skipped_plans = [p for p in plans if p.path not in diff_paths]
            plans = [p for p in plans if p.path in diff_paths]

            per_file = compute_localization_metrics_per_file(
                plans=plans,
                head_dir=head_dir,
                diffs_by_path=diffs_by_path,
                perf_relevant_by_path=perf_relevant_by_path or None,
                perfect_span_by_path=perfect_span_by_path or None,
                strip_markers=True,
            )
            if not per_file:
                raise EvalStatsError(
                    "No per-file localization metrics computed (no plans matched diff files)."
                )

        except EvalStatsError as e:
            console.print(f"[red]Error:[/] {e}")
            raise typer.Exit(2)

        # Summary focused on "no spans for perf-neutral diffs".
        perf_files = 0
        perf_files_with_spans = 0
        perf_files_missed = 0
        non_perf_files = 0
        non_perf_files_with_spans = 0
        total_plans = 0
        valid_plans = 0
        tp_within_k = 0
        total_within_k = 0
        perf_files_hit_within_k = 0
        non_perf_files_hit_within_k = 0
        line_tolerance = None

        for _, m in per_file.items():
            has_perf = bool(m.get("has_perf_relevant_changes"))
            span_count = int(m.get("planned_span_count") or 0)
            total_plans += span_count
            located_spans = m.get("located_spans") or []
            if isinstance(located_spans, list):
                valid_plans += sum(1 for ls in located_spans if ls.get("start_found") is True)
            within_k = m.get("within_k_lines")
            if isinstance(within_k, dict):
                if line_tolerance is None:
                    line_tolerance = within_k.get("k")
                tp_within_k += int(
                    within_k.get("predicted_span_hits", within_k.get("per_plan_hits")) or 0
                )
                total_within_k += int(
                    within_k.get("predicted_span_total", within_k.get("per_plan_total")) or 0
                )
                if within_k.get("file_hit") is True:
                    if has_perf:
                        perf_files_hit_within_k += 1
                    else:
                        non_perf_files_hit_within_k += 1
            if has_perf:
                perf_files += 1
                if span_count > 0:
                    perf_files_with_spans += 1
                else:
                    perf_files_missed += 1
            else:
                non_perf_files += 1
                if span_count > 0:
                    non_perf_files_with_spans += 1

        summary = {
            "files_total": len(per_file),
            "files_perf_relevant": perf_files,
            "files_perf_relevant_with_spans": perf_files_with_spans,
            "files_perf_relevant_missed": perf_files_missed,
            "files_perf_neutral": non_perf_files,
            "files_perf_neutral_with_spans_false_positive": non_perf_files_with_spans,
            "no_span_success_rate_on_perf_neutral_files": (
                None
                if non_perf_files == 0
                else (non_perf_files - non_perf_files_with_spans) / float(non_perf_files)
            ),
            "plan_validity_rate": (
                None if total_plans == 0 else valid_plans / float(total_plans)
            ),
            "precision": (
                None
                if (perf_files_with_spans + non_perf_files_with_spans) == 0
                else perf_files_with_spans
                / float(perf_files_with_spans + non_perf_files_with_spans)
            ),
            "sensitivity": (
                None
                if (perf_files_with_spans + perf_files_missed) == 0
                else perf_files_with_spans / float(perf_files_with_spans + perf_files_missed)
            ),
            "specificity": (
                None
                if non_perf_files == 0
                else (non_perf_files - non_perf_files_with_spans) / float(non_perf_files)
            ),
            "line_tolerance": line_tolerance,
            "precision_within_k_lines": (
                None if total_within_k == 0 else tp_within_k / float(total_within_k)
            ),
            "recall_within_k_lines": (
                None if perf_files == 0 else perf_files_hit_within_k / float(perf_files)
            ),
            "specificity_within_k_lines": (
                None
                if non_perf_files == 0
                else (non_perf_files - non_perf_files_hit_within_k) / float(non_perf_files)
            ),
        }

        report = {
            "per_file": per_file,
            "summary": summary,
            "meta": {
                "head_dir": str(head_dir),
                "plan_path": str(plan),
                "changes_path": str(changes),
                "note": "Line numbers are computed on HEAD source with perf-marker lines stripped to remain comparable to unified diffs. Localization accuracy is file-level (perf-relevant file has any planned span). Plan validity rate counts plans whose start anchor is found in HEAD. Localization error merges all located spans per file into [min_start, max_end] and computes boundary distance (|start_diff| + |end_diff|) against the perfect span, penalising both undershoot and overshoot; falls back to nearest changed line distance when no perfect span is provided. Precision within k lines counts a predicted span group as correct if it is within k lines of a perf-relevant change (or perfect span), where zero-gap adjacent spans are treated as one predicted span. Recall and specificity within k lines are file-level via file_hit.",
                "skipped_plans_not_in_changeset": [p.__dict__ for p in skipped_plans],
            },
        }

        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        console.print(f"[green]✓[/] Wrote localization report to {out}")


@app.command()
def doctor() -> None:
    """Check environment and dependencies."""
    import platform
    import sys

    with cli_span("cli.doctor"):
        console.print(Panel(f"[bold]{APP_NAME} doctor[/]", expand=False))

        checks = [
            ("Python", f"{sys.version.split()[0]} ({platform.system()})"),
            ("CWD", str(Path.cwd())),
        ]

        # Check dependencies
        for pkg, hint in [
            ("yaml", "PyYAML for config files"),
            ("git", "GitPython for git operations"),
            ("smolagents", "smolagents for AI analysis"),
        ]:
            try:
                __import__(pkg)
                checks.append((pkg, "[green]✓[/]"))
            except ImportError:
                checks.append((pkg, f"[yellow]missing[/] ({hint})"))

        # Check git CLI
        if shutil.which("git"):
            checks.append(("git CLI", "[green]✓[/]"))
        else:
            checks.append(("git CLI", "[yellow]not found[/]"))

        # Check node
        if shutil.which("node"):
            checks.append(("node", "[green]✓[/]"))
        else:
            checks.append(("node", "[dim]not found (optional)[/]"))

        # Check bun (used for TypeScript linting)
        if shutil.which("bun"):
            checks.append(("bun", "[green]✓[/] (TypeScript linting)"))
        else:
            checks.append(("bun", "[yellow]not found[/] (needed for TS linting)"))

        import os

        if os.environ.get("OPENAI_API_KEY"):
            checks.append(("OPENAI_API_KEY", "[green]set[/]"))
        else:
            checks.append(("OPENAI_API_KEY", "[yellow]not set[/]"))

        table = Table(box=box.SIMPLE, show_header=False)
        table.add_column("check", style="dim")
        table.add_column("status")
        for name, status in checks:
            table.add_row(name, status)
        console.print(table)
