from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

import yaml
from typer.testing import CliRunner

from src.cli import app


runner = CliRunner()


@contextmanager
def _chdir(path: Path):
    prev = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


def _init_repo(repo_dir: Path) -> None:
    import git

    repo = git.Repo.init(repo_dir)
    (repo_dir / "src").mkdir(parents=True, exist_ok=True)
    (repo_dir / "src" / "app.ts").write_text('export const x = 1;\n', encoding="utf-8")
    repo.index.add(["src/app.ts"])
    repo.index.commit("v1")

    (repo_dir / "src" / "app.ts").write_text('export const x = 2;\n', encoding="utf-8")
    repo.index.add(["src/app.ts"])
    repo.index.commit("v2")


def _create_app_folder(app_dir: Path) -> None:
    """Create a minimal app folder structure for testing."""
    (app_dir / "src").mkdir(parents=True, exist_ok=True)
    (app_dir / "src" / "app.ts").write_text(
        'export const x = 1;\nconsole.log("hello");\n',
        encoding="utf-8",
    )


def _create_changes_file(changes_path: Path) -> None:
    """Create a mock changes JSON file for testing."""
    changes = {
        "base": "v1",
        "head": "v2",
        "files": [
            {
                "path": "src/app.ts",
                "change_type": "modified",
                "summary": "Change x to 2",
                "diff_unified": (
                    '--- a/src/app.ts\n'
                    '+++ b/src/app.ts\n'
                    '@@ -1,2 +1,2 @@\n'
                    '-export const x = 1;\n'
                    '+export const x = 2;\n'
                    ' console.log("hello");'
                ),
            }
        ],
    }
    changes_path.write_text(json.dumps(changes), encoding="utf-8")


def test_version_flag() -> None:
    res = runner.invoke(app, ["--version"])
    assert res.exit_code == 0
    assert "otel-agent" in res.stdout


def test_doctor() -> None:
    res = runner.invoke(app, ["doctor"])
    assert res.exit_code == 0
    assert "Python" in res.stdout


def test_run_git_mode(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_repo(repo_dir)
    workdir = tmp_path / "workdir"

    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "\n".join(
            [
                f"repository_url: {repo_dir}",
                "base_ref: HEAD~1",
                "head_ref: HEAD",
                f"working_dir: {workdir}",
                "instrument: false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with _chdir(tmp_path):
        res = runner.invoke(app, ["run", "--config", str(config_path)], env={})

    assert res.exit_code == 0, res.stdout
    assert "Git mode" in res.stdout
    assert "src/app.ts" in res.stdout
    assert (workdir / "base").exists()
    assert (workdir / "head").exists()
    assert (workdir / "docker-compose.duet.yml").exists()
    assert (workdir / ".env.duet").exists()
    assert (workdir / "docker-compose.experiment.yml").exists()


def test_run_json_mode(tmp_path: Path) -> None:
    app_dir = tmp_path / "example_app"
    app_dir.mkdir()
    _create_app_folder(app_dir)

    changes_file = tmp_path / "changes.json"
    _create_changes_file(changes_file)

    workdir = tmp_path / "workdir"

    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "\n".join(
            [
                f"repository_dir: {app_dir}",
                f"mock_changes_file: {changes_file}",
                f"working_dir: {workdir}",
                "instrument: false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with _chdir(tmp_path):
        res = runner.invoke(app, ["run", "--config", str(config_path)], env={})

    assert res.exit_code == 0, res.stdout
    assert "JSON mode" in res.stdout
    assert "src/app.ts" in res.stdout
    assert (workdir / "base").exists()
    assert (workdir / "head").exists()
    assert (workdir / "docker-compose.duet.yml").exists()
    assert (workdir / ".env.duet").exists()
    assert (workdir / "docker-compose.experiment.yml").exists()

    # Check that the base version has original content
    base_content = (workdir / "base" / "src" / "app.ts").read_text(encoding="utf-8")
    assert "export const x = 1;" in base_content

    # Check that the head version has modified content
    head_content = (workdir / "head" / "src" / "app.ts").read_text(encoding="utf-8")
    assert "export const x = 2;" in head_content


def test_run_requires_source(tmp_path: Path) -> None:
    """Test that run fails when no config file is available."""
    with _chdir(tmp_path):
        res = runner.invoke(app, ["run"], env={})
    assert res.exit_code == 2
    assert "No config file found" in res.stdout


def test_run_cannot_mix_modes(tmp_path: Path) -> None:
    """Kept for backward compat: config validation should reject mixed source settings."""
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "\n".join(
            [
                "repository_url: /some/repo",
                f"repository_dir: {tmp_path}",
                "instrument: false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with _chdir(tmp_path):
        res = runner.invoke(app, ["run", "--config", str(config_path)], env={})
    assert res.exit_code == 2


def test_run_json_mode_requires_both_args(tmp_path: Path) -> None:
    """Test that JSON mode requires both repository_dir and mock_changes_file in config."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    _create_app_folder(app_dir)

    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "\n".join(
            [
                f"repository_dir: {app_dir}",
                "instrument: false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with _chdir(tmp_path):
        res = runner.invoke(app, ["run", "--config", str(config_path)], env={})

    assert res.exit_code == 2


def test_metadata_written(tmp_path: Path) -> None:
    """Test that metadata.json is written after run."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    _create_app_folder(app_dir)
    changes_file = tmp_path / "changes.json"
    _create_changes_file(changes_file)
    workdir = tmp_path / "workdir"

    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "\n".join(
            [
                f"repository_dir: {app_dir}",
                f"mock_changes_file: {changes_file}",
                f"working_dir: {workdir}",
                "instrument: false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with _chdir(tmp_path):
        res = runner.invoke(app, ["run", "--config", str(config_path)], env={})

    assert res.exit_code == 0
    metadata_path = workdir / "metadata.json"
    assert metadata_path.exists()
    
    metadata = json.loads(metadata_path.read_text())
    assert metadata["source"] == "json"
    assert len(metadata["files"]) == 1


def test_run_generates_duet_and_experiment_compose(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    _create_app_folder(app_dir)

    # Minimal compose file used as the template for duet generation
    base_compose = {
        "services": {
            "app": {
                "build": ".",
                "ports": ["3000:3000"],
                "environment": {"JWT_SECRET": "${JWT_SECRET}"},
            }
        }
    }
    (app_dir / "docker-compose.yml").write_text(
        yaml.safe_dump(base_compose, sort_keys=False),
        encoding="utf-8",
    )

    # Optional ext compose (e.g., mongodb) should be merged into experiment compose
    ext_compose = {
        "services": {"mongodb": {"image": "mongo:7", "healthcheck": {"test": "true"}}},
        "volumes": {"dbdata": {}},
    }
    (app_dir / "docker-compose.ext.yml").write_text(
        yaml.safe_dump(ext_compose, sort_keys=False),
        encoding="utf-8",
    )

    changes_file = tmp_path / "changes.json"
    _create_changes_file(changes_file)

    workdir = tmp_path / ".otel-agent"
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "\n".join(
            [
                f"repository_dir: {app_dir}",
                f"mock_changes_file: {changes_file}",
                f"working_dir: {workdir}",
                "instrument: false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with _chdir(tmp_path):
        res = runner.invoke(app, ["run", "--config", str(config_path)], env={})

    assert res.exit_code == 0, res.stdout
    assert (workdir / "docker-compose.duet.yml").exists()
    assert (workdir / ".env.duet").exists()
    exp_out = workdir / "docker-compose.experiment.yml"
    assert exp_out.exists()

    composed = yaml.safe_load(exp_out.read_text(encoding="utf-8"))
    assert "mongodb" in composed["services"]

    base = composed["services"]["app-base"]
    head = composed["services"]["app-head"]
    assert base["build"]["context"] == "${BASE_DIR}"
    assert head["build"]["context"] == "${HEAD_DIR}"
    assert base["environment"]["MONGO_URL"] == "mongodb://mongodb:27017/test"
    assert head["environment"]["MONGO_URL"] == "mongodb://mongodb:27017/test"


def test_removed_prepare_commands(tmp_path: Path) -> None:
    # The old compose helper commands were removed in favor of integrating into `run`.
    res1 = runner.invoke(app, ["prepare-duet"], env={})
    res2 = runner.invoke(app, ["prepare-experiment-compose"], env={})
    res3 = runner.invoke(app, ["prepare-duet-setup"], env={})
    combined = (
        (res1.stdout or "")
        + getattr(res1, "stderr", "")
        + (res2.stdout or "")
        + getattr(res2, "stderr", "")
        + (res3.stdout or "")
        + getattr(res3, "stderr", "")
    )
    assert res1.exit_code != 0
    assert res2.exit_code != 0
    assert res3.exit_code != 0
    assert "No such command" in combined


def test_eval_sqlite_per_span_name(tmp_path: Path) -> None:
    """When both inputs are SQLite and no --span is provided, eval reports per span_name."""
    import sqlite3

    def make_db(p: Path, rows: list[tuple[float, str, int]]):
        conn = sqlite3.connect(p)
        conn.execute(
            """
            CREATE TABLE spans (
                id INTEGER PRIMARY KEY,
                duration_ms REAL,
                span_name TEXT,
                warmup INTEGER
            )
            """
        )
        for duration_ms, span_name, warmup in rows:
            conn.execute(
                "INSERT INTO spans (duration_ms, span_name, warmup) VALUES (?, ?, ?)",
                (duration_ms, span_name, warmup),
            )
        conn.commit()
        conn.close()

    old_db = tmp_path / "old.db"
    new_db = tmp_path / "new.db"
    make_db(
        old_db,
        [
            (10.0, "span-a", 0),
            (12.0, "span-a", 0),
            (30.0, "span-b", 0),
        ],
    )
    make_db(
        new_db,
        [
            (11.0, "span-a", 0),
            (13.0, "span-a", 0),
            (27.0, "span-b", 0),
        ],
    )

    out_path = tmp_path / "report.json"
    res = runner.invoke(
        app,
        [
            "eval",
            "-o",
            str(old_db),
            "-n",
            str(new_db),
            "--out",
            str(out_path),
            "--bootstrap",
            "200",
        ],
        env={},
    )

    assert res.exit_code == 0, res.stdout
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert "per_span" in report
    assert set(report["per_span"].keys()) == {"span-a", "span-b"}
    assert "overall" in report
    assert "deltas" in report["per_span"]["span-a"]
