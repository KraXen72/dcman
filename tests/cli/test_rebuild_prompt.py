from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from dcman import cli, container, state
from tests.helpers import make_workspace, write_text


def _stub_container_up(monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, object]]) -> None:
	def fake_devcontainer_up(
		ws: Path,
		*,
		rebuild: bool,
		no_cache: bool = False,
		lockfile: bool = False,
		env: dict[str, str],
	) -> None:
		calls.append({"rebuild": rebuild, "env": env, "no_cache": no_cache, "lockfile": lockfile})

	monkeypatch.setattr(cli, "devcontainer_up", fake_devcontainer_up)


@pytest.mark.cli
def test_container_up_prompt_yes_rebuilds_and_saves_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	workspace = make_workspace(tmp_path / "ws", {".devcontainer.json": json.dumps({"name": "root"}) + "\n"})
	calls: list[dict[str, object]] = []

	_stub_container_up(monkeypatch, calls)
	monkeypatch.setattr(cli, "_initialized_container_ids", lambda ws: set())
	monkeypatch.setattr(cli, "wait_for_container", lambda ws: "container123")
	monkeypatch.setattr(cli, "_devcontainer_env", lambda ws: {"DCMAN_SSH_PORT": "2222"})
	monkeypatch.setattr(cli, "_sync_agent_instructions_if_configured", lambda *args, **kwargs: None)
	monkeypatch.setattr(cli, "_copy_codex_cli_auth_if_needed", lambda *args, **kwargs: None)
	monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: "y")

	env, rebuilt = cli._container_up(workspace, no_rebuild=False)
	assert rebuilt is True
	assert calls[0]["rebuild"] is True

	snapshot = container.stored_devcontainer_config_snapshot(workspace)
	assert snapshot is not None
	assert snapshot[".devcontainer.json"].strip() == '{"name": "root"}'
	assert env["DCMAN_SSH_PORT"] == "2222"


@pytest.mark.cli
def test_container_up_prompt_no_skips_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	workspace = make_workspace(tmp_path / "ws", {".devcontainer.json": json.dumps({"name": "root"}) + "\n"})
	calls: list[dict[str, object]] = []

	_stub_container_up(monkeypatch, calls)
	monkeypatch.setattr(cli, "_initialized_container_ids", lambda ws: set())
	monkeypatch.setattr(cli, "wait_for_container", lambda ws: "container123")
	monkeypatch.setattr(cli, "_devcontainer_env", lambda ws: {"DCMAN_SSH_PORT": "2222"})
	monkeypatch.setattr(cli, "_sync_agent_instructions_if_configured", lambda *args, **kwargs: None)
	monkeypatch.setattr(cli, "_copy_codex_cli_auth_if_needed", lambda *args, **kwargs: None)
	monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: "n")

	env, rebuilt = cli._container_up(workspace, no_rebuild=False)
	assert rebuilt is True
	assert calls[0]["rebuild"] is False
	assert container.stored_devcontainer_config_snapshot(workspace) is None
	assert state.load_state(workspace).get("devcontainer_hash") is None
	assert env["DCMAN_SSH_PORT"] == "2222"


@pytest.mark.cli
def test_container_up_prompt_abort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	workspace = make_workspace(tmp_path / "ws", {".devcontainer.json": json.dumps({"name": "root"}) + "\n"})

	monkeypatch.setattr(cli, "_initialized_container_ids", lambda ws: set())
	monkeypatch.setattr(cli, "wait_for_container", lambda ws: "container123")
	monkeypatch.setattr(cli, "_devcontainer_env", lambda ws: {"DCMAN_SSH_PORT": "2222"})
	monkeypatch.setattr(cli, "_sync_agent_instructions_if_configured", lambda *args, **kwargs: None)
	monkeypatch.setattr(cli, "_copy_codex_cli_auth_if_needed", lambda *args, **kwargs: None)
	monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: "a")

	with pytest.raises(click.Abort):
		cli._container_up(workspace, no_rebuild=False)


@pytest.mark.cli
def test_rebuild_prompt_shows_diff_with_click_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	workspace = make_workspace(tmp_path / "ws", {".devcontainer.json": json.dumps({"name": "before"}) + "\n"})
	container.save_devcontainer_hash(workspace)

	write_text(workspace / ".devcontainer.json", json.dumps({"name": "after"}) + "\n")
	monkeypatch.setenv("DCMAN_DIFF_RENDERER", "plain")

	@click.command()
	def prompt_cmd() -> None:
		cli._confirm_rebuild_for_config_change(workspace)

	result = CliRunner().invoke(prompt_cmd, input="n\n")
	assert result.exit_code == 0
	assert "--- a/.devcontainer.json" in result.output
	assert '"after"' in result.output
