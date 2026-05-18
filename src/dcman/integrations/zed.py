from __future__ import annotations

import subprocess

from ..config import HOST_SSH_PUBKEY, REMOTE_USER, SSH_CONTAINER_PORT
from ..container import container_exec, container_exec_input, container_exec_ok

USER_HOME = f"/home/{REMOTE_USER}"
SSH_DIR = f"{USER_HOME}/.ssh"
AUTHORIZED_KEYS = f"{SSH_DIR}/authorized_keys"
ZED_STATE_DIR = f"{USER_HOME}/.local/share/zed"
LOCAL_DIR = f"{USER_HOME}/.local"
LOCAL_SHARE_DIR = f"{LOCAL_DIR}/share"


def _create_zed_user_dirs(container_id: str) -> bool:
	if not container_exec_ok(container_id, ["mkdir", "-p", SSH_DIR, ZED_STATE_DIR], user=REMOTE_USER):
		return False
	container_exec(container_id, ["chmod", "700", USER_HOME, SSH_DIR], user=REMOTE_USER)
	return True


def _ensure_zed_user_dirs(container_id: str) -> None:
	# Zed starts its remote proxy by creating logs/sockets below
	# ~/.local/share/zed. Some older images left ~/.local/share owned by root;
	# repair only that runtime path so existing containers do not need pruning.
	if _create_zed_user_dirs(container_id):
		return

	# Runtime root has a reduced capability set: it can chmod root-owned dirs,
	# but cannot chown them. Use the sticky bit on .local/share as a narrow
	# compatibility repair, then retry as the remote user.
	container_exec(container_id, ["chmod", "o+x", USER_HOME], user=REMOTE_USER)
	if container_exec_ok(container_id, ["test", "-d", LOCAL_DIR], user="root"):
		container_exec(container_id, ["chmod", "u+rwx,go+rx", LOCAL_DIR], user="root")
	container_exec(container_id, ["mkdir", "-p", LOCAL_SHARE_DIR], user="root")
	container_exec(container_id, ["chmod", "1777", LOCAL_SHARE_DIR], user="root")
	if not _create_zed_user_dirs(container_id):
		container_exec(container_id, ["mkdir", "-p", SSH_DIR, ZED_STATE_DIR], user=REMOTE_USER)
		container_exec(container_id, ["chmod", "700", USER_HOME, SSH_DIR], user=REMOTE_USER)


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

	_ensure_zed_user_dirs(container_id)

	pub_key = HOST_SSH_PUBKEY.read_text().strip()

	container_exec(container_id, ["mkdir", "-p", SSH_DIR], user=REMOTE_USER)
	has_authorized_keys = container_exec_ok(container_id, ["test", "-f", AUTHORIZED_KEYS], user=REMOTE_USER)
	key_is_present = has_authorized_keys and container_exec_ok(
		container_id,
		["grep", "-qxF", "--", pub_key, AUTHORIZED_KEYS],
		user=REMOTE_USER,
	)
	if not key_is_present:
		container_exec_input(container_id, ["tee", "-a", AUTHORIZED_KEYS], f"{pub_key}\n".encode(), user=REMOTE_USER)
	# SSH implementations ignore overly-open key files; enforce strict perms.
	container_exec(container_id, ["chmod", "700", SSH_DIR], user=REMOTE_USER)
	container_exec(container_id, ["chmod", "600", AUTHORIZED_KEYS], user=REMOTE_USER)

	if not container_exec_ok(container_id, ["pgrep", "-x", "dropbear"], user="root"):
		# Dropbear flags: -E log to stderr, -s disable password auth,
		# -g disable root login, -R auto-generate host keys if missing.
		container_exec(container_id, ["dropbear", "-p", str(SSH_CONTAINER_PORT), "-E", "-s", "-g", "-R"], user="root")
	return None


def open_editor(host_port: int, workspace_folder: str) -> str:
	zed_uri = f"ssh://{REMOTE_USER}@127.0.0.1:{host_port}{workspace_folder}"
	# Fire-and-forget keeps dcman attached to terminal shell lifecycle.
	subprocess.Popen(["zed", zed_uri])
	return zed_uri
