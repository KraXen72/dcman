from __future__ import annotations

from pathlib import Path

from ..config import REMOTE_USER
from ..container import container_user_home, workspace_uses_feature, write_container_file
from ..errors import CmdError
from ..state import content_hash, mark_seeded_file_current, seeded_file_is_current

# Seed only auth.json instead of bind-mounting host ~/.codex into the sandbox.

CODEX_CLI_FEATURE_ID = "codex-cli"
CODEX_HOST_AUTH = Path.home() / ".codex" / "auth.json"
SEED_CACHE_KEY = "codex_auth"
SEED_CACHE_VERSION = 1


def _read_host_auth() -> bytes:
	try:
		return CODEX_HOST_AUTH.read_bytes()
	except OSError as exc:
		raise CmdError(f"failed to read host Codex auth file {CODEX_HOST_AUTH}: {exc}") from exc


def _copy_auth_to_container(container_id: str, auth_bytes: bytes, *, user: str) -> None:
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

	auth_bytes = _read_host_auth()
	auth_hash = content_hash(auth_bytes)
	if seeded_file_is_current(
		workspace,
		SEED_CACHE_KEY,
		version=SEED_CACHE_VERSION,
		container_id=container_id,
		source_path=CODEX_HOST_AUTH,
		source_hash=auth_hash,
	):
		return None

	_copy_auth_to_container(container_id, auth_bytes, user=user)
	mark_seeded_file_current(
		workspace,
		SEED_CACHE_KEY,
		version=SEED_CACHE_VERSION,
		container_id=container_id,
		source_path=CODEX_HOST_AUTH,
		source_hash=auth_hash,
	)
	return "Copied Codex CLI auth into the container volume."
