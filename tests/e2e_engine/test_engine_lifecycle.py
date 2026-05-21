from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pexpect
import pytest

from tests.helpers import make_workspace


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


def _run_dcman(args: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
	cmd = [sys.executable, "-m", "dcman", *args]
	_progress(f"running: {' '.join(cmd)}")
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
			raise pexpect.TIMEOUT(f"timed out after {timeout}s waiting for shell prompt")
		result = child.expect([r"\$ ", r"# ", pexpect.EOF, pexpect.TIMEOUT], timeout=min(30, remaining))
		if result in {0, 1}:
			return
		if result == 2:
			raise pexpect.EOF("dcman start exited before shell prompt appeared")
		_progress(f"still waiting for shell prompt ({int(remaining)}s before timeout)")


def _engine_binary() -> str | None:
	for candidate in ("podman", "docker"):
		if shutil.which(candidate):
			return candidate
	return None


def _repo_root() -> Path:
	return Path(__file__).resolve().parents[2]


@pytest.mark.e2e_engine
def test_start_list_kill_prune(tmp_path: Path) -> None:
	engine = _engine_binary()
	if engine is None:
		pytest.skip("no container engine available")

	_progress(f"using container engine: {engine}")
	workspace = make_workspace(
		tmp_path / "ws",
		{".devcontainer.json": (_repo_root() / ".devcontainer.json").read_text()},
	)
	_progress(f"workspace: {workspace}")

	env = os.environ.copy()
	env["DCMAN_CONTAINER_ENGINE"] = engine

	_progress("starting managed shell; devcontainer build/pull/setup output follows")
	child = pexpect.spawn(
		sys.executable,
		["-m", "dcman", "start", "--idle-seconds", "600", "--no-rebuild"],
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

	kill_result = _run_dcman(["kill", str(workspace)], cwd=workspace, env=env)
	assert "Stopped devcontainer" in kill_result.stdout or "No running devcontainer" in kill_result.stdout

	prune_result = _run_dcman(["prune", str(workspace), "-y"], cwd=workspace, env=env)
	assert "Removed" in prune_result.stdout or "Cleared dcman tracking state" in prune_result.stdout
