from __future__ import annotations

import subprocess
from typing import Any

from .errors import CmdError

# Thin subprocess wrapper used across modules to keep command error handling
# uniform and avoid repeating `capture_output/text/check` boilerplate.


def run(
	cmd: list[str],
	*,
	capture: bool = False,
	check: bool = True,
	env: dict[str, str] | None = None,
	input_data: str | None = None,
) -> subprocess.CompletedProcess[str]:
	# `text=True` gives decoded strings instead of raw bytes in stdout/stderr.
	kwargs: dict[str, Any] = {"text": True, "env": env}
	if input_data is not None:
		kwargs["input"] = input_data
	if capture:
		# Capture is opt-in so normal commands still stream directly to terminal.
		kwargs["stdout"] = subprocess.PIPE
		kwargs["stderr"] = subprocess.PIPE
	result = subprocess.run(cmd, **kwargs)
	if check and result.returncode != 0:
		# Include command text in the error so callers can surface actionable output.
		raise CmdError(f"command failed ({result.returncode}): {' '.join(cmd)}")
	return result
