from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from dcman import config, container, devcontainer_cli, state
from tests.helpers import make_workspace, write_text


@pytest.mark.unit
def test_devcontainer_hash_root_file(tmp_path: Path) -> None:
	workspace = make_workspace(
		tmp_path / "ws",
		{".devcontainer.json": json.dumps({"name": "root"}) + "\n"},
	)

	digest = container.devcontainer_hash(workspace)
	assert digest is not None

	file_digest = hashlib.sha256((workspace / ".devcontainer.json").read_bytes()).digest()
	expected = hashlib.sha256(file_digest).hexdigest()
	assert digest == expected

	write_text(workspace / ".devcontainer.json", json.dumps({"name": "changed"}) + "\n")
	changed = container.devcontainer_hash(workspace)
	assert changed is not None
	assert changed != digest


@pytest.mark.unit
def test_devcontainer_hash_folder_config_snapshot(tmp_path: Path) -> None:
	workspace = make_workspace(
		tmp_path / "ws",
		{
			".devcontainer/devcontainer.json": json.dumps({"name": "nested"}) + "\n",
			".devcontainer/scripts/setup.sh": "echo hello\n",
		},
	)

	digest = container.devcontainer_hash(workspace)
	assert digest is not None

	files = [
		workspace / ".devcontainer/devcontainer.json",
		workspace / ".devcontainer/scripts/setup.sh",
	]
	content_digests = sorted(hashlib.sha256(path.read_bytes()).digest() for path in files)
	expected = hashlib.sha256(b"".join(content_digests)).hexdigest()
	assert digest == expected

	snapshot = container.devcontainer_config_snapshot(workspace)
	assert snapshot[".devcontainer/devcontainer.json"].strip() == '{"name": "nested"}'
	assert snapshot[".devcontainer/scripts/setup.sh"] == "echo hello\n"


@pytest.mark.unit
def test_stored_devcontainer_config_snapshot_round_trip(tmp_path: Path) -> None:
	workspace = make_workspace(
		tmp_path / "ws",
		{".devcontainer.json": json.dumps({"name": "root"}) + "\n"},
	)
	state.save_state(
		workspace,
		{
			"workspace": str(workspace),
			"devcontainer_snapshot": {"version": 1, "files": {".devcontainer.json": "snapshot text"}},
		},
	)
	snapshot = container.stored_devcontainer_config_snapshot(workspace)
	assert snapshot == {".devcontainer.json": "snapshot text"}


@pytest.mark.unit
def test_format_devcontainer_config_diff_plain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	workspace = make_workspace(
		tmp_path / "ws",
		{".devcontainer.json": json.dumps({"name": "before"}) + "\n"},
	)
	container.save_devcontainer_hash(workspace)

	write_text(workspace / ".devcontainer.json", json.dumps({"name": "after"}) + "\n")
	monkeypatch.setenv("DCMAN_DIFF_RENDERER", "plain")
	diff = container.format_devcontainer_config_diff(workspace)
	assert diff is not None
	assert "--- a/.devcontainer.json" in diff
	assert "+++ b/.devcontainer.json" in diff
	assert '"after"' in diff


@pytest.mark.unit
def test_remote_workspace_folder_uses_read_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	workspace = make_workspace(
		tmp_path / "ws",
		{".devcontainer.json": json.dumps({"name": "root"}) + "\n"},
	)

	payload = {
		"workspace": {},
		"mergedConfiguration": {"workspaceFolder": f"/home/vscode/workspaces/{workspace.name}"},
		"configuration": {},
	}

	container._devcontainer_config.cache_clear()
	monkeypatch.setattr(container, "container_engine", lambda: "podman")
	monkeypatch.setattr(devcontainer_cli, "read_configuration", lambda ws, docker_path: payload)

	assert container.remote_workspace_folder(workspace) == f"/home/vscode/workspaces/{workspace.name}"


@pytest.mark.unit
def test_remote_workspace_folder_falls_back_for_unresolved_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	workspace = make_workspace(
		tmp_path / "ws",
		{".devcontainer.json": json.dumps({"name": "root"}) + "\n"},
	)
	payload = {"configuration": {"workspaceFolder": "/home/vscode/workspaces/${localWorkspaceFolderBasename}"}}

	container._devcontainer_config.cache_clear()
	monkeypatch.setattr(container, "container_engine", lambda: "podman")
	monkeypatch.setattr(devcontainer_cli, "read_configuration", lambda ws, docker_path: payload)

	assert container.remote_workspace_folder(workspace) == config.DEFAULT_WORKSPACE_FOLDER
