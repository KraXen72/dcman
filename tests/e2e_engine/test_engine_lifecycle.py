from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pexpect
import pytest

from tests.helpers import make_workspace


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

	workspace = make_workspace(
		tmp_path / "ws",
		{".devcontainer.json": (_repo_root() / ".devcontainer.json").read_text()},
	)

	env = os.environ.copy()
	env["DCMAN_CONTAINER_ENGINE"] = engine

	child = pexpect.spawn(
		sys.executable,
		["-m", "dcman", "start", "--idle-seconds", "600", "--no-rebuild"],
		cwd=str(workspace),
		env=env,
		encoding="utf-8",
		timeout=600,
	)
	child.expect([r"\$ ", r"# "], timeout=600)
	child.sendline("exit")
	child.expect(pexpect.EOF, timeout=600)

	list_result = subprocess.run(
		[sys.executable, "-m", "dcman", "list"],
		cwd=str(workspace),
		env=env,
		capture_output=True,
		text=True,
		check=False,
	)
	assert str(workspace) in list_result.stdout

	kill_result = subprocess.run(
		[sys.executable, "-m", "dcman", "kill", str(workspace)],
		cwd=str(workspace),
		env=env,
		capture_output=True,
		text=True,
		check=False,
	)
	assert "Stopped devcontainer" in kill_result.stdout or "No running devcontainer" in kill_result.stdout

	prune_result = subprocess.run(
		[sys.executable, "-m", "dcman", "prune", "--workspace", str(workspace), "-y"],
		cwd=str(workspace),
		env=env,
		capture_output=True,
		text=True,
		check=False,
	)
	assert "Removed" in prune_result.stdout or "Cleared dcman tracking state" in prune_result.stdout
