"""Deterministic dispatch payload pruning.

Trims comment history, caps Hindsight context sections, and caps validation
result output to keep dispatch payloads within tight coder context budgets.

All pruning is rule-based — no LLM calls.  Runs before backend allocator
selection; the result does not depend on which backend is chosen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

HINDSIGHT_MAX_CHARS_PER_SECTION: int = 800
"""Maximum characters retained per Hindsight context section."""

VALIDATION_OUTPUT_MAX_CHARS: int = 600
"""Maximum characters retained for stdout or stderr per validation result."""

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

_ROUTINE_ROUTER_PREFIXES: tuple[str, ...] = (
    "## ROUTER_GATE:",
    "## ROUTER_HARD_CANCEL:",
)

_ROUTINE_ROUTER_SUBSTRINGS: tuple[str, ...] = (
    "Router could not create worktree",
    "Router could not allocate",
    "Router halted dispatch",
    "baseline validation failed",
    "Fix the failing validators",
)

_STALE_APPROVAL_MARKER: str = "## REVIEW_DECISION: APPROVED"
_CHANGES_REQUESTED_MARKER: str = "## REVIEW_DECISION: CHANGES_REQUESTED"


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


@dataclass
class PrunedPayload:
    """Compact structured payload ready for dispatch prompt assembly.

    Attributes:
        ticket: Full ticket data dict (unchanged).
        comments: Filtered comment list with stale/routine entries removed.
        hindsight_context: Memory context sections, capped to budget.
        validation_commands: Commands to run (unchanged; passed through as-is).
        validation_results: Pre-run validation results with stdout/stderr capped.
        truncation_markers: Human-readable list of what was pruned/capped.
    """

    ticket: dict[str, Any]
    comments: list[dict[str, str]]
    hindsight_context: dict[str, str]
    validation_commands: list[str]
    validation_results: list[dict[str, Any]]
    truncation_markers: list[str] = field(default_factory=list)


def prune_dispatch_payload(
    agent_role: str,
    ticket_state: str,  # reserved for future role-aware tuning
    ticket: dict[str, Any],
    comments: list[dict[str, str]],
    hindsight_context: dict[str, str],
    validation_data: dict[str, Any],
) -> PrunedPayload:
    """Deterministically prune and compact the dispatch payload.

    Only the keys ``validation_commands`` and ``validation_results`` are read
    from *validation_data*; all other keys (including any backend failure
    history) are ignored so they never appear in the returned payload.

    Args:
        agent_role: Logical agent type, e.g. "coder", "reviewer", "merger".
        ticket_state: Pre-dispatch ticket state, e.g. "To Do", "Rework".
        ticket: Ticket data dict — returned unchanged.
        comments: Raw comment list, each ``{author, body}``.
        hindsight_context: Memory context keyed by label.
        validation_data: Dict with optional keys ``validation_commands`` and
            ``validation_results``.  Extra keys are silently dropped.

    Returns:
        ``PrunedPayload`` with comments filtered, hindsight capped, and
        validation output capped.  ``truncation_markers`` records every
        pruning action with a visible ``[… N chars truncated]`` note.
    """
    markers: list[str] = []

    # Extract only the keys we care about — strips backend failure history
    # and any other extra data that must not appear in the payload.
    validation_commands: list[str] = list(validation_data.get("validation_commands") or [])
    raw_results: list[dict[str, Any]] = list(validation_data.get("validation_results") or [])

    if agent_role == "coder":
        pruned_comments = _prune_coder_comments(comments, markers, ticket_state)
    elif agent_role == "reviewer":
        pruned_comments = _prune_reviewer_comments(comments, markers)
    else:
        pruned_comments = list(comments)

    pruned_hindsight = _cap_hindsight_context(hindsight_context, markers)
    pruned_results = _cap_validation_results(raw_results, markers)

    return PrunedPayload(
        ticket=dict(ticket),
        comments=pruned_comments,
        hindsight_context=pruned_hindsight,
        validation_commands=validation_commands,
        validation_results=pruned_results,
        truncation_markers=markers,
    )


# ---------------------------------------------------------------------------
# Comment pruning (coder role)
# ---------------------------------------------------------------------------


def _prune_coder_comments(
    comments: list[dict[str, str]],
    markers: list[str],
    ticket_state: str = "To Do",
) -> list[dict[str, str]]:
    """Filter comments for coder dispatch payloads.

    For all states keeps:
    - The *latest* ``coder`` comment only (handoff to next coder run).
    - The *latest* non-routine ``router`` comment (e.g. a step-budget handoff).
    - Non-approval ``reviewer`` comments (e.g. CHANGES_REQUESTED).

    For To Do state keeps:
    - All ``human`` comments (always actionable).

    For Rework state additionally:
    - Identifies a *rework anchor* = latest of (CHANGES_REQUESTED reviewer comment,
      latest actionable router comment).  Human comments before the anchor belong to
      a stale earlier review cycle and are dropped; only humans after the anchor
      are kept.  Earlier CHANGES_REQUESTED cycles are also dropped.

    Drops always:
    - ``REVIEW_DECISION: APPROVED`` entries — stale approvals add noise.
    - ``ROUTER_GATE:`` / ``ROUTER_HARD_CANCEL:`` and similar routine status.
    - Older ``coder`` comments that precede the latest handoff.
    """
    is_rework = ticket_state == "Rework"
    last_coder_idx: int = -1
    last_actionable_router_idx: int = -1
    last_changes_requested_idx: int = -1

    for i, c in enumerate(comments):
        author = c.get("author", "")
        body = c.get("body", "")
        if author == "coder":
            last_coder_idx = i
        if author == "router" and not _is_routine_router_comment(body):
            last_actionable_router_idx = i
        if is_rework and author == "reviewer" and _is_changes_requested(body):
            last_changes_requested_idx = i

    # Anchor = latest comment that initiated the rework cycle.
    # Human comments at or before the anchor belong to a stale earlier cycle.
    rework_anchor_idx: int = -1
    if is_rework:
        rework_anchor_idx = max(last_changes_requested_idx, last_actionable_router_idx)

    kept: list[dict[str, str]] = []
    dropped: int = 0

    for i, comment in enumerate(comments):
        author = comment.get("author", "")
        body = comment.get("body", "")

        if author == "human":
            if is_rework and rework_anchor_idx >= 0 and i <= rework_anchor_idx:
                dropped += 1
            else:
                kept.append(comment)
        elif author == "coder":
            if i == last_coder_idx:
                kept.append(comment)
            else:
                dropped += 1
        elif author == "router":
            if _is_routine_router_comment(body):
                dropped += 1
            elif i == last_actionable_router_idx:
                kept.append(comment)
            else:
                # Older actionable router comment — dropped in favour of latest.
                dropped += 1
        elif author == "reviewer":
            if _is_stale_approval(body):
                dropped += 1
            elif is_rework and _is_changes_requested(body) and i != last_changes_requested_idx:
                # Stale earlier CHANGES_REQUESTED cycle — drop in favour of latest.
                dropped += 1
            else:
                kept.append(comment)
        else:
            kept.append(comment)

    if dropped:
        markers.append(f"[… {dropped} stale or routine comment(s) omitted]")

    return kept


def _is_routine_router_comment(body: str) -> bool:
    for prefix in _ROUTINE_ROUTER_PREFIXES:
        if body.startswith(prefix):
            return True
    return any(sub in body for sub in _ROUTINE_ROUTER_SUBSTRINGS)


def _is_stale_approval(body: str) -> bool:
    return _STALE_APPROVAL_MARKER in body


def _is_changes_requested(body: str) -> bool:
    return _CHANGES_REQUESTED_MARKER in body


def _prune_reviewer_comments(
    comments: list[dict[str, str]],
    markers: list[str],
) -> list[dict[str, str]]:
    """Filter comments for reviewer dispatch payloads.

    Keeps the latest coder implementation summary, all delegation records,
    human comments, and the latest prior review decision for re-review context.
    Drops old coder summaries, stale review decisions, routine router chatter,
    and stale approvals.
    """
    last_coder_idx = -1
    last_review_decision_idx = -1
    for i, c in enumerate(comments):
        author = c.get("author", "")
        body = c.get("body", "")
        if author == "coder":
            last_coder_idx = i
        if author == "reviewer" and body.startswith("## REVIEW_DECISION:"):
            last_review_decision_idx = i

    kept: list[dict[str, str]] = []
    dropped = 0
    for i, comment in enumerate(comments):
        author = comment.get("author", "")
        body = comment.get("body", "")

        if author == "coder":
            if i == last_coder_idx:
                kept.append(comment)
            else:
                dropped += 1
        elif author.startswith("delegation:") or author in {
            "patch-reviewer",
            "codebase-scout",
            "leaf-coder",
        }:
            kept.append(comment)
        elif author == "reviewer":
            if body.startswith("## REVIEW_DECISION:"):
                if i == last_review_decision_idx:
                    kept.append(comment)
                else:
                    dropped += 1
            elif _is_stale_approval(body):
                dropped += 1
            else:
                kept.append(comment)
        elif author == "router" and _is_routine_router_comment(body):
            dropped += 1
        else:
            kept.append(comment)

    if dropped:
        markers.append(f"[… {dropped} stale reviewer-dispatch comment(s) omitted]")

    return kept


# ---------------------------------------------------------------------------
# Hindsight context capping
# ---------------------------------------------------------------------------


def _cap_hindsight_context(
    context: dict[str, str],
    markers: list[str],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for label, content in context.items():
        if len(content) > HINDSIGHT_MAX_CHARS_PER_SECTION:
            n = len(content) - HINDSIGHT_MAX_CHARS_PER_SECTION
            truncation = f"[… {n} chars truncated]"
            result[label] = content[:HINDSIGHT_MAX_CHARS_PER_SECTION] + "\n" + truncation
            markers.append(f"Hindsight '{label}': {truncation}")
        else:
            result[label] = content
    return result


# ---------------------------------------------------------------------------
# Validation result capping
# ---------------------------------------------------------------------------


def _cap_validation_results(
    results: list[dict[str, Any]],
    markers: list[str],
) -> list[dict[str, Any]]:
    capped: list[dict[str, Any]] = []
    for result in results:
        r = dict(result)
        cmd = r.get("command", "?")
        for key in ("stdout", "stderr"):
            val = r.get(key) or ""
            if len(val) > VALIDATION_OUTPUT_MAX_CHARS:
                n = len(val) - VALIDATION_OUTPUT_MAX_CHARS
                truncation = f"[… {n} chars truncated]"
                r[key] = val[:VALIDATION_OUTPUT_MAX_CHARS] + "\n" + truncation
                markers.append(f"Validation '{cmd}' {key}: {truncation}")
        capped.append(r)
    return capped
