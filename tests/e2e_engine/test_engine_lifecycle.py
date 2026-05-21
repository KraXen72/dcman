from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pexpect
import pytest

from tests.helpers import make_workspace


_PHASE_STARTED_AT: float | None = None
_HOST_HOME = Path(os.environ.get("HOME", str(Path.home()))).expanduser()
_HOST_XDG_DATA_HOME = os.environ.get("XDG_DATA_HOME") or str(_HOST_HOME / ".local" / "share")


class _LiveLog:
	def write(self, data: str) -> int:
		sys.__stderr__.write(data)
		sys.__stderr__.flush()
		return len(data)

	def flush(self) -> None:
		sys.__stderr__.flush()


def _progress(message: str) -> None:
	timestamp = time.strftime("%H:%M:%S")
	sys.__stderr__.write(f"\n[e2e {timestamp}] {message}\n")
	sys.__stderr__.flush()


@contextmanager
def _phase(name: str) -> Iterator[None]:
	global _PHASE_STARTED_AT
	previous_started_at = _PHASE_STARTED_AT
	_PHASE_STARTED_AT = time.monotonic()
	_progress(f"start: {name}")
	try:
		yield
	finally:
		elapsed = time.monotonic() - _PHASE_STARTED_AT
		_progress(f"done: {name} ({elapsed:.1f}s)")
		_PHASE_STARTED_AT = previous_started_at


def _run_dcman(args: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
	cmd = [sys.executable, "-m", "dcman", *args]
	with _phase(f"dcman {' '.join(args)}"):
		proc = subprocess.Popen(
			cmd,
			cwd=str(cwd),
			env=env,
			stdout=subprocess.PIPE,
			stderr=subprocess.STDOUT,
			text=True,
		)
		output = []
		assert proc.stdout is not None
		for line in proc.stdout:
			output.append(line)
			sys.__stderr__.write(line)
			sys.__stderr__.flush()
		rc = proc.wait()
		_progress(f"exit {rc}: {' '.join(cmd)}")
		return subprocess.CompletedProcess(cmd, rc, stdout="".join(output), stderr="")


def _expect_shell_prompt(child: pexpect.spawn, *, timeout: int) -> None:
	deadline = time.monotonic() + timeout
	while True:
		remaining = deadline - time.monotonic()
		if remaining <= 0:
			output = child.before or ""
			raise pexpect.TIMEOUT(f"timed out after {timeout}s waiting for shell prompt\n{output}")
		result = child.expect([r"\$ ", r"# ", pexpect.EOF, pexpect.TIMEOUT], timeout=min(30, remaining))
		if result in {0, 1}:
			return
		if result == 2:
			output = child.before or ""
			raise pexpect.EOF(f"dcman start exited before shell prompt appeared\n{output}")
		elapsed = 0 if _PHASE_STARTED_AT is None else time.monotonic() - _PHASE_STARTED_AT
		_progress(f"still waiting for shell prompt ({elapsed:.0f}s elapsed, {int(remaining)}s before timeout)")


def _engine_binary() -> str | None:
	for candidate in ("podman", "docker"):
		if shutil.which(candidate):
			return candidate
	return None


def _repo_root() -> Path:
	return Path(__file__).resolve().parents[2]


def _dcman_env(engine: str) -> dict[str, str]:
	env = os.environ.copy()
	env["DCMAN_CONTAINER_ENGINE"] = engine
	env["PYTHONUNBUFFERED"] = "1"
	if engine == "podman":
		env["XDG_DATA_HOME"] = _HOST_XDG_DATA_HOME
	return env


def _kill_workspace_args(workspace: Path) -> list[str]:
	return ["kill", str(workspace)]


def _prune_workspace_args(workspace: Path) -> list[str]:
	return ["prune", str(workspace), "-y"]


def _cleanup_workspace(workspace: Path, env: dict[str, str]) -> None:
	for args in (_kill_workspace_args(workspace), _prune_workspace_args(workspace)):
		try:
			_run_dcman(args, cwd=workspace, env=env)
		except Exception:
			pass


@pytest.mark.e2e_engine
def test_start_list_kill_prune(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
	with capfd.disabled():
		_start_list_kill_prune(tmp_path)


def _start_list_kill_prune(tmp_path: Path) -> None:
	engine = _engine_binary()
	if engine is None:
		pytest.skip("no container engine available")

	_progress(f"using container engine: {engine}")
	workspace = make_workspace(
		tmp_path / "ws",
		{".devcontainer.json": (_repo_root() / ".devcontainer.json").read_text()},
	)
	_progress(f"workspace: {workspace}")

	env = _dcman_env(engine)

	child: pexpect.spawn | None = None
	try:
		with _phase("dcman start"):
			_progress("devcontainer build/pull/setup output follows")
			child = pexpect.spawn(
				sys.executable,
				["-m", "dcman", "start", "--idle-seconds", "2", "--no-rebuild"],
				cwd=str(workspace),
				env=env,
				encoding="utf-8",
				timeout=600,
			)
			child.logfile_read = _LiveLog()
			_progress("waiting for shell prompt")
			_expect_shell_prompt(child, timeout=600)
			_progress("shell prompt reached; exiting shell")
			child.sendline("exit")
			child.expect(pexpect.EOF, timeout=600)
			_progress("managed shell exited")

		list_result = _run_dcman(["list"], cwd=workspace, env=env)
		assert str(workspace) in list_result.stdout

		kill_result = _run_dcman(_kill_workspace_args(workspace), cwd=workspace, env=env)
		assert "Stopped devcontainer" in kill_result.stdout or "No running devcontainer" in kill_result.stdout

		prune_result = _run_dcman(_prune_workspace_args(workspace), cwd=workspace, env=env)
		assert "Removed" in prune_result.stdout or "Cleared dcman tracking state" in prune_result.stdout
	finally:
		if child is not None and child.isalive():
			try:
				child.sendline("exit")
				child.expect(pexpect.EOF, timeout=30)
			except Exception:
				child.close(force=True)
		_cleanup_workspace(workspace, env)
