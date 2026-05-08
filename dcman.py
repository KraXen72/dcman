#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

import click

__desc = "manage devcontainers with start, rebuild, kill, zed remote, and delayed idle stop"

WORKSPACE_DEST = "/home/vscode/workspace"
REMOTE_USER = "vscode"
STATE_ROOT = Path.home() / ".cache" / "devcontainer-lifecycle"
DEFAULT_IDLE_SECONDS = 300
SSH_CONTAINER_PORT = 2222  # must match --publish=...:2222 in devcontainer.json runArgs
HOST_SSH_PUBKEY = Path.home() / ".ssh" / "id_ed25519.pub"
DEVCONTAINER_TEMPLATE_URL = "ghcr.io/KraXen72/devcontainer-templates/fedora-sandbox"

# Map provider name → the env var that carries its token inside the container.
# Add new entries here to support additional auth providers (cursor, opencode, etc.).
# Secret-tool storage key is derived as: app=dcman-devcontainer, provider=<name>
AUTH_PROVIDERS: dict[str, str] = {
	"copilot": "COPILOT_GITHUB_TOKEN",
}

# Presets: map a short key to a command that runs automatically once the shell opens.
# Usage: dcman start <key>  (workspace defaults to cwd)
# Example: dcman start copilot  → opens shell and immediately runs "copilot --yolo"
PRESETS: dict[str, str] = {
	"copilot": "copilot --yolo",
}


class CmdError(RuntimeError):
	pass


class SecretToolUnavailable(CmdError):
	pass


def run(
	cmd: list[str], *, capture: bool = False, check: bool = True, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
	kwargs: dict[str, Any] = {"text": True, "env": env}
	if capture:
		kwargs["stdout"] = subprocess.PIPE
		kwargs["stderr"] = subprocess.PIPE
	result = subprocess.run(cmd, **kwargs)
	if check and result.returncode != 0:
		raise CmdError(f"command failed ({result.returncode}): {' '.join(cmd)}")
	return result


def workspace_path(raw: str | None) -> Path:
	return Path(raw or os.getcwd()).expanduser().resolve()


def workspace_key(workspace: Path) -> str:
	return sha256(str(workspace).encode("utf-8")).hexdigest()[:16]


def workspace_state_dir(workspace: Path) -> Path:
	return STATE_ROOT / workspace_key(workspace)


def state_file(workspace: Path) -> Path:
	return workspace_state_dir(workspace) / "state.json"


def sessions_dir(workspace: Path) -> Path:
	return workspace_state_dir(workspace) / "sessions"


def ensure_state_dirs(workspace: Path) -> None:
	sessions_dir(workspace).mkdir(parents=True, exist_ok=True)


def load_state(workspace: Path) -> dict[str, Any]:
	path = state_file(workspace)
	if not path.exists():
		return {"workspace": str(workspace)}
	try:
		data = json.loads(path.read_text())
	except Exception:
		return {"workspace": str(workspace)}
	if not isinstance(data, dict):
		return {"workspace": str(workspace)}
	data.setdefault("workspace", str(workspace))
	return data


def save_state(workspace: Path, data: dict[str, Any]) -> None:
	ensure_state_dirs(workspace)
	path = state_file(workspace)
	tmp = path.with_suffix(".tmp")
	tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
	tmp.replace(path)


def pid_alive(pid: int | None) -> bool:
	if not pid or pid <= 0:
		return False
	try:
		os.kill(pid, 0)
	except ProcessLookupError:
		return False
	except PermissionError:
		return True
	else:
		return True


def prune_stale_sessions(workspace: Path) -> int:
	ensure_state_dirs(workspace)
	removed = 0
	for entry in sessions_dir(workspace).glob("*.json"):
		try:
			payload = json.loads(entry.read_text())
		except Exception:
			entry.unlink(missing_ok=True)
			removed += 1
			continue
		pid = payload.get("manager_pid")
		if not isinstance(pid, int) or not pid_alive(pid):
			entry.unlink(missing_ok=True)
			removed += 1
	return removed


def active_session_files(workspace: Path) -> list[Path]:
	ensure_state_dirs(workspace)
	prune_stale_sessions(workspace)
	return sorted(sessions_dir(workspace).glob("*.json"))


def active_session_count(workspace: Path) -> int:
	return len(active_session_files(workspace))


def register_session(workspace: Path, session_id: str) -> Path:
	ensure_state_dirs(workspace)
	payload = {
		"session_id": session_id,
		"manager_pid": os.getpid(),
		"created_at": int(time.time()),
	}
	path = sessions_dir(workspace) / f"{session_id}.json"
	path.write_text(json.dumps(payload, indent=2) + "\n")
	return path


def unregister_session(workspace: Path, session_id: str) -> None:
	(sessions_dir(workspace) / f"{session_id}.json").unlink(missing_ok=True)


def clear_timer(workspace: Path) -> None:
	state = load_state(workspace)
	if state.get("timer_token") or state.get("timer_pid"):
		state["timer_token"] = None
		state["timer_pid"] = None
		state["timer_started_at"] = None
		save_state(workspace, state)


def schedule_idle_stop(workspace: Path, delay: int) -> None:
	ensure_state_dirs(workspace)
	token = secrets.token_hex(16)
	cmd = [
		sys.executable,
		str(Path(__file__).resolve()),
		"_idle-stop",
		"--workspace",
		str(workspace),
		"--delay",
		str(delay),
		"--token",
		token,
	]
	proc = subprocess.Popen(
		cmd,
		stdin=subprocess.DEVNULL,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
		start_new_session=True,
	)
	state = load_state(workspace)
	state["timer_token"] = token
	state["timer_pid"] = proc.pid
	state["timer_started_at"] = int(time.time())
	state["idle_delay_seconds"] = delay
	save_state(workspace, state)


def clear_all_sessions(workspace: Path) -> None:
	for entry in sessions_dir(workspace).glob("*.json"):
		entry.unlink(missing_ok=True)


def _workspace_from_inspect(container: dict[str, Any]) -> str | None:
	config = container.get("Config")
	labels: dict[str, Any] = {}
	if isinstance(config, dict):
		raw_labels = config.get("Labels")
		if isinstance(raw_labels, dict):
			labels = raw_labels

	workspace = labels.get("devcontainer.local_folder")
	if isinstance(workspace, str) and workspace.strip():
		return str(Path(workspace).expanduser().resolve())

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
	return any(isinstance(key, str) and key.startswith("devcontainer.") for key in labels)


def list_initialized_devcontainers() -> list[dict[str, str]]:
	ids_result = run(["podman", "ps", "-a", "-q"], capture=True, check=False)
	container_ids = [line.strip() for line in ids_result.stdout.splitlines() if line.strip()]
	if not container_ids:
		return []

	inspect_result = run(["podman", "inspect", *container_ids], capture=True, check=False)
	if inspect_result.returncode != 0:
		raise CmdError("failed to inspect podman containers")

	try:
		inspected = json.loads(inspect_result.stdout)
	except json.JSONDecodeError as exc:
		raise CmdError("failed to parse podman inspect output") from exc
	if isinstance(inspected, dict):
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


def clear_workspace_tracking(workspace: Path) -> None:
	ensure_state_dirs(workspace)
	clear_timer(workspace)
	clear_all_sessions(workspace)
	state = load_state(workspace)
	state["devcontainer_hash"] = None
	save_state(workspace, state)


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
	lines = [fmt.format(*headers), fmt.format(*["-" * width for width in widths])]
	lines.extend(fmt.format(*row) for row in rows)
	return "\n".join(lines)


def detect_shell(container_id: str, preset_cmd: str | None = None) -> list[str]:
	bash_ok = subprocess.run(["podman", "exec", container_id, "test", "-x", "/bin/bash"]).returncode == 0
	if preset_cmd:
		if bash_ok:
			return ["/bin/bash", "--login", "-c", f"{preset_cmd}; exec /bin/bash -il"]
		return ["/bin/sh", "-l", "-c", f"{preset_cmd}; exec /bin/sh -il"]
	if bash_ok:
		return ["/bin/bash", "--login"]
	return ["/bin/sh", "-l"]


def find_container(workspace: Path) -> str | None:
	label = f"devcontainer.local_folder={workspace}"
	result = run(["podman", "ps", "-q", "--filter", f"label={label}"], capture=True, check=False)
	lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
	if lines:
		return lines[0]

	all_ids = run(["podman", "ps", "-q"], capture=True, check=False)
	for container_id in [line.strip() for line in all_ids.stdout.splitlines() if line.strip()]:
		inspect = run(
			[
				"podman",
				"inspect",
				"-f",
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
		time.sleep(0.25)
	return find_container(workspace)


def require_secret_tool() -> None:
	if shutil.which("secret-tool") is None:
		raise SecretToolUnavailable("secret-tool was not found in PATH.")


def _secret_attrs(provider: str) -> list[str]:
	"""Return the secret-tool attribute list for a given provider."""
	return ["app", "dcman-devcontainer", "provider", provider]


def get_provider_token(provider: str) -> str | None:
	require_secret_tool()
	result = run(["secret-tool", "lookup", *_secret_attrs(provider)], capture=True, check=False)
	if result.returncode != 0:
		return None
	token = result.stdout.strip()
	return token or None


def store_provider_token(provider: str, token: str) -> None:
	require_secret_tool()
	label = f"dcman token: {provider}"
	proc = subprocess.run(
		["secret-tool", "store", f"--label={label}", *_secret_attrs(provider)],
		input=token + "\n",
		text=True,
	)
	if proc.returncode != 0:
		raise CmdError(f"failed to store {provider} token in secret-tool")


def clear_provider_token(provider: str) -> bool:
	require_secret_tool()
	result = run(["secret-tool", "clear", *_secret_attrs(provider)], capture=True, check=False)
	return result.returncode == 0


def build_env(with_tokens: bool) -> dict[str, str]:
	env = os.environ.copy()
	if not with_tokens:
		return env
	for provider, env_var in AUTH_PROVIDERS.items():
		try:
			token = get_provider_token(provider)
		except SecretToolUnavailable:
			click.echo("Warning: secret-tool not found; starting without any stored tokens.", err=True)
			return env
		if token:
			env[env_var] = token
		else:
			click.echo(f"Warning: no token found for provider {provider!r}; starting without it.", err=True)
	return env


def alloc_ssh_port(workspace: Path) -> int:
	"""Return the persisted SSH port for this workspace, allocating a free one if needed."""
	state = load_state(workspace)
	if port := state.get("ssh_port"):
		return int(port)
	with socket.socket() as s:
		s.bind(("", 0))
		port = s.getsockname()[1]
	state["ssh_port"] = port
	save_state(workspace, state)
	return port


def resolve_devcontainer_config_path(workspace: Path) -> Path | None:
	single_file = workspace / ".devcontainer.json"
	if single_file.is_file():
		return single_file

	folder_config = workspace / ".devcontainer" / "devcontainer.json"
	if folder_config.is_file():
		return folder_config

	return None


def ensure_devcontainer_config(workspace: Path) -> None:
	if resolve_devcontainer_config_path(workspace) is not None:
		return
	raise click.ClickException(
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
	for digest in sorted(content_digests):
		h.update(digest)
	return h.hexdigest()


def save_devcontainer_hash(workspace: Path) -> None:
	digest = devcontainer_hash(workspace)
	if digest is not None:
		state = load_state(workspace)
		state["devcontainer_hash"] = digest
		save_state(workspace, state)


def devcontainer_up(workspace: Path, *, rebuild: bool, no_cache: bool = False, env: dict[str, str]) -> None:
	cmd = ["devcontainer", "up", "--docker-path", "podman", "--workspace-folder", str(workspace)]
	if rebuild:
		cmd[2:2] = ["--remove-existing-container"]
	if no_cache:
		cmd[2:2] = ["--build-no-cache"]
	result = subprocess.run(cmd, env=env)
	if result.returncode != 0:
		raise CmdError(f"devcontainer up failed ({result.returncode})")


def podman_stop(container_id: str) -> int:
	return subprocess.run(["podman", "stop", "-t", "1", container_id]).returncode


def _container_up(
	ws: Path, *, force_rebuild: bool = False, no_rebuild: bool = False, no_cache: bool = False
) -> tuple[dict[str, str], bool]:
	"""Bring the devcontainer up and save the config hash. Returns (env, did_rebuild)."""
	ensure_devcontainer_config(ws)
	env = build_env(with_tokens=True)
	env["DCMAN_SSH_PORT"] = str(alloc_ssh_port(ws))

	if force_rebuild:
		devcontainer_up(ws, rebuild=True, no_cache=no_cache, env=env)
		save_devcontainer_hash(ws)
		return env, True

	current_hash = devcontainer_hash(ws)
	stored_hash = load_state(ws).get("devcontainer_hash")
	config_changed = current_hash is not None and current_hash != stored_hash

	if no_rebuild and config_changed:
		click.echo("Warning: devcontainer config has changed; run 'dcman rebuild' to apply.", err=True)
	elif config_changed:
		click.echo("Devcontainer config changed; rebuilding.")

	do_rebuild = not no_rebuild and config_changed
	devcontainer_up(ws, rebuild=do_rebuild, env=env)
	if not config_changed or do_rebuild:
		save_devcontainer_hash(ws)
	return env, do_rebuild


# fmt: off
def ssh_bootstrap(container_id: str, host_port: int, *, clear_known_host: bool) -> None:
	"""Inject the host public key and start Dropbear in the container (idempotent)."""
	if clear_known_host:
		subprocess.run(
			["ssh-keygen", "-R", f"[127.0.0.1]:{host_port}"],
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
		)

	if not HOST_SSH_PUBKEY.exists():
		click.echo(f"Warning: {HOST_SSH_PUBKEY} not found; skipping SSH bootstrap.", err=True)
		return

	pub_key = shlex.quote(HOST_SSH_PUBKEY.read_text().strip())
	ssh_dir = f"/home/{REMOTE_USER}/.ssh"

	run([
		"podman", "exec", "-u", REMOTE_USER, container_id, "bash", "-c",
		f"mkdir -p {ssh_dir} && "
		f"grep -qxF {pub_key} {ssh_dir}/authorized_keys 2>/dev/null "
		f"|| echo {pub_key} >> {ssh_dir}/authorized_keys && "
		f"chmod 700 {ssh_dir} && chmod 600 {ssh_dir}/authorized_keys",
	])

	run([
		"podman", "exec", "-u", "root", container_id, "bash", "-c",
		f"pgrep -x dropbear >/dev/null || dropbear -p {SSH_CONTAINER_PORT} -E -s -g -R",
	])
# fmt: on


def require_binaries() -> None:
	if shutil.which("devcontainer") is None:
		raise click.ClickException("devcontainer CLI was not found in PATH.")
	if shutil.which("podman") is None:
		raise click.ClickException("podman was not found in PATH.")


@click.group(help=__desc)
def cli() -> None:
	require_binaries()


def _run_shell(workspace: str | None, idle_seconds: int, preset: str | None, no_rebuild: bool) -> None:
	preset_cmd: str | None = None
	if preset:
		if preset not in PRESETS:
			raise click.ClickException(f"unknown preset {preset!r}. defined presets: {', '.join(PRESETS) or '(none)'}")
		preset_cmd = PRESETS[preset]

	ws = workspace_path(workspace)
	ensure_state_dirs(ws)
	prune_stale_sessions(ws)
	clear_timer(ws)

	env, did_rebuild = _container_up(ws, no_rebuild=no_rebuild)
	container_id = wait_for_container(ws)
	if not container_id:
		raise click.ClickException(f"no matching devcontainer found for {ws}")
	ssh_bootstrap(container_id, int(env["DCMAN_SSH_PORT"]), clear_known_host=did_rebuild)

	session_id = secrets.token_hex(8)
	register_session(ws, session_id)
	shell_cmd = detect_shell(container_id, preset_cmd)

	# Inject all known provider env vars into the shell environment.
	shell_env = {k: env[k] for k in AUTH_PROVIDERS.values() if k in env}

	try:
		env_args = []
		for k in shell_env:
			env_args += ["-e", k]
		rc = subprocess.run(
			["podman", "exec", "-it", "-u", REMOTE_USER, "-w", WORKSPACE_DEST, *env_args, container_id, *shell_cmd],
			env=env,
		).returncode
	finally:
		unregister_session(ws, session_id)
		prune_stale_sessions(ws)
		if active_session_count(ws) == 0:
			schedule_idle_stop(ws, idle_seconds)
			click.echo(f"Armed idle stop for {ws} in {idle_seconds} seconds.")

	raise SystemExit(rc)


@click.command(help="start or reuse the devcontainer, then open a shell (alias: shell)")
@click.argument("preset", required=False, metavar="[PRESET]")
@click.option("-w", "--workspace", default=None, help="workspace folder (default: cwd)")
@click.option(
	"--idle-seconds",
	default=DEFAULT_IDLE_SECONDS,
	show_default=True,
	type=int,
	help="delay before auto-stopping after the last shell exits",
)
@click.option("--no-rebuild", "no_rebuild", is_flag=True, help="skip rebuild even if devcontainer config changed (warns instead)")
def start(preset: str | None, workspace: str | None, idle_seconds: int, no_rebuild: bool) -> None:
	_run_shell(workspace, idle_seconds, preset, no_rebuild)


@click.command(help="start or reuse the devcontainer, then open a shell (alias: start)")
@click.option("-w", "--workspace", default=None, help="workspace folder (default: cwd)")
@click.option(
	"--idle-seconds",
	default=DEFAULT_IDLE_SECONDS,
	show_default=True,
	type=int,
	help="delay before auto-stopping after the last shell exits",
)
def shell(workspace: str | None, idle_seconds: int) -> None:
	_run_shell(workspace, idle_seconds, preset=None, no_rebuild=True)


@click.command(help="rebuild the devcontainer, reusing the layer cache unless --no-cache is passed")
@click.argument("workspace", required=False)
@click.option("--no-cache", "no_cache", is_flag=True, help="bypass BuildKit layer cache (full reinstall of all features)")
def rebuild(workspace: str | None, no_cache: bool) -> None:
	ws = workspace_path(workspace)
	ensure_state_dirs(ws)
	prune_stale_sessions(ws)
	clear_timer(ws)
	if active_session_count(ws) > 0:
		click.echo("Warning: rebuilding while another managed shell session is still active.", err=True)
	_container_up(ws, force_rebuild=True, no_cache=no_cache)
	container_id = wait_for_container(ws)
	if container_id:
		ssh_bootstrap(container_id, alloc_ssh_port(ws), clear_known_host=True)


@click.command(name="kill", help="stop the running devcontainer for the workspace")
@click.argument("workspace", required=False)
def kill_cmd(workspace: str | None) -> None:
	ws = workspace_path(workspace)
	ensure_state_dirs(ws)
	clear_timer(ws)
	clear_all_sessions(ws)
	container_id = find_container(ws)
	if not container_id:
		click.echo(f"No running devcontainer found for {ws}.")
		return
	rc = podman_stop(container_id)
	if rc == 0:
		click.echo(f"Stopped devcontainer for {ws}.")
	raise SystemExit(rc)


@click.command(name="list", help="list initialized podman devcontainers across workspaces")
def list_cmd() -> None:
	click.echo("Prune from anywhere: dcman prune --workspace /absolute/path/to/workspace")
	click.echo("Interactive prune:  dcman prune --select")

	entries = list_initialized_devcontainers()
	if not entries:
		click.echo("No initialized podman devcontainers found.")
		return

	click.echo("")
	click.echo(render_devcontainer_table(entries))


@click.command(name="prune", help="delete initialized devcontainer(s) for a workspace and clear dcman tracking")
@click.option("-w", "--workspace", default=None, help="workspace folder to prune")
@click.option("--select", "select_mode", is_flag=True, help="interactively choose from initialized devcontainers")
@click.option("-y", "--yes", is_flag=True, help="skip confirmation prompt")
def prune_cmd(workspace: str | None, select_mode: bool, yes: bool) -> None:
	if workspace and select_mode:
		raise click.ClickException("use either --workspace or --select, not both")
	if not workspace and not select_mode:
		raise click.ClickException("pass --workspace or --select")

	target_ws: Path
	if select_mode:
		entries = list_initialized_devcontainers()
		if not entries:
			click.echo("No initialized podman devcontainers found.")
			return
		click.echo(render_devcontainer_table(entries))
		choice = click.prompt("Select container number", type=click.IntRange(1, len(entries)))
		target_ws = Path(entries[choice - 1]["workspace"])
	else:
		target_ws = workspace_path(workspace)

	matches = find_initialized_devcontainers(target_ws)
	if not matches:
		clear_workspace_tracking(target_ws)
		click.echo(f"No initialized devcontainers found for {target_ws}. Cleared dcman tracking state.")
		return

	if not yes and not click.confirm(f"Delete {len(matches)} container(s) for {target_ws}?", default=True):
		click.echo("Nothing changed.")
		return

	for entry in matches:
		run(["podman", "rm", "-f", entry["id"]])
	clear_workspace_tracking(target_ws)
	click.echo(f"Removed {len(matches)} container(s) for {target_ws}.")


@click.command(name="zed", help="start the devcontainer, open it in Zed via SSH, and keep a shell")
@click.argument("preset", required=False, metavar="[PRESET]")
@click.option("-w", "--workspace", default=None, help="workspace folder (default: cwd)")
@click.option("--no-rebuild", "no_rebuild", is_flag=True, help="skip rebuild even if devcontainer config changed")
@click.option(
	"--idle-seconds",
	default=DEFAULT_IDLE_SECONDS,
	show_default=True,
	type=int,
	help="delay before auto-stopping after the last shell exits",
)
def zed_cmd(workspace: str | None, no_rebuild: bool, preset: str | None, idle_seconds: int) -> None:
	preset_cmd: str | None = None
	if preset:
		if preset not in PRESETS:
			raise click.ClickException(f"unknown preset {preset!r}. defined presets: {', '.join(PRESETS) or '(none)'}")
		preset_cmd = PRESETS[preset]

	ws = workspace_path(workspace)
	ensure_state_dirs(ws)
	prune_stale_sessions(ws)
	clear_timer(ws)

	env, did_rebuild = _container_up(ws, no_rebuild=no_rebuild)
	container_id = wait_for_container(ws)
	if not container_id:
		raise click.ClickException(f"no matching devcontainer found for {ws}")
	ssh_bootstrap(container_id, int(env["DCMAN_SSH_PORT"]), clear_known_host=did_rebuild)

	host_port = int(env["DCMAN_SSH_PORT"])
	zed_uri = f"ssh://{REMOTE_USER}@127.0.0.1:{host_port}{WORKSPACE_DEST}"
	click.echo(f"Opening {zed_uri}")
	subprocess.Popen(["zed", zed_uri])

	# Drop into a shell so the container stays alive while you work.
	# Idle stop arms when you exit, same as `dcman start`.
	session_id = secrets.token_hex(8)
	register_session(ws, session_id)
	shell_cmd = detect_shell(container_id, preset_cmd)
	env_args = []
	for k in AUTH_PROVIDERS.values():
		if k in env:
			env_args += ["-e", k]
	try:
		rc = subprocess.run(
			["podman", "exec", "-it", "-u", REMOTE_USER, "-w", WORKSPACE_DEST, *env_args, container_id, *shell_cmd],
			env=env,
		).returncode
	finally:
		unregister_session(ws, session_id)
		prune_stale_sessions(ws)
		if active_session_count(ws) == 0:
			schedule_idle_stop(ws, idle_seconds)
			click.echo(f"Armed idle stop for {ws} in {idle_seconds} seconds.")

	raise SystemExit(rc)


@click.command(name="auth", help="store or clear credentials in secret-tool.  PROVIDER: " + " | ".join(AUTH_PROVIDERS))
@click.argument("provider")
@click.option("--clear", "clear_token", is_flag=True, help="remove the stored token instead of storing one")
def auth(provider: str, clear_token: bool) -> None:
	if provider not in AUTH_PROVIDERS:
		raise click.ClickException(f"unknown provider {provider!r}. known providers: {', '.join(AUTH_PROVIDERS)}")

	if clear_token:
		try:
			removed = clear_provider_token(provider)
		except SecretToolUnavailable as exc:
			raise click.ClickException(str(exc))
		if removed:
			click.echo(f"Cleared {provider} token from secret store.")
		else:
			click.echo(f"No {provider} token was stored.")
		return

	try:
		existing = get_provider_token(provider)
	except SecretToolUnavailable as exc:
		raise click.ClickException(str(exc))

	if existing and not click.confirm(f"A {provider} token is already stored. Replace it?", default=True):
		click.echo("Nothing changed.")
		return

	token = click.prompt(f"{provider} token", hide_input=True, confirmation_prompt=True).strip()
	if not token:
		raise click.ClickException("empty token not allowed")

	store_provider_token(provider, token)
	click.echo(f"Stored {provider} token in secret store.")


@click.command(name="_idle-stop", hidden=True)
@click.option("--workspace", required=True, type=click.Path(path_type=Path))
@click.option("--delay", required=True, type=int)
@click.option("--token", required=True)
def idle_stop(workspace: Path, delay: int, token: str) -> None:
	ws = workspace.expanduser().resolve()
	time.sleep(delay)
	prune_stale_sessions(ws)
	if active_session_count(ws) > 0:
		return

	state = load_state(ws)
	if state.get("timer_token") != token:
		return

	container_id = find_container(ws)
	if container_id:
		podman_stop(container_id)

	state = load_state(ws)
	if state.get("timer_token") == token:
		state["timer_token"] = None
		state["timer_pid"] = None
		state["timer_started_at"] = None
		save_state(ws, state)


cli.add_command(start)
cli.add_command(shell)
cli.add_command(rebuild)
cli.add_command(kill_cmd)
cli.add_command(list_cmd)
cli.add_command(prune_cmd)
cli.add_command(zed_cmd)
cli.add_command(auth)
cli.add_command(idle_stop)


if __name__ == "__main__":
	try:
		cli()
	except CmdError as exc:
		raise click.ClickException(str(exc))
