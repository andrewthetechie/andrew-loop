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
