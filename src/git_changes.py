from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    from git import GitCommandError, InvalidGitRepositoryError, NoSuchPathError, Repo  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    Repo = None  # type: ignore
    GitCommandError = Exception  # type: ignore
    InvalidGitRepositoryError = Exception  # type: ignore
    NoSuchPathError = Exception  # type: ignore


class GitChangesError(RuntimeError):
    """Raised when real git changes cannot be obtained."""


@dataclass(frozen=True)
class FileChange:
    path: str
    change_type: str
    summary: str | None = None
    diff_unified: str | None = None
    old_path: str | None = None  # for renames


@dataclass(frozen=True)
class ChangeSet:
    base: str
    head: str
    files: list[FileChange]


def _looks_like_remote(repo: str) -> bool:
    if "://" in repo:
        return True
    if repo.startswith("git@"):
        return True
    # scp-like syntax: user@host:org/repo.git
    if ":" in repo and "@" in repo.split(":", 1)[0]:
        return True
    return False


def _hash_key(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def _inject_https_token(url: str, token: str) -> str:
    """
    Inject a token into an https URL without logging it.

    - https://github.com/org/repo.git -> https://<token>@github.com/org/repo.git
    - If username/password already present, we keep it as-is.
    """
    if not token:
        return url
    if not url.startswith("https://"):
        return url
    rest = url[len("https://") :]
    if "@" in rest.split("/", 1)[0]:
        return url
    return f"https://{token}@{rest}"


def _ensure_repo_from_source(
    repo_source: str,
    *,
    cache_dir: Path,
    token_env: str | None,
) -> tuple["Repo", Path]:
    if Repo is None:  # pragma: no cover
        raise GitChangesError("gitpython is required. Install dependencies (e.g. `uv sync`).")

    if not _looks_like_remote(repo_source):
        p = Path(repo_source).expanduser().resolve()
        try:
            return Repo(p.as_posix()), p
        except (InvalidGitRepositoryError, NoSuchPathError) as e:
            raise GitChangesError(f"Not a git repository: {p}") from e

    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / _hash_key(repo_source)

    token = os.environ.get(token_env) if token_env else None
    url = _inject_https_token(repo_source, token or "")

    if target.exists():
        repo = Repo(target.as_posix())
        try:
            repo.remote().fetch(prune=True)
        except Exception:
            # If remote is not set up as expected, re-clone.
            # We avoid deleting here; caller can clear cache if needed.
            pass
        return repo, target

    try:
        repo = Repo.clone_from(url, target.as_posix())
    except GitCommandError as e:
        hint = (
            f"Failed to clone remote repository. "
            f"If authentication is required, set {token_env}=... in your environment."
            if token_env
            else "Failed to clone remote repository."
        )
        raise GitChangesError(hint) from e

    return repo, target


def open_repo(
    *,
    repo_source: str,
    cache_dir: Path,
    token_env: str | None,
) -> tuple["Repo", Path]:
    """
    Open a repo from a local path or clone/fetch a remote into cache_dir.
    Returns (repo, repo_path).
    """

    return _ensure_repo_from_source(repo_source, cache_dir=cache_dir, token_env=token_env)


def read_file_at_ref(repo: "Repo", ref: str, path: str) -> str:
    """
    Read file content from the given git ref (commit-ish).
    """

    try:
        return repo.git.show(f"{ref}:{path}")
    except Exception as e:
        raise GitChangesError(f"Unable to read {path} at {ref}") from e


def _parse_name_status(lines: Iterable[str]) -> list[FileChange]:
    out: list[FileChange] = []
    for raw in lines:
        line = raw.strip("\n")
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith("R") and len(parts) >= 3:
            old, new = parts[1], parts[2]
            out.append(FileChange(path=new, old_path=old, change_type="renamed"))
        elif status.startswith("C") and len(parts) >= 3:
            old, new = parts[1], parts[2]
            out.append(FileChange(path=new, old_path=old, change_type="copied"))
        elif len(parts) >= 2:
            path = parts[1]
            mapping = {"A": "added", "M": "modified", "D": "deleted", "T": "typechange"}
            out.append(FileChange(path=path, change_type=mapping.get(status, status)))
        else:
            out.append(FileChange(path=line, change_type="modified"))
    return out


def get_changes(
    *,
    repo_source: str,
    base_ref: str,
    head_ref: str,
    cache_dir: Path,
    token_env: str | None,
) -> ChangeSet:
    repo, _repo_path = _ensure_repo_from_source(repo_source, cache_dir=cache_dir, token_env=token_env)

    try:
        _ = repo.commit(base_ref)
        _ = repo.commit(head_ref)
    except Exception as e:
        raise GitChangesError(f"Unable to resolve base/head refs: {base_ref}..{head_ref}") from e

    try:
        name_status = repo.git.diff("--name-status", base_ref, head_ref)
    except GitCommandError as e:
        raise GitChangesError(f"Failed to compute diff name-status: {base_ref}..{head_ref}") from e

    changes = _parse_name_status(name_status.splitlines())

    # Attach unified diff per file (bounded context; adjustable later).
    enriched: list[FileChange] = []
    for ch in changes:
        try:
            diff = repo.git.diff(base_ref, head_ref, "--", ch.path, unified=200)
        except GitCommandError:
            diff = None
        summary = ch.summary
        if summary is None and ch.old_path and ch.change_type in {"renamed", "copied"}:
            summary = f"{ch.change_type}: {ch.old_path} -> {ch.path}"
        enriched.append(
            FileChange(
                path=ch.path,
                change_type=ch.change_type,
                summary=summary,
                diff_unified=diff,
                old_path=ch.old_path,
            )
        )

    return ChangeSet(base=base_ref, head=head_ref, files=enriched)

