"""SQLite database for orchestrator state."""

from pathlib import Path
from types import TracebackType

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Integer,
    Text,
    event,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry

VALID_STATES = (
    "Draft",
    "To Do",
    "Rework",
    "In Progress",
    "Code Review",
    "Ready for Security Review",
    "Ready to Merge",
    "Done",
    "Needs Human Review",
    "Human Merge",
)

_STATES_CHECK = ", ".join(f"'{s}'" for s in VALID_STATES)


class Base(DeclarativeBase):
    pass


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    acceptance_criteria: Mapped[str] = mapped_column(Text, nullable=False)
    file_paths: Mapped[str | None] = mapped_column(Text)
    test_expectations: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    assignee: Mapped[str | None] = mapped_column(Text)
    risk_score: Mapped[int | None] = mapped_column(Integer)
    rework_loop_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    linked_pr: Mapped[str | None] = mapped_column(Text)
    worktree_path: Mapped[str | None] = mapped_column(Text)
    issue_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(f"state IN ({_STATES_CHECK})", name="valid_state"),
        CheckConstraint("risk_score BETWEEN 1 AND 5", name="valid_risk_score"),
    )


class TicketComment(Base):
    __tablename__ = "ticket_comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[str] = mapped_column(Text, ForeignKey("tickets.id"), nullable=False)
    author: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class SubtaskContext(Base):
    __tablename__ = "subtask_context"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[str] = mapped_column(Text, ForeignKey("tickets.id"), nullable=False)
    step: Mapped[str] = mapped_column(Text, nullable=False)
    output: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[str] = mapped_column(Text, ForeignKey("tickets.id"), nullable=False)
    timestamp: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    old_state: Mapped[str] = mapped_column(Text, nullable=False)
    new_state: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[str | None] = mapped_column(Text)


class TicketDependency(Base):
    __tablename__ = "ticket_dependencies"

    ticket_id: Mapped[str] = mapped_column(Text, ForeignKey("tickets.id"), primary_key=True)
    depends_on_ticket_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tickets.id"), primary_key=True
    )


class TicketMetrics(Base):
    __tablename__ = "ticket_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[str] = mapped_column(Text, ForeignKey("tickets.id"), nullable=False)
    agent_type: Mapped[str] = mapped_column(Text, nullable=False)
    logical_agent: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    backend_id: Mapped[str | None] = mapped_column(Text)
    physical_alias: Mapped[str | None] = mapped_column(Text)
    allocation_reason: Mapped[str | None] = mapped_column(Text)
    dispatched_at: Mapped[str] = mapped_column(Text, nullable=False)


class BackendLease(Base):
    __tablename__ = "backend_leases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    backend_id: Mapped[str] = mapped_column(Text, nullable=False)
    logical_agent: Mapped[str] = mapped_column(Text, nullable=False)
    physical_alias: Mapped[str] = mapped_column(Text, nullable=False)
    ticket_id: Mapped[str] = mapped_column(Text, nullable=False)
    step_reserve: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    lease_started_at: Mapped[str] = mapped_column(Text, nullable=False)
    stale_after: Mapped[str] = mapped_column(Text, nullable=False)
    completed_at: Mapped[str | None] = mapped_column(Text)
    stale_marked_at: Mapped[str | None] = mapped_column(Text)
    actual_steps: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'completed', 'stale')",
            name="valid_backend_lease_status",
        ),
    )


class BackendStepUsage(Base):
    __tablename__ = "backend_step_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    backend_id: Mapped[str] = mapped_column(Text, nullable=False)
    logical_agent: Mapped[str] = mapped_column(Text, nullable=False)
    ticket_id: Mapped[str] = mapped_column(Text, nullable=False)
    reserved_steps: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_steps: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    reserved_at: Mapped[str] = mapped_column(Text, nullable=False)
    reconciled_at: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "status IN ('reserved', 'reconciled')",
            name="valid_backend_step_usage_status",
        ),
    )


class BackendCooldown(Base):
    __tablename__ = "backend_cooldowns"

    backend_id: Mapped[str] = mapped_column(Text, primary_key=True)
    logical_agent: Mapped[str] = mapped_column(Text, nullable=False)
    cooldown_until: Mapped[str] = mapped_column(Text, nullable=False)
    failure_classification: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    set_at: Mapped[str] = mapped_column(Text, nullable=False)


class BackendDispatchAttempt(Base):
    __tablename__ = "backend_dispatch_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[str] = mapped_column(Text, nullable=False)
    logical_agent: Mapped[str] = mapped_column(Text, nullable=False)
    selected_backend_id: Mapped[str] = mapped_column(Text, nullable=False)
    physical_alias: Mapped[str] = mapped_column(Text, nullable=False)
    failure_classification: Mapped[str | None] = mapped_column(Text)
    skipped_reason: Mapped[str | None] = mapped_column(Text)
    attempted_at: Mapped[str] = mapped_column(Text, nullable=False)


class BackendFailureState(Base):
    __tablename__ = "backend_failure_state"

    backend_id: Mapped[str] = mapped_column(Text, primary_key=True)
    logical_agent: Mapped[str] = mapped_column(Text, nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False)
    last_failure_classification: Mapped[str] = mapped_column(Text, nullable=False)
    last_failure_at: Mapped[str] = mapped_column(Text, nullable=False)


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection: object, _connection_record: ConnectionPoolEntry) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


class Database:
    """Async SQLite database wrapper for orchestrator state."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._engine = None
        self._session_factory = None

    @property
    def path(self) -> Path:
        return self._path

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite+aiosqlite:///{self._path}"
        self._engine = create_async_engine(url, echo=False)

        async with self._engine.begin() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.run_sync(Base.metadata.create_all)

        self._session_factory = sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )

    async def close(self) -> None:
        if self._engine:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None

    async def __aenter__(self) -> Database:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            msg = "Database not connected. Use 'async with Database(path)' or call connect()."
            raise RuntimeError(msg)
        return self._engine

    def session(self) -> AsyncSession:
        if self._session_factory is None:
            msg = "Database not connected. Use 'async with Database(path)' or call connect()."
            raise RuntimeError(msg)
        return self._session_factory()

    async def list_tables(self) -> list[str]:
        from sqlalchemy import inspect as sa_inspect

        async with self.engine.connect() as conn:
            table_names = await conn.run_sync(
                lambda sync_conn: sa_inspect(sync_conn).get_table_names()
            )
        return sorted(table_names)

    async def count_tickets(self) -> int:
        """Return the total number of tickets in the database."""
        from sqlalchemy import text

        async with self.session() as s:
            result = await s.execute(text("SELECT COUNT(*) FROM tickets"))
            row = result.fetchone()
            return int(row[0]) if row else 0
