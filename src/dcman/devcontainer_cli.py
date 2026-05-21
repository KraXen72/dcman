from __future__ import annotations

import json
import shutil
import subprocess
from functools import cache
from pathlib import Path
from typing import Any

from .errors import CmdError
from .process import run as run_process

# All direct calls to the external Dev Container CLI live here.  The CLI is the
# source of truth for config parsing/merging and registry metadata; dcman should
# not reimplement devcontainer.json semantics in Python.

_BINARY = "devcontainer"


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
) -> subprocess.CompletedProcess[str]:
	require()
	cmd = [_BINARY, *args]
	return run_process(cmd, env=env, capture=capture, check=False)


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
