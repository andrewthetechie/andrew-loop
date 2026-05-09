"""State directory resolution for orch.

The orch state directory lives outside the target git repo to prevent data loss
from git operations (reset --hard, clean, etc.).

Default layout:
    ~/.local/share/orch/{repo_id}/
        state.db          — SQLite database
        logs/             — per-ticket agent logs
        worktrees/        — git worktrees for each ticket

repo_id is derived from the sanitised git remote URL, e.g.:
    https://github.com/andrewthetechie/jelly-swipe.git
        → github.com-andrewthetechie-jelly-swipe

Override repo_id by placing a `.orchestra-id` file in the repo root
(add it to .gitignore).

The base dir is configurable in ~/.config/orchestra/config.toml:
    [state]
    base_dir = "~/.local/share/orch"
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


_ORCHESTRA_ID_FILE = ".orchestra-id"


def repo_id_from_remote(repo_root: Path) -> str:
    """Derive a directory-safe repo identifier from the git remote URL.

    Falls back to the repo directory name if no remote is configured.
    """
    # Check for explicit override file first
    override = repo_root / _ORCHESTRA_ID_FILE
    if override.is_file():
        oid = override.read_text().strip()
        if oid:
            return oid

    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
        )
        if result.returncode == 0:
            return _sanitize_url(result.stdout.strip())
    except Exception:
        pass

    return repo_root.name


def _sanitize_url(url: str) -> str:
    """Sanitize a git remote URL into a directory-safe string.

    Examples:
        https://github.com/user/repo.git  → github.com-user-repo
        git@github.com:user/repo.git      → github.com-user-repo
    """
    # SSH format: git@github.com:user/repo.git → github.com/user/repo
    url = re.sub(r"^git@([^:]+):", r"\1/", url)
    # Strip https:// or http://
    url = re.sub(r"^https?://", "", url)
    # Strip .git suffix
    url = re.sub(r"\.git$", "", url)
    # Replace separators with hyphens
    url = re.sub(r"[/:@\\]", "-", url)
    # Remove anything not alphanumeric, hyphen, or dot
    url = re.sub(r"[^a-zA-Z0-9\-.]", "", url)
    # Collapse repeated hyphens
    url = re.sub(r"-+", "-", url)
    return url.lower().strip("-")


def resolve_state_dir(repo_root: Path, *, base_dir: str | None = None) -> Path:
    """Return the orch state directory for a repo.

    Args:
        repo_root: Root of the target git repository.
        base_dir: Override the base directory (e.g. from config). Defaults to
                  ~/.local/share/orch if not supplied.
    """
    if base_dir is None:
        base_dir = "~/.local/share/orch"

    base = Path(base_dir).expanduser()
    rid = repo_id_from_remote(repo_root)
    return base / rid


def state_db_path(repo_root: Path, *, base_dir: str | None = None) -> Path:
    """Return the SQLite database path for a repo."""
    return resolve_state_dir(repo_root, base_dir=base_dir) / "state.db"
