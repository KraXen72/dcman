from __future__ import annotations

import socket
from pathlib import Path

from .container import container_exec_ok
from .state import load_state, save_state

# SSH utilities shared by lifecycle commands.


def alloc_ssh_port(workspace: Path) -> int:
	state = load_state(workspace)
	if port := state.get("ssh_port"):
		# Reuse one host port per workspace to keep SSH endpoints stable.
		return int(port)
	# Bind to port 0 to ask the kernel for any currently-free ephemeral port.
	with socket.socket() as sock:
		sock.bind(("", 0))
		port = sock.getsockname()[1]
	state["ssh_port"] = port
	# Persist so future runs reconnect to same endpoint.
	save_state(workspace, state)
	return port


def detect_shell(container_id: str, preset_cmd: str | None = None) -> list[str]:
	# Some minimal images only ship /bin/sh; probe before assuming bash.
	bash_ok = container_exec_ok(container_id, ["test", "-x", "/bin/bash"])
	if preset_cmd:
		if bash_ok:
			# `exec` replaces the bootstrap shell so signal handling/job control
			# behaves like a normal interactive login shell.
			# `-il`: -i (interactive) -l (login) — equivalent to `--login` long form.
			return ["/bin/bash", "--login", "-c", f"{preset_cmd}; exec /bin/bash -il"]
		return ["/bin/sh", "-l", "-c", f"{preset_cmd}; exec /bin/sh -il"]
	if bash_ok:
		# `--login` ensures profile scripts run similarly to normal terminal logins.
		return ["/bin/bash", "--login"]
	return ["/bin/sh", "-l"]
