from __future__ import annotations

import shlex
import socket
import subprocess
from pathlib import Path

from .config import HOST_SSH_PUBKEY, REMOTE_USER, SSH_CONTAINER_PORT
from .container import container_exec, container_exec_ok
from .state import load_state, save_state

# SSH bootstrap utilities: reserve host SSH port, choose an interactive shell,
# and make sure the container trusts the host key + runs Dropbear.


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
			return ["/bin/bash", "--login", "-c", f"{preset_cmd}; exec /bin/bash -il"]
		return ["/bin/sh", "-l", "-c", f"{preset_cmd}; exec /bin/sh -il"]
	if bash_ok:
		# `--login` ensures profile scripts run similarly to normal terminal logins.
		return ["/bin/bash", "--login"]
	return ["/bin/sh", "-l"]


def ssh_bootstrap(container_id: str, host_port: int, *, clear_known_host: bool) -> str | None:
	if clear_known_host:
		# Rebuilds rotate host keys; remove stale known_hosts entry to avoid
		# scary MITM prompts when reconnecting to 127.0.0.1:<port>.
		subprocess.run(
			["ssh-keygen", "-R", f"[127.0.0.1]:{host_port}"],
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
		)

	if not HOST_SSH_PUBKEY.exists():
		# Not fatal: user can still open a shell directly through the engine.
		return f"{HOST_SSH_PUBKEY} not found; skipping SSH bootstrap."

	# Quote key text because it is interpolated into a shell command string.
	pub_key = shlex.quote(HOST_SSH_PUBKEY.read_text().strip())
	ssh_dir = f"/home/{REMOTE_USER}/.ssh"

	container_exec(
		container_id,
		[
			"bash",
			"-c",
			f"mkdir -p {ssh_dir} && "
			# `-q` quiet, `-x` exact line, `-F` fixed string: avoid duplicates.
			f"grep -qxF {pub_key} {ssh_dir}/authorized_keys 2>/dev/null "
			f"|| echo {pub_key} >> {ssh_dir}/authorized_keys && "
			# OpenSSH ignores overly-open key files; enforce strict perms.
			f"chmod 700 {ssh_dir} && chmod 600 {ssh_dir}/authorized_keys",
		],
		user=REMOTE_USER,
	)

	container_exec(
		container_id,
		[
			"bash",
			"-c",
			# dropbear flags:
			# -E log to stderr, -s disable password logins, -g disable root logins,
			# -R auto-generate host keys if missing.
			f"pgrep -x dropbear >/dev/null || dropbear -p {SSH_CONTAINER_PORT} -E -s -g -R",
		],
		user="root",
	)
	return None
