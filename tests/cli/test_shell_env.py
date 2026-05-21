from __future__ import annotations

import click
from pathlib import Path
import pytest
from click.testing import CliRunner

from dcman import cli, config
from tests.helpers import make_workspace


@pytest.mark.cli
def test_shell_env_forwards_terminal_and_auth_vars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	workspace = make_workspace(tmp_path / "ws", {})
	monkeypatch.setenv("TERM", "xterm")
	monkeypatch.setenv("COLORTERM", "truecolor")
	monkeypatch.setenv("FORCE_COLOR", "1")
	monkeypatch.setenv("SHOULD_NOT_FORWARD", "nope")

	monkeypatch.setattr(cli, "_rich_color_system", lambda env: None)

	env = {
		config.AUTH_PROVIDERS["copilot"]: "token-123",
		"SHOULD_NOT_FORWARD": "nope",
		"DCMAN_SSH_PORT": "2222",
	}
	monkeypatch.setattr(cli, "_container_up", lambda ws, **kwargs: (env, False))
	monkeypatch.setattr(cli, "wait_for_container", lambda ws: "container123")
	monkeypatch.setattr(cli.zed, "bootstrap_ssh", lambda *args, **kwargs: None)
	monkeypatch.setattr(cli, "remote_workspace_folder", lambda ws: f"/home/vscode/workspaces/{ws.name}")
	monkeypatch.setattr(cli, "detect_shell", lambda container_id, preset_cmd: ["/bin/bash"])
	monkeypatch.setattr(cli, "schedule_idle_stop", lambda *args, **kwargs: None)

	captured: dict[str, dict[str, str]] = {}

	def record_exec(
		container_id: str,
		command: list[str],
		*,
		user: str | None = None,
		workdir: str | None = None,
		env: dict | None = None,
	) -> int:
		captured["env"] = env or {}
		return 0

	monkeypatch.setattr(cli, "container_exec_interactive", record_exec)

	@click.command()
	def run() -> None:
		cli._run_managed_shell(str(workspace), idle_seconds=1, preset=None, no_rebuild=True)

	result = CliRunner().invoke(run)
	assert result.exit_code == 0

	container_env = captured["env"]
	assert container_env["TERM"] == "xterm"
	assert container_env["COLORTERM"] == "truecolor"
	assert container_env["FORCE_COLOR"] == "1"
	assert container_env[config.AUTH_PROVIDERS["copilot"]] == "token-123"
	assert "SHOULD_NOT_FORWARD" not in container_env
