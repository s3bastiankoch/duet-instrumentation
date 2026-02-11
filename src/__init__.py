"""otel-agent package (v2 skeleton).

This package is intentionally minimal for now: it only provides a beautiful CLI.
Functionality will be reintroduced incrementally.
"""

from __future__ import annotations

from pathlib import Path
import tomllib
from importlib.metadata import PackageNotFoundError, version


def _pkg_version() -> str:
    try:
        return version("otel-agent")
    except PackageNotFoundError:
        # Fallback for `python -m src ...` without installing the package.
        try:
            repo_root = Path(__file__).resolve().parents[1]
            pyproject = repo_root / "pyproject.toml"
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            return str(data.get("project", {}).get("version", "0.0.0"))
        except Exception:
            return "0.0.0"


__version__ = _pkg_version()
