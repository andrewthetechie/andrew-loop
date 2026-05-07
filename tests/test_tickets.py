"""Tests for ticket CRUD operations via CLI."""

import json
from pathlib import Path

import yaml
from click.testing import CliRunner

from orch.cli import main


def _write_ticket_yaml(path: Path, **overrides: object) -> Path:
    """Write a ticket YAML file with sensible defaults."""
    data = {
        "title": "Add utility function",
        "description": "Add a `slugify` function to `src/utils.py`.",
        "acceptance_criteria": "- [ ] Function exists\n- [ ] Tests pass",
        **overrides,
    }
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    return path


def test_create_ticket_from_file(tmp_path: Path) -> None:
    """Creating a ticket from a YAML file produces a ticket in Draft state."""
    db_path = tmp_path / ".orchestra" / "state.db"
    ticket_file = _write_ticket_yaml(tmp_path / "ticket.yaml")

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--db", str(db_path), "tickets", "create", "--from-file", str(ticket_file)],
    )

    assert result.exit_code == 0, result.output
    assert "ORCH-" in result.output

    # Verify the ticket exists via show --json
    ticket_id = result.output.strip().split()[-1]
    show_result = runner.invoke(
        main,
        ["--db", str(db_path), "tickets", "show", ticket_id, "--json"],
    )
    assert show_result.exit_code == 0
    ticket = json.loads(show_result.output)
    assert ticket["title"] == "Add utility function"
    assert ticket["state"] == "Draft"


def _create_ticket(runner: CliRunner, db_path: Path, tmp_path: Path, **overrides: object) -> str:
    """Helper: create a ticket and return its ID."""
    ticket_file = _write_ticket_yaml(tmp_path / "ticket.yaml", **overrides)
    result = runner.invoke(
        main,
        ["--db", str(db_path), "tickets", "create", "--from-file", str(ticket_file)],
    )
    assert result.exit_code == 0, result.output
    return result.output.strip().split()[-1]


def test_create_rejects_missing_required_fields(tmp_path: Path) -> None:
    """Creating a ticket without required fields fails."""
    db_path = tmp_path / ".orchestra" / "state.db"
    ticket_file = tmp_path / "bad.yaml"
    ticket_file.write_text(yaml.dump({"title": "Missing fields"}))

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--db", str(db_path), "tickets", "create", "--from-file", str(ticket_file)],
    )
    assert result.exit_code != 0
    assert "required" in result.output.lower() or "missing" in result.output.lower()


def test_list_tickets(tmp_path: Path) -> None:
    """Listing tickets shows all tickets; --state filters; --json returns JSON."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()

    _create_ticket(runner, db_path, tmp_path, title="First ticket")
    _create_ticket(runner, db_path, tmp_path, title="Second ticket")

    # List all
    result = runner.invoke(main, ["--db", str(db_path), "tickets", "list"])
    assert result.exit_code == 0
    assert "First ticket" in result.output
    assert "Second ticket" in result.output

    # List as JSON
    result = runner.invoke(main, ["--db", str(db_path), "tickets", "list", "--json"])
    assert result.exit_code == 0
    items = json.loads(result.output)
    assert len(items) == 2

    # Filter by state — both are Draft
    result = runner.invoke(
        main, ["--db", str(db_path), "tickets", "list", "--state", "Draft", "--json"]
    )
    items = json.loads(result.output)
    assert len(items) == 2

    # Filter by state that has no tickets
    result = runner.invoke(
        main, ["--db", str(db_path), "tickets", "list", "--state", "Done", "--json"]
    )
    items = json.loads(result.output)
    assert len(items) == 0


def test_show_yaml_is_round_trippable(tmp_path: Path) -> None:
    """show --yaml output can be fed back into edit --from-file."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()
    ticket_id = _create_ticket(runner, db_path, tmp_path)

    # Get YAML
    result = runner.invoke(main, ["--db", str(db_path), "tickets", "show", ticket_id, "--yaml"])
    assert result.exit_code == 0
    original_yaml = result.output

    # Modify the YAML
    data = yaml.safe_load(original_yaml)
    data["title"] = "Updated title"
    updated_file = tmp_path / "updated.yaml"
    updated_file.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))

    # Edit from file
    result = runner.invoke(
        main,
        ["--db", str(db_path), "tickets", "edit", ticket_id, "--from-file", str(updated_file)],
    )
    assert result.exit_code == 0

    # Verify
    result = runner.invoke(main, ["--db", str(db_path), "tickets", "show", ticket_id, "--json"])
    ticket = json.loads(result.output)
    assert ticket["title"] == "Updated title"


def test_move_ticket(tmp_path: Path) -> None:
    """Moving a ticket changes its state."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()
    ticket_id = _create_ticket(runner, db_path, tmp_path)

    result = runner.invoke(main, ["--db", str(db_path), "tickets", "move", ticket_id, "To Do"])
    assert result.exit_code == 0
    assert "To Do" in result.output

    # Verify state changed
    result = runner.invoke(main, ["--db", str(db_path), "tickets", "show", ticket_id, "--json"])
    ticket = json.loads(result.output)
    assert ticket["state"] == "To Do"


def test_move_rejects_invalid_state(tmp_path: Path) -> None:
    """Moving to an invalid state fails."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()
    ticket_id = _create_ticket(runner, db_path, tmp_path)

    result = runner.invoke(main, ["--db", str(db_path), "tickets", "move", ticket_id, "Nonsense"])
    assert result.exit_code != 0
    assert "invalid" in result.output.lower() or "error" in result.output.lower()


def test_move_nonexistent_ticket(tmp_path: Path) -> None:
    """Moving a ticket that doesn't exist fails."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()

    # Need to init the DB first by creating any ticket
    _create_ticket(runner, db_path, tmp_path)

    result = runner.invoke(main, ["--db", str(db_path), "tickets", "move", "ORCH-999", "To Do"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_update_fields(tmp_path: Path) -> None:
    """Update specific fields on a ticket."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()
    ticket_id = _create_ticket(runner, db_path, tmp_path)

    result = runner.invoke(
        main,
        [
            "--db",
            str(db_path),
            "tickets",
            "update",
            ticket_id,
            "--linked-pr",
            "https://github.com/org/repo/pull/42",
            "--assignee",
            "coder",
        ],
    )
    assert result.exit_code == 0

    result = runner.invoke(main, ["--db", str(db_path), "tickets", "show", ticket_id, "--json"])
    ticket = json.loads(result.output)
    assert ticket["linked_pr"] == "https://github.com/org/repo/pull/42"
    assert ticket["assignee"] == "coder"


def test_comment_on_ticket(tmp_path: Path) -> None:
    """Adding a comment shows up in ticket detail."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()
    ticket_id = _create_ticket(runner, db_path, tmp_path)

    result = runner.invoke(
        main,
        ["--db", str(db_path), "tickets", "comment", ticket_id, "Fixed the criteria"],
    )
    assert result.exit_code == 0

    result = runner.invoke(main, ["--db", str(db_path), "tickets", "show", ticket_id, "--json"])
    ticket = json.loads(result.output)
    assert len(ticket["comments"]) == 1
    assert ticket["comments"][0]["body"] == "Fixed the criteria"
    assert ticket["comments"][0]["author"] == "human"


def test_comment_on_nonexistent_ticket(tmp_path: Path) -> None:
    """Commenting on a nonexistent ticket fails."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()

    # Init DB
    _create_ticket(runner, db_path, tmp_path)

    result = runner.invoke(
        main,
        ["--db", str(db_path), "tickets", "comment", "ORCH-999", "Hello"],
    )
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_show_nonexistent_ticket(tmp_path: Path) -> None:
    """Showing a nonexistent ticket fails."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()

    # Init DB
    _create_ticket(runner, db_path, tmp_path)

    result = runner.invoke(main, ["--db", str(db_path), "tickets", "show", "ORCH-999"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_ticket_ids_increment(tmp_path: Path) -> None:
    """Each new ticket gets an incrementing ID."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()

    id1 = _create_ticket(runner, db_path, tmp_path, title="First")
    id2 = _create_ticket(runner, db_path, tmp_path, title="Second")
    id3 = _create_ticket(runner, db_path, tmp_path, title="Third")

    assert id1 == "ORCH-001"
    assert id2 == "ORCH-002"
    assert id3 == "ORCH-003"


def test_show_human_readable(tmp_path: Path) -> None:
    """Default show output includes key fields in readable format."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()
    ticket_id = _create_ticket(runner, db_path, tmp_path, title="My Task")

    result = runner.invoke(main, ["--db", str(db_path), "tickets", "show", ticket_id])
    assert result.exit_code == 0
    assert "My Task" in result.output
    assert "Draft" in result.output


def test_create_from_editor(tmp_path: Path, monkeypatch: object) -> None:
    """Creating a ticket via $EDITOR uses the template."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()

    # Mock click.edit to return valid YAML
    import orch.cli

    monkeypatch.setattr(
        orch.cli,
        "click",
        type("MockClick", (), {**{k: getattr(orch.cli.click, k) for k in dir(orch.cli.click)}}),
    )

    yaml_content = (
        "title: Editor ticket\ndescription: Created via editor\nacceptance_criteria: It works\n"
    )
    monkeypatch.setattr(orch.cli.click, "edit", lambda *_a, **_kw: yaml_content)

    result = runner.invoke(main, ["--db", str(db_path), "tickets", "create"])
    assert result.exit_code == 0, result.output
    assert "ORCH-" in result.output


def test_create_from_editor_aborted(tmp_path: Path, monkeypatch: object) -> None:
    """Aborting the editor during create exits with error."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()

    import orch.cli

    monkeypatch.setattr(orch.cli.click, "edit", lambda *_a, **_kw: None)

    result = runner.invoke(main, ["--db", str(db_path), "tickets", "create"])
    assert result.exit_code != 0


def test_create_with_invalid_yaml(tmp_path: Path) -> None:
    """Creating from a file with invalid YAML content fails."""
    db_path = tmp_path / ".orchestra" / "state.db"
    ticket_file = tmp_path / "bad.yaml"
    ticket_file.write_text("just a plain string, not a mapping")

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--db", str(db_path), "tickets", "create", "--from-file", str(ticket_file)],
    )
    assert result.exit_code != 0


def test_create_with_invalid_risk_score(tmp_path: Path) -> None:
    """Creating a ticket with risk_score out of range fails."""
    db_path = tmp_path / ".orchestra" / "state.db"
    ticket_file = _write_ticket_yaml(tmp_path / "ticket.yaml", risk_score=99)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--db", str(db_path), "tickets", "create", "--from-file", str(ticket_file)],
    )
    assert result.exit_code != 0
    assert "risk_score" in result.output.lower()


def test_edit_from_editor(tmp_path: Path, monkeypatch: object) -> None:
    """Editing a ticket via $EDITOR updates it."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()
    ticket_id = _create_ticket(runner, db_path, tmp_path)

    import orch.cli

    yaml_content = (
        "title: Edited via editor\ndescription: Updated desc\nacceptance_criteria: Still works\n"
    )
    monkeypatch.setattr(orch.cli.click, "edit", lambda *_a, **_kw: yaml_content)

    result = runner.invoke(main, ["--db", str(db_path), "tickets", "edit", ticket_id])
    assert result.exit_code == 0

    result = runner.invoke(main, ["--db", str(db_path), "tickets", "show", ticket_id, "--json"])
    ticket = json.loads(result.output)
    assert ticket["title"] == "Edited via editor"


def test_edit_from_editor_aborted(tmp_path: Path, monkeypatch: object) -> None:
    """Aborting the editor during edit exits with error."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()
    ticket_id = _create_ticket(runner, db_path, tmp_path)

    import orch.cli

    monkeypatch.setattr(orch.cli.click, "edit", lambda *_a, **_kw: None)

    result = runner.invoke(main, ["--db", str(db_path), "tickets", "edit", ticket_id])
    assert result.exit_code != 0


def test_edit_nonexistent_ticket(tmp_path: Path) -> None:
    """Editing a nonexistent ticket fails."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()
    _create_ticket(runner, db_path, tmp_path)  # init DB

    result = runner.invoke(
        main, ["--db", str(db_path), "tickets", "edit", "ORCH-999", "--from-file", "/dev/null"]
    )
    assert result.exit_code != 0


def test_update_nonexistent_ticket(tmp_path: Path) -> None:
    """Updating a nonexistent ticket fails."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()
    _create_ticket(runner, db_path, tmp_path)  # init DB

    result = runner.invoke(
        main, ["--db", str(db_path), "tickets", "update", "ORCH-999", "--assignee", "x"]
    )
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_show_with_file_paths_and_test_expectations(tmp_path: Path) -> None:
    """Show output includes file_paths and test_expectations when present."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()
    ticket_id = _create_ticket(
        runner,
        db_path,
        tmp_path,
        file_paths="src/utils.py",
        test_expectations="test_slugify in tests/test_utils.py",
    )

    result = runner.invoke(main, ["--db", str(db_path), "tickets", "show", ticket_id])
    assert result.exit_code == 0
    assert "src/utils.py" in result.output
    assert "test_slugify" in result.output


def test_show_with_comments_human_readable(tmp_path: Path) -> None:
    """Human-readable show includes comments."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()
    ticket_id = _create_ticket(runner, db_path, tmp_path)

    runner.invoke(main, ["--db", str(db_path), "tickets", "comment", ticket_id, "A comment"])

    result = runner.invoke(main, ["--db", str(db_path), "tickets", "show", ticket_id])
    assert result.exit_code == 0
    assert "A comment" in result.output


def test_list_empty(tmp_path: Path) -> None:
    """Listing tickets when none exist shows a message."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()

    # Init DB by running any command that triggers DB creation
    result = runner.invoke(main, ["--db", str(db_path), "tickets", "list"])
    assert result.exit_code == 0
    assert "no tickets" in result.output.lower()


def test_create_with_title_flag(tmp_path: Path) -> None:
    """Quick creation with --title flag works."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()

    result = runner.invoke(
        main,
        ["--db", str(db_path), "tickets", "create", "--title", "Quick ticket"],
    )
    # This creates a ticket with empty description/acceptance_criteria
    # which is technically valid for Draft state
    assert result.exit_code == 0, result.output
