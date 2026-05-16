"""Deterministic handoff gates for ticket state transitions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select

from orch.db import Database, SubtaskContext
from orch.tickets import get_ticket, get_ticket_comments, list_events

# Regex patterns that flag a file path as belonging to a sensitive category.
# Sensitive categories: configuration, prompts, database/migrations, router
# behavior, workflow state, pull request handling, subprocess execution,
# authentication, security, persistence.
_SENSITIVE_PATH_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p)
    for p in [
        r"config",
        r"\.toml$",
        r"\.yaml$",
        r"\.yml$",
        r"prompt",
        r"migration",
        r"alembic",
        r"\.sql$",
        r"router",
        r"workflow",
        r"pull.?request",
        r"\bpr\b",
        r"subprocess",
        r"\bauth\b",
        r"security",
        r"persist",
        r"/db\.py$",
        r"database",
        r"models\.py$",
    ]
]

# Keywords in rework comments that indicate an architecture/behavior/coverage
# concern and force patch-review on the next attempt.
_REWORK_CONCERN_KEYWORDS = (
    "architecture",
    "behavior",
    "coverage",
)


def _is_sensitive_path(path: str) -> bool:
    """Return True if the file path falls into a sensitive category."""
    lower = path.lower()
    return any(p.search(lower) for p in _SENSITIVE_PATH_PATTERNS)


@dataclass
class PatchReviewContext:
    """Inputs for the patch-review handoff gate decision."""

    risk_score: int | None
    changed_file_count: int
    changed_file_paths: list[str] = field(default_factory=list)
    leaf_coder_used: bool = False
    rework_loop_count: int = 0
    has_current_rework_concern: bool = False
    has_weakened_tests: bool = False
    first_pass_validation_success: bool = True
    # Non-empty string means a verdict exists for the current attempt.
    patch_reviewer_verdict: str | None = None


@dataclass
class GateDecision:
    """Result of a handoff gate check."""

    allowed: bool
    reason: str | None = None
    message: str | None = None


def is_trivial_diff(ctx: PatchReviewContext) -> bool:
    """Return True only when ALL trivial-skip conditions hold.

    A diff is trivial only when:
    - risk score is 1 or 2 (low risk)
    - exactly one changed file
    - the changed file is not in a sensitive path category
    - tests were not weakened
    - Leaf coder was not used
    - no current rework concern (architecture / behavior / coverage)
    - first-pass dispatch (rework_loop_count == 0)
    - first-pass validation succeeded
    """
    if ctx.risk_score is None or ctx.risk_score > 2:
        return False
    if ctx.changed_file_count != 1:
        return False
    if any(_is_sensitive_path(p) for p in ctx.changed_file_paths):
        return False
    if ctx.has_weakened_tests:
        return False
    if ctx.leaf_coder_used:
        return False
    if ctx.rework_loop_count > 0:
        return False
    if not ctx.first_pass_validation_success:
        return False
    return not ctx.has_current_rework_concern


_BLOCKED_MESSAGE = "\n".join(
    [
        "## ROUTER_GATE: PATCH_REVIEW_REQUIRED",
        "",
        "**Result:** Patch-reviewer output is required before this ticket can"
        " move to `Code Review`.",
        "**Action:** Run patch-reviewer, record the delegation output via"
        " `delegation-record`, and retry the `Code Review` transition.",
    ]
)


def check_patch_review_handoff_gate(ctx: PatchReviewContext) -> GateDecision:
    """Check whether a ticket may proceed from Code Review to reviewer dispatch.

    Returns ``GateDecision(allowed=True)`` when:
    - A non-empty patch-reviewer verdict exists for the current dispatch attempt, OR
    - The diff qualifies as trivial (see ``is_trivial_diff``).

    Returns ``GateDecision(allowed=False, message=...)`` with an actionable
    message otherwise.
    """
    if ctx.patch_reviewer_verdict:
        return GateDecision(allowed=True)

    if is_trivial_diff(ctx):
        return GateDecision(allowed=True, reason="trivial_diff")

    return GateDecision(
        allowed=False,
        reason="patch_review_required",
        message=_BLOCKED_MESSAGE,
    )


async def build_patch_review_context(db: Database, ticket_id: str) -> PatchReviewContext:
    """Build a PatchReviewContext from the database for the given ticket.

    The "current dispatch attempt" is defined as the period since the most
    recent ``In Progress`` event for this ticket.  Delegation outputs
    (patch-reviewer, leaf-coder) recorded before that event do not count.
    """
    ticket = await get_ticket(db, ticket_id)
    if ticket is None:
        msg = f"Ticket '{ticket_id}' not found."
        raise ValueError(msg)

    # Determine start of current attempt: timestamp of latest In Progress event.
    events = await list_events(db, ticket_id=ticket_id)
    current_attempt_start: str | None = None
    for ev in reversed(events):
        if ev.new_state == "In Progress":
            current_attempt_start = ev.timestamp
            break

    # Parse file_paths (newline-separated).
    raw_paths = (ticket.file_paths or "").strip()
    if raw_paths:
        changed_file_paths = [p.strip() for p in raw_paths.splitlines() if p.strip()]
    else:
        changed_file_paths = []
    changed_file_count = len(changed_file_paths)

    # Load delegation outputs scoped to current attempt.
    async with db.session() as session:
        stmt = (
            select(SubtaskContext)
            .where(
                SubtaskContext.ticket_id == ticket_id,
                SubtaskContext.step.in_(["delegation:patch-reviewer", "delegation:leaf-coder"]),
            )
            .order_by(SubtaskContext.id)
        )
        result = await session.execute(stmt)
        all_delegations = list(result.scalars().all())

    patch_reviewer_verdict: str | None = None
    leaf_coder_used = False

    for d in all_delegations:
        # Skip outputs that predate the current attempt start.
        if current_attempt_start and d.created_at <= current_attempt_start:
            continue
        if d.step == "delegation:patch-reviewer":
            patch_reviewer_verdict = d.output or None
        elif d.step == "delegation:leaf-coder":
            leaf_coder_used = True

    # Detect rework concerns, weakened tests, and validation failures from comments.
    comments = await get_ticket_comments(db, ticket_id)
    has_current_rework_concern = _has_rework_concern(comments)
    relevant_comments = _comments_at_or_after(comments, current_attempt_start)
    has_weakened_tests = _has_weakened_tests(relevant_comments)
    first_pass_validation_success = not _has_validation_failure(relevant_comments)

    return PatchReviewContext(
        risk_score=ticket.risk_score,
        changed_file_count=changed_file_count,
        changed_file_paths=changed_file_paths,
        leaf_coder_used=leaf_coder_used,
        rework_loop_count=ticket.rework_loop_count,
        has_current_rework_concern=has_current_rework_concern,
        has_weakened_tests=has_weakened_tests,
        first_pass_validation_success=first_pass_validation_success,
        patch_reviewer_verdict=patch_reviewer_verdict,
    )


def _has_rework_concern(comments: list) -> bool:
    """Return True if any comment contains architecture/behavior/coverage keywords."""
    for comment in comments:
        body_lower = (comment.body or "").lower()
        if any(kw in body_lower for kw in _REWORK_CONCERN_KEYWORDS):
            return True
    return False


def _comments_at_or_after(comments: list, timestamp: str | None) -> list:
    if not timestamp:
        return comments
    return [comment for comment in comments if getattr(comment, "created_at", "") >= timestamp]


def _has_weakened_tests(comments: list) -> bool:
    keywords = (
        "weakened test",
        "weakened tests",
        "removed test",
        "deleted test",
        "disabled test",
        "skipped test",
        "skip test",
        "xfail",
        "coverage regression",
    )
    for comment in comments:
        body_lower = (comment.body or "").lower()
        if any(keyword in body_lower for keyword in keywords):
            return True
    return False


def _has_validation_failure(comments: list) -> bool:
    failure_markers = (
        "## router_gate: validation_failed",
        "failures present",
        "validation failed",
        "failed:",
    )
    for comment in comments:
        body_lower = (comment.body or "").lower()
        if any(marker in body_lower for marker in failure_markers):
            return True
    return False
