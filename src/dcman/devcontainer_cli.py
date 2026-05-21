from __future__ import annotations

import json
import os
import select
import shutil
import signal
import subprocess
import sys
import time
from functools import cache
from pathlib import Path
from typing import Any

from .errors import CmdError
from .process import run as run_process

# All direct calls to the external Dev Container CLI live here.  The CLI is the
# source of truth for config parsing/merging and registry metadata; dcman should
# not reimplement devcontainer.json semantics in Python.

_BINARY = "devcontainer"
_PODMAN_STARTED_HANG_GRACE_SECONDS = 5.0


class ContainerStartedCliHang(RuntimeError):
	"""Dev Container CLI started the container but did not continue."""


def require() -> None:
	if shutil.which(_BINARY) is None:
		raise CmdError("devcontainer CLI was not found in PATH.")


@cache
def supports_up_no_lockfile() -> bool:
	# Support depends on the installed CLI version.
	require()
	result = run_process(
		[_BINARY, "up", "--help"],
		capture=True,
		check=False,
	)
	return "--no-lockfile" in result.stdout or "--no-lockfile" in result.stderr


def run(
	args: list[str],
	*,
	env: dict[str, str] | None = None,
	capture: bool = False,
	recover_podman_started_hang: bool = False,
) -> subprocess.CompletedProcess[str]:
	require()
	cmd = [_BINARY, *args]
	if recover_podman_started_hang and not capture:
		return _run_with_podman_started_hang_recovery(cmd, env=env)
	return run_process(cmd, env=env, capture=capture, check=False)


def _run_with_podman_started_hang_recovery(
	cmd: list[str],
	*,
	env: dict[str, str] | None,
) -> subprocess.CompletedProcess[str]:
	# Work around an upstream Dev Containers CLI + Podman race observed with
	# @devcontainers/cli 0.86/0.87 and Podman 5.8.2. In the create/recreate path
	# the CLI starts `podman events --filter event=start`, then `podman run`, and
	# waits for the matching start event. Sometimes the container starts and the
	# event is visible in `podman events --since ...`, but the live CLI listener
	# misses it and waits forever after printing "Container started".
	#
	# When upstream fixes that event wait (or replaces it with inspect fallback),
	# this helper and ContainerStartedCliHang should be removable; plain
	# run_process(..., check=False) should be enough again.
	proc = subprocess.Popen(  # noqa: S603
		cmd,
		env=env,
		stdout=subprocess.PIPE,
		stderr=subprocess.STDOUT,
		text=True,
		bufsize=1,
		start_new_session=True,
	)
	if proc.stdout is None:
		return _completed_after_process_group_cleanup(cmd, proc)

	saw_podman_run = False
	started_at: float | None = None
	progressed_after_started = False

	while proc.poll() is None:
		readable, _, _ = select.select([proc.stdout], [], [], 0.1)
		if not readable:
			if (
				started_at is not None
				and not progressed_after_started
				and proc.poll() is None
				and time.monotonic() - started_at >= _PODMAN_STARTED_HANG_GRACE_SECONDS
			):
				_terminate_process_group(proc)
				raise ContainerStartedCliHang("devcontainer up did not continue after Podman reported Container started")
			continue

		line = proc.stdout.readline()
		if not line:
			continue

		sys.stdout.write(line)
		sys.stdout.flush()

		text = line.strip()
		if "Start: Run: podman run " in line:
			saw_podman_run = True
		elif saw_podman_run and text == "Container started":
			started_at = time.monotonic()
		elif started_at is not None and text:
			progressed_after_started = True

	return _completed_after_process_group_cleanup(cmd, proc)


def _completed_after_process_group_cleanup(
	cmd: list[str],
	proc: subprocess.Popen[str],
) -> subprocess.CompletedProcess[str]:
	returncode = proc.wait()
	_terminate_process_group(proc)
	return subprocess.CompletedProcess(cmd, returncode)


def _terminate_process_group(proc: subprocess.Popen[str]) -> None:
	try:
		os.killpg(proc.pid, signal.SIGTERM)
	except ProcessLookupError:
		return
	try:
		proc.wait(timeout=3)
	except subprocess.TimeoutExpired:
		try:
			os.killpg(proc.pid, signal.SIGKILL)
		except ProcessLookupError:
			return
		proc.wait()


def _command_detail(result: subprocess.CompletedProcess[str]) -> str:
	return result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"


def _loads_last_json_object(output: str, *, required_key: str | None = None) -> dict[str, Any]:
	try:
		payload = json.loads(output)
	except json.JSONDecodeError:
		payload = None
	if isinstance(payload, dict) and (required_key is None or required_key in payload):
		return payload

	# `--log-format json` prints newline-delimited log objects before the final
	# payload.  Walk backwards and pick the last JSON object with the expected
	# shape instead of relying on a particular number of log lines.
	for line in reversed(output.splitlines()):
		text = line.strip()
		if not text.startswith("{"):
			continue
		try:
			payload = json.loads(text)
		except json.JSONDecodeError:
			continue
		if isinstance(payload, dict) and (required_key is None or required_key in payload):
			return payload
	requirement = f" containing key {required_key!r}" if required_key else ""
	raise CmdError(f"devcontainer CLI did not return a JSON object{requirement}.")


def run_json(
	args: list[str],
	*,
	env: dict[str, str] | None = None,
	required_key: str | None = None,
) -> dict[str, Any]:
	result = run(args, env=env, capture=True)
	if result.returncode != 0:
		raise CmdError(f"devcontainer {' '.join(args)} failed: {_command_detail(result)}")
	return _loads_last_json_object(result.stdout, required_key=required_key)


def read_configuration(workspace: Path, *, docker_path: str) -> dict[str, Any]:
	return run_json(
		[
			"read-configuration",
			"--workspace-folder",
			str(workspace),
			"--include-merged-configuration",
			"--log-format",
			"json",
			"--docker-path",
			docker_path,
		],
		required_key="configuration",
	)
