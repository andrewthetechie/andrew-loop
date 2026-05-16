"""Backend failure classification for safe router failover."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

FailureClassification = Literal[
    "pre_execution",
    "post_execution",
    "rate_limit",
    "offline",
    "ambiguous",
]

DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 300
DEFAULT_OFFLINE_COOLDOWN_SECONDS = 60
MAX_OFFLINE_COOLDOWN_SECONDS = 900

_OFFLINE_MARKERS = (
    "connection refused",
    "connect: refused",
    "connection reset",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "host is down",
    "no route to host",
    "network is unreachable",
    "econnrefused",
    "econnreset",
    "enotfound",
)


@dataclass(frozen=True)
class AgentRunResult:
    """Normalized router-visible outcome of one agent dispatch attempt."""

    exit_code: int
    completed_steps: int | None = None
    total_tokens: int | None = None
    failure_detail: str = ""
    failure_stage: Literal["pre_execution", "ambiguous"] | None = None
    retry_after_seconds: int | None = None


@dataclass(frozen=True)
class BackendFailureDecision:
    """How router should treat a failed backend attempt."""

    classification: FailureClassification
    safe_to_retry: bool
    cooldown_seconds: int | None = None


def classify_backend_failure(
    *,
    exit_code: int,
    completed_steps: int,
    failure_detail: str = "",
    failure_stage: Literal["pre_execution", "ambiguous"] | None = None,
    retry_after_seconds: int | None = None,
) -> BackendFailureDecision | None:
    """Classify a failed backend attempt for router retry safety."""
    if exit_code == 0:
        return None

    if completed_steps > 0:
        return BackendFailureDecision(classification="post_execution", safe_to_retry=False)

    normalized = failure_detail.lower()
    if _looks_like_rate_limit(normalized):
        return BackendFailureDecision(
            classification="rate_limit",
            safe_to_retry=True,
            cooldown_seconds=retry_after_seconds or _parse_retry_after_seconds(normalized),
        )

    if _looks_like_offline(normalized):
        return BackendFailureDecision(classification="offline", safe_to_retry=True)

    if failure_stage == "pre_execution":
        return BackendFailureDecision(classification="pre_execution", safe_to_retry=True)

    return BackendFailureDecision(classification="ambiguous", safe_to_retry=False)


def offline_cooldown_seconds(consecutive_failures: int) -> int:
    """Return an exponential cooldown for repeated offline failures."""
    exponent = max(consecutive_failures - 1, 0)
    seconds = DEFAULT_OFFLINE_COOLDOWN_SECONDS * (2**exponent)
    return min(seconds, MAX_OFFLINE_COOLDOWN_SECONDS)


def _looks_like_rate_limit(detail: str) -> bool:
    return "429" in detail or "rate limit" in detail or "retry-after" in detail


def _looks_like_offline(detail: str) -> bool:
    return any(marker in detail for marker in _OFFLINE_MARKERS)


def _parse_retry_after_seconds(detail: str) -> int:
    for token in detail.replace("=", " ").replace(":", " ").split():
        if token.isdigit():
            return max(int(token), 1)
    return DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
