from __future__ import annotations

import json
import subprocess
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

from .config import DEVCONTAINER_TEMPLATE_URL, WORKSPACE_DEST
from .errors import CmdError
from .process import run
from .state import load_state, save_state

# Handles devcontainer/container discovery and lifecycle by interrogating Podman
# metadata, then mapping containers back to local workspaces.


def _non_empty_lines(text: str) -> list[str]:
	# Podman often emits trailing newlines; normalize once for all callers.
	return [line.strip() for line in text.splitlines() if line.strip()]


def _workspace_from_inspect(container: dict[str, Any]) -> str | None:
	config = container.get("Config")
	labels: dict[str, Any] = {}
	if isinstance(config, dict):
		raw_labels = config.get("Labels")
		if isinstance(raw_labels, dict):
			labels = raw_labels

	workspace = labels.get("devcontainer.local_folder")
	if isinstance(workspace, str) and workspace.strip():
		# Normalize value from inspect output to match local path comparisons.
		return str(Path(workspace).expanduser().resolve())

	# Fallback for cases where label metadata is missing: infer workspace from
	# the bind mount used by this project.
	mounts = container.get("Mounts")
	if not isinstance(mounts, list):
		return None
	for mount in mounts:
		if not isinstance(mount, dict):
			continue
		if mount.get("Destination") != WORKSPACE_DEST:
			continue
		source = mount.get("Source")
		if isinstance(source, str) and source.strip():
			return str(Path(source).expanduser().resolve())
	return None


def _is_devcontainer(container: dict[str, Any]) -> bool:
	config = container.get("Config")
	if not isinstance(config, dict):
		return False
	labels = config.get("Labels")
	if not isinstance(labels, dict):
		return False
	# The devcontainer CLI stamps labels with the `devcontainer.` prefix.
	return any(isinstance(key, str) and key.startswith("devcontainer.") for key in labels)


def list_initialized_devcontainers() -> list[dict[str, str]]:
	ids_result = run(["podman", "ps", "-a", "-q"], capture=True, check=False)
	# `-a` includes exited containers; `-q` returns just IDs.
	container_ids = _non_empty_lines(ids_result.stdout)
	if not container_ids:
		return []

	# Batch inspect is much faster than shelling out once per container.
	inspect_result = run(["podman", "inspect", *container_ids], capture=True, check=False)
	if inspect_result.returncode != 0:
		raise CmdError("failed to inspect podman containers")

	try:
		inspected = json.loads(inspect_result.stdout)
	except json.JSONDecodeError as exc:
		raise CmdError("failed to parse podman inspect output") from exc

	if isinstance(inspected, dict):
		# Podman can return one object or an array depending on invocation/path.
		containers = [inspected]
	elif isinstance(inspected, list):
		containers = [entry for entry in inspected if isinstance(entry, dict)]
	else:
		containers = []

	entries: list[dict[str, str]] = []
	for container in containers:
		if not _is_devcontainer(container):
			continue
		workspace = _workspace_from_inspect(container)
		if workspace is None:
			continue

		container_id = container.get("Id")
		if not isinstance(container_id, str) or not container_id:
			continue

		name = container.get("Name")
		# Podman names are usually prefixed with "/" in inspect output.
		short_name = name.lstrip("/") if isinstance(name, str) else ""

		status = "unknown"
		state = container.get("State")
		if isinstance(state, dict):
			raw_status = state.get("Status")
			if isinstance(raw_status, str) and raw_status:
				status = raw_status
		elif isinstance(state, str) and state:
			status = state

		entries.append(
			{
				"id": container_id,
				"short_id": container_id[:12],
				"name": short_name,
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
	label = f"devcontainer.local_folder={workspace}"
	result = run(["podman", "ps", "-q", "--filter", f"label={label}"], capture=True, check=False)
	lines = _non_empty_lines(result.stdout)
	if lines:
		return lines[0]

	# Older/manual containers may not have expected labels; infer from mounts.
	all_ids = run(["podman", "ps", "-q"], capture=True, check=False)
	for container_id in _non_empty_lines(all_ids.stdout):
		inspect = run(
			[
				"podman",
				"inspect",
				"-f",
				# Go-template: print source path for the mount targeting
				# /home/vscode/workspace so we can match local workspace path.
				f'{{{{range .Mounts}}}}{{{{if eq .Destination "{WORKSPACE_DEST}"}}}}{{{{.Source}}}}{{{{end}}}}{{{{end}}}}',
				container_id,
			],
			capture=True,
			check=False,
		)
		if inspect.returncode == 0 and inspect.stdout.strip() == str(workspace):
			return container_id
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
	cmd = ["devcontainer", "up", "--docker-path", "podman", "--workspace-folder", str(workspace)]
	if rebuild:
		# Recreate container to apply changed run args/features safely.
		cmd[2:2] = ["--remove-existing-container"]
	if no_cache:
		# Forces a cold build when debugging feature/image-layer issues.
		cmd[2:2] = ["--build-no-cache"]
	result = subprocess.run(cmd, env=env)
	if result.returncode != 0:
		raise CmdError(f"devcontainer up failed ({result.returncode})")


def podman_stop(container_id: str) -> int:
	# `-t 1` keeps shutdown quick but still gives PID 1 a moment to exit cleanly.
	return subprocess.run(["podman", "stop", "-t", "1", container_id]).returncode
