from __future__ import annotations

from pathlib import Path

import pytest

from dcman import cli
from tests.helpers import make_workspace


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

	with pytest.raises(SystemExit) as exc:
		cli._run_managed_shell(str(workspace), idle_seconds=123, preset=None, no_rebuild=True)
	assert exc.value.code == 0
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

	with pytest.raises(SystemExit) as exc:
		cli._run_managed_shell(str(workspace), idle_seconds=30, preset=None, no_rebuild=True)
	assert exc.value.code == 0
