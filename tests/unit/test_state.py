from __future__ import annotations

from pathlib import Path

import pytest

from dcman import state


@pytest.mark.unit
def test_active_session_count_and_pruning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	workspace = tmp_path / "ws"
	workspace.mkdir()

	state.register_session(workspace, "abc123")
	assert state.active_session_count(workspace) == 1

	monkeypatch.setattr(state, "pid_alive", lambda pid: False)
	assert state.active_session_count(workspace) == 0


@pytest.mark.unit
def test_prune_removes_reused_pid_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	workspace = tmp_path / "ws"
	workspace.mkdir()

	monkeypatch.setattr(state, "pid_started_at", lambda pid: 222)
	session = state.register_session(workspace, "abc123")

	monkeypatch.setattr(state, "pid_alive", lambda pid: True)
	monkeypatch.setattr(state, "pid_started_at", lambda pid: 111)

	assert state.active_session_count(workspace) == 0
	assert not session.exists()


@pytest.mark.unit
def test_prune_removes_legacy_marker_for_unrelated_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	workspace = tmp_path / "ws"
	workspace.mkdir()
	state.ensure_state_dirs(workspace)
	session = state.sessions_dir(workspace) / "legacy.json"
	session.write_text('{"manager_pid": 123, "created_at": 1}\n')

	monkeypatch.setattr(state, "pid_alive", lambda pid: True)
	monkeypatch.setattr(state, "pid_cmdline", lambda pid: "bash")

	assert state.active_session_count(workspace) == 0
	assert not session.exists()


@pytest.mark.unit
def test_schedule_idle_stop_writes_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	workspace = tmp_path / "ws"
	workspace.mkdir()
	popen_calls: list[dict[str, object]] = []

	class DummyProc:
		pid = 4242

	monkeypatch.setattr(state, "secrets", type("Secrets", (), {"token_hex": staticmethod(lambda n: "deadbeef")}))

	def fake_popen(cmd: list[str], **kwargs: object) -> DummyProc:
		popen_calls.append({"cmd": cmd, **kwargs})
		return DummyProc()

	monkeypatch.setattr(state.subprocess, "Popen", fake_popen)

	state.schedule_idle_stop(workspace, delay=120)

	assert len(popen_calls) == 1
	call = popen_calls[0]
	assert call["cmd"] == [
		state.sys.executable,
		"-m",
		"dcman",
		"_idle-stop",
		"--workspace",
		str(workspace),
		"--delay",
		"120",
		"--token",
		"deadbeef",
	]
	assert call["stdin"] is state.subprocess.DEVNULL
	assert call["stdout"] is state.subprocess.DEVNULL
	assert call["stderr"] is state.subprocess.DEVNULL
	assert call["start_new_session"] is True
	assert "PYTHONPATH" in call["env"]

	payload = state.load_state(workspace)
	assert payload["timer_token"] == "deadbeef"
	assert payload["timer_pid"] == 4242
	assert payload["idle_delay_seconds"] == 120
	assert payload["timer_started_at"] is not None
