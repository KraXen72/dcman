from __future__ import annotations

import subprocess
from pathlib import Path

from ..config import REMOTE_USER
from ..container import container_engine, workspace_uses_feature
from ..errors import CmdError

CODEX_CLI_FEATURE_ID = "codex-cli"
CODEX_HOST_AUTH = Path.home() / ".codex" / "auth.json"


def _copy_auth_to_container(container_id: str, *, user: str) -> None:
	try:
		auth_bytes = CODEX_HOST_AUTH.read_bytes()
	except OSError as exc:
		raise CmdError(f"failed to read host Codex auth file {CODEX_HOST_AUTH}: {exc}") from exc

	engine = container_engine()
	script = r"""
set -eu
user="$1"
home="$(getent passwd "$user" | cut -d: -f6)"
[ -n "$home" ]
group="$(id -gn "$user")"
target_dir="${home}/.codex"
target="${target_dir}/auth.json"

install -d -m 700 -o "$user" -g "$group" "$target_dir"
tmp="$(mktemp "${target_dir}/.auth.json.tmp.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
cat > "$tmp"
chown "$user:$group" "$tmp"
chmod 600 "$tmp"
mv -f "$tmp" "$target"
trap - EXIT
"""
	result = subprocess.run(
		[engine, "exec", "-i", container_id, "sh", "-c", script, "sh", user],
		input=auth_bytes,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
	)
	if result.returncode != 0:
		stderr = result.stderr.decode(errors="replace").strip()
		detail = f": {stderr}" if stderr else ""
		raise CmdError(f"failed to copy Codex auth into the container{detail}")


def seed_auth_if_enabled(workspace: Path, container_id: str, *, user: str = REMOTE_USER) -> str | None:
	if not workspace_uses_feature(workspace, CODEX_CLI_FEATURE_ID):
		return None
	if not CODEX_HOST_AUTH.is_file():
		return f"Warning: codex-cli feature is enabled but {CODEX_HOST_AUTH} was not found."

	_copy_auth_to_container(container_id, user=user)
	return "Copied Codex CLI auth into the container volume."
