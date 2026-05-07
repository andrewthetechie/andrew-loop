"""Tests for three-tier config loading."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from orch.config import Config


def test_defaults_when_no_config_files_exist(tmp_path: Path) -> None:
    """Loading config with no files produces sensible defaults."""
    config = Config.load(repo_root=tmp_path)

    # Webhook defaults
    assert config.webhook.url == ""
    assert config.webhook.triggers == ["Needs Human Review", "Human Merge"]

    # Harness defaults
    assert "opencode" in config.harness.command

    # Validation defaults
    assert config.validation.commands == []

    # Router defaults
    assert config.router.poll_interval == 10.0

    # Hindsight defaults
    assert config.hindsight.url == "http://localhost:8888"
    assert config.hindsight.bank_id == ""
    assert config.hindsight.api_key == ""

    # MCP defaults
    assert config.mcp.firecrawl.url == ""
    assert config.mcp.firecrawl.api_key == ""
    assert config.mcp.context7.api_key == ""
    assert config.mcp.pullmd.url == ""


def test_loads_repo_config_file(tmp_path: Path) -> None:
    """Config values are read from repo .orchestra/config.toml."""
    config_dir = tmp_path / ".orchestra"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        '[webhook]\nurl = "https://hooks.example.com"\n\n[router]\npoll_interval = 5.0\n'
    )

    config = Config.load(repo_root=tmp_path)

    assert config.webhook.url == "https://hooks.example.com"
    assert config.router.poll_interval == 5.0
    # Unset fields keep defaults
    assert config.webhook.triggers == ["Needs Human Review", "Human Merge"]
    assert config.hindsight.url == "http://localhost:8888"


def test_deep_merge_repo_overrides_global(tmp_path: Path) -> None:
    """Repo config overrides global on a per-key basis within nested tables."""
    # Set up global config
    global_dir = tmp_path / "global_home" / ".config" / "orchestra"
    global_dir.mkdir(parents=True)
    (global_dir / "config.toml").write_text(
        '[webhook]\nurl = "https://global.example.com"\n'
        'triggers = ["Needs Human Review"]\n\n'
        '[hindsight]\nurl = "http://global:8888"\n'
        'bank_id = "global-bank"\napi_key = "global-key"\n'
    )

    # Set up repo config — overrides only webhook.url and hindsight.bank_id
    repo_dir = tmp_path / "repo" / ".orchestra"
    repo_dir.mkdir(parents=True)
    (repo_dir / "config.toml").write_text(
        '[webhook]\nurl = "https://repo.example.com"\n\n[hindsight]\nbank_id = "repo-bank"\n'
    )

    config = Config.load(
        repo_root=tmp_path / "repo",
        global_config_path=global_dir / "config.toml",
    )

    # Repo overrides specific keys
    assert config.webhook.url == "https://repo.example.com"
    assert config.hindsight.bank_id == "repo-bank"
    # Global siblings preserved (deep merge, not replace)
    assert config.hindsight.url == "http://global:8888"
    assert config.hindsight.api_key == "global-key"
    # Webhook triggers: repo didn't set it, so global value preserved
    assert config.webhook.triggers == ["Needs Human Review"]


def test_overrides_trump_file_configs(tmp_path: Path) -> None:
    """Explicit overrides (CLI flags / env vars) beat file configs."""
    config_dir = tmp_path / ".orchestra"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        '[router]\npoll_interval = 5.0\n\n[webhook]\nurl = "https://file.example.com"\n'
    )

    config = Config.load(
        repo_root=tmp_path,
        overrides={
            "router": {"poll_interval": 1.0},
            "webhook": {"url": "https://cli.example.com"},
        },
    )

    assert config.router.poll_interval == 1.0
    assert config.webhook.url == "https://cli.example.com"


def test_override_precedence_cli_beats_repo_beats_global(tmp_path: Path) -> None:
    """Full three-tier test: CLI > repo > global > defaults."""
    global_dir = tmp_path / "home" / ".config" / "orchestra"
    global_dir.mkdir(parents=True)
    (global_dir / "config.toml").write_text(
        "[router]\npoll_interval = 30.0\n\n"
        '[hindsight]\nurl = "http://global:8888"\nbank_id = "global-bank"\n'
    )

    repo_dir = tmp_path / "repo" / ".orchestra"
    repo_dir.mkdir(parents=True)
    (repo_dir / "config.toml").write_text("[router]\npoll_interval = 15.0\n")

    config = Config.load(
        repo_root=tmp_path / "repo",
        global_config_path=global_dir / "config.toml",
        overrides={"router": {"poll_interval": 2.0}},
    )

    # CLI override wins
    assert config.router.poll_interval == 2.0
    # Global value preserved (repo didn't override hindsight)
    assert config.hindsight.url == "http://global:8888"
    assert config.hindsight.bank_id == "global-bank"


def test_validation_rejects_bad_poll_interval(tmp_path: Path) -> None:
    """Pydantic rejects non-numeric poll_interval."""
    config_dir = tmp_path / ".orchestra"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('[router]\npoll_interval = "not-a-number"\n')

    with pytest.raises(ValidationError, match="poll_interval"):
        Config.load(repo_root=tmp_path)


def test_validation_rejects_bad_triggers_type(tmp_path: Path) -> None:
    """Pydantic rejects non-list triggers."""
    config_dir = tmp_path / ".orchestra"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('[webhook]\ntriggers = "not-a-list"\n')

    with pytest.raises(ValidationError, match="triggers"):
        Config.load(repo_root=tmp_path)


def test_loads_mcp_nested_config(tmp_path: Path) -> None:
    """MCP sub-tables (firecrawl, context7, pullmd) load correctly from TOML."""
    config_dir = tmp_path / ".orchestra"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        '[mcp.firecrawl]\nurl = "https://fc.example.com"\napi_key = "fc-key"\n\n'
        '[mcp.context7]\napi_key = "c7-key"\n\n'
        '[mcp.pullmd]\nurl = "https://pullmd.example.com"\n'
    )

    config = Config.load(repo_root=tmp_path)

    assert config.mcp.firecrawl.url == "https://fc.example.com"
    assert config.mcp.firecrawl.api_key == "fc-key"
    assert config.mcp.context7.api_key == "c7-key"
    assert config.mcp.pullmd.url == "https://pullmd.example.com"


def test_deep_merge_preserves_mcp_siblings(tmp_path: Path) -> None:
    """Deep merge on mcp section: overriding firecrawl doesn't lose context7."""
    global_dir = tmp_path / "home" / ".config" / "orchestra"
    global_dir.mkdir(parents=True)
    (global_dir / "config.toml").write_text(
        '[mcp.firecrawl]\nurl = "https://global-fc.example.com"\napi_key = "global-fc-key"\n\n'
        '[mcp.context7]\napi_key = "global-c7-key"\n'
    )

    repo_dir = tmp_path / "repo" / ".orchestra"
    repo_dir.mkdir(parents=True)
    (repo_dir / "config.toml").write_text('[mcp.firecrawl]\nurl = "https://repo-fc.example.com"\n')

    config = Config.load(
        repo_root=tmp_path / "repo",
        global_config_path=global_dir / "config.toml",
    )

    # Repo overrides firecrawl.url
    assert config.mcp.firecrawl.url == "https://repo-fc.example.com"
    # Global firecrawl.api_key preserved
    assert config.mcp.firecrawl.api_key == "global-fc-key"
    # Global context7 untouched
    assert config.mcp.context7.api_key == "global-c7-key"
