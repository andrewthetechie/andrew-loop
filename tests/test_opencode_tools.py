"""Integration tests for opencode custom tools (.opencode/tools/).

These tests verify the full roundtrip: create a ticket via orch CLI,
invoke the TS tool via bun, verify the output matches.
"""

import json
import subprocess
from pathlib import Path

import yaml
from click.testing import CliRunner

from orch.cli import main

# Path to the tools relative to the project root
PROJECT_ROOT = Path(__file__).parent.parent
TOOLS_DIR = PROJECT_ROOT / ".opencode" / "tools"


def _create_ticket(runner: CliRunner, db_path: Path, tmp_path: Path) -> str:
    """Create a test ticket and return its ID."""
    ticket_file = tmp_path / "ticket.yaml"
    data = {
        "title": "Test ticket for tools",
        "description": "A test ticket for opencode tool integration.",
        "acceptance_criteria": "- [ ] Tool returns correct data",
    }
    ticket_file.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    result = runner.invoke(
        main,
        ["--db", str(db_path), "tickets", "create", "--from-file", str(ticket_file)],
    )
    assert result.exit_code == 0, result.output
    return result.output.strip().split()[-1]


def _run_tool(
    tool_name: str, args_json: str, db_path: Path, tmp_path: Path
) -> subprocess.CompletedProcess:
    """Run an opencode tool via bun and return the result."""
    runner_script = f"""
const tool = await import("{TOOLS_DIR / tool_name}.ts");
const def = tool.default;
const args = {args_json};
const context = {{ directory: ".", worktree: "{PROJECT_ROOT}" }};
try {{
    const result = await def.execute(args, context);
    console.log(JSON.stringify({{ ok: true, result }}));
}} catch (e) {{
    console.log(JSON.stringify({{ ok: false, error: e.message }}));
}}
"""
    script_file = tmp_path / "_runner.ts"
    script_file.write_text(runner_script)
    env = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/Users/aherrington/.bun/bin",
        "HOME": str(Path.home()),
        "ORCH_DB_PATH": str(db_path),
    }
    return subprocess.run(
        ["bun", "run", str(script_file)],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def test_ticket_read_returns_json(tmp_path: Path) -> None:
    """ticket-read tool returns structured ticket data."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()
    ticket_id = _create_ticket(runner, db_path, tmp_path)

    proc = _run_tool("ticket-read", json.dumps({"ticket_id": ticket_id}), db_path, tmp_path)
    assert proc.returncode == 0, f"stderr: {proc.stderr}"

    output = json.loads(proc.stdout)
    assert output["ok"] is True
    result = _parse_ticket_result(output)
    assert result["id"] == ticket_id
    assert result["title"] == "Test ticket for tools"
    assert result["state"] == "Draft"


def _parse_tool_output(proc: subprocess.CompletedProcess) -> dict:
    """Parse the JSON wrapper from the runner script."""
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    return json.loads(proc.stdout)


def _parse_ticket_result(output: dict) -> dict:
    """Extract the ticket dict from a ticket-read tool response."""
    raw = output["result"]
    return json.loads(raw) if isinstance(raw, str) else raw


def test_ticket_read_handles_missing_ticket(tmp_path: Path) -> None:
    """ticket-read returns an error message for a nonexistent ticket."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()
    _create_ticket(runner, db_path, tmp_path)  # init DB

    proc = _run_tool("ticket-read", json.dumps({"ticket_id": "ORCH-999"}), db_path, tmp_path)
    output = _parse_tool_output(proc)
    assert output["ok"] is True  # tool doesn't throw, returns error string
    assert "not found" in output["result"].lower() or "error" in output["result"].lower()


def test_ticket_update_changes_state(tmp_path: Path) -> None:
    """ticket-update with state moves the ticket."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()
    ticket_id = _create_ticket(runner, db_path, tmp_path)

    proc = _run_tool(
        "ticket-update",
        json.dumps({"ticket_id": ticket_id, "state": "To Do"}),
        db_path,
        tmp_path,
    )
    output = _parse_tool_output(proc)
    assert output["ok"] is True
    assert "Updated" in output["result"] or "ORCH" in output["result"]

    # Verify via ticket-read
    proc = _run_tool("ticket-read", json.dumps({"ticket_id": ticket_id}), db_path, tmp_path)
    output = _parse_tool_output(proc)
    result = _parse_ticket_result(output)
    assert result["state"] == "To Do"


def test_ticket_update_links_pr(tmp_path: Path) -> None:
    """ticket-update with linked_pr sets the PR URL."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()
    ticket_id = _create_ticket(runner, db_path, tmp_path)
    pr_url = "https://github.com/org/repo/pull/42"

    proc = _run_tool(
        "ticket-update",
        json.dumps({"ticket_id": ticket_id, "linked_pr": pr_url}),
        db_path,
        tmp_path,
    )
    output = _parse_tool_output(proc)
    assert output["ok"] is True

    # Verify
    proc = _run_tool("ticket-read", json.dumps({"ticket_id": ticket_id}), db_path, tmp_path)
    output = _parse_tool_output(proc)
    result = _parse_ticket_result(output)
    assert result["linked_pr"] == pr_url


def test_ticket_comment_adds_comment(tmp_path: Path) -> None:
    """ticket-comment tool adds a comment to the ticket."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()
    ticket_id = _create_ticket(runner, db_path, tmp_path)

    proc = _run_tool(
        "ticket-comment",
        json.dumps(
            {
                "ticket_id": ticket_id,
                "body": "Implementation complete",
                "author": "coder",
            }
        ),
        db_path,
        tmp_path,
    )
    output = _parse_tool_output(proc)
    assert output["ok"] is True

    # Verify comment exists
    proc = _run_tool("ticket-read", json.dumps({"ticket_id": ticket_id}), db_path, tmp_path)
    output = _parse_tool_output(proc)
    result = _parse_ticket_result(output)
    assert len(result["comments"]) == 1
    assert result["comments"][0]["body"] == "Implementation complete"
    assert result["comments"][0]["author"] == "coder"


def test_ticket_update_handles_invalid_state(tmp_path: Path) -> None:
    """ticket-update with an invalid state returns an error."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()
    ticket_id = _create_ticket(runner, db_path, tmp_path)

    proc = _run_tool(
        "ticket-update",
        json.dumps({"ticket_id": ticket_id, "state": "Nonsense"}),
        db_path,
        tmp_path,
    )
    output = _parse_tool_output(proc)
    assert output["ok"] is True  # tool doesn't throw
    assert "error" in output["result"].lower()


def test_ticket_update_no_args_returns_message(tmp_path: Path) -> None:
    """ticket-update with no update fields returns a helpful message."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()
    ticket_id = _create_ticket(runner, db_path, tmp_path)

    proc = _run_tool(
        "ticket-update",
        json.dumps({"ticket_id": ticket_id}),
        db_path,
        tmp_path,
    )
    output = _parse_tool_output(proc)
    assert output["ok"] is True
    assert "no updates" in output["result"].lower()
