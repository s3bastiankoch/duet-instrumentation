from __future__ import annotations

from pathlib import Path

import pytest

from src.apply_changes import (
    ApplyChangesError,
    apply_diff_to_file,
    prepare_versions,
)
from src.mock_git import MockGitChanges, MockGitFileChange


def test_apply_diff_to_file_simple() -> None:
    original = """line 1
line 2
line 3
line 4
"""
    diff = """--- a/test.txt
+++ b/test.txt
@@ -1,4 +1,4 @@
 line 1
-line 2
+line 2 modified
 line 3
 line 4
"""
    result = apply_diff_to_file(original, diff)
    assert "line 2 modified" in result
    assert "line 1" in result
    assert "line 3" in result


def test_apply_diff_to_file_add_lines() -> None:
    original = """export const x = 1;
console.log("hello");
"""
    diff = """--- a/src/app.ts
+++ b/src/app.ts
@@ -1,2 +1,3 @@
 export const x = 1;
+export const y = 2;
 console.log("hello");
"""
    result = apply_diff_to_file(original, diff)
    assert "export const y = 2;" in result
    assert "export const x = 1;" in result


def test_apply_diff_to_file_modify_lines() -> None:
    original = """export const x = 1;
console.log("hello");
"""
    diff = """--- a/src/app.ts
+++ b/src/app.ts
@@ -1,2 +1,2 @@
-export const x = 1;
+export const x = 2;
 console.log("hello");
"""
    result = apply_diff_to_file(original, diff)
    assert "export const x = 2;" in result
    assert "export const x = 1;" not in result


def test_prepare_versions_creates_directories(tmp_path: Path) -> None:
    # Create app folder
    app_folder = tmp_path / "app"
    app_folder.mkdir()
    (app_folder / "src").mkdir()
    (app_folder / "src" / "app.ts").write_text("export const x = 1;\n", encoding="utf-8")

    # Create mock changes
    changes = MockGitChanges(
        base="v1",
        head="v2",
        files=[
            MockGitFileChange(
                path="src/app.ts",
                change_type="modified",
                summary="Change x to 2",
                diff_unified="""--- a/src/app.ts
+++ b/src/app.ts
@@ -1 +1 @@
-export const x = 1;
+export const x = 2;
""",
            )
        ],
    )

    working_dir = tmp_path / "working"

    result = prepare_versions(
        app_folder=app_folder,
        changes=changes,
        working_dir=working_dir,
    )

    assert result.base_dir.exists()
    assert result.head_dir.exists()
    assert result.changes_applied == 1
    assert result.changes_skipped == 0

    # Check base version unchanged
    base_content = (result.base_dir / "src" / "app.ts").read_text()
    assert "export const x = 1;" in base_content

    # Check head version has changes applied
    head_content = (result.head_dir / "src" / "app.ts").read_text()
    assert "export const x = 2;" in head_content


def test_prepare_versions_handles_added_files(tmp_path: Path) -> None:
    # Create app folder
    app_folder = tmp_path / "app"
    app_folder.mkdir()
    (app_folder / "src").mkdir()
    (app_folder / "src" / "app.ts").write_text("export const x = 1;\n", encoding="utf-8")

    # Create mock changes with an added file
    changes = MockGitChanges(
        base="v1",
        head="v2",
        files=[
            MockGitFileChange(
                path="src/new-file.ts",
                change_type="added",
                summary="Add new file",
                diff_unified="""--- /dev/null
+++ b/src/new-file.ts
@@ -0,0 +1,2 @@
+export const y = 2;
+console.log("new file");
""",
            )
        ],
    )

    working_dir = tmp_path / "working"

    result = prepare_versions(
        app_folder=app_folder,
        changes=changes,
        working_dir=working_dir,
    )

    assert result.changes_applied == 1

    # Check new file exists in head version
    new_file = result.head_dir / "src" / "new-file.ts"
    assert new_file.exists()
    content = new_file.read_text()
    assert "export const y = 2;" in content


def test_prepare_versions_app_folder_not_found(tmp_path: Path) -> None:
    changes = MockGitChanges(base="v1", head="v2", files=[])
    working_dir = tmp_path / "working"

    with pytest.raises(ApplyChangesError, match="Application folder not found"):
        prepare_versions(
            app_folder=tmp_path / "nonexistent",
            changes=changes,
            working_dir=working_dir,
        )


def test_prepare_versions_ignores_node_modules(tmp_path: Path) -> None:
    # Create app folder with node_modules
    app_folder = tmp_path / "app"
    app_folder.mkdir()
    (app_folder / "src").mkdir()
    (app_folder / "src" / "app.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (app_folder / "node_modules").mkdir()
    (app_folder / "node_modules" / "some-lib").mkdir()
    (app_folder / "node_modules" / "some-lib" / "index.js").write_text("module.exports = {}", encoding="utf-8")

    changes = MockGitChanges(base="v1", head="v2", files=[])
    working_dir = tmp_path / "working"

    result = prepare_versions(
        app_folder=app_folder,
        changes=changes,
        working_dir=working_dir,
    )

    # node_modules should not be copied
    assert not (result.base_dir / "node_modules").exists()
    assert not (result.head_dir / "node_modules").exists()
    # But other files should be
    assert (result.base_dir / "src" / "app.ts").exists()

