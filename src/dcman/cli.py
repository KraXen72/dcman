from __future__ import annotations

import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import click

from .auth import build_env, clear_provider_token, get_provider_token, store_provider_token
from .config import AUTH_PROVIDERS, DEFAULT_IDLE_SECONDS, DESCRIPTION, PRESETS, REMOTE_USER, WORKSPACE_DEST
from .container import (
	container_engine,
	container_exec_interactive,
	devcontainer_hash,
	devcontainer_up,
	ensure_devcontainer_config,
	find_container,
	find_initialized_devcontainers,
	list_initialized_devcontainers,
	remove_container,
	render_devcontainer_table,
	save_devcontainer_hash,
	stop_container,
	wait_for_container,
)
from .errors import CmdError, SecretToolUnavailable
from .ssh import alloc_ssh_port, detect_shell, ssh_bootstrap
from .state import (
	active_session_count,
	clear_all_sessions,
	clear_timer,
	clear_workspace_tracking,
	ensure_state_dirs,
	load_state,
	prune_stale_sessions,
	register_session,
	save_state,
	schedule_idle_stop,
	unregister_session,
	workspace_path,
)

# User-facing command layer: validates prerequisites, orchestrates container/
# state/auth/SSH modules, and keeps command behavior consistent across flows.


def require_binaries() -> None:
	# Fail early with clear guidance before running any long lifecycle command.
	if shutil.which("devcontainer") is None:
		raise click.ClickException("devcontainer CLI was not found in PATH.")
	try:
		container_engine()
	except CmdError as exc:
		raise click.ClickException(str(exc))


def _resolve_preset(preset: str | None) -> str | None:
	# Presets are named shorthand commands (defined in config.PRESETS) that run
	# inside the container right before handing the user an interactive shell.
	# Example: the "copilot" preset runs `copilot --yolo` then drops into bash.
	if preset is None:
		return None
	if preset not in PRESETS:
		# Show available keys so typo recovery is immediate.
		raise click.ClickException(f"unknown preset {preset!r}. defined presets: {', '.join(PRESETS) or '(none)'}")
	return PRESETS[preset]


def _prepare_workspace(raw_workspace: str | None) -> Path:
	ws = workspace_path(raw_workspace)
	ensure_state_dirs(ws)
	# Always clean stale session markers before deciding whether a workspace
	# still has active managed shells.
	prune_stale_sessions(ws)
	# Any explicit command should cancel previous pending idle shutdown.
	clear_timer(ws)
	return ws


def _container_up(
	ws: Path, *, force_rebuild: bool = False, no_rebuild: bool = False, no_cache: bool = False
) -> tuple[dict[str, str], bool]:
	ensure_devcontainer_config(ws)
	env, warnings = build_env(with_tokens=True)
	for warning in warnings:
		click.echo(f"Warning: {warning}", err=True)
	# The devcontainer's runArgs maps this host env var to published SSH port.
	env["DCMAN_SSH_PORT"] = str(alloc_ssh_port(ws))

	if force_rebuild:
		devcontainer_up(ws, rebuild=True, no_cache=no_cache, env=env)
		save_devcontainer_hash(ws)
		return env, True

	current_hash = devcontainer_hash(ws)
	stored_hash = load_state(ws).get("devcontainer_hash")
	# Hash comparison is our lightweight "did devcontainer config change?" signal.
	config_changed = current_hash is not None and current_hash != stored_hash

	if no_rebuild and config_changed:
		# Let power users skip rebuild for speed while still making drift explicit.
		click.echo("Warning: devcontainer config has changed; run 'dcman rebuild' to apply.", err=True)
	elif config_changed:
		click.echo("Devcontainer config changed; rebuilding.")

	do_rebuild = not no_rebuild and config_changed
	devcontainer_up(ws, rebuild=do_rebuild, env=env)
	if not config_changed or do_rebuild:
		save_devcontainer_hash(ws)
	return env, do_rebuild


def _shell_env(env: dict[str, str]) -> dict[str, str]:
	# Only pass known provider vars through to the interactive container shell.
	container_env: dict[str, str] = {}
	for env_var in AUTH_PROVIDERS.values():
		if env_var in env:
			container_env[env_var] = env[env_var]
	return container_env


def _run_managed_shell(
	workspace: str | None,
	idle_seconds: int,
	preset: str | None,
	no_rebuild: bool,
	*,
	open_zed: bool = False,
) -> None:
	ws = _prepare_workspace(workspace)
	preset_cmd = _resolve_preset(preset)

	env, did_rebuild = _container_up(ws, no_rebuild=no_rebuild)
	container_id = wait_for_container(ws)
	if not container_id:
		raise click.ClickException(f"no matching devcontainer found for {ws}")

	host_port = int(env["DCMAN_SSH_PORT"])
	# Clear known_hosts only after rebuilds, when container host keys may rotate.
	warning = ssh_bootstrap(container_id, host_port, clear_known_host=did_rebuild)
	if warning:
		click.echo(f"Warning: {warning}", err=True)

	if open_zed:
		zed_uri = f"ssh://{REMOTE_USER}@127.0.0.1:{host_port}{WORKSPACE_DEST}"
		click.echo(f"Opening {zed_uri}")
		# Fire-and-forget keeps dcman attached to terminal shell lifecycle.
		subprocess.Popen(["zed", zed_uri])

	session_id = secrets.token_hex(8)
	# Session markers are the source of truth for "is this workspace still in use?".
	register_session(ws, session_id)
	shell_cmd = detect_shell(container_id, preset_cmd)
	shell_env = _shell_env(env)

	try:
		rc = container_exec_interactive(
			container_id,
			shell_cmd,
			user=REMOTE_USER,
			workdir=WORKSPACE_DEST,
			env=shell_env,
		)
	finally:
		# Cleanup must always run, even when shell exits due to crash/signal.
		unregister_session(ws, session_id)
		prune_stale_sessions(ws)
		if active_session_count(ws) == 0:
			# Only arm shutdown when last managed shell exits for this workspace.
			schedule_idle_stop(ws, idle_seconds)
			click.echo(f"Armed idle stop for {ws} in {idle_seconds} seconds.")

	raise SystemExit(rc)


@click.group(help=DESCRIPTION)
def cli() -> None:
	# Group callback runs before subcommands, so this validates dependencies once.
	require_binaries()


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
	_run_managed_shell(workspace, idle_seconds, preset, no_rebuild)


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
	# `shell` alias intentionally skips auto-rebuild to stay snappy for quick re-entry.
	_run_managed_shell(workspace, idle_seconds, preset=None, no_rebuild=True)


@click.command(help="rebuild the devcontainer, reusing the layer cache unless --no-cache is passed")
@click.argument("workspace", required=False)
@click.option("--no-cache", "no_cache", is_flag=True, help="bypass BuildKit layer cache (full reinstall of all features)")
def rebuild(workspace: str | None, no_cache: bool) -> None:
	ws = _prepare_workspace(workspace)
	if active_session_count(ws) > 0:
		click.echo("Warning: rebuilding while another managed shell session is still active.", err=True)
	_container_up(ws, force_rebuild=True, no_cache=no_cache)
	container_id = wait_for_container(ws)
	if container_id:
		# Rebuild path always clears known-host entry to avoid key-mismatch warnings.
		warning = ssh_bootstrap(container_id, alloc_ssh_port(ws), clear_known_host=True)
		if warning:
			click.echo(f"Warning: {warning}", err=True)


@click.command(name="kill", help="stop the running devcontainer for the workspace")
@click.argument("workspace", required=False)
def kill_cmd(workspace: str | None) -> None:
	ws = workspace_path(workspace)
	ensure_state_dirs(ws)
	# Manual stop should also cancel queued idle-stop worker.
	clear_timer(ws)
	# Prevent stale "active shell" markers from affecting subsequent runs.
	clear_all_sessions(ws)
	container_id = find_container(ws)
	if not container_id:
		click.echo(f"No running devcontainer found for {ws}.")
		return
	rc = stop_container(container_id)
	if rc == 0:
		click.echo(f"Stopped devcontainer for {ws}.")
	raise SystemExit(rc)


@click.command(name="list", help="list initialized devcontainers across workspaces")
def list_cmd() -> None:
	# Keep prune affordances visible because list+prune are commonly paired.
	click.echo("Prune from anywhere: dcman prune --workspace /absolute/path/to/workspace")
	click.echo("Interactive prune:  dcman prune --select")

	entries = list_initialized_devcontainers()
	if not entries:
		click.echo("No initialized devcontainers found.")
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
		# Interactive mode helps when many workspaces are present.
		entries = list_initialized_devcontainers()
		if not entries:
			click.echo("No initialized devcontainers found.")
			return
		click.echo(render_devcontainer_table(entries))
		choice = click.prompt("Select container number", type=click.IntRange(1, len(entries)))
		target_ws = Path(entries[choice - 1]["workspace"])
	else:
		target_ws = workspace_path(workspace)

	matches = find_initialized_devcontainers(target_ws)
	if not matches:
		# Even with no containers left, clearing tracking avoids stale local state.
		clear_workspace_tracking(target_ws)
		click.echo(f"No initialized devcontainers found for {target_ws}. Cleared dcman tracking state.")
		return

	if not yes and not click.confirm(f"Delete {len(matches)} container(s) for {target_ws}?", default=True):
		click.echo("Nothing changed.")
		return

	for entry in matches:
		remove_container(entry["id"])
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
	# Reuses exactly the same lifecycle path as `start`, adding only Zed launch.
	_run_managed_shell(workspace, idle_seconds, preset, no_rebuild, open_zed=True)


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
	# Internal command: launched as a detached background subprocess by
	# schedule_idle_stop() in state.py whenever the last managed shell exits.
	# After sleeping `delay` seconds it stops the container — unless a new
	# shell session has started (token mismatch) or sessions are still active.
	ws = workspace.expanduser().resolve()
	# Sleep in child process keeps parent shell exit path fast.
	time.sleep(delay)
	prune_stale_sessions(ws)
	if active_session_count(ws) > 0:
		return

	state = load_state(ws)
	# Token check prevents older timers from stopping a container after a newer
	# shell session has already replaced the timer.
	if state.get("timer_token") != token:
		return

	container_id = find_container(ws)
	if container_id:
		stop_container(container_id)

	state = load_state(ws)
	if state.get("timer_token") == token:
		state["timer_token"] = None
		state["timer_pid"] = None
		state["timer_started_at"] = None
		save_state(ws, state)


for command in (start, shell, rebuild, kill_cmd, list_cmd, prune_cmd, zed_cmd, auth, idle_stop):
	cli.add_command(command)


def main() -> None:
	try:
		cli()
	except CmdError as exc:
		# If workspace lacks a devcontainer config, show only the friendly message
		# without exception chaining or a Python traceback.
		msg = str(exc)
		if "No devcontainer config found in" in msg:
			click.echo(msg)
			# Exit with non-zero status to indicate failure.
			sys.exit(1)
		# Convert other internal errors into Click-style CLI errors (clean message + exit code).
		raise click.ClickException(msg) from None


if __name__ == "__main__":
	main()
