from __future__ import annotations

import secrets
from pathlib import Path

from ..config import REMOTE_USER
from ..container import container_exec, container_exec_input, workspace_uses_feature
from ..errors import CmdError

CODEX_CLI_FEATURE_ID = "codex-cli"
CODEX_HOST_AUTH = Path.home() / ".codex" / "auth.json"


def _container_user_home(container_id: str, user: str) -> str:
	passwd_entry = container_exec(container_id, ["getent", "passwd", user])
	fields = passwd_entry.strip().split(":")
	if len(fields) < 6 or not fields[5]:
		raise CmdError(f"failed to resolve home directory for container user {user!r}")
	return fields[5]


def _copy_auth_to_container(container_id: str, *, user: str) -> None:
	try:
		auth_bytes = CODEX_HOST_AUTH.read_bytes()
	except OSError as exc:
		raise CmdError(f"failed to read host Codex auth file {CODEX_HOST_AUTH}: {exc}") from exc

	home = _container_user_home(container_id, user)
	target_dir = f"{home}/.codex"
	target = f"{target_dir}/auth.json"
	tmp = f"{target_dir}/.auth.json.tmp.{secrets.token_hex(8)}"

	try:
		container_exec(container_id, ["mkdir", "-p", target_dir], user=user)
		container_exec(container_id, ["chmod", "700", target_dir], user=user)
		container_exec_input(container_id, ["dd", f"of={tmp}", "status=none"], auth_bytes, user=user)
		container_exec(container_id, ["chmod", "600", tmp], user=user)
		container_exec(container_id, ["mv", "-f", tmp, target], user=user)
	except CmdError as exc:
		try:
			container_exec(container_id, ["rm", "-f", tmp], user=user)
		except CmdError:
			pass
		raise CmdError(
			"failed to copy Codex auth into the container"
			f": {exc}. If this is an old root-owned codex-shared volume, remove or migrate that volume and rebuild."
		) from exc


def seed_auth_if_enabled(workspace: Path, container_id: str, *, user: str = REMOTE_USER) -> str | None:
	if not workspace_uses_feature(workspace, CODEX_CLI_FEATURE_ID):
		return None
	if not CODEX_HOST_AUTH.is_file():
		return f"Warning: codex-cli feature is enabled but {CODEX_HOST_AUTH} was not found."

	_copy_auth_to_container(container_id, user=user)
	return "Copied Codex CLI auth into the container volume."
