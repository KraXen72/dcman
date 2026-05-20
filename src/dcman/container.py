from __future__ import annotations

import json
import os
import posixpath
import secrets
import shutil
import subprocess
import sys
import time
from difflib import unified_diff
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from python_on_whales import DockerClient
from python_on_whales.components.container.cli_wrapper import Container
from python_on_whales.exceptions import DockerException

from . import devcontainer_cli
from .config import DEFAULT_DEVCONTAINER_TEMPLATE, DEFAULT_WORKSPACE_FOLDER, DEVCONTAINER_TEMPLATES, UidFastPath
from .errors import CmdError
from .rendering import render_diff, render_table
from .state import load_state, save_state

# Handles devcontainer/container discovery and lifecycle by interrogating the
# active container engine (podman/docker) via python-on-whales,
# then mapping containers back to local workspaces. Dev Container CLI calls are
# centralized in devcontainer_cli.py.


def _format_process_output(value: object) -> str:
	if isinstance(value, bytes):
		return value.decode(errors="replace").strip()
	if isinstance(value, str):
		return value.strip()
	return ""


def _format_docker_exception(exc: DockerException) -> str:
	# DockerException stores output from the engine's stdout/stderr; normalize it
	# so we can surface readable detail in CmdError messages.
	parts = [_format_process_output(exc.stderr), _format_process_output(exc.stdout)]
	return "\n".join(part for part in parts if part)


def _docker_cmd_error(message: str, exc: DockerException) -> CmdError:
	details = _format_docker_exception(exc)
	if details:
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


def _warn_if_docker(engine: str) -> None:
	# Docker support is intentionally best-effort; warn once per process without
	# making simple commands like `dcman list` noisy on every engine lookup.
	if engine != "docker" or os.environ.get("DCMAN_DOCKER_WARNING_SHOWN") == "1":
		return
	os.environ["DCMAN_DOCKER_WARNING_SHOWN"] = "1"
	print(
		"Warning: Docker support in dcman is experimental; rootless Podman is the primary tested engine.",
		file=sys.stderr,
	)


def require_devcontainer_cli() -> None:
	devcontainer_cli.require()


def container_engine() -> str:
	# Resolution order: explicit env override > podman > docker.
	# Podman is preferred when both are installed because it runs rootless by default.
	requested = os.environ.get("DCMAN_CONTAINER_ENGINE")
	if requested:
		engine = _validate_engine_binary(requested)
		_warn_if_docker(engine)
		return engine

	for candidate in ("podman", "docker"):
		if shutil.which(candidate) is not None:
			_warn_if_docker(candidate)
			return candidate

	raise CmdError("neither podman nor docker was found in PATH.")


@lru_cache(maxsize=1)
def _client() -> DockerClient:
	# Cached so all calls within one dcman invocation share the same client
	# instance without re-resolving the engine binary on every operation.
	return DockerClient(client_call=[container_engine()])


def _resolved_mount_source(mount: Any) -> str | None:
	source = getattr(mount, "source", None)
	if not source:
		return None
	try:
		return str(Path(source).expanduser().resolve())
	except OSError:
		return None


def _workspace_from_container(container: Container) -> str | None:
	labels = container.config.labels or {}
	workspace = labels.get("devcontainer.local_folder")
	if workspace:
		# Normalize value from inspect output to match local path comparisons.
		return str(Path(workspace).expanduser().resolve())

	# Fallback for older/manual containers where label metadata is missing. Match
	# by the host-side bind source instead of the container-side destination, since
	# newer templates intentionally use project-specific workspaceFolder paths.
	for mount in container.mounts:
		source = _resolved_mount_source(mount)
		if source and resolve_devcontainer_config_path(Path(source)) is not None:
			return source
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
	rows: list[tuple[str, str, str, str]] = []
	for idx, entry in enumerate(entries, start=1):
		name_and_id = entry["short_id"]
		if entry["name"]:
			name_and_id = f"{entry['name']} ({entry['short_id']})"
		rows.append((str(idx), name_and_id, entry["status"], entry["workspace"]))
	return render_table(headers, rows)


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
		for mount in container.mounts:
			if _resolved_mount_source(mount) == target:
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


@lru_cache(maxsize=32)
def _devcontainer_config(workspace: Path) -> dict[str, Any]:
	return devcontainer_cli.read_configuration(workspace, docker_path=container_engine())


def _mapping(value: object) -> dict[str, Any]:
	return value if isinstance(value, dict) else {}


def _valid_absolute_container_path(value: str) -> str | None:
	path = value.strip()
	if not path.startswith("/") or "${" in path:
		return None
	return path


def remote_workspace_folder(workspace: Path) -> str:
	if resolve_devcontainer_config_path(workspace) is None:
		return DEFAULT_WORKSPACE_FOLDER

	payload = _devcontainer_config(workspace)
	sections = (
		_mapping(payload.get("workspace")),
		_mapping(payload.get("mergedConfiguration")),
		_mapping(payload.get("configuration")),
	)
	for section in sections:
		workspace_folder = section.get("workspaceFolder")
		if isinstance(workspace_folder, str):
			resolved = _valid_absolute_container_path(workspace_folder)
			if resolved is not None:
				return resolved
	return DEFAULT_WORKSPACE_FOLDER


def devcontainer_feature_refs(workspace: Path) -> list[str]:
	if resolve_devcontainer_config_path(workspace) is None:
		return []

	payload = _devcontainer_config(workspace)
	for section_name in ("mergedConfiguration", "configuration"):
		features = _mapping(payload.get(section_name)).get("features")
		if isinstance(features, dict):
			return [ref for ref in features if isinstance(ref, str)]
	return []


def _feature_ref_matches(feature_ref: str, feature_id: str) -> bool:
	name = feature_ref.rstrip("/").rsplit("/", 1)[-1].split(":", 1)[0]
	return name == feature_id


def workspace_uses_feature(workspace: Path, feature_id: str) -> bool:
	return any(_feature_ref_matches(ref, feature_id) for ref in devcontainer_feature_refs(workspace))


def _raw_devcontainer_config(workspace: Path) -> dict[str, Any] | None:
	config_path = resolve_devcontainer_config_path(workspace)
	if config_path is None:
		return None
	try:
		payload = json.loads(config_path.read_text(errors="replace"))
	except (OSError, ValueError):
		return None
	return payload if isinstance(payload, dict) else None


def _host_matches_uid_fast_path(spec: UidFastPath) -> bool:
	if not hasattr(os, "getuid") or not hasattr(os, "getgid"):
		return False
	return os.getuid() == spec.uid and os.getgid() == spec.gid


def _config_matches_uid_fast_path(config: dict[str, Any], spec: UidFastPath) -> bool:
	image = config.get("image")
	remote_user = config.get("remoteUser")
	return isinstance(image, str) and image.lower().startswith(spec.image_prefix.lower()) and remote_user == spec.remote_user


def _use_template_uid_fast_path(workspace: Path) -> bool:
	# The Dev Container CLI creates a second `-uid` image whenever remote UID
	# updates are enabled. Some dcman template presets declare that their image
	# already contains the configured remote user with a fixed UID/GID. When the
	# host IDs match that preset, the rewrite layer is guaranteed to be a no-op.
	config = _raw_devcontainer_config(workspace)
	if config is None:
		return False

	for preset in DEVCONTAINER_TEMPLATES.values():
		spec = preset.uid_fast_path
		if spec is None:
			continue
		if _host_matches_uid_fast_path(spec) and _config_matches_uid_fast_path(config, spec):
			return True
	return False


def ensure_devcontainer_config(workspace: Path) -> None:
	if resolve_devcontainer_config_path(workspace) is not None:
		return
	template_hint = DEFAULT_DEVCONTAINER_TEMPLATE
	raise CmdError(
		"\n".join(
			[
				f"No devcontainer config found in {workspace}.",
				"Expected either .devcontainer.json or .devcontainer/devcontainer.json.",
				f"Hint: run `dcman template apply {template_hint}`.",
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


def devcontainer_config_snapshot(workspace: Path) -> dict[str, str]:
	# Store file text, not just a hash, so the next run can show a reviewable
	# diff before rebuilding from a changed config.
	files: dict[str, str] = {}

	single_file = workspace / ".devcontainer.json"
	if single_file.is_file():
		files[".devcontainer.json"] = single_file.read_text(errors="replace")

	dc_dir = workspace / ".devcontainer"
	if dc_dir.is_dir():
		for path in sorted(dc_dir.rglob("*")):
			if path.is_file():
				files[str(path.relative_to(workspace))] = path.read_text(errors="replace")

	return files


def stored_devcontainer_config_snapshot(workspace: Path) -> dict[str, str] | None:
	# Be strict about the on-disk shape because state files are user-writable
	# cache data; malformed snapshots should degrade to "no snapshot".
	snapshot = load_state(workspace).get("devcontainer_snapshot")
	if not isinstance(snapshot, dict):
		return None
	files = snapshot.get("files")
	if not isinstance(files, dict):
		return None
	return {path: content for path, content in files.items() if isinstance(path, str) and isinstance(content, str)}


def _render_unified_config_diff(old_files: dict[str, str], new_files: dict[str, str]) -> str:
	lines: list[str] = []
	for rel_path in sorted(set(old_files) | set(new_files)):
		old_text = old_files.get(rel_path)
		new_text = new_files.get(rel_path)
		if old_text == new_text:
			continue
		# Use git-style names so delta and other diff tools can colorize added,
		# removed, and changed files without needing real files on disk.
		old_name = f"a/{rel_path}" if old_text is not None else "/dev/null"
		new_name = f"b/{rel_path}" if new_text is not None else "/dev/null"
		lines.extend(
			unified_diff(
				[] if old_text is None else old_text.splitlines(keepends=True),
				[] if new_text is None else new_text.splitlines(keepends=True),
				fromfile=old_name,
				tofile=new_name,
			)
		)
	return "".join(lines)


def _format_diff_with_delta(diff: str) -> str | None:
	# delta gives better inline highlighting when present, but the security
	# prompt must work on a minimal system too, so this is a soft dependency.
	if not diff or shutil.which("delta") is None:
		return None
	result = subprocess.run(
		[
			"delta",
			"--paging=never",
			"--hunk-header-style",
			"file line-number syntax",
			"--file-style",
			"omit",
			"--hunk-header-decoration-style",
			"blue ul",
		],
		input=diff,
		stdout=subprocess.PIPE,
		stderr=subprocess.DEVNULL,
		text=True,
		check=False,
	)
	if result.returncode != 0:
		return None
	return result.stdout


def _format_diff_with_rich(diff: str) -> str:
	return render_diff(diff)


def format_devcontainer_config_diff(workspace: Path) -> str | None:
	old_files = stored_devcontainer_config_snapshot(workspace)
	if old_files is None:
		return None
	diff = _render_unified_config_diff(old_files, devcontainer_config_snapshot(workspace))
	if not diff:
		return ""

	renderer = os.environ.get("DCMAN_DIFF_RENDERER", "auto").strip().lower()
	if renderer == "rich":
		return _format_diff_with_rich(diff)
	if renderer == "plain":
		return diff
	# Prefer word-level/high-level rendering, then fall back to Rich syntax highlighting.
	return _format_diff_with_delta(diff) or _format_diff_with_rich(diff)


def save_devcontainer_hash(workspace: Path) -> None:
	digest = devcontainer_hash(workspace)
	if digest is None:
		# If no config exists yet, do not mutate hash tracking.
		return
	state = load_state(workspace)
	state["devcontainer_hash"] = digest
	# This snapshot represents the config the user has accepted as safe to use
	# for rebuild decisions; it intentionally updates only after accepted starts.
	state["devcontainer_snapshot"] = {
		"version": 1,
		"files": devcontainer_config_snapshot(workspace),
	}
	save_state(workspace, state)


def devcontainer_feature_metadata(ref: str) -> tuple[str | None, str | None, str | None]:
	# Let the official Dev Container CLI resolve floating tags and registry
	# metadata, so dcman does not need to understand every registry detail.
	payload = devcontainer_cli.run_json(
		[
			"features",
			"info",
			"verbose",
			ref,
			"--output-format",
			"json",
		]
	)
	canonical_id = payload.get("canonicalId")
	annotations = payload.get("manifest", {}).get("annotations", {})
	metadata_raw = annotations.get("dev.containers.metadata")
	metadata = json.loads(metadata_raw) if isinstance(metadata_raw, str) else {}
	name = metadata.get("name") if isinstance(metadata.get("name"), str) else None
	version = metadata.get("version") if isinstance(metadata.get("version"), str) else None
	return name, version, canonical_id if isinstance(canonical_id, str) else None


def devcontainer_template_apply(template_ref: str) -> None:
	result = devcontainer_cli.run(["templates", "apply", "-t", template_ref])
	if result.returncode != 0:
		raise CmdError(f"devcontainer templates apply failed ({result.returncode})")


def _devcontainer_lockfile_path(workspace: Path) -> Path | None:
	config_path = resolve_devcontainer_config_path(workspace)
	if config_path is None:
		return None
	# Match Dev Container CLI naming: a root `.devcontainer.json` gets a root
	# `.devcontainer-lock.json`; `.devcontainer/devcontainer.json` gets
	# `.devcontainer/devcontainer-lock.json`.
	name = ".devcontainer-lock.json" if config_path.name.startswith(".") else "devcontainer-lock.json"
	return config_path.parent / name


def _delete_created_lockfile(lockfile_path: Path | None, *, existed_before: bool) -> None:
	if lockfile_path is None or existed_before or not lockfile_path.is_file():
		return
	try:
		lockfile_path.unlink()
	except OSError as exc:
		raise CmdError(f"devcontainer lockfile was created but could not be deleted: {lockfile_path}: {exc}") from exc


def devcontainer_up(
	workspace: Path,
	*,
	rebuild: bool,
	no_cache: bool = False,
	lockfile: bool = False,
	env: dict[str, str],
) -> None:
	flags: list[str] = []
	if no_cache:
		# Forces a cold build when debugging feature/image-layer issues.
		flags.append("--build-no-cache")
	if rebuild:
		# Recreate container to apply changed run args/features safely.
		flags.append("--remove-existing-container")
	if not lockfile and devcontainer_cli.supports_up_no_lockfile():
		# Dev Container CLI now generates feature lockfiles by default. dcman
		# keeps that opt-in where the installed CLI supports suppression.
		flags.append("--no-lockfile")

	lockfile_path = _devcontainer_lockfile_path(workspace)
	lockfile_existed_before = lockfile_path.is_file() if lockfile_path else False

	args = [
		"up",
		*flags,
		"--docker-path",
		container_engine(),
		"--update-remote-user-uid-default",
		"never" if _use_template_uid_fast_path(workspace) else "on",
		"--workspace-folder",
		str(workspace),
	]
	result = devcontainer_cli.run(args, env=env)
	if not lockfile:
		_delete_created_lockfile(lockfile_path, existed_before=lockfile_existed_before)
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
		return cast(str, _client().container.execute(container_id, command, user=user, workdir=workdir, envs=env or {}))
	except DockerException as exc:
		raise _docker_cmd_error(f"failed to execute command in container {container_id[:12]}", exc)


def container_exec_input(
	container_id: str,
	command: list[str],
	input_bytes: bytes,
	*,
	user: str | None = None,
	workdir: str | None = None,
) -> str:
	# python-on-whales does not expose stdin for exec calls.
	cmd = [container_engine(), "exec", "-i"]
	if user:
		cmd += ["-u", user]
	if workdir:
		cmd += ["-w", workdir]
	cmd += [container_id, *command]
	result = subprocess.run(cmd, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
	if result.returncode != 0:
		details = "\n".join(
			part for part in (_format_process_output(result.stderr), _format_process_output(result.stdout)) if part
		)
		message = f"failed to execute command in container {container_id[:12]}"
		if details:
			message = f"{message}: {details}"
		raise CmdError(message)
	return result.stdout.decode(errors="replace")


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


def container_user_home(container_id: str, user: str) -> str:
	# Avoid assuming /home/<user>; templates can pick a different home.
	passwd_entry = container_exec(container_id, ["getent", "passwd", user])
	fields = passwd_entry.strip().split(":")
	if len(fields) < 6 or not fields[5]:
		raise CmdError(f"failed to resolve home directory for container user {user!r}")
	return fields[5]


def write_container_file(
	container_id: str,
	target: str,
	content: bytes,
	*,
	user: str | None = None,
	mode: str = "600",
	parent_mode: str | None = None,
) -> None:
	# Avoid leaving truncated files at paths tools read directly.
	target_dir = posixpath.dirname(target)
	target_name = posixpath.basename(target)
	tmp = posixpath.join(target_dir, f".{target_name}.tmp.{secrets.token_hex(8)}")

	try:
		container_exec(container_id, ["mkdir", "-p", target_dir], user=user)
		if parent_mode is not None:
			container_exec(container_id, ["chmod", parent_mode, target_dir], user=user)
		container_exec_input(container_id, ["dd", f"of={tmp}", "status=none"], content, user=user)
		container_exec(container_id, ["chmod", mode, tmp], user=user)
		container_exec(container_id, ["mv", "-f", tmp, target], user=user)
	except CmdError:
		try:
			container_exec(container_id, ["rm", "-f", tmp], user=user)
		except CmdError:
			pass
		raise


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
