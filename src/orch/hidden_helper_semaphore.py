"""Per-helper-type async semaphores that bound hidden-helper concurrency.

The semaphore pool is owned by the Router at the harness layer — separate from
the BackendAllocator which governs coder backend selection. When an
Implementation coordinator wants to delegate to a hidden helper
(patch-reviewer or codebase-scout) it must acquire a slot from the pool first.
The pool prevents helper sessions from being started unboundedly when many
parallel coder dispatches are running.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orch.config import AgentsConfig

_HELPER_ROLE_TO_CONFIG_ATTR: dict[str, str] = {
    "patch-reviewer": "patch_reviewer",
    "codebase-scout": "codebase_scout",
}


class HiddenHelperSemaphorePool:
    """Manages per-helper-type asyncio semaphores.

    Limits the number of concurrent hidden-helper sessions across all
    parallel coder dispatches. Helper roles that are not configured in the
    pool pass through without blocking.

    In-flight tracking (``wait_for_in_flight``, ``in_flight_count``) enables
    graceful router stop to drain active sessions before hard-cancelling
    dispatch tasks.
    """

    def __init__(self, max_concurrent: dict[str, int]) -> None:
        self._max_concurrent: dict[str, int] = dict(max_concurrent)
        self._semaphores: dict[str, asyncio.Semaphore] = {
            helper: asyncio.Semaphore(limit)
            for helper, limit in max_concurrent.items()
        }
        self._in_flight: set[asyncio.Future[None]] = set()
        self._in_flight_counts: dict[str, int] = {}

    # ── Factory ─────────────────────────────────────────────────────────────

    @classmethod
    def from_config(cls, agents_config: AgentsConfig) -> HiddenHelperSemaphorePool:
        """Build the pool from an AgentsConfig, reading max_concurrent per helper."""
        max_concurrent: dict[str, int] = {}
        for role, attr in _HELPER_ROLE_TO_CONFIG_ATTR.items():
            helper_cfg = getattr(agents_config, attr, None)
            if helper_cfg is not None and hasattr(helper_cfg, "max_concurrent"):
                max_concurrent[role] = helper_cfg.max_concurrent
        return cls(max_concurrent)

    # ── Public API ───────────────────────────────────────────────────────────

    @asynccontextmanager
    async def acquire(self, helper_role: str) -> AsyncIterator[None]:
        """Async context manager that blocks until a slot is available.

        If ``helper_role`` is not configured in the pool, the context manager
        yields immediately (no blocking). This lets leaf-coder and any future
        helpers pass through without configuration.

        While the caller holds the context, a future is registered in the
        in-flight set so ``wait_for_in_flight`` can await it during shutdown.
        """
        sem = self._semaphores.get(helper_role)
        if sem is None:
            yield
            return
        async with sem:
            fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._in_flight.add(fut)
            self._in_flight_counts[helper_role] = (
                self._in_flight_counts.get(helper_role, 0) + 1
            )
            try:
                yield
            finally:
                if not fut.done():
                    fut.set_result(None)
                self._in_flight.discard(fut)
                self._in_flight_counts[helper_role] = max(
                    0, self._in_flight_counts.get(helper_role, 0) - 1
                )

    def limit_for(self, helper_role: str) -> int | None:
        """Return the configured concurrency limit for a helper role, or None."""
        return self._max_concurrent.get(helper_role)

    def waiting_count(self, helper_role: str) -> int:
        """Return the number of tasks currently waiting for a slot.

        Uses the semaphore's internal ``_waiters`` deque. Returns 0 if the
        role is not in the pool or no waiters are queued.
        """
        sem = self._semaphores.get(helper_role)
        if sem is None:
            return 0
        waiters = getattr(sem, "_waiters", None)
        if waiters is None:
            return 0
        return len(waiters)

    def in_flight_count(self, helper_role: str) -> int:
        """Return the number of sessions currently executing for a helper role."""
        return self._in_flight_counts.get(helper_role, 0)

    async def wait_for_in_flight(self) -> None:
        """Wait for all currently executing helper sessions to complete.

        Returns immediately when no sessions are in flight. Used by graceful
        router stop to drain helpers before hard-cancelling dispatch tasks.
        """
        in_flight = list(self._in_flight)
        if not in_flight:
            return
        await asyncio.gather(*in_flight, return_exceptions=True)
