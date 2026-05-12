"""Worktree management for worktrees stored in the orch state directory."""

import asyncio
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from orch.config import Config
from orch.db import Database
from orch.state import resolve_state_dir
from orch.tickets import get_ticket

CheckFn = Callable[[Path], Awaitable[bool]]
RemoveFn = Callable[[Path], Awaitable[None]]


@dataclass
class WorktreeInfo:
    ticket_id: str
    path: Path
    state: str | None


@dataclass
class PruneResult:
    pruned: int = 0
    skipped: int = 0
    pruned_ids: list[str] = field(default_factory=list)
    skipped_ids: list[str] = field(default_factory=list)


async def list_worktrees(db: Database, repo_root: Path) -> list[WorktreeInfo]:
    """Scan the state-dir worktrees tree and return info for each ticket worktree."""
    config = Config.load(repo_root=repo_root)
    state_dir = resolve_state_dir(repo_root, base_dir=config.state.base_dir)
    worktrees_dir = state_dir / "worktrees"
    if not worktrees_dir.is_dir():
        return []

    infos: list[WorktreeInfo] = []
    for issue_dir in sorted(worktrees_dir.iterdir()):
        if not issue_dir.is_dir():
            continue
        for child in sorted(issue_dir.iterdir()):
            if not child.is_dir():
                continue
            ticket_id = child.name
            ticket = await get_ticket(db, ticket_id)
            state = ticket.state if ticket else None
            infos.append(WorktreeInfo(ticket_id=ticket_id, path=child, state=state))
    return infos


async def prune_worktrees(
    db: Database,
    repo_root: Path,
    *,
    check_dirty: CheckFn | None = None,
    check_unpushed: CheckFn | None = None,
    remove_worktree: RemoveFn | None = None,
) -> PruneResult:
    """Prune worktrees for Done tickets. Skips dirty or unpushed worktrees."""
    check_dirty = check_dirty or _default_check_dirty
    check_unpushed = check_unpushed or _default_check_unpushed
    remove_worktree = remove_worktree or _default_remove_worktree

    infos = await list_worktrees(db, repo_root)
    result = PruneResult()

    for info in infos:
        if info.state != "Done":
            result.skipped += 1
            result.skipped_ids.append(info.ticket_id)
            continue

        if await check_dirty(info.path):
            result.skipped += 1
            result.skipped_ids.append(info.ticket_id)
            continue

        if await check_unpushed(info.path):
            result.skipped += 1
            result.skipped_ids.append(info.ticket_id)
            continue

        await remove_worktree(info.path)
        result.pruned += 1
        result.pruned_ids.append(info.ticket_id)

    return result


async def _default_check_dirty(path: Path) -> bool:
    """Check if a worktree has uncommitted changes."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(path),
        "status",
        "--porcelain",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return len(stdout.strip()) > 0


async def _default_check_unpushed(path: Path) -> bool:
    """Check if a worktree has unpushed commits."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(path),
        "log",
        "--oneline",
        "@{u}..HEAD",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    # If the command fails (no upstream), treat as unpushed
    if proc.returncode != 0:
        return True
    return len(stdout.strip()) > 0


async def _default_remove_worktree(path: Path) -> None:
    """Remove a worktree via git worktree remove."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "worktree",
        "remove",
        str(path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    await proc.communicate()
    if proc.returncode != 0:
        msg = f"Failed to remove worktree at {path}"
        raise RuntimeError(msg)
