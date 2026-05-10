"""PRD decomposition orchestration."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from orch import state as orch_state
from orch.config import Config

RunCmd = Callable[[list[str], Path | None], subprocess.CompletedProcess[str]]
LaunchTUI = Callable[[Path], int]
WarnFn = Callable[[str], None]
InfoFn = Callable[[str], None]

DECOMPOSE_DISPATCH_FILE = "ORCH_DECOMPOSE.md"


def _default_run_cmd(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run a command and return the completed process."""
    return subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd) if cwd else None)


def _default_launch_tui(repo_root: Path) -> int:
    """Launch opencode TUI interactively with inherited stdio for Q&A sessions."""
    result = subprocess.run(["opencode", str(repo_root)], cwd=str(repo_root))
    return result.returncode


def extract_prd_title(content: str) -> str:
    """Extract the first H1 heading from PRD markdown."""
    match = re.search(r"^#\s+(.+?)\s*$", content, flags=re.MULTILINE)
    if match is None:
        msg = "PRD must include an H1 heading for the GitHub issue title."
        raise ValueError(msg)
    return match.group(1).strip()


def _extract_issue_number(output: str) -> int:
    """Extract the GitHub issue number from gh output."""
    match = re.search(r"/issues/(\d+)", output)
    if match is None:
        msg = f"Could not determine GitHub issue number from output: {output!r}"
        raise RuntimeError(msg)
    return int(match.group(1))


def _build_dispatch_payload(prd_body: str, issue_id: int, feature_branch: str) -> str:
    """Build the prompt payload for the decomposer agent."""
    return (
        "DECOMPOSER DISPATCH\n\n"
        f"Issue ID: {issue_id}\n"
        f"Feature branch: {feature_branch}\n\n"
        "When creating tickets, pass `issue_id` on every `ticket-create` call so all "
        "tickets are linked to this PRD issue.\n\n"
        "PRD:\n\n"
        f"{prd_body}"
    )


def _resolve_labels(
    run: RunCmd, repo_root: Path, configured_labels: list[str], warn: WarnFn | None = None
) -> list[str]:
    """Return only labels that exist on the repo, warning on missing ones."""
    if not configured_labels:
        return []

    result = run(["gh", "label", "list", "--json", "name"], repo_root)
    if result.returncode != 0:
        return configured_labels

    existing = {item["name"] for item in json.loads(result.stdout or "[]")}
    valid: list[str] = []
    for label in configured_labels:
        if label in existing:
            valid.append(label)
        elif warn is not None:
            warn(f"GitHub label '{label}' does not exist on this repo; skipping it.")
    return valid


def _remote_branch_exists(run: RunCmd, repo_root: Path, branch: str) -> bool:
    """Return True when the branch already exists on origin."""
    result = run(["git", "ls-remote", "--exit-code", "--heads", "origin", branch], repo_root)
    return result.returncode == 0


def run_decompose(
    repo_root: Path,
    prd_path: Path,
    *,
    issue_id: int | None = None,
    no_active: bool = False,
    run_cmd: RunCmd | None = None,
    launch_tui: LaunchTUI | None = None,
    confirm_overwrite: Callable[[int], bool] | None = None,
    warn: WarnFn | None = None,
    info: InfoFn | None = None,
) -> int:
    """Run the PRD-to-decomposer workflow and return the GitHub issue number."""
    run = run_cmd or _default_run_cmd
    _launch = launch_tui or _default_launch_tui
    cfg = Config.load(repo_root=repo_root)
    prd_body = prd_path.read_text()
    title = extract_prd_title(prd_body)
    overwrite_issue = issue_id is not None

    if issue_id is None:
        labels = _resolve_labels(run, repo_root, cfg.github.prd_labels, warn)
        cmd = ["gh", "issue", "create", "--title", title, "--body-file", str(prd_path)]
        for label in labels:
            cmd.extend(["--label", label])
        if info is not None:
            info(f"Creating GitHub issue: {title!r}...")
        result = run(cmd, repo_root)
        if result.returncode != 0:
            msg = result.stderr.strip() or "gh issue create failed"
            raise RuntimeError(msg)
        issue_id = _extract_issue_number(result.stdout.strip())
        if info is not None:
            info(f"✓ Issue #{issue_id} created")
    else:
        confirm = confirm_overwrite or (lambda _issue_id: True)
        if not confirm(issue_id):
            raise RuntimeError("Aborted.")
        result = run(
            ["gh", "issue", "edit", str(issue_id), "--body-file", str(prd_path)], repo_root
        )
        if result.returncode != 0:
            msg = result.stderr.strip() or "gh issue edit failed"
            raise RuntimeError(msg)
        if info is not None:
            info(f"✓ Issue #{issue_id} updated")

    feature_branch = f"issue-{issue_id}"

    if overwrite_issue and _remote_branch_exists(run, repo_root, feature_branch):
        if info is not None:
            info(f"✓ Remote branch {feature_branch} already exists; reusing it.")
    else:
        if info is not None:
            info(f"Creating feature branch {feature_branch!r}...")
        result = run(["git", "checkout", "-b", feature_branch], repo_root)
        if result.returncode != 0:
            msg = result.stderr.strip() or "git checkout failed"
            raise RuntimeError(msg)

        if info is not None:
            info(f"Pushing {feature_branch!r} to origin...")
        result = run(["git", "push", "-u", "origin", feature_branch], repo_root)
        if result.returncode != 0:
            msg = result.stderr.strip() or "git push failed"
            raise RuntimeError(msg)
        if info is not None:
            info(f"✓ Feature branch {feature_branch!r} pushed")

    if not no_active:
        orch_state.write_active_issue(repo_root, issue_id, base_dir=cfg.state.base_dir)

    payload = _build_dispatch_payload(prd_body, issue_id, feature_branch)
    dispatch_file = repo_root / DECOMPOSE_DISPATCH_FILE
    dispatch_file.write_text(payload)
    if info is not None:
        info(f"✓ Dispatch payload written to {DECOMPOSE_DISPATCH_FILE}")
        info("")
        info("Launching decomposer (interactive Q&A session).")
        info(f"  In opencode, select the 'decomposer' agent and read {DECOMPOSE_DISPATCH_FILE}.")
        info("")

    exit_code = _launch(repo_root)
    if exit_code != 0:
        msg = f"opencode exited with code {exit_code}"
        raise RuntimeError(msg)

    return issue_id
