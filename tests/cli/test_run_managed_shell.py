from __future__ import annotations

from pathlib import Path

import pytest

from dcman import cli
from dcman import state
from tests.helpers import invoke_in_click_context, make_workspace


@pytest.mark.cli
def test_run_managed_shell_registers_and_schedules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	workspace = make_workspace(tmp_path / "ws", {})
	events: list[str] = []

	monkeypatch.setattr(cli, "_container_up", lambda ws, **kwargs: ({"DCMAN_SSH_PORT": "2222"}, False))
	monkeypatch.setattr(cli, "wait_for_container", lambda ws: "container123")
	monkeypatch.setattr(cli, "remote_workspace_folder", lambda ws: f"/home/vscode/workspaces/{ws.name}")
	monkeypatch.setattr(cli.zed, "bootstrap_ssh", lambda *args, **kwargs: None)
	monkeypatch.setattr(cli, "detect_shell", lambda container_id, preset_cmd: ["/bin/bash"])

	orig_register = cli.register_session
	orig_unregister = cli.unregister_session
	monkeypatch.setattr(cli, "register_session", lambda ws, sid: (events.append("register"), orig_register(ws, sid))[1])
	monkeypatch.setattr(cli, "unregister_session", lambda ws, sid: (events.append("unregister"), orig_unregister(ws, sid))[1])

	def record_exec(container_id: str, command: list[str], *, user: str | None = None, workdir: str | None = None, env: dict | None = None) -> int:
		events.append("exec")
		assert workdir == f"/home/vscode/workspaces/{workspace.name}"
		return 0

	monkeypatch.setattr(cli, "container_exec_interactive", record_exec)

	scheduled: dict[str, int] = {}

	def record_schedule(ws: Path, delay: int) -> None:
		events.append("schedule")
		scheduled["delay"] = delay

	monkeypatch.setattr(cli, "schedule_idle_stop", record_schedule)

	def run() -> None:
		cli._run_managed_shell(str(workspace), idle_seconds=123, preset=None, no_rebuild=True)

	result = invoke_in_click_context(run)
	assert result.exit_code == 0
	assert events == ["register", "exec", "unregister", "schedule"]
	assert scheduled["delay"] == 123


@pytest.mark.cli
def test_run_managed_shell_skips_schedule_when_other_sessions_active(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	workspace = make_workspace(tmp_path / "ws", {})

	monkeypatch.setattr(cli, "_container_up", lambda ws, **kwargs: ({"DCMAN_SSH_PORT": "2222"}, False))
	monkeypatch.setattr(cli, "wait_for_container", lambda ws: "container123")
	monkeypatch.setattr(cli, "remote_workspace_folder", lambda ws: f"/home/vscode/workspaces/{ws.name}")
	monkeypatch.setattr(cli.zed, "bootstrap_ssh", lambda *args, **kwargs: None)
	monkeypatch.setattr(cli, "detect_shell", lambda container_id, preset_cmd: ["/bin/bash"])
	monkeypatch.setattr(cli, "container_exec_interactive", lambda *args, **kwargs: 0)
	monkeypatch.setattr(cli, "active_session_count", lambda ws: 1)

	monkeypatch.setattr(cli, "schedule_idle_stop", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("schedule should not run")))

	def run() -> None:
		cli._run_managed_shell(str(workspace), idle_seconds=30, preset=None, no_rebuild=True)

	result = invoke_in_click_context(run)
	assert result.exit_code == 0


@pytest.mark.cli
def test_idle_stop_fires_and_clears_current_timer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	workspace = make_workspace(tmp_path / "ws", {})
	state.save_state(workspace, {"workspace": str(workspace), "timer_token": "token-1", "timer_pid": 4242, "timer_started_at": 100})
	stopped: list[str] = []

	monkeypatch.setattr(cli.time, "sleep", lambda delay: None)
	monkeypatch.setattr(cli, "prune_stale_sessions", lambda ws: 0)
	monkeypatch.setattr(cli, "active_session_count", lambda ws: 0)
	monkeypatch.setattr(cli, "find_container", lambda ws: "container123")
	monkeypatch.setattr(cli, "stop_container", lambda container_id: stopped.append(container_id) or 0)

	result = invoke_in_click_context(lambda: cli.idle_stop.callback(workspace, 5, "token-1"))

	assert result.exit_code == 0
	assert stopped == ["container123"]
	payload = state.load_state(workspace)
	assert payload["timer_token"] is None
	assert payload["timer_pid"] is None
	assert payload["timer_started_at"] is None


@pytest.mark.cli
def test_idle_stop_ignores_stale_timer_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	workspace = make_workspace(tmp_path / "ws", {})
	state.save_state(workspace, {"workspace": str(workspace), "timer_token": "newer-token", "timer_pid": 4242})

	monkeypatch.setattr(cli.time, "sleep", lambda delay: None)
	monkeypatch.setattr(cli, "prune_stale_sessions", lambda ws: 0)
	monkeypatch.setattr(cli, "active_session_count", lambda ws: 0)
	monkeypatch.setattr(cli, "find_container", lambda ws: (_ for _ in ()).throw(AssertionError("stale timer should not inspect container")))
	monkeypatch.setattr(cli, "stop_container", lambda container_id: (_ for _ in ()).throw(AssertionError("stale timer should not stop container")))

	result = invoke_in_click_context(lambda: cli.idle_stop.callback(workspace, 5, "old-token"))

	assert result.exit_code == 0
	assert state.load_state(workspace)["timer_token"] == "newer-token"
