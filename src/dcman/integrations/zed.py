from __future__ import annotations

import shlex
import subprocess

from ..config import HOST_SSH_PUBKEY, REMOTE_USER, SSH_CONTAINER_PORT, WORKSPACE_DEST
from ..container import container_exec


def bootstrap_ssh(container_id: str, host_port: int, *, clear_known_host: bool) -> str | None:
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
			# `pgrep -x dropbear` checks for an already-running Dropbear daemon
			# (-x = exact process name); the `||` means we only launch one if absent.
			# Dropbear flags: -E log to stderr, -s disable password auth,
			# -g disable root login, -R auto-generate host keys if missing.
			f"pgrep -x dropbear >/dev/null || dropbear -p {SSH_CONTAINER_PORT} -E -s -g -R",
		],
		user="root",
	)
	return None


def open_editor(host_port: int) -> str:
	zed_uri = f"ssh://{REMOTE_USER}@127.0.0.1:{host_port}{WORKSPACE_DEST}"
	# Fire-and-forget keeps dcman attached to terminal shell lifecycle.
	subprocess.Popen(["zed", zed_uri])
	return zed_uri
