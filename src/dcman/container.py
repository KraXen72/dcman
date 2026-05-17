from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

from python_on_whales import DockerClient
from python_on_whales.components.container.cli_wrapper import Container
from python_on_whales.exceptions import DockerException

from .config import DEVCONTAINER_TEMPLATE_URL, WORKSPACE_DEST
from .errors import CmdError
from .state import load_state, save_state

# Handles devcontainer/container discovery and lifecycle by interrogating the
# active container engine (podman/docker) via python-on-whales,
# then mapping containers back to local workspaces.


def _format_docker_exception(exc: DockerException) -> str:
	# DockerException stores raw bytes from the engine's stdout/stderr; decode
	# them so we can surface readable detail in CmdError messages.
	parts = []
	if exc.stderr:
		parts.append(exc.stderr.decode(errors="replace").strip())
	if exc.stdout:
		parts.append(exc.stdout.decode(errors="replace").strip())
	return "\n".join(part for part in parts if part)


def _docker_cmd_error(message: str, exc: DockerException) -> CmdError:
	if details := _format_docker_exception(exc):
		return CmdError(f"{message}: {details}")
	return CmdError(message)


def _validate_engine_binary(name: str) -> str:
	# Only called when the user has explicitly set DCMAN_CONTAINER_ENGINE,
	# so an empty or missing binary is unambiguously a misconfiguration.
	engine = name.strip()
	if not engine:
		raise CmdError("DCMAN_CONTAINER_ENGINE is set but empty.")
	if shutil.which(engine) is None:
		raise CmdError(f"configured container engine {engine!r} was not found in PATH.")
	return engine


def container_engine() -> str:
	# Resolution order: explicit env override > podman > docker.
	# Podman is preferred when both are installed because it runs rootless by default.
	if requested := os.environ.get("DCMAN_CONTAINER_ENGINE"):
		return _validate_engine_binary(requested)

	for candidate in ("podman", "docker"):
		if shutil.which(candidate) is not None:
			return candidate

	raise CmdError("neither podman nor docker was found in PATH.")


@lru_cache(maxsize=1)
def _client() -> DockerClient:
	# Cached so all calls within one dcman invocation share the same client
	# instance without re-resolving the engine binary on every operation.
	return DockerClient(client_call=[container_engine()])


def _workspace_from_container(container: Container) -> str | None:
	labels = container.config.labels or {}
	workspace = labels.get("devcontainer.local_folder")
	if workspace:
		# Normalize value from inspect output to match local path comparisons.
		return str(Path(workspace).expanduser().resolve())

	# Fallback for cases where label metadata is missing: infer workspace from
	# the bind mount used by this project.
	for mount in container.mounts:
		if mount.destination != WORKSPACE_DEST:
			continue
		source = mount.source
		if source:
			return str(Path(source).expanduser().resolve())
	return None


def _is_devcontainer(container: Container) -> bool:
	labels = container.config.labels or {}
	# The devcontainer CLI stamps labels with the `devcontainer.` prefix.
	return any(key.startswith("devcontainer.") for key in labels)


def _list_containers(*, all_containers: bool) -> list[Container]:
	try:
		return _client().container.list(all=all_containers)
	except DockerException as exc:
		raise _docker_cmd_error("failed to list containers", exc)


def list_initialized_devcontainers() -> list[dict[str, str]]:
	entries: list[dict[str, str]] = []
	for container in _list_containers(all_containers=True):
		if not _is_devcontainer(container):
			continue
		workspace = _workspace_from_container(container)
		if workspace is None:
			continue

		container_id = container.id
		status = container.state.status or "unknown"

		entries.append(
			{
				"id": container_id,
				"short_id": container_id[:12],
				"name": container.name,
				"status": status,
				"workspace": workspace,
			}
		)

	return sorted(entries, key=lambda row: (row["workspace"], row["name"], row["id"]))


def find_initialized_devcontainers(workspace: Path) -> list[dict[str, str]]:
	target = str(workspace)
	return [row for row in list_initialized_devcontainers() if row["workspace"] == target]


def render_devcontainer_table(entries: list[dict[str, str]]) -> str:
	headers = ("#", "container (name/id)", "state", "workspace")
	rows = []
	for idx, entry in enumerate(entries, start=1):
		name_and_id = entry["short_id"]
		if entry["name"]:
			name_and_id = f"{entry['name']} ({entry['short_id']})"
		rows.append((str(idx), name_and_id, entry["status"], entry["workspace"]))

	widths = [len(header) for header in headers]
	for row in rows:
		for idx, value in enumerate(row):
			widths[idx] = max(widths[idx], len(value))

	fmt = "  ".join(f"{{:<{width}}}" for width in widths)
	# Fixed-width text table keeps output readable without extra dependencies.
	lines = [fmt.format(*headers), fmt.format(*["-" * width for width in widths])]
	lines.extend(fmt.format(*row) for row in rows)
	return "\n".join(lines)


def find_container(workspace: Path) -> str | None:
	target = str(workspace)
	label = f"devcontainer.local_folder={target}"
	try:
		matches = _client().container.list(filters={"label": label})
	except DockerException as exc:
		raise _docker_cmd_error("failed to list containers for workspace lookup", exc)
	if matches:
		return matches[0].id

	# Older/manual containers may not have expected labels; infer from mounts.
	for container in _list_containers(all_containers=False):
		if _workspace_from_container(container) == target:
			return container.id
	return None


def wait_for_container(workspace: Path, timeout: float = 10.0) -> str | None:
	deadline = time.time() + timeout
	while time.time() < deadline:
		container_id = find_container(workspace)
		if container_id:
			return container_id
		# Short poll interval balances responsiveness with low CPU overhead.
		time.sleep(0.25)
	return find_container(workspace)


def resolve_devcontainer_config_path(workspace: Path) -> Path | None:
	single_file = workspace / ".devcontainer.json"
	if single_file.is_file():
		# VS Code supports single-file config at workspace root.
		return single_file

	folder_config = workspace / ".devcontainer" / "devcontainer.json"
	if folder_config.is_file():
		# Also support canonical folder layout.
		return folder_config

	return None


def _feature_ref_matches(feature_ref: str, feature_id: str) -> bool:
	name = feature_ref.rstrip("/").rsplit("/", 1)[-1].split(":", 1)[0]
	return name == feature_id


def _json_feature_keys(path: Path) -> list[str] | None:
	try:
		data = json.loads(path.read_text())
	except (OSError, json.JSONDecodeError):
		return None

	features = data.get("features")
	if not isinstance(features, dict):
		return []
	return [key for key in features if isinstance(key, str)]


def _jsonc_like_feature_keys(path: Path) -> list[str]:
	try:
		lines = path.read_text(errors="replace").splitlines()
	except OSError:
		return []

	keys: list[str] = []
	in_features = False
	depth = 0
	for line in lines:
		stripped = line.strip()
		if stripped.startswith("//"):
			continue

		if not in_features:
			if re.match(r'"features"\s*:\s*{', stripped):
				in_features = True
				depth = stripped.count("{") - stripped.count("}")
			continue

		if depth == 1 and (match := re.match(r'"([^"]+)"\s*:', stripped)):
			keys.append(match.group(1))

		depth += stripped.count("{") - stripped.count("}")
		if depth <= 0:
			break
	return keys


def workspace_uses_feature(workspace: Path, feature_id: str) -> bool:
	for path in (workspace / ".devcontainer.json", workspace / ".devcontainer" / "devcontainer.json"):
		if not path.is_file():
			continue
		feature_keys = _json_feature_keys(path)
		if feature_keys is None:
			feature_keys = _jsonc_like_feature_keys(path)
		if any(_feature_ref_matches(key, feature_id) for key in feature_keys):
			return True
	return False


def ensure_devcontainer_config(workspace: Path) -> None:
	if resolve_devcontainer_config_path(workspace) is not None:
		return
	raise CmdError(
		"\n".join(
			[
				f"No devcontainer config found in {workspace}.",
				"Expected either .devcontainer.json or .devcontainer/devcontainer.json.",
				f"Hint: run `devcontainer templates apply -t {DEVCONTAINER_TEMPLATE_URL}`.",
			]
		)
	)


def devcontainer_hash(workspace: Path) -> str | None:
	content_digests: list[bytes] = []

	single_file = workspace / ".devcontainer.json"
	if single_file.is_file():
		content_digests.append(sha256(single_file.read_bytes()).digest())

	dc_dir = workspace / ".devcontainer"
	if dc_dir.is_dir():
		for path in sorted(dc_dir.rglob("*")):
			if path.is_file():
				content_digests.append(sha256(path.read_bytes()).digest())

	if not content_digests:
		return None

	h = sha256()
	# Sort digest list so hash result is stable regardless of filesystem walk order.
	for digest in sorted(content_digests):
		h.update(digest)
	return h.hexdigest()


def save_devcontainer_hash(workspace: Path) -> None:
	digest = devcontainer_hash(workspace)
	if digest is None:
		# If no config exists yet, do not mutate hash tracking.
		return
	state = load_state(workspace)
	state["devcontainer_hash"] = digest
	save_state(workspace, state)


def devcontainer_up(workspace: Path, *, rebuild: bool, no_cache: bool = False, env: dict[str, str]) -> None:
	cmd = [
		"devcontainer",
		"up",
		"--docker-path",
		container_engine(),
		"--workspace-folder",
		str(workspace),
	]
	# Splice optional flags at index 2 (right after "up") so they come before
	# --docker-path. cmd[2:2] is a zero-width slice insert, not a replacement.
	if rebuild:
		# Recreate container to apply changed run args/features safely.
		cmd[2:2] = ["--remove-existing-container"]
	if no_cache:
		# Forces a cold build when debugging feature/image-layer issues.
		cmd[2:2] = ["--build-no-cache"]
	result = subprocess.run(cmd, env=env)
	if result.returncode != 0:
		raise CmdError(f"devcontainer up failed ({result.returncode})")


def container_exec(
	container_id: str,
	command: list[str],
	*,
	user: str | None = None,
	workdir: str | None = None,
	env: dict[str, str] | None = None,
) -> str:
	# Non-interactive exec: captures and returns stdout as a string.
	# Raises CmdError if the command exits non-zero.
	try:
		return _client().container.execute(container_id, command, user=user, workdir=workdir, envs=env or {})
	except DockerException as exc:
		raise _docker_cmd_error(f"failed to execute command in container {container_id[:12]}", exc)


def container_exec_ok(container_id: str, command: list[str], *, user: str | None = None) -> bool:
	# Runs a command and returns True/False based on its exit code.
	# Exit code 1 is the POSIX convention for "condition false" (e.g. `test -x`);
	# any other non-zero code is an unexpected error and is re-raised.
	try:
		_client().container.execute(container_id, command, user=user)
	except DockerException as exc:
		if exc.return_code == 1:
			return False
		raise _docker_cmd_error(f"failed to execute command in container {container_id[:12]}", exc)
	return True


def container_exec_interactive(
	container_id: str,
	command: list[str],
	*,
	user: str | None = None,
	workdir: str | None = None,
	env: dict[str, str] | None = None,
) -> int:
	# python-on-whales manages subprocess I/O internally and cannot provide a
	# real interactive TTY (tab completion, arrow keys, and colors all break).
	# For the shell the user actually lives in, we bypass the library and let
	# subprocess inherit stdin/stdout/stderr directly from the calling process.
	cmd = [container_engine(), "exec", "-it"]
	if user:
		cmd += ["-u", user]
	if workdir:
		cmd += ["-w", workdir]
	for key, value in (env or {}).items():
		# Pass values explicitly rather than relying on the parent env being
		# forwarded, so only the intended vars reach the container.
		cmd += ["-e", f"{key}={value}"]
	cmd += [container_id, *command]
	return subprocess.run(cmd).returncode


def stop_container(container_id: str) -> int:
	# `time=1` keeps shutdown quick but still gives PID 1 a moment to exit cleanly.
	try:
		_client().container.stop(container_id, time=1)
	except DockerException as exc:
		return exc.return_code
	return 0


def remove_container(container_id: str) -> None:
	try:
		_client().container.remove(container_id, force=True)
	except DockerException as exc:
		raise _docker_cmd_error(f"failed to remove container {container_id[:12]}", exc)
