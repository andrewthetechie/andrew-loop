"""Persistence helpers for backend runtime state."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text

from orch.db import Database


@dataclass(frozen=True)
class BackendLeaseRecord:
    id: int
    backend_id: str
    logical_agent: str
    physical_alias: str
    ticket_id: str
    step_reserve: int
    status: str
    lease_started_at: str
    stale_after: str
    completed_at: str | None
    stale_marked_at: str | None
    actual_steps: int | None


@dataclass(frozen=True)
class BackendStepUsageRecord:
    id: int
    backend_id: str
    logical_agent: str
    ticket_id: str
    reserved_steps: int
    actual_steps: int | None
    status: str
    reserved_at: str
    reconciled_at: str | None


@dataclass(frozen=True)
class BackendCooldownRecord:
    backend_id: str
    logical_agent: str
    cooldown_until: str
    failure_classification: str
    reason: str
    set_at: str


@dataclass(frozen=True)
class BackendDispatchAttemptRecord:
    id: int
    ticket_id: str
    logical_agent: str
    selected_backend_id: str
    physical_alias: str
    failure_classification: str | None
    skipped_reason: str | None
    attempted_at: str


@dataclass(frozen=True)
class BackendFailureStateRecord:
    backend_id: str
    logical_agent: str
    consecutive_failures: int
    last_failure_classification: str
    last_failure_at: str


class BackendStateStore:
    """Small public API for persisted backend runtime state."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create_lease(
        self,
        *,
        backend_id: str,
        logical_agent: str,
        physical_alias: str,
        ticket_id: str,
        step_reserve: int,
        lease_started_at: str,
        stale_after: str,
    ) -> BackendLeaseRecord:
        """Create an active backend lease."""
        async with self._db.engine.begin() as conn:
            result = await conn.execute(
                text(
                    """
                    INSERT INTO backend_leases (
                        backend_id,
                        logical_agent,
                        physical_alias,
                        ticket_id,
                        step_reserve,
                        status,
                        lease_started_at,
                        stale_after
                    )
                    VALUES (
                        :backend_id,
                        :logical_agent,
                        :physical_alias,
                        :ticket_id,
                        :step_reserve,
                        'active',
                        :lease_started_at,
                        :stale_after
                    )
                    RETURNING
                        id,
                        backend_id,
                        logical_agent,
                        physical_alias,
                        ticket_id,
                        step_reserve,
                        status,
                        lease_started_at,
                        stale_after,
                        completed_at,
                        stale_marked_at,
                        actual_steps
                    """
                ),
                {
                    "backend_id": backend_id,
                    "logical_agent": logical_agent,
                    "physical_alias": physical_alias,
                    "ticket_id": ticket_id,
                    "step_reserve": step_reserve,
                    "lease_started_at": lease_started_at,
                    "stale_after": stale_after,
                },
            )
            row = result.mappings().one()
        return _lease_from_row(row)

    async def create_allocation(
        self,
        *,
        backend_id: str,
        logical_agent: str,
        physical_alias: str,
        ticket_id: str,
        step_reserve: int,
        concurrency: int,
        lease_started_at: str,
        stale_after: str,
        attempted_at: str,
        reserve_steps: bool,
        step_limit: int | None = None,
        step_window_start: str | None = None,
        dispatch_limit: int | None = None,
        dispatch_window_start: str | None = None,
    ) -> (
        tuple[
            BackendLeaseRecord,
            BackendStepUsageRecord | None,
            BackendDispatchAttemptRecord,
        ]
        | None
    ):
        """Atomically create a lease and optional step reserve if capacity allows."""
        async with self._db.engine.connect() as conn:
            trans = await conn.begin()
            try:
                lease_result = await conn.execute(
                    text(
                        """
                        INSERT INTO backend_leases (
                            backend_id,
                            logical_agent,
                            physical_alias,
                            ticket_id,
                            step_reserve,
                            status,
                            lease_started_at,
                            stale_after
                        )
                        SELECT
                            :backend_id,
                            :logical_agent,
                            :physical_alias,
                            :ticket_id,
                            :step_reserve,
                            'active',
                            :lease_started_at,
                            :stale_after
                        WHERE (
                            SELECT COUNT(*)
                            FROM backend_leases
                            WHERE backend_id = :backend_id AND status = 'active'
                        ) < :concurrency
                        AND (
                            :dispatch_limit IS NULL
                            OR (
                                SELECT COUNT(*)
                                FROM backend_dispatch_attempts
                                WHERE selected_backend_id = :backend_id
                                  AND skipped_reason IS NULL
                                  AND (
                                      :dispatch_window_start IS NULL
                                      OR attempted_at >= :dispatch_window_start
                                  )
                            ) < :dispatch_limit
                        )
                        RETURNING
                            id,
                            backend_id,
                            logical_agent,
                            physical_alias,
                            ticket_id,
                            step_reserve,
                            status,
                            lease_started_at,
                            stale_after,
                            completed_at,
                            stale_marked_at,
                            actual_steps
                        """
                    ),
                    {
                        "backend_id": backend_id,
                        "logical_agent": logical_agent,
                        "physical_alias": physical_alias,
                        "ticket_id": ticket_id,
                        "step_reserve": step_reserve,
                        "lease_started_at": lease_started_at,
                        "stale_after": stale_after,
                        "concurrency": concurrency,
                        "dispatch_limit": dispatch_limit,
                        "dispatch_window_start": dispatch_window_start,
                    },
                )
                lease_row = lease_result.mappings().one_or_none()
                if lease_row is None:
                    await trans.rollback()
                    return None

                step_reservation = None
                if reserve_steps:
                    reserve_result = await conn.execute(
                        text(
                            """
                            INSERT INTO backend_step_usage (
                                backend_id,
                                logical_agent,
                                ticket_id,
                                reserved_steps,
                                status,
                                reserved_at
                            )
                            SELECT
                                :backend_id,
                                :logical_agent,
                                :ticket_id,
                                :reserved_steps,
                                'reserved',
                                :reserved_at
                            WHERE :step_limit IS NULL OR (
                                SELECT COALESCE(SUM(
                                    CASE
                                        WHEN status = 'reserved' THEN reserved_steps
                                        WHEN status = 'reconciled' THEN COALESCE(actual_steps, 0)
                                        ELSE 0
                                    END
                                ), 0)
                                FROM backend_step_usage
                                WHERE backend_id = :backend_id
                                  AND (
                                      :step_window_start IS NULL
                                      OR reserved_at >= :step_window_start
                                  )
                            ) + :reserved_steps <= :step_limit
                            RETURNING
                                id,
                                backend_id,
                                logical_agent,
                                ticket_id,
                                reserved_steps,
                                actual_steps,
                                status,
                                reserved_at,
                                reconciled_at
                            """
                        ),
                        {
                            "backend_id": backend_id,
                            "logical_agent": logical_agent,
                            "ticket_id": ticket_id,
                            "reserved_steps": step_reserve,
                            "reserved_at": lease_started_at,
                            "step_limit": step_limit,
                            "step_window_start": step_window_start,
                        },
                    )
                    reserve_row = reserve_result.mappings().one_or_none()
                    if reserve_row is None:
                        await trans.rollback()
                        return None
                    step_reservation = _step_usage_from_row(reserve_row)

                attempt_result = await conn.execute(
                    text(
                        """
                        INSERT INTO backend_dispatch_attempts (
                            ticket_id,
                            logical_agent,
                            selected_backend_id,
                            physical_alias,
                            failure_classification,
                            skipped_reason,
                            attempted_at
                        )
                        VALUES (
                            :ticket_id,
                            :logical_agent,
                            :selected_backend_id,
                            :physical_alias,
                            NULL,
                            NULL,
                            :attempted_at
                        )
                        RETURNING
                            id,
                            ticket_id,
                            logical_agent,
                            selected_backend_id,
                            physical_alias,
                            failure_classification,
                            skipped_reason,
                            attempted_at
                        """
                    ),
                    {
                        "ticket_id": ticket_id,
                        "logical_agent": logical_agent,
                        "selected_backend_id": backend_id,
                        "physical_alias": physical_alias,
                        "attempted_at": attempted_at,
                    },
                )
                attempt_row = attempt_result.mappings().one()

                await trans.commit()
            except Exception:
                await trans.rollback()
                raise

        return (
            _lease_from_row(lease_row),
            step_reservation,
            _dispatch_attempt_from_row(attempt_row),
        )

    async def get_active_lease(self, backend_id: str) -> BackendLeaseRecord | None:
        """Return the active lease for a backend, if one exists."""
        async with self._db.engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT
                        id,
                        backend_id,
                        logical_agent,
                        physical_alias,
                        ticket_id,
                        step_reserve,
                        status,
                        lease_started_at,
                        stale_after,
                        completed_at,
                        stale_marked_at,
                        actual_steps
                    FROM backend_leases
                    WHERE backend_id = :backend_id AND status = 'active'
                    ORDER BY id
                    LIMIT 1
                    """
                ),
                {"backend_id": backend_id},
            )
            row = result.mappings().one_or_none()
        if row is None:
            return None
        return _lease_from_row(row)

    async def count_active_leases(self, backend_id: str) -> int:
        """Return the number of active leases for a backend."""
        async with self._db.engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM backend_leases
                    WHERE backend_id = :backend_id AND status = 'active'
                    """
                ),
                {"backend_id": backend_id},
            )
            row = result.one()
        return int(row[0])

    async def get_lease(self, lease_id: int) -> BackendLeaseRecord | None:
        """Return a lease by id, regardless of status."""
        async with self._db.engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT
                        id,
                        backend_id,
                        logical_agent,
                        physical_alias,
                        ticket_id,
                        step_reserve,
                        status,
                        lease_started_at,
                        stale_after,
                        completed_at,
                        stale_marked_at,
                        actual_steps
                    FROM backend_leases
                    WHERE id = :lease_id
                    """
                ),
                {"lease_id": lease_id},
            )
            row = result.mappings().one_or_none()
        if row is None:
            return None
        return _lease_from_row(row)

    async def list_ticket_leases(self, ticket_id: str) -> list[BackendLeaseRecord]:
        """List all leases recorded for a ticket in insertion order."""
        async with self._db.engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT
                        id,
                        backend_id,
                        logical_agent,
                        physical_alias,
                        ticket_id,
                        step_reserve,
                        status,
                        lease_started_at,
                        stale_after,
                        completed_at,
                        stale_marked_at,
                        actual_steps
                    FROM backend_leases
                    WHERE ticket_id = :ticket_id
                    ORDER BY id
                    """
                ),
                {"ticket_id": ticket_id},
            )
            rows = result.mappings().all()
        return [_lease_from_row(row) for row in rows]

    async def complete_lease(
        self,
        lease_id: int,
        *,
        completed_at: str,
        actual_steps: int | None = None,
    ) -> BackendLeaseRecord:
        """Mark an active lease as completed."""
        async with self._db.engine.begin() as conn:
            result = await conn.execute(
                text(
                    """
                    UPDATE backend_leases
                    SET status = 'completed',
                        completed_at = :completed_at,
                        actual_steps = :actual_steps
                    WHERE id = :lease_id
                    RETURNING
                        id,
                        backend_id,
                        logical_agent,
                        physical_alias,
                        ticket_id,
                        step_reserve,
                        status,
                        lease_started_at,
                        stale_after,
                        completed_at,
                        stale_marked_at,
                        actual_steps
                    """
                ),
                {
                    "lease_id": lease_id,
                    "completed_at": completed_at,
                    "actual_steps": actual_steps,
                },
            )
            row = result.mappings().one()
        return _lease_from_row(row)

    async def mark_stale_leases(self, stale_as_of: str) -> list[BackendLeaseRecord]:
        """Mark expired active leases as stale and return the changed rows."""
        async with self._db.engine.begin() as conn:
            result = await conn.execute(
                text(
                    """
                    UPDATE backend_leases
                    SET status = 'stale',
                        stale_marked_at = :stale_as_of
                    WHERE status = 'active' AND stale_after <= :stale_as_of
                    RETURNING
                        id,
                        backend_id,
                        logical_agent,
                        physical_alias,
                        ticket_id,
                        step_reserve,
                        status,
                        lease_started_at,
                        stale_after,
                        completed_at,
                        stale_marked_at,
                        actual_steps
                    """
                ),
                {"stale_as_of": stale_as_of},
            )
            rows = result.mappings().all()
        return [_lease_from_row(row) for row in rows]

    async def mark_active_leases_stale(self, stale_as_of: str) -> list[BackendLeaseRecord]:
        """Mark every active lease stale for crash/startup recovery."""
        async with self._db.engine.begin() as conn:
            result = await conn.execute(
                text(
                    """
                    UPDATE backend_leases
                    SET status = 'stale',
                        stale_marked_at = :stale_as_of
                    WHERE status = 'active'
                    RETURNING
                        id,
                        backend_id,
                        logical_agent,
                        physical_alias,
                        ticket_id,
                        step_reserve,
                        status,
                        lease_started_at,
                        stale_after,
                        completed_at,
                        stale_marked_at,
                        actual_steps
                    """
                ),
                {"stale_as_of": stale_as_of},
            )
            rows = result.mappings().all()
        return [_lease_from_row(row) for row in rows]

    async def reserve_steps(
        self,
        *,
        backend_id: str,
        logical_agent: str,
        ticket_id: str,
        reserved_steps: int,
        reserved_at: str,
    ) -> BackendStepUsageRecord:
        """Record a step reserve before dispatch."""
        async with self._db.engine.begin() as conn:
            result = await conn.execute(
                text(
                    """
                    INSERT INTO backend_step_usage (
                        backend_id,
                        logical_agent,
                        ticket_id,
                        reserved_steps,
                        status,
                        reserved_at
                    )
                    VALUES (
                        :backend_id,
                        :logical_agent,
                        :ticket_id,
                        :reserved_steps,
                        'reserved',
                        :reserved_at
                    )
                    RETURNING
                        id,
                        backend_id,
                        logical_agent,
                        ticket_id,
                        reserved_steps,
                        actual_steps,
                        status,
                        reserved_at,
                        reconciled_at
                    """
                ),
                {
                    "backend_id": backend_id,
                    "logical_agent": logical_agent,
                    "ticket_id": ticket_id,
                    "reserved_steps": reserved_steps,
                    "reserved_at": reserved_at,
                },
            )
            row = result.mappings().one()
        return _step_usage_from_row(row)

    async def get_step_reservation(self, reservation_id: int) -> BackendStepUsageRecord | None:
        """Return a step reservation by id."""
        async with self._db.engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT
                        id,
                        backend_id,
                        logical_agent,
                        ticket_id,
                        reserved_steps,
                        actual_steps,
                        status,
                        reserved_at,
                        reconciled_at
                    FROM backend_step_usage
                    WHERE id = :reservation_id
                    """
                ),
                {"reservation_id": reservation_id},
            )
            row = result.mappings().one_or_none()
        if row is None:
            return None
        return _step_usage_from_row(row)

    async def list_ticket_step_usage(self, ticket_id: str) -> list[BackendStepUsageRecord]:
        """List all step-usage rows recorded for a ticket in insertion order."""
        async with self._db.engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT
                        id,
                        backend_id,
                        logical_agent,
                        ticket_id,
                        reserved_steps,
                        actual_steps,
                        status,
                        reserved_at,
                        reconciled_at
                    FROM backend_step_usage
                    WHERE ticket_id = :ticket_id
                    ORDER BY id
                    """
                ),
                {"ticket_id": ticket_id},
            )
            rows = result.mappings().all()
        return [_step_usage_from_row(row) for row in rows]

    async def reconcile_step_usage(
        self,
        reservation_id: int,
        *,
        actual_steps: int,
        reconciled_at: str,
    ) -> BackendStepUsageRecord:
        """Reconcile reserved step usage to actual usage."""
        async with self._db.engine.begin() as conn:
            result = await conn.execute(
                text(
                    """
                    UPDATE backend_step_usage
                    SET status = 'reconciled',
                        actual_steps = :actual_steps,
                        reconciled_at = :reconciled_at
                    WHERE id = :reservation_id
                    RETURNING
                        id,
                        backend_id,
                        logical_agent,
                        ticket_id,
                        reserved_steps,
                        actual_steps,
                        status,
                        reserved_at,
                        reconciled_at
                    """
                ),
                {
                    "reservation_id": reservation_id,
                    "actual_steps": actual_steps,
                    "reconciled_at": reconciled_at,
                },
            )
            row = result.mappings().one()
        return _step_usage_from_row(row)

    async def get_recorded_step_usage(self, backend_id: str, *, since: str | None = None) -> int:
        """Return recorded step usage for a backend across reserved and reconciled rows."""
        async with self._db.engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT COALESCE(SUM(
                        CASE
                            WHEN status = 'reserved' THEN reserved_steps
                            WHEN status = 'reconciled' THEN COALESCE(actual_steps, 0)
                            ELSE 0
                        END
                    ), 0)
                    FROM backend_step_usage
                    WHERE backend_id = :backend_id
                      AND (:since IS NULL OR reserved_at >= :since)
                    """
                ),
                {"backend_id": backend_id, "since": since},
            )
            row = result.one()
        return int(row[0])

    async def count_backend_dispatches(self, backend_id: str, *, since: str | None = None) -> int:
        """Return selected dispatch-attempt count for a backend."""
        async with self._db.engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM backend_dispatch_attempts
                    WHERE selected_backend_id = :backend_id
                      AND skipped_reason IS NULL
                      AND (:since IS NULL OR attempted_at >= :since)
                    """
                ),
                {"backend_id": backend_id, "since": since},
            )
            row = result.one()
        return int(row[0])

    async def set_cooldown(
        self,
        *,
        backend_id: str,
        logical_agent: str,
        cooldown_until: str,
        failure_classification: str,
        reason: str,
        set_at: str,
    ) -> BackendCooldownRecord:
        """Create or replace backend cooldown state."""
        async with self._db.engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM backend_cooldowns WHERE backend_id = :backend_id"),
                {"backend_id": backend_id},
            )
            result = await conn.execute(
                text(
                    """
                    INSERT INTO backend_cooldowns (
                        backend_id,
                        logical_agent,
                        cooldown_until,
                        failure_classification,
                        reason,
                        set_at
                    )
                    VALUES (
                        :backend_id,
                        :logical_agent,
                        :cooldown_until,
                        :failure_classification,
                        :reason,
                        :set_at
                    )
                    RETURNING
                        backend_id,
                        logical_agent,
                        cooldown_until,
                        failure_classification,
                        reason,
                        set_at
                    """
                ),
                {
                    "backend_id": backend_id,
                    "logical_agent": logical_agent,
                    "cooldown_until": cooldown_until,
                    "failure_classification": failure_classification,
                    "reason": reason,
                    "set_at": set_at,
                },
            )
            row = result.mappings().one()
        return _cooldown_from_row(row)

    async def get_cooldown(self, backend_id: str) -> BackendCooldownRecord | None:
        """Return the persisted cooldown for a backend, if set."""
        async with self._db.engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT
                        backend_id,
                        logical_agent,
                        cooldown_until,
                        failure_classification,
                        reason,
                        set_at
                    FROM backend_cooldowns
                    WHERE backend_id = :backend_id
                    """
                ),
                {"backend_id": backend_id},
            )
            row = result.mappings().one_or_none()
        if row is None:
            return None
        return _cooldown_from_row(row)

    async def clear_cooldown(self, backend_id: str) -> None:
        """Remove persisted cooldown state for a backend."""
        async with self._db.engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM backend_cooldowns WHERE backend_id = :backend_id"),
                {"backend_id": backend_id},
            )

    async def record_dispatch_attempt(
        self,
        *,
        ticket_id: str,
        logical_agent: str,
        selected_backend_id: str,
        physical_alias: str,
        failure_classification: str | None,
        skipped_reason: str | None,
        attempted_at: str,
    ) -> BackendDispatchAttemptRecord:
        """Append one backend dispatch attempt record."""
        async with self._db.engine.begin() as conn:
            result = await conn.execute(
                text(
                    """
                    INSERT INTO backend_dispatch_attempts (
                        ticket_id,
                        logical_agent,
                        selected_backend_id,
                        physical_alias,
                        failure_classification,
                        skipped_reason,
                        attempted_at
                    )
                    VALUES (
                        :ticket_id,
                        :logical_agent,
                        :selected_backend_id,
                        :physical_alias,
                        :failure_classification,
                        :skipped_reason,
                        :attempted_at
                    )
                    RETURNING
                        id,
                        ticket_id,
                        logical_agent,
                        selected_backend_id,
                        physical_alias,
                        failure_classification,
                        skipped_reason,
                        attempted_at
                    """
                ),
                {
                    "ticket_id": ticket_id,
                    "logical_agent": logical_agent,
                    "selected_backend_id": selected_backend_id,
                    "physical_alias": physical_alias,
                    "failure_classification": failure_classification,
                    "skipped_reason": skipped_reason,
                    "attempted_at": attempted_at,
                },
            )
            row = result.mappings().one()
        return _dispatch_attempt_from_row(row)

    async def list_dispatch_attempts(self, ticket_id: str) -> list[BackendDispatchAttemptRecord]:
        """List dispatch attempts for a ticket in insertion order."""
        async with self._db.engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT
                        id,
                        ticket_id,
                        logical_agent,
                        selected_backend_id,
                        physical_alias,
                        failure_classification,
                        skipped_reason,
                        attempted_at
                    FROM backend_dispatch_attempts
                    WHERE ticket_id = :ticket_id
                    ORDER BY id
                    """
                ),
                {"ticket_id": ticket_id},
            )
            rows = result.mappings().all()
        return [_dispatch_attempt_from_row(row) for row in rows]

    async def set_dispatch_attempt_failure(
        self, attempt_id: int, *, failure_classification: str
    ) -> BackendDispatchAttemptRecord:
        """Attach a failure classification to an existing dispatch attempt."""
        async with self._db.engine.begin() as conn:
            result = await conn.execute(
                text(
                    """
                    UPDATE backend_dispatch_attempts
                    SET failure_classification = :failure_classification
                    WHERE id = :attempt_id
                    RETURNING
                        id,
                        ticket_id,
                        logical_agent,
                        selected_backend_id,
                        physical_alias,
                        failure_classification,
                        skipped_reason,
                        attempted_at
                    """
                ),
                {
                    "attempt_id": attempt_id,
                    "failure_classification": failure_classification,
                },
            )
            row = result.mappings().one()
        return _dispatch_attempt_from_row(row)

    async def record_failure(
        self,
        *,
        backend_id: str,
        logical_agent: str,
        failure_classification: str,
        failed_at: str,
    ) -> BackendFailureStateRecord:
        """Increment the persisted consecutive failure state for a backend."""
        existing = await self.get_failure_state(backend_id)
        consecutive_failures = 1 if existing is None else existing.consecutive_failures + 1

        async with self._db.engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM backend_failure_state WHERE backend_id = :backend_id"),
                {"backend_id": backend_id},
            )
            result = await conn.execute(
                text(
                    """
                    INSERT INTO backend_failure_state (
                        backend_id,
                        logical_agent,
                        consecutive_failures,
                        last_failure_classification,
                        last_failure_at
                    )
                    VALUES (
                        :backend_id,
                        :logical_agent,
                        :consecutive_failures,
                        :last_failure_classification,
                        :last_failure_at
                    )
                    RETURNING
                        backend_id,
                        logical_agent,
                        consecutive_failures,
                        last_failure_classification,
                        last_failure_at
                    """
                ),
                {
                    "backend_id": backend_id,
                    "logical_agent": logical_agent,
                    "consecutive_failures": consecutive_failures,
                    "last_failure_classification": failure_classification,
                    "last_failure_at": failed_at,
                },
            )
            row = result.mappings().one()
        return _failure_state_from_row(row)

    async def get_failure_state(self, backend_id: str) -> BackendFailureStateRecord | None:
        """Return persisted consecutive failure state for a backend."""
        async with self._db.engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT
                        backend_id,
                        logical_agent,
                        consecutive_failures,
                        last_failure_classification,
                        last_failure_at
                    FROM backend_failure_state
                    WHERE backend_id = :backend_id
                    """
                ),
                {"backend_id": backend_id},
            )
            row = result.mappings().one_or_none()
        if row is None:
            return None
        return _failure_state_from_row(row)

    async def clear_failure_state(self, backend_id: str) -> None:
        """Remove persisted failure state for a backend."""
        async with self._db.engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM backend_failure_state WHERE backend_id = :backend_id"),
                {"backend_id": backend_id},
            )


def _lease_from_row(row: object) -> BackendLeaseRecord:
    mapping = dict(row)
    return BackendLeaseRecord(
        id=int(mapping["id"]),
        backend_id=str(mapping["backend_id"]),
        logical_agent=str(mapping["logical_agent"]),
        physical_alias=str(mapping["physical_alias"]),
        ticket_id=str(mapping["ticket_id"]),
        step_reserve=int(mapping["step_reserve"]),
        status=str(mapping["status"]),
        lease_started_at=str(mapping["lease_started_at"]),
        stale_after=str(mapping["stale_after"]),
        completed_at=str(mapping["completed_at"]) if mapping["completed_at"] else None,
        stale_marked_at=str(mapping["stale_marked_at"]) if mapping["stale_marked_at"] else None,
        actual_steps=int(mapping["actual_steps"]) if mapping["actual_steps"] is not None else None,
    )


def _step_usage_from_row(row: object) -> BackendStepUsageRecord:
    mapping = dict(row)
    return BackendStepUsageRecord(
        id=int(mapping["id"]),
        backend_id=str(mapping["backend_id"]),
        logical_agent=str(mapping["logical_agent"]),
        ticket_id=str(mapping["ticket_id"]),
        reserved_steps=int(mapping["reserved_steps"]),
        actual_steps=int(mapping["actual_steps"]) if mapping["actual_steps"] is not None else None,
        status=str(mapping["status"]),
        reserved_at=str(mapping["reserved_at"]),
        reconciled_at=str(mapping["reconciled_at"]) if mapping["reconciled_at"] else None,
    )


def _cooldown_from_row(row: object) -> BackendCooldownRecord:
    mapping = dict(row)
    return BackendCooldownRecord(
        backend_id=str(mapping["backend_id"]),
        logical_agent=str(mapping["logical_agent"]),
        cooldown_until=str(mapping["cooldown_until"]),
        failure_classification=str(mapping["failure_classification"]),
        reason=str(mapping["reason"]),
        set_at=str(mapping["set_at"]),
    )


def _dispatch_attempt_from_row(row: object) -> BackendDispatchAttemptRecord:
    mapping = dict(row)
    return BackendDispatchAttemptRecord(
        id=int(mapping["id"]),
        ticket_id=str(mapping["ticket_id"]),
        logical_agent=str(mapping["logical_agent"]),
        selected_backend_id=str(mapping["selected_backend_id"]),
        physical_alias=str(mapping["physical_alias"]),
        failure_classification=str(mapping["failure_classification"])
        if mapping["failure_classification"]
        else None,
        skipped_reason=str(mapping["skipped_reason"]) if mapping["skipped_reason"] else None,
        attempted_at=str(mapping["attempted_at"]),
    )


def _failure_state_from_row(row: object) -> BackendFailureStateRecord:
    mapping = dict(row)
    return BackendFailureStateRecord(
        backend_id=str(mapping["backend_id"]),
        logical_agent=str(mapping["logical_agent"]),
        consecutive_failures=int(mapping["consecutive_failures"]),
        last_failure_classification=str(mapping["last_failure_classification"]),
        last_failure_at=str(mapping["last_failure_at"]),
    )
