"""Pre-flight validation for devcontainer configurations.

Houses checks that catch container-name problems early so the Dev Container
CLI and container engine never see an invalid argument.  All functions in
this module are self-contained (no imports from dcman.container) to avoid
circular dependencies.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from python_on_whales import DockerClient
from python_on_whales.exceptions import DockerException

from .config import DEFAULT_WORKSPACE_FOLDER
from .errors import CmdError

log = logging.getLogger(__name__)

_VARIABLE_PATTERN = re.compile(r"\$\{([^}]+)\}")

# Single source of truth for valid container-name characters
# (Docker and Podman both enforce [a-zA-Z0-9][a-zA-Z0-9_.-]*).
_VALID_CHARS_CLASS = r"a-zA-Z0-9_.-"
_VALID_NAME_RE = re.compile(rf"^[a-zA-Z0-9][{_VALID_CHARS_CLASS}]*$")


def _is_valid_container_name(name: str) -> bool:
	return bool(_VALID_NAME_RE.match(name))


def sanitize_container_name(name: str) -> str:
	"""Replace characters forbidden in container names with underscores.

	Podman and Docker both require container names to match
	``[a-zA-Z0-9][a-zA-Z0-9_.-]*``.  This function replaces any character
	outside that set with`_`and, if the result would start with a
	non-alphanumeric character, prepends a second underscore.

	The result is guaranteed to be a syntactically valid container name.
	"""
	sanitized = re.sub(rf"[^{_VALID_CHARS_CLASS}]", "_", name)
	if sanitized and not sanitized[0].isalnum():
		sanitized = "_" + sanitized
	return sanitized


def _resolve_name_template(template: str, workspace: Path) -> tuple[str, dict[str, str]]:
	"""Resolve VS Code / Dev Container CLI variables in *template*.

	Supports the subset of variables that are commonly used inside`runArgs``
	``--name`values:

	* ``${localWorkspaceFolderBasename}``
	* ``${localWorkspaceFolder}``
	* ``${containerWorkspaceFolder}``       (read from devcontainer config)
	* ``${containerWorkspaceFolderBasename}``
	* ``${localEnv:NAME}``
	* ``${userHome}``

	Unknown or container-side-only variables (e.g. ``${env:...}``,
	``${config:...}``) are left as-is because they cannot be resolved on the
	host.

	Returns the resolved string and a mapping of every substituted variable
	to its resolved value.
	"""
	config = _raw_devcontainer_config(workspace) or {}
	workspace_folder = config.get("workspaceFolder") or DEFAULT_WORKSPACE_FOLDER
	substitutions: dict[str, str] = {}

	def _resolve(m: re.Match) -> str:
		var = m.group(1)
		full = m.group(0)

		if var == "localWorkspaceFolderBasename":
			val = workspace.name
		elif var == "localWorkspaceFolder":
			val = str(workspace.resolve())
		elif var == "containerWorkspaceFolder":
			val = workspace_folder
		elif var == "containerWorkspaceFolderBasename":
			val = Path(workspace_folder).name
		elif var == "userHome":
			val = str(Path.home())
		elif var.startswith("localEnv:"):
			val = os.environ.get(var[9:], "")
		else:
			return full

		substitutions[full] = val
		return val

	resolved = _VARIABLE_PATTERN.sub(_resolve, template)
	return resolved, substitutions


def _config_label(config_path: Path) -> str:
	return ".devcontainer.json" if config_path.name == ".devcontainer.json" else ".devcontainer/devcontainer.json"


def _load_devcontainer_config(
	workspace: Path,
) -> tuple[Path, dict[str, Any]] | None:
	"""Find and parse the devcontainer config, returning (path, parsed_dict).

	Returns`None`if the file cannot be found, read, or parsed as a JSON
	object, so callers can treat a missing/invalid config the same as "no
	constraint".
	"""
	config_path = _find_devcontainer_config(workspace)
	if config_path is None:
		return None
	try:
		config = json.loads(config_path.read_text(errors="replace"))
	except (OSError, ValueError):
		return None
	if not isinstance(config, dict):
		return None
	return config_path, config


def validate_runargs_container_name(workspace: Path) -> None:
	"""Check that ``--name`in`runArgs`produces a valid container name.

	Reads the workspace's ``.devcontainer.json`(or
	``.devcontainer/devcontainer.json``), resolves variables in every
	``--name=...`` entry inside`runArgs``, and verifies the result is
	acceptable to the container engine.  Raises`CmdError`with a
	descriptive message and suggested fix if the name would be rejected.

	This check runs *before* the Dev Container CLI is invoked, so the user
	gets a clear Python-level error instead of a cryptic engine error.
	"""
	loaded = _load_devcontainer_config(workspace)
	if loaded is None:
		return

	config_path, config = loaded

	run_args = config.get("runArgs")
	if not isinstance(run_args, list):
		return

	config_label = _config_label(config_path)
	for arg in run_args:
		if not isinstance(arg, str):
			continue
		if not arg.startswith("--name="):
			continue

		name_template = arg[len("--name=") :]
		resolved_name, substitutions = _resolve_name_template(name_template, workspace)

		if "${" in resolved_name:
			continue

		if _is_valid_container_name(resolved_name):
			continue

		problem_var: str | None = None
		problem_val: str | None = None
		for var_full, var_value in substitutions.items():
			if not _is_valid_container_name(var_value):
				problem_var = var_full
				problem_val = var_value
				break

		lines: list[str] = [
			f"invalid devcontainer name {resolved_name!r}",
		]

		if problem_var is not None and problem_val is not None:
			lines.append(
				f"The {problem_var} variable for this folder, {problem_val!r}, would generate an invalid container name."
			)

		lines.append("To fix, either:")
		lines.append("  - Rename the folder to remove spaces and special characters (only [a-zA-Z0-9_.-] allowed)")
		sanitized = sanitize_container_name(resolved_name)
		lines.append(f"  - Override --name in {config_label} runArgs, e.g.:")
		lines.append(f'    "--name={sanitized}"')

		raise CmdError("\n".join(lines))


def check_container_name_conflict(workspace: Path, engine: str = "podman") -> None:
	"""Verify the container name does not collide with another (possibly stopped) container.

	After resolving `--name`from the workspace's devcontainer config, this function queries 
	the container engine for any running or stopped container with that name.  
	If one exists **and** its `devcontainer.local_folder` label points at a different 
	workspace **or** the container was created outside dcman entirely, it raises `CmdError`:
	the name collision would cause the Dev Container CLI's `--remove-existing-container` flag to
	destroy the wrong container.
	"""
	loaded = _load_devcontainer_config(workspace)
	if loaded is None:
		return

	config_path, config = loaded

	run_args = config.get("runArgs")
	if not isinstance(run_args, list):
		return

	config_label = _config_label(config_path)
	for arg in run_args:
		if not isinstance(arg, str):
			continue
		if not arg.startswith("--name="):
			continue

		name_template = arg[len("--name=") :]
		resolved_name, _ = _resolve_name_template(name_template, workspace)

		if not resolved_name or not _is_valid_container_name(resolved_name):
			continue

		try:
			client = DockerClient(client_call=[engine])
			existing = client.container.list(all=True, filters={"name": resolved_name})
		except DockerException:
			log.warning(
				"Could not check container name %r for conflicts: container engine %s is unavailable",
				resolved_name,
				engine,
			)
			return

		for cont in existing:
			labels = cont.config.labels or {}
			other_workspace = labels.get("devcontainer.local_folder")
			if other_workspace:
				if Path(other_workspace).resolve() != workspace.resolve():
					raise CmdError(
						f"Container name {resolved_name!r} is already used by"
						f" workspace {other_workspace!r}.\n"
						f"To fix, override --name in {config_label} runArgs with"
						" a unique value."
					)
			else:
				raise CmdError(
					f"Container name {resolved_name!r} is already in use by"
					" a container that is not managed by dcman.\n"
					f"To fix, override --name in {config_label} runArgs with"
					" a unique value."
				)


def _find_devcontainer_config(workspace: Path) -> Path | None:
	single = workspace / ".devcontainer.json"
	if single.is_file():
		return single
	nested = workspace / ".devcontainer" / "devcontainer.json"
	return nested if nested.is_file() else None


def _raw_devcontainer_config(workspace: Path) -> dict[str, Any] | None:
	loaded = _load_devcontainer_config(workspace)
	if loaded is None:
		return None
	return loaded[1]
