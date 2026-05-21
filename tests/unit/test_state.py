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
def test_schedule_idle_stop_writes_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	workspace = tmp_path / "ws"
	workspace.mkdir()

	class DummyProc:
		pid = 4242

	monkeypatch.setattr(state, "secrets", type("Secrets", (), {"token_hex": staticmethod(lambda n: "deadbeef")}))
	monkeypatch.setattr(state.subprocess, "Popen", lambda *args, **kwargs: DummyProc())

	state.schedule_idle_stop(workspace, delay=120)
	payload = state.load_state(workspace)
	assert payload["timer_token"] == "deadbeef"
	assert payload["timer_pid"] == 4242
	assert payload["idle_delay_seconds"] == 120
	assert payload["timer_started_at"] is not None
