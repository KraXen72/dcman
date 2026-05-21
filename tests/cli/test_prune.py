from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from dcman import cli


@pytest.mark.cli
def test_prune_accepts_workspace_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	workspace = tmp_path / "ws"
	workspace.mkdir()
	cleared: list[Path] = []

	monkeypatch.setattr(cli, "find_initialized_devcontainers", lambda ws: [])
	monkeypatch.setattr(cli, "_clear_known_host_for_workspace", lambda ws: None)
	monkeypatch.setattr(cli, "clear_workspace_tracking", lambda ws: cleared.append(ws))

	result = CliRunner().invoke(cli.prune_cmd, [str(workspace), "-y"], catch_exceptions=False)

	assert result.exit_code == 0
	assert f"No initialized devcontainers found for {workspace}." in result.output
	assert "Cleared dcman tracking state." in result.output
	assert cleared == [workspace.resolve()]


@pytest.mark.cli
def test_list_prints_positional_prune_usage(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(cli, "list_initialized_devcontainers", lambda: [])

	result = CliRunner().invoke(cli.list_cmd, [], catch_exceptions=False)

	assert result.exit_code == 0
	assert "dcman prune /absolute/path/to/workspace" in result.output
	assert "dcman prune select" in result.output
