from __future__ import annotations

from pathlib import Path

import pytest

from dcman.integrations import codex


@pytest.mark.unit
def test_seed_auth_skips_when_source_hash_and_container_match(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	workspace = tmp_path / "ws"
	workspace.mkdir()
	auth_path = tmp_path / "auth.json"
	auth_path.write_text('{"token": "one"}\n')
	writes: list[tuple[str, str, bytes]] = []

	monkeypatch.setattr(codex, "CODEX_HOST_AUTH", auth_path)
	monkeypatch.setattr(codex, "workspace_uses_feature", lambda workspace, feature_id: True)
	monkeypatch.setattr(codex, "container_user_home", lambda container_id, user: "/home/vscode")
	monkeypatch.setattr(
		codex,
		"write_container_file",
		lambda container_id, path, content, **kwargs: writes.append((container_id, path, content)),
	)

	assert codex.seed_auth_if_enabled(workspace, "container1") == "Copied Codex CLI auth into the container volume."
	assert writes == [("container1", "/home/vscode/.codex/auth.json", b'{"token": "one"}\n')]

	assert codex.seed_auth_if_enabled(workspace, "container1") is None
	assert len(writes) == 1

	auth_path.write_text('{"token": "two"}\n')
	assert codex.seed_auth_if_enabled(workspace, "container1") == "Copied Codex CLI auth into the container volume."
	assert len(writes) == 2

	assert codex.seed_auth_if_enabled(workspace, "container2") == "Copied Codex CLI auth into the container volume."
	assert len(writes) == 3


@pytest.mark.unit
def test_seed_auth_keeps_existing_feature_and_missing_file_behavior(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	workspace = tmp_path / "ws"
	workspace.mkdir()
	auth_path = tmp_path / "missing-auth.json"

	monkeypatch.setattr(codex, "CODEX_HOST_AUTH", auth_path)
	monkeypatch.setattr(codex, "workspace_uses_feature", lambda workspace, feature_id: False)
	assert codex.seed_auth_if_enabled(workspace, "container1") is None

	monkeypatch.setattr(codex, "workspace_uses_feature", lambda workspace, feature_id: True)
	assert codex.seed_auth_if_enabled(workspace, "container1") == (
		f"Warning: codex-cli feature is enabled but {auth_path} was not found."
	)
