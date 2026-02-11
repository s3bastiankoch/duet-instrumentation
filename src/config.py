from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from pydantic import model_validator


class ConfigError(RuntimeError):
    """Raised when config loading/validation fails."""


# Default config file names to search for (in order of priority)
DEFAULT_CONFIG_NAMES = ["otel-agent.yml", "otel-agent.yaml", "config.yml", "config.yaml"]


try:
    import yaml  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    yaml = None


class AppConfig(BaseModel):
    """
    Configuration model for the otel-agent CLI.
    
    All CLI options can be specified in the config file. CLI options take precedence
    over config file values.
    """

    model_config = ConfigDict(extra="allow")

    # === Source: Git mode ===
    # Either a local directory (existing) OR a remote URL to clone/fetch.
    repository_dir: Path | None = None
    repository_url: str | None = None
    
    # Git diff range (commit-ish). Defaults to last commit vs current HEAD.
    base_ref: str = "HEAD~1"
    head_ref: str = "HEAD"

    # Remote auth: read token from env and inject into https URL if needed.
    auth_token_env: str | None = "GIT_TOKEN"

    # Where to clone remote repositories (ignored via .gitignore).
    clone_cache_dir: Path | None = None

    # === Source: JSON mode ===
    # Mock changes file for JSON mode (use with repository_dir as app folder)
    mock_changes_file: Path | None = None

    # === Output ===
    # Working directory for output (default: .otel-agent)
    working_dir: Path | None = None

    # === Instrumentation ===
    # Entrypoint file path relative to app root (e.g., "src/app.ts")
    entrypoint: str | None = None
    
    # Whether to add performance instrumentation (default: True)
    instrument: bool = True
    
    # Model ID for AI analysis
    model: str = "gpt-5.2"
    
    # Skip cache and force re-analysis
    force: bool = False
    
    # Chunk size for smolagent analysis (number of files per chunk)
    # Default is 1 (per-file processing). Set to 0 or None to process all files at once.
    analysis_chunk_size: int = 1
    
    # Number of parallel workers for smolagent analysis
    # Default is 4. Set to 1 for sequential processing.
    analysis_max_workers: int = 4

    # === Duet / experiment compose generation ===
    duet_cpu_limit: str = "2"
    duet_cpu_reservation: str = "1"
    duet_memory_limit: str = "4G"
    duet_base_port: int = 3001
    duet_head_port: int = 3002
    duet_prefix: str = "duet"
    duet_base_cpuset: str | None = None
    duet_head_cpuset: str | None = None
    duet_mongo_cpuset: str | None = None
    duet_separate_mongo: bool = False

    # Where to write the merged experiment compose. If not set, defaults to
    # <working_dir>/docker-compose.experiment.yml.
    experiment_compose_out: Path | None = None

    @field_validator("repository_dir", "working_dir")
    @classmethod
    def _validate_dir_exists(cls, v: Path | None) -> Path | None:
        if v is None:
            return None
        # Don't validate existence for working_dir - it will be created
        return v

    @field_validator("repository_dir")
    @classmethod
    def _validate_repository_dir(cls, v: Path | None) -> Path | None:
        if v is None:
            return None
        if not v.exists():
            raise ValueError(f"repository_dir does not exist: {v}")
        if not v.is_dir():
            raise ValueError(f"repository_dir is not a directory: {v}")
        return v

    @model_validator(mode="after")
    def _validate_repo_source(self) -> "AppConfig":
        if not self.repository_dir and not self.repository_url:
            raise ValueError("Either repository_dir or repository_url must be set")
        return self


def _load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:  # pragma: no cover
        raise ConfigError(
            "PyYAML is required to read config files. Install dependencies (e.g. `uv sync`)."
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"Config not found: {path}") from e
    except Exception as e:
        raise ConfigError(f"Failed to read config: {path} ({e})") from e

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"Config must be a YAML mapping at top-level: {path}")
    return raw


def _resolve_path_fields(data: dict[str, Any], *, base_dir: Path) -> dict[str, Any]:
    """
    Resolve known path-like fields relative to the config file directory.
    """

    out = dict(data)
    for key in (
        "repository_dir",
        "mock_changes_file",
        "clone_cache_dir",
        "working_dir",
        "experiment_compose_out",
    ):
        val = out.get(key)
        if isinstance(val, str) and val.strip():
            p = Path(val).expanduser()
            if not p.is_absolute():
                p = (base_dir / p).resolve()
            out[key] = p
    return out


def find_default_config(search_dir: Path | None = None) -> Path | None:
    """
    Search for a default config file in the given directory (or CWD).
    
    Searches for files in order: otel-agent.yml, otel-agent.yaml, config.yml, config.yaml
    
    Args:
        search_dir: Directory to search in. Defaults to current working directory.
    
    Returns:
        Path to the first config file found, or None if no config file exists.
    """
    if search_dir is None:
        search_dir = Path.cwd()
    
    for name in DEFAULT_CONFIG_NAMES:
        config_path = search_dir / name
        if config_path.exists() and config_path.is_file():
            return config_path
    
    return None


def load_config(config_path: Path) -> AppConfig:
    """
    Load YAML config and validate it.

    - Relative paths are resolved relative to the config file directory.
    - Only repository_dir is required for now.
    """

    data = _load_yaml(config_path)
    data = _resolve_path_fields(data, base_dir=config_path.parent.resolve())

    try:
        return AppConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"Invalid config: {config_path}\n{e}") from e

