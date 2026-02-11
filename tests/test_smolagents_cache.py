from __future__ import annotations

from pathlib import Path

from src.git_changes import get_changes
from src.perf_instrument_pipeline import plan_perf_markers_with_smolagents
from src.smolagents_analysis import PerfMarkerPlan


def test_smolagents_plan_is_cached(tmp_path: Path, monkeypatch) -> None:
    # Create a tiny git repo
    import git

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    repo = git.Repo.init(repo_dir)
    (repo_dir / "src").mkdir()
    (repo_dir / "src" / "a.ts").write_text("export const a = 1;\n", encoding="utf-8")
    repo.index.add(["src/a.ts"])
    repo.index.commit("v1")
    (repo_dir / "src" / "a.ts").write_text("export const a = 2;\n", encoding="utf-8")
    repo.index.add(["src/a.ts"])
    repo.index.commit("v2")

    changes = get_changes(
        repo_source=str(repo_dir),
        base_ref="HEAD~1",
        head_ref="HEAD",
        cache_dir=tmp_path / ".otel-agent" / "repos",
        token_env=None,
    )

    calls = {"n": 0}

    def fake_analyze(*, model_id: str, changes: list[dict], repo_context_hint: str):
        calls["n"] += 1
        # Minimal dual-anchor marker plan
        return [
            PerfMarkerPlan(
                path="src/a.ts",
                insert_after_new="export const a = 2;",
                label="a",
                reason="test",
                occurrence_new=1,
                insert_after_old="export const a = 1;",
                occurrence_old=1,
                severity="low",
            )
        ]

    # Patch the underlying analyzer called by the pipeline.
    monkeypatch.setattr("src.perf_instrument_pipeline.analyze_changes_for_perf_markers", fake_analyze)

    cache_dir = tmp_path / ".otel-agent" / "repos"
    p1 = plan_perf_markers_with_smolagents(
        model_id="test-model",
        changeset=changes,
        repo_context_hint="x",
        repo_source=str(repo_dir),
        base_ref="HEAD~1",
        head_ref="HEAD",
        cache_dir=cache_dir,
        token_env=None,
    )
    p2 = plan_perf_markers_with_smolagents(
        model_id="test-model",
        changeset=changes,
        repo_context_hint="x",
        repo_source=str(repo_dir),
        base_ref="HEAD~1",
        head_ref="HEAD",
        cache_dir=cache_dir,
        token_env=None,
    )

    assert calls["n"] == 1
    assert len(p1) == 1
    assert len(p2) == 1
    assert p1[0].insert_after_old == "export const a = 1;"
    assert p2[0].insert_after_new == "export const a = 2;"

