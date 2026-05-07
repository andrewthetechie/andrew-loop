"""Tests for opencode custom tools (.opencode/tools/).

Static analysis tests that verify each tool file contains the expected
structure: correct orch command, zod args, error handling, and ORCH_DB_PATH.
"""

from pathlib import Path

TOOLS_DIR = Path(__file__).parent.parent / ".opencode" / "tools"


def _read_tool(name: str) -> str:
    path = TOOLS_DIR / name
    assert path.is_file(), f"Tool file not found: {path}"
    return path.read_text()


# ── ticket-comment ──────────────────────────────────────────────────


class TestTicketComment:
    """ticket-comment.ts: shells out to orch tickets comment."""

    def test_tool_exists(self) -> None:
        _read_tool("ticket-comment.ts")

    def test_calls_orch_tickets_comment(self) -> None:
        content = _read_tool("ticket-comment.ts")
        assert "orch" in content
        assert "tickets" in content
        assert "comment" in content

    def test_has_ticket_id_arg(self) -> None:
        content = _read_tool("ticket-comment.ts")
        assert "ticket_id" in content
        assert "z.string()" in content

    def test_has_body_arg(self) -> None:
        content = _read_tool("ticket-comment.ts")
        assert "body" in content

    def test_has_author_arg(self) -> None:
        content = _read_tool("ticket-comment.ts")
        assert "author" in content
        assert "--author" in content

    def test_has_error_handling(self) -> None:
        content = _read_tool("ticket-comment.ts")
        assert "exitCode" in content
        assert "stderr" in content

    def test_passes_orch_db_path(self) -> None:
        content = _read_tool("ticket-comment.ts")
        assert "ORCH_DB_PATH" in content


# ── ticket-list ─────────────────────────────────────────────────────


class TestTicketList:
    """ticket-list.ts: shells out to orch status --json."""

    def test_tool_exists(self) -> None:
        _read_tool("ticket-list.ts")

    def test_calls_orch_status_json(self) -> None:
        content = _read_tool("ticket-list.ts")
        assert "orch" in content
        assert "status" in content
        assert "--json" in content

    def test_has_no_required_args(self) -> None:
        """ticket-list takes no required args — it lists all tickets."""
        content = _read_tool("ticket-list.ts")
        # Should have an args block but no required string args
        assert "args" in content

    def test_has_error_handling(self) -> None:
        content = _read_tool("ticket-list.ts")
        assert "exitCode" in content
        assert "stderr" in content

    def test_passes_orch_db_path(self) -> None:
        content = _read_tool("ticket-list.ts")
        assert "ORCH_DB_PATH" in content


# ── ticket-update ───────────────────────────────────────────────────


class TestTicketUpdate:
    """ticket-update.ts: shells out to orch tickets update."""

    def test_tool_exists(self) -> None:
        _read_tool("ticket-update.ts")

    def test_calls_orch_tickets_update(self) -> None:
        content = _read_tool("ticket-update.ts")
        assert "orch" in content
        assert "tickets" in content

    def test_has_ticket_id_arg(self) -> None:
        content = _read_tool("ticket-update.ts")
        assert "ticket_id" in content
        assert "z.string()" in content

    def test_has_state_flag(self) -> None:
        content = _read_tool("ticket-update.ts")
        assert "--state" in content

    def test_has_linked_pr_flag(self) -> None:
        content = _read_tool("ticket-update.ts")
        assert "--linked-pr" in content

    def test_has_assignee_flag(self) -> None:
        content = _read_tool("ticket-update.ts")
        assert "--assignee" in content

    def test_no_comment_functionality(self) -> None:
        """Comment functionality moved to ticket-comment tool."""
        content = _read_tool("ticket-update.ts")
        assert "--author" not in content
        # Should not have a comment arg in the zod schema
        lines = content.split("\n")
        arg_lines = [line for line in lines if "comment" in line.lower() and "z." in line]
        assert len(arg_lines) == 0, "ticket-update should not have a comment zod arg"

    def test_has_error_handling(self) -> None:
        content = _read_tool("ticket-update.ts")
        assert "exitCode" in content
        assert "stderr" in content

    def test_passes_orch_db_path(self) -> None:
        content = _read_tool("ticket-update.ts")
        assert "ORCH_DB_PATH" in content


# ── ticket-read ─────────────────────────────────────────────────────


class TestTicketRead:
    """ticket-read.ts: shells out to orch tickets show --json."""

    def test_tool_exists(self) -> None:
        _read_tool("ticket-read.ts")

    def test_calls_orch_tickets_show_json(self) -> None:
        content = _read_tool("ticket-read.ts")
        assert "orch" in content
        assert "tickets" in content
        assert "show" in content
        assert "--json" in content

    def test_has_ticket_id_arg(self) -> None:
        content = _read_tool("ticket-read.ts")
        assert "ticket_id" in content
        assert "z.string()" in content
        assert ".describe(" in content

    def test_has_error_handling(self) -> None:
        content = _read_tool("ticket-read.ts")
        assert "exitCode" in content
        assert "stderr" in content

    def test_passes_orch_db_path(self) -> None:
        content = _read_tool("ticket-read.ts")
        assert "ORCH_DB_PATH" in content


# ── pr-create ───────────────────────────────────────────────────────


class TestPrCreate:
    """pr-create.ts: shells out to orch pr create."""

    def test_tool_exists(self) -> None:
        _read_tool("pr-create.ts")

    def test_calls_orch_pr_create(self) -> None:
        content = _read_tool("pr-create.ts")
        assert "orch" in content
        assert "pr" in content
        assert "create" in content

    def test_has_ticket_id_arg(self) -> None:
        content = _read_tool("pr-create.ts")
        assert "ticket_id" in content
        assert "z.string()" in content

    def test_has_base_branch_arg(self) -> None:
        content = _read_tool("pr-create.ts")
        assert "base_branch" in content or "base" in content

    def test_has_error_handling(self) -> None:
        content = _read_tool("pr-create.ts")
        assert "exitCode" in content
        assert "stderr" in content

    def test_passes_orch_db_path(self) -> None:
        content = _read_tool("pr-create.ts")
        assert "ORCH_DB_PATH" in content


# ── pr-update ───────────────────────────────────────────────────────


class TestPrUpdate:
    """pr-update.ts: shells out to orch pr update."""

    def test_tool_exists(self) -> None:
        _read_tool("pr-update.ts")

    def test_calls_orch_pr_update(self) -> None:
        content = _read_tool("pr-update.ts")
        assert "orch" in content
        assert "pr" in content
        assert "update" in content

    def test_has_ticket_id_arg(self) -> None:
        content = _read_tool("pr-update.ts")
        assert "ticket_id" in content
        assert "z.string()" in content

    def test_has_error_handling(self) -> None:
        content = _read_tool("pr-update.ts")
        assert "exitCode" in content
        assert "stderr" in content

    def test_passes_orch_db_path(self) -> None:
        content = _read_tool("pr-update.ts")
        assert "ORCH_DB_PATH" in content
