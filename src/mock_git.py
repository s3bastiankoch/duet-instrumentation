from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError


class MockGitError(RuntimeError):
    """Raised when mocked git changes cannot be loaded/validated."""


class MockGitFileChange(BaseModel):
    model_config = ConfigDict(extra="allow")

    path: str
    change_type: str  # added/modified/deleted (kept loose for now)
    summary: str | None = None
    diff_unified: str | None = None


class MockGitChanges(BaseModel):
    model_config = ConfigDict(extra="allow")

    base: str
    head: str
    files: list[MockGitFileChange]


def load_mock_changes(path: Path) -> MockGitChanges:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise MockGitError(f"Mock changes not found: {path}") from e
    except Exception as e:
        raise MockGitError(f"Failed to read mock changes: {path} ({e})") from e

    try:
        return MockGitChanges.model_validate(raw)
    except ValidationError as e:
        raise MockGitError(f"Invalid mock changes file: {path}\n{e}") from e

