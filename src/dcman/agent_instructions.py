from __future__ import annotations

import os
from pathlib import Path

from .config import REMOTE_USER
from .container import container_user_home, write_container_file
from .errors import CmdError
from .state import content_hash, mark_seeded_file_current, seeded_file_is_current

SOURCE_PATH_ENV = "DCMAN_AGENTS_MD"
SEED_CACHE_KEY = "agent_instructions"
SEED_CACHE_VERSION = 1

HOME_RELATIVE_TOOL_PATHS = (
	Path(".codex/AGENTS.md"),
	Path(".copilot/copilot-instructions.md"),
)
CONFIG_RELATIVE_TOOL_PATHS = (
	Path("zed/AGENTS.md"),
)
# home-relative as well
DEFAULT_SOURCE_CONFIG_PATH = Path("dcman/AGENTS.md")


def _host_config_home() -> Path:
	config_home = os.environ.get("XDG_CONFIG_HOME")
	if config_home:
		return Path(config_home).expanduser()
	return Path.home() / ".config"


def source_path() -> Path:
	overridden = os.environ.get(SOURCE_PATH_ENV)
	if overridden:
		return Path(overridden).expanduser()
	return _host_config_home() / DEFAULT_SOURCE_CONFIG_PATH


def _host_tool_paths() -> tuple[Path, ...]:
	home = Path.home()
	config_home = _host_config_home()
	paths = [home / path for path in HOME_RELATIVE_TOOL_PATHS]
	paths += [config_home / path for path in CONFIG_RELATIVE_TOOL_PATHS]
	return tuple(paths)


def _container_tool_paths(home: str) -> tuple[str, ...]:
	paths = [f"{home}/{path.as_posix()}" for path in HOME_RELATIVE_TOOL_PATHS]
	paths += [f"{home}/.config/{path.as_posix()}" for path in CONFIG_RELATIVE_TOOL_PATHS]
	return tuple(paths)


def _ensure_source_file(source: Path) -> None:
	try:
		source.parent.mkdir(parents=True, exist_ok=True)
		source.touch(exist_ok=True)
	except OSError as exc:
		raise CmdError(f"failed to create global agent instructions file {source}: {exc}") from exc


def _host_link_conflict(source: Path, path: Path) -> str | None:
	if path.resolve(strict=False) == source:
		return None
	if path.exists() or path.is_symlink():
		return f"refusing to replace existing agent instructions file {path}"
	return None


def _link_host_path(source: Path, path: Path) -> str:
	if path.resolve(strict=False) == source:
		return f"Already configured: {path}"
	try:
		path.parent.mkdir(parents=True, exist_ok=True)
		path.symlink_to(source)
	except OSError as exc:
		raise CmdError(f"failed to link {path} -> {source}: {exc}") from exc
	return f"Linked: {path} -> {source}"


def configure_host_links() -> list[str]:
	source = source_path().resolve(strict=False)
	paths = _host_tool_paths()
	conflicts = [conflict for path in paths if (conflict := _host_link_conflict(source, path))]
	if conflicts:
		raise CmdError("\n".join(conflicts))
	_ensure_source_file(source)
	return [_link_host_path(source, path) for path in paths]


def unlink_host_links() -> list[str]:
	source = source_path().resolve(strict=False)
	paths = _host_tool_paths()
	links = [p for p in paths if p.is_symlink() and p.resolve(strict=False) == source]
	for link in links:
		link.unlink()
	return [str(link) for link in links]


def sync_to_container(workspace: Path, container_id: str, *, user: str = REMOTE_USER) -> str | None:
	source = source_path()
	if not source.is_file():
		# Optional by design; avoid noisy starts for users without this file.
		return None

	try:
		content = source.read_bytes()
	except OSError as exc:
		raise CmdError(f"failed to read global agent instructions file {source}: {exc}") from exc

	source_hash = content_hash(content)
	if seeded_file_is_current(
		workspace,
		SEED_CACHE_KEY,
		version=SEED_CACHE_VERSION,
		container_id=container_id,
		source_path=source,
		source_hash=source_hash,
	):
		return None

	paths = _container_tool_paths(container_user_home(container_id, user))
	try:
		for path in paths:
			write_container_file(container_id, path, content, user=user)
	except CmdError as exc:
		raise CmdError(f"failed to sync global agent instructions into the container: {exc}") from exc

	mark_seeded_file_current(
		workspace,
		SEED_CACHE_KEY,
		version=SEED_CACHE_VERSION,
		container_id=container_id,
		source_path=source,
		source_hash=source_hash,
	)
	return f"Synced global agent instructions from {source}."
