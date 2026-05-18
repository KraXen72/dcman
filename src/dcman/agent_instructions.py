from __future__ import annotations

import os
from pathlib import Path

from .config import REMOTE_USER
from .container import container_user_home, write_container_file
from .errors import CmdError

HOST_AGENTS_ENV = "DCMAN_AGENTS_MD"

HOME_RELATIVE_TARGETS = (
	".codex/AGENTS.md",
	".copilot/copilot-instructions.md",
	".config/zed/AGENTS.md",
)


def host_agents_path() -> Path:
	overridden = os.environ.get(HOST_AGENTS_ENV)
	if overridden:
		return Path(overridden).expanduser()
	config_home = os.environ.get("XDG_CONFIG_HOME")
	if config_home:
		return Path(config_home).expanduser() / "dcman" / "AGENTS.md"
	return Path.home() / ".config" / "dcman" / "AGENTS.md"


def sync_to_container(container_id: str, *, user: str = REMOTE_USER) -> str | None:
	source = host_agents_path()
	if not source.is_file():
		# Optional by design; avoid noisy starts for users without this file.
		return None

	try:
		content = source.read_bytes()
	except OSError as exc:
		raise CmdError(f"failed to read global agent instructions file {source}: {exc}") from exc

	home = container_user_home(container_id, user)
	try:
		for relative_target in HOME_RELATIVE_TARGETS:
			write_container_file(container_id, f"{home}/{relative_target}", content, user=user)
	except CmdError as exc:
		raise CmdError(f"failed to sync global agent instructions into the container: {exc}") from exc

	return f"Synced global agent instructions from {source}."
