from __future__ import annotations

import json
from pathlib import Path

import pytest

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
