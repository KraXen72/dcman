from __future__ import annotations

from pathlib import Path

from ..config import REMOTE_USER
from ..container import container_user_home, workspace_uses_feature, write_container_file
from ..errors import CmdError

# Seed only auth.json instead of bind-mounting host ~/.codex into the sandbox.

CODEX_CLI_FEATURE_ID = "codex-cli"
CODEX_HOST_AUTH = Path.home() / ".codex" / "auth.json"


def _copy_auth_to_container(container_id: str, *, user: str) -> None:
	try:
		auth_bytes = CODEX_HOST_AUTH.read_bytes()
	except OSError as exc:
		raise CmdError(f"failed to read host Codex auth file {CODEX_HOST_AUTH}: {exc}") from exc

	try:
		write_container_file(
			container_id,
			f"{container_user_home(container_id, user)}/.codex/auth.json",
			auth_bytes,
			user=user,
			parent_mode="700",
		)
	except CmdError as exc:
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
