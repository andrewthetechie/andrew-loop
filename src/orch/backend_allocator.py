"""Deterministic backend allocation for logical agents."""

from __future__ import annotations

from dataclasses import dataclass

from orch.backend_state import BackendStateStore, BackendStepUsageRecord
from orch.backends import BackendDefinition, fixed_window_start


@dataclass(frozen=True)
class BackendAllocation:
    backend: BackendDefinition
    lease: object
    step_reservation: BackendStepUsageRecord | None
    attempt_id: int | None = None


@dataclass(frozen=True)
class BackendNoCapacity:
    logical_agent: str
    ticket_id: str


class BackendAllocator:
    """Select an eligible backend and persist allocation state."""

    def __init__(self, store: BackendStateStore) -> None:
        self._store = store

    async def allocate(
        self,
        *,
        backends: list[BackendDefinition],
        logical_agent: str,
        ticket_id: str,
        required_steps: int,
        allocated_at: str,
        stale_after: str,
    ) -> BackendAllocation | BackendNoCapacity:
        """Allocate the first eligible backend from the configured list."""
        for backend in sorted(backends, key=lambda backend: backend.priority):
            if not backend.enabled:
                await self._record_skip(
                    ticket_id=ticket_id,
                    logical_agent=logical_agent,
                    backend=backend,
                    skipped_reason="backend disabled",
                    attempted_at=allocated_at,
                )
                continue

            if logical_agent not in backend.logical_agents:
                await self._record_skip(
                    ticket_id=ticket_id,
                    logical_agent=logical_agent,
                    backend=backend,
                    skipped_reason="logical agent unsupported",
                    attempted_at=allocated_at,
                )
                continue

            if await self._store.count_active_leases(backend.id) >= backend.concurrency:
                await self._record_skip(
                    ticket_id=ticket_id,
                    logical_agent=logical_agent,
                    backend=backend,
                    skipped_reason="backend at concurrency capacity",
                    attempted_at=allocated_at,
                )
                continue

            cooldown = await self._store.get_cooldown(backend.id)
            if cooldown is not None and cooldown.cooldown_until > allocated_at:
                await self._record_skip(
                    ticket_id=ticket_id,
                    logical_agent=logical_agent,
                    backend=backend,
                    skipped_reason="backend in cooldown",
                    attempted_at=allocated_at,
                )
                continue

            reserve = backend.min_reserve if backend.min_reserve is not None else required_steps
            requires_step_reserve = backend.quota.mode != "unlimited"
            quota_window_start = fixed_window_start(backend.quota, allocated_at)
            if backend.quota.dispatch_limit is not None:
                recorded_dispatches = await self._store.count_backend_dispatches(
                    backend.id,
                    since=quota_window_start,
                )
                if recorded_dispatches >= backend.quota.dispatch_limit:
                    await self._record_skip(
                        ticket_id=ticket_id,
                        logical_agent=logical_agent,
                        backend=backend,
                        skipped_reason="dispatch quota exhausted",
                        attempted_at=allocated_at,
                    )
                    continue

            if backend.quota.mode == "fixed-window":
                step_limit = backend.quota.step_limit
                recorded_usage = await self._store.get_recorded_step_usage(
                    backend.id,
                    since=quota_window_start,
                )
                remaining_steps = None if step_limit is None else step_limit - recorded_usage
                if remaining_steps is None or remaining_steps < reserve:
                    await self._record_skip(
                        ticket_id=ticket_id,
                        logical_agent=logical_agent,
                        backend=backend,
                        skipped_reason="step quota below reserve",
                        attempted_at=allocated_at,
                    )
                    continue

            allocation = await self._store.create_allocation(
                backend_id=backend.id,
                logical_agent=logical_agent,
                physical_alias=backend.physical_alias,
                ticket_id=ticket_id,
                step_reserve=reserve if requires_step_reserve else 0,
                concurrency=backend.concurrency,
                lease_started_at=allocated_at,
                stale_after=stale_after,
                attempted_at=allocated_at,
                reserve_steps=requires_step_reserve,
                step_limit=backend.quota.step_limit
                if backend.quota.mode == "fixed-window"
                else None,
                step_window_start=quota_window_start,
                dispatch_limit=backend.quota.dispatch_limit,
                dispatch_window_start=quota_window_start,
            )
            if allocation is None:
                skipped_reason = "backend at concurrency capacity"
                if backend.quota.dispatch_limit is not None:
                    recorded_dispatches = await self._store.count_backend_dispatches(
                        backend.id,
                        since=quota_window_start,
                    )
                    if recorded_dispatches >= backend.quota.dispatch_limit:
                        skipped_reason = "dispatch quota exhausted"
                if backend.quota.mode == "fixed-window":
                    recorded_usage = await self._store.get_recorded_step_usage(
                        backend.id,
                        since=quota_window_start,
                    )
                    step_limit = backend.quota.step_limit
                    if step_limit is None or step_limit - recorded_usage < reserve:
                        skipped_reason = "step quota below reserve"
                await self._record_skip(
                    ticket_id=ticket_id,
                    logical_agent=logical_agent,
                    backend=backend,
                    skipped_reason=skipped_reason,
                    attempted_at=allocated_at,
                )
                continue

            lease, step_reservation, attempt = allocation
            return BackendAllocation(
                backend=backend,
                lease=lease,
                step_reservation=step_reservation,
                attempt_id=attempt.id,
            )

        return BackendNoCapacity(logical_agent=logical_agent, ticket_id=ticket_id)

    async def _record_skip(
        self,
        *,
        ticket_id: str,
        logical_agent: str,
        backend: BackendDefinition,
        skipped_reason: str,
        attempted_at: str,
    ) -> None:
        await self._store.record_dispatch_attempt(
            ticket_id=ticket_id,
            logical_agent=logical_agent,
            selected_backend_id=backend.id,
            physical_alias=backend.physical_alias,
            failure_classification=None,
            skipped_reason=skipped_reason,
            attempted_at=attempted_at,
        )
