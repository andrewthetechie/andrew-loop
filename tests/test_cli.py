from click.testing import CliRunner

from orch.cli import main


def test_cli_help_exits_zero() -> None:
    """The CLI prints help and exits with code 0."""
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "orch" in result.output.lower() or "usage" in result.output.lower()
