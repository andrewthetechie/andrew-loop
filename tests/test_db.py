from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from orch.db import VALID_STATES, Database


async def _insert_ticket(
    db: Database,
    *,
    ticket_id: str = "TEST-001",
    title: str = "Test ticket",
    description: str = "A test",
    acceptance_criteria: str = "It works",
    state: str = "Draft",
    risk_score: int | None = None,
) -> None:
    """Insert a ticket using raw SQL to test schema constraints directly."""
    async with db.engine.begin() as conn:
        await conn.execute(
            text(
                """INSERT INTO tickets
                   (id, title, description, acceptance_criteria, state,
                    risk_score, rework_loop_count, created_at, updated_at)
                   VALUES (:id, :title, :desc, :ac, :state,
                           :risk, 0, datetime('now'), datetime('now'))"""
            ),
            {
                "id": ticket_id,
                "title": title,
                "desc": description,
                "ac": acceptance_criteria,
                "state": state,
                "risk": risk_score,
            },
        )


async def test_database_creates_file_on_init(tmp_db_path: Path) -> None:
    """Database file and all tables are created when Database is initialized."""
    assert not tmp_db_path.exists()

    async with Database(tmp_db_path) as db:
        tables = await db.list_tables()

    assert tmp_db_path.exists()
    assert "tickets" in tables
    assert "ticket_comments" in tables
    assert "subtask_context" in tables


async def test_schema_accepts_all_valid_states(tmp_db_path: Path) -> None:
    """Every defined valid state is accepted by the CHECK constraint."""
    async with Database(tmp_db_path) as db:
        for i, state in enumerate(VALID_STATES):
            await _insert_ticket(db, ticket_id=f"TEST-{i:03d}", state=state)

        async with db.engine.connect() as conn:
            result = await conn.execute(text("SELECT COUNT(*) FROM tickets"))
            (count,) = result.one()
        assert count == len(VALID_STATES)


async def test_schema_rejects_invalid_state(tmp_db_path: Path) -> None:
    """Inserting a ticket with an invalid state raises IntegrityError."""
    async with Database(tmp_db_path) as db:
        with pytest.raises(IntegrityError):
            await _insert_ticket(db, state="InvalidState")


async def test_schema_rejects_null_required_fields(tmp_db_path: Path) -> None:
    """Required fields (title, description, acceptance_criteria) cannot be NULL."""
    async with Database(tmp_db_path) as db:
        for i, field in enumerate(("title", "description", "acceptance_criteria")):
            overrides = {field: None}
            defaults = {
                "title": "Test",
                "description": "Desc",
                "acceptance_criteria": "AC",
            }
            defaults.update(overrides)
            with pytest.raises(IntegrityError):
                async with db.engine.begin() as conn:
                    await conn.execute(
                        text(
                            """INSERT INTO tickets
                               (id, title, description, acceptance_criteria, state,
                                rework_loop_count, created_at, updated_at)
                               VALUES (:id, :title, :desc, :ac, 'Draft',
                                       0, datetime('now'), datetime('now'))"""
                        ),
                        {
                            "id": f"NULL-{i}",
                            "title": defaults["title"],
                            "desc": defaults["description"],
                            "ac": defaults["acceptance_criteria"],
                        },
                    )


async def test_schema_rejects_risk_score_out_of_range(tmp_db_path: Path) -> None:
    """Risk score must be between 1 and 5."""
    async with Database(tmp_db_path) as db:
        for bad_score in (0, 6, -1, 100):
            with pytest.raises(IntegrityError):
                await _insert_ticket(
                    db,
                    ticket_id=f"RISK-{bad_score}",
                    risk_score=bad_score,
                )


async def test_schema_accepts_valid_risk_scores(tmp_db_path: Path) -> None:
    """Risk scores 1 through 5 are accepted. NULL is also accepted (not yet assigned)."""
    async with Database(tmp_db_path) as db:
        for score in range(1, 6):
            await _insert_ticket(db, ticket_id=f"SCORE-{score}", risk_score=score)
        await _insert_ticket(db, ticket_id="SCORE-NULL", risk_score=None)


async def test_schema_enforces_foreign_keys(tmp_db_path: Path) -> None:
    """Comments referencing a non-existent ticket are rejected."""
    async with Database(tmp_db_path) as db:
        with pytest.raises(IntegrityError):
            async with db.engine.begin() as conn:
                await conn.execute(
                    text(
                        """INSERT INTO ticket_comments
                           (ticket_id, author, body, created_at)
                           VALUES ('NONEXISTENT', 'human', 'hello', datetime('now'))"""
                    )
                )


async def test_wal_mode_enabled(tmp_db_path: Path) -> None:
    """Database uses WAL journal mode for concurrent read safety."""
    async with Database(tmp_db_path) as db:
        async with db.engine.connect() as conn:
            result = await conn.execute(text("PRAGMA journal_mode"))
            (mode,) = result.one()
        assert mode == "wal"


async def test_schema_has_worktree_path_column(tmp_db_path: Path) -> None:
    """Ticket table has a nullable worktree_path column."""
    async with Database(tmp_db_path) as db:
        await _insert_ticket(db, ticket_id="WT-001")
        async with db.engine.connect() as conn:
            result = await conn.execute(
                text("SELECT worktree_path FROM tickets WHERE id = 'WT-001'")
            )
            row = result.one()
        assert row[0] is None


async def test_engine_property_raises_when_not_connected(tmp_db_path: Path) -> None:
    """Accessing engine before connect() raises RuntimeError."""
    db = Database(tmp_db_path)
    with pytest.raises(RuntimeError, match="not connected"):
        _ = db.engine


async def test_session_raises_when_not_connected(tmp_db_path: Path) -> None:
    """Accessing session before connect() raises RuntimeError."""
    db = Database(tmp_db_path)
    with pytest.raises(RuntimeError, match="not connected"):
        _ = db.session()
