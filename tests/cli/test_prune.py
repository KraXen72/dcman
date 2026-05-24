from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from dcman import cli


def _entry(container_id: str, workspace: Path, *, name: str = "dcman_ws") -> dict[str, str]:
	return {
		"id": container_id,
		"short_id": container_id[:12],
		"name": name,
		"status": "exited",
		"workspace": str(workspace),
	}


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


@pytest.mark.cli
def test_prune_workspace_shows_estimated_freed_space(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	workspace = tmp_path / "ws"
	workspace.mkdir()
	removed: list[str] = []

	monkeypatch.setattr(cli, "find_initialized_devcontainers", lambda ws: [_entry("container123456", workspace)])
	monkeypatch.setattr(cli, "container_writable_size", lambda container_id: 1536)
	monkeypatch.setattr(cli, "remove_container", lambda container_id: removed.append(container_id))
	monkeypatch.setattr(cli, "_clear_known_host_for_workspace", lambda ws: None)
	monkeypatch.setattr(cli, "clear_workspace_tracking", lambda ws: None)

	result = CliRunner().invoke(cli.prune_cmd, [str(workspace)], input="y\n", catch_exceptions=False)

	assert result.exit_code == 0
	assert f"Delete 1 container(s) for {workspace.resolve()}, freeing about 1.5 kB?" in result.output
	assert f"Removed 1 container(s) for {workspace.resolve()}, freed about 1.5 kB." in result.output
	assert removed == ["container123456"]


@pytest.mark.cli
def test_prune_all_aggregates_estimated_freed_space(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	workspace_one = tmp_path / "one"
	workspace_two = tmp_path / "two"
	containers = [_entry("container111111", workspace_one), _entry("container222222", workspace_two)]
	removed: list[str] = []
	cleared: list[Path] = []

	monkeypatch.setattr(cli, "list_initialized_devcontainers", lambda: containers)
	monkeypatch.setattr(cli, "container_writable_size", lambda container_id: {"container111111": 1000, "container222222": 2500}[container_id])
	monkeypatch.setattr(cli, "remove_container", lambda container_id: removed.append(container_id))
	monkeypatch.setattr(cli, "_clear_known_host_for_workspace", lambda ws: None)
	monkeypatch.setattr(cli, "clear_workspace_tracking", lambda ws: cleared.append(ws))

	result = CliRunner().invoke(cli.prune_cmd, ["all", "-y"], catch_exceptions=False)

	assert result.exit_code == 0
	assert "writable size" in result.output
	assert "1.0 kB" in result.output
	assert "2.5 kB" in result.output
	assert "Removed 2 container(s), freed about 3.5 kB." in result.output
	assert removed == ["container111111", "container222222"]
	assert cleared == [workspace_one, workspace_two]


@pytest.mark.cli
def test_prune_select_renders_writable_size_column(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	workspace = tmp_path / "ws"
	containers = [_entry("container123456", workspace)]
	removed: list[str] = []

	monkeypatch.setattr(cli, "list_initialized_devcontainers", lambda: containers)
	monkeypatch.setattr(cli, "find_initialized_devcontainers", lambda ws: containers)
	monkeypatch.setattr(cli, "container_writable_size", lambda container_id: 1048576)
	monkeypatch.setattr(cli, "remove_container", lambda container_id: removed.append(container_id))
	monkeypatch.setattr(cli, "_clear_known_host_for_workspace", lambda ws: None)
	monkeypatch.setattr(cli, "clear_workspace_tracking", lambda ws: None)

	result = CliRunner().invoke(cli.prune_cmd, ["select", "-y"], input="1\n", catch_exceptions=False)

	assert result.exit_code == 0
	assert "writable size" in result.output
	assert "1.0 MB" in result.output
	assert f"Removed 1 container(s) for {workspace}, freed about 1.0 MB." in result.output
	assert removed == ["container123456"]


@pytest.mark.cli
def test_prune_continues_when_estimated_freed_space_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	workspace = tmp_path / "ws"
	containers = [_entry("container123456", workspace)]
	removed: list[str] = []

	monkeypatch.setattr(cli, "find_initialized_devcontainers", lambda ws: containers)
	monkeypatch.setattr(cli, "container_writable_size", lambda container_id: None)
	monkeypatch.setattr(cli, "remove_container", lambda container_id: removed.append(container_id))
	monkeypatch.setattr(cli, "_clear_known_host_for_workspace", lambda ws: None)
	monkeypatch.setattr(cli, "clear_workspace_tracking", lambda ws: None)

	result = CliRunner().invoke(cli.prune_cmd, [str(workspace), "-y"], catch_exceptions=False)

	assert result.exit_code == 0
	assert f"Removed 1 container(s) for {workspace.resolve()}. Estimated freed space: unknown." in result.output
	assert removed == ["container123456"]
