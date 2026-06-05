from __future__ import annotations

from pathlib import Path

import pytest

from dcman import agent_instructions


@pytest.mark.unit
def test_sync_to_container_skips_when_source_hash_and_container_match(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	workspace = tmp_path / "ws"
	workspace.mkdir()
	source = tmp_path / "AGENTS.md"
	source.write_text("be direct\n")
	writes: list[tuple[str, str, bytes]] = []

	monkeypatch.setenv("DCMAN_AGENTS_MD", str(source))
	monkeypatch.setattr(agent_instructions, "container_user_home", lambda container_id, user: "/home/vscode")
	monkeypatch.setattr(
		agent_instructions,
		"write_container_file",
		lambda container_id, path, content, **kwargs: writes.append((container_id, path, content)),
	)

	assert agent_instructions.sync_to_container(workspace, "container1") == f"Synced global agent instructions from {source}."
	assert [path for _, path, _ in writes] == [
		"/home/vscode/.codex/AGENTS.md",
		"/home/vscode/.copilot/copilot-instructions.md",
		"/home/vscode/.config/zed/AGENTS.md",
		"/home/vscode/.config/opencode/AGENTS.md",
	]

	assert agent_instructions.sync_to_container(workspace, "container1") is None
	assert len(writes) == 4

	source.write_text("be concise\n")
	assert agent_instructions.sync_to_container(workspace, "container1") == f"Synced global agent instructions from {source}."
	assert len(writes) == 8

	assert agent_instructions.sync_to_container(workspace, "container2") == f"Synced global agent instructions from {source}."
	assert len(writes) == 12
