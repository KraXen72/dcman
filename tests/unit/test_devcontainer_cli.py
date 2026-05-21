from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from dcman import container
from dcman import devcontainer_cli
from tests.helpers import make_workspace, write_executable, write_json


@pytest.mark.unit
def test_loads_last_json_object_with_logs() -> None:
	output = "\n".join(
		[
			json.dumps({"level": "info", "msg": "first"}),
			json.dumps({"configuration": {"workspaceFolder": "/tmp"}}),
		]
	)
	payload = devcontainer_cli._loads_last_json_object(output, required_key="configuration")
	assert payload["configuration"]["workspaceFolder"] == "/tmp"


@pytest.mark.cli
def test_read_configuration_uses_fake_cli(tmp_path: Path, fake_bin_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	workspace = make_workspace(
		tmp_path / "ws",
		{".devcontainer.json": json.dumps({"name": "root"}) + "\n"},
	)
	payload_path = tmp_path / "payload.json"
	write_json(payload_path, {"configuration": {"workspaceFolder": "/home/vscode/workspaces/ws"}})

	args_path = tmp_path / "devcontainer-args.txt"
	script = """#!/usr/bin/env bash
set -euo pipefail
if [[ -n "${DCMAN_FAKE_DEVCONTAINER_ARGS:-}" ]]; then
  printf '%s\n' "$@" >> "$DCMAN_FAKE_DEVCONTAINER_ARGS"
fi
if [[ "$1" == "read-configuration" ]]; then
  cat "$DCMAN_FAKE_DEVCONTAINER_PATH"
  exit 0
fi
echo '{"configuration": {}}'
"""
	write_executable(fake_bin_dir / "devcontainer", script)
	monkeypatch.setenv("DCMAN_FAKE_DEVCONTAINER_PATH", str(payload_path))
	monkeypatch.setenv("DCMAN_FAKE_DEVCONTAINER_ARGS", str(args_path))

	config = devcontainer_cli.read_configuration(workspace, docker_path="podman")
	assert config["configuration"]["workspaceFolder"] == "/home/vscode/workspaces/ws"

	args = args_path.read_text().splitlines()
	assert "--docker-path" in args
	assert "podman" in args


@pytest.mark.unit
def test_devcontainer_up_runs_user_commands_after_podman_started_recovery(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	workspace = make_workspace(
		tmp_path / "ws",
		{".devcontainer.json": json.dumps({"name": "root"}) + "\n"},
	)
	calls: list[dict[str, object]] = []

	def fake_run(
		args: list[str],
		*,
		env: dict[str, str] | None = None,
		capture: bool = False,
		recover_podman_started_hang: bool = False,
	) -> subprocess.CompletedProcess[str]:
		calls.append(
			{
				"args": args,
				"env": env,
				"capture": capture,
				"recover_podman_started_hang": recover_podman_started_hang,
			}
		)
		if args[0] == "up":
			raise devcontainer_cli.ContainerStartedCliHang("started")
		return subprocess.CompletedProcess(["devcontainer", *args], 0)

	monkeypatch.setattr(container, "container_engine", lambda: "podman")
	monkeypatch.setattr(container, "wait_for_container", lambda workspace: "abc123")
	monkeypatch.setattr(devcontainer_cli, "run", fake_run)
	monkeypatch.setattr(devcontainer_cli, "supports_up_no_lockfile", lambda: True)

	container.devcontainer_up(workspace, rebuild=True, env={"PATH": "/bin"})

	assert calls[0]["args"][:2] == ["up", "--remove-existing-container"]
	assert calls[0]["recover_podman_started_hang"] is True
	assert calls[1]["args"] == [
		"run-user-commands",
		"--docker-path",
		"podman",
		"--workspace-folder",
		str(workspace),
		"--container-id",
		"abc123",
	]
