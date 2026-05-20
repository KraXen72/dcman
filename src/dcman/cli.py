from __future__ import annotations

import os
import secrets
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypeVar, cast

import click
from rich.console import Console

from . import agent_instructions
from .auth import (
	build_env,
	clear_provider_token,
	get_provider_token,
	store_provider_token,
)
from .config import (
	AUTH_PROVIDERS,
	DEFAULT_IDLE_SECONDS,
	DESCRIPTION,
	DEVCONTAINER_TEMPLATES,
	PRESETS,
	REMOTE_USER,
	DevcontainerTemplatePreset,
)
from .container import (
	container_engine,
	container_exec_interactive,
	devcontainer_hash,
	devcontainer_template_apply,
	devcontainer_up,
	ensure_devcontainer_config,
	find_container,
	find_initialized_devcontainers,
	format_devcontainer_config_diff,
	list_initialized_devcontainers,
	remote_workspace_folder,
	remove_container,
	render_devcontainer_table,
	require_devcontainer_cli,
	save_devcontainer_hash,
	stop_container,
	stored_devcontainer_config_snapshot,
	wait_for_container,
)
from .errors import CmdError, SecretToolUnavailable
from .features import format_feature_versions, resolve_feature_versions
from .integrations import codex, zed
from .ssh import alloc_ssh_port, detect_shell
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

T = TypeVar("T")

# allowlist of vars to preserve, not a set of "enable color" flags.
TERMINAL_ENV_KEYS = (
	"COLORTERM",
	"TERM_PROGRAM",
	"TERM_PROGRAM_VERSION",
	"KONSOLE_VERSION",
	"VTE_VERSION",
	"WT_SESSION",
	"CLICOLOR",
	"CLICOLOR_FORCE",
	"FORCE_COLOR",
	"NO_COLOR",
)


def require_binaries() -> None:
	# Fail early with clear guidance before running any long lifecycle command.
	try:
		require_devcontainer_cli()
		container_engine()
	except CmdError as exc:
		raise click.ClickException(str(exc))


def _format_aliases(aliases: Mapping[str, object]) -> str:
	return ", ".join(aliases) or "(none)"


def _resolve_alias(kind: str, alias: str, aliases: Mapping[str, T]) -> T:
	try:
		return aliases[alias]
	except KeyError:
		# Show available keys so typo recovery is immediate.
		raise click.ClickException(f"unknown {kind} {alias!r}. defined {kind}s: {_format_aliases(aliases)}") from None


def _resolve_preset(preset: str | None) -> str | None:
	# Presets are named shorthand commands that run inside the container right
	# before handing the user an interactive shell.
	if preset is None:
		return None
	return _resolve_alias("preset", preset, PRESETS)


def _resolve_template(template: str) -> DevcontainerTemplatePreset:
	# Blessed templates are named shorthand refs plus dcman-specific metadata.
	return _resolve_alias("template", template, DEVCONTAINER_TEMPLATES)


def _clear_known_host_for_workspace(workspace: Path) -> None:
	state = load_state(workspace)
	port = state.get("ssh_port")
	if isinstance(port, int):
		host_port = port
	elif isinstance(port, str) and port.isdigit():
		host_port = int(port)
	else:
		return
	if host_port > 0:
		zed.clear_known_host(host_port)



def _prepare_workspace(raw_workspace: str | None) -> Path:
	ws = workspace_path(raw_workspace)
	ensure_state_dirs(ws)
	# Always clean stale session markers before deciding whether a workspace
	# still has active managed shells.
	prune_stale_sessions(ws)
	# Any explicit command should cancel previous pending idle shutdown.
	clear_timer(ws)
	return ws


def _copy_codex_cli_auth_if_needed(workspace: Path, container_id: str) -> None:
	try:
		message = codex.seed_auth_if_enabled(workspace, container_id, user=REMOTE_USER)
	except CmdError as exc:
		click.echo(f"Warning: failed to copy Codex CLI auth into the container: {exc}", err=True)
		return

	if message:
		click.echo(message, err=message.startswith("Warning:"))


def _sync_agent_instructions_if_configured(container_id: str) -> None:
	try:
		message = agent_instructions.sync_to_container(container_id, user=REMOTE_USER)
	except CmdError as exc:
		# Do not block shell access on optional instruction sync.
		click.echo(f"Warning: {exc}", err=True)
		return

	if message:
		click.echo(message)


def _confirm_rebuild_for_config_change(ws: Path) -> bool:
	click.echo("The devcontainer config changed:")
	diff = format_devcontainer_config_diff(ws)
	if diff:
		click.echo(diff.rstrip())
	elif stored_devcontainer_config_snapshot(ws) is None:
		# First run after upgrading dcman may only have the old hash state.
		click.echo("No previous accepted devcontainer config snapshot is available yet.")
	else:
		click.echo("No textual diff is available.")

	answer = click.prompt("Rebuild before starting? [Y/n/a]", default="y", show_default=False).strip().lower()
	if answer in {"y", "yes"}:
		return True
	if answer in {"n", "no"}:
		return False
	raise click.Abort()


def _devcontainer_env(ws: Path) -> dict[str, str]:
	# Keep token lookup and SSH port allocation in one place; both are needed
	# only after the user has accepted any changed devcontainer config.
	env, warnings = build_env(with_tokens=True)
	for warning in warnings:
		click.echo(f"Warning: {warning}", err=True)
	# The devcontainer's runArgs maps this host env var to published SSH port.
	env["DCMAN_SSH_PORT"] = str(alloc_ssh_port(ws))
	return env


def _container_up(
	ws: Path,
	*,
	force_rebuild: bool = False,
	no_rebuild: bool = False,
	no_cache: bool = False,
	lockfile: bool = False,
	debug: bool = False,
) -> tuple[dict[str, str], bool]:
	ensure_devcontainer_config(ws)

	if force_rebuild:
		# Explicit `dcman rebuild` is already an affirmative action, so it does
		# not need the change-review prompt used by automatic starts.
		# Persist the accepted config before invoking the devcontainer CLI; even
		# if the rebuild fails, future changes still have a real diff baseline.
		save_devcontainer_hash(ws)
		env = _devcontainer_env(ws)
		if debug:
			click.echo("Resolving devcontainer feature versions...")
			feature_report = format_feature_versions(resolve_feature_versions(ws))
			if feature_report:
				click.echo(feature_report)
		devcontainer_up(ws, rebuild=True, no_cache=no_cache, lockfile=lockfile, env=env)
		container_id = wait_for_container(ws)
		if container_id:
			_sync_agent_instructions_if_configured(container_id)
			_copy_codex_cli_auth_if_needed(ws, container_id)
		return env, True

	current_hash = devcontainer_hash(ws)
	stored_hash = load_state(ws).get("devcontainer_hash")
	# Hash comparison is our lightweight "did devcontainer config change?" signal.
	config_changed = current_hash is not None and current_hash != stored_hash

	do_rebuild = False
	if config_changed:
		if no_rebuild:
			click.echo("Devcontainer config changed; starting without rebuilding.")
		else:
			# Prompt before env/token setup and before feature resolution. A changed
			# config can point at new registries or alter mounts, so review comes first.
			do_rebuild = _confirm_rebuild_for_config_change(ws)
			if do_rebuild:
				# Acceptance is separate from build success. Store the reviewed config
				# now so a later failed build still gives the next change a diff base.
				save_devcontainer_hash(ws)
			if not do_rebuild:
				click.echo("Starting without rebuilding; devcontainer config changes remain unapplied.")
	elif current_hash is not None and stored_devcontainer_config_snapshot(ws) is None:
		# Migrate users from the older hash-only state without forcing a rebuild.
		save_devcontainer_hash(ws)

	env = _devcontainer_env(ws)
	if do_rebuild and debug:
		feature_report = format_feature_versions(resolve_feature_versions(ws))
		if feature_report:
			click.echo(feature_report)
	devcontainer_up(ws, rebuild=do_rebuild, lockfile=lockfile, env=env)
	container_id = wait_for_container(ws)
	if container_id:
		_sync_agent_instructions_if_configured(container_id)
		_copy_codex_cli_auth_if_needed(ws, container_id)
	if not config_changed:
		save_devcontainer_hash(ws)
	return env, do_rebuild


def _shell_env(env: dict[str, str]) -> dict[str, str]:
	container_env = _terminal_env()
	for env_var in AUTH_PROVIDERS.values():
		if env_var in env:
			container_env[env_var] = env[env_var]
	return container_env


def _terminal_env() -> dict[str, str]:
	# Preserve host terminal capabilities for TUIs inside the container.
	term = os.environ.get("TERM")
	container_env: dict[str, str] = {}
	for key in TERMINAL_ENV_KEYS:
		value = os.environ.get(key)
		if value is not None:
			container_env[key] = value
	if term:
		container_env["TERM"] = _term_for_container(term, container_env)
	return container_env


def _term_for_container(term: str, env: Mapping[str, str]) -> str:
	if _rich_color_system({"TERM": term, **env}) not in {"256", "truecolor"}:
		return term

	if _term_advertises_extended_color(term):
		return term
	if term == "screen":
		return "screen-256color"
	if term == "tmux":
		return "tmux-256color"
	return "xterm-256color"


def _rich_color_system(env: Mapping[str, str]) -> str | None:
	return Console(force_terminal=True, color_system="auto", _environ=env).color_system


def _term_advertises_extended_color(term: str) -> bool:
	return "256color" in term or "direct" in term or "truecolor" in term


def _run_managed_shell(
	workspace: str | None,
	idle_seconds: int,
	preset: str | None,
	no_rebuild: bool,
	*,
	lockfile: bool = False,
	debug: bool = False,
	open_zed: bool = False,
) -> None:
	ws = _prepare_workspace(workspace)
	preset_cmd = _resolve_preset(preset)

	env, did_rebuild = _container_up(ws, no_rebuild=no_rebuild, lockfile=lockfile, debug=debug)
	container_id = wait_for_container(ws)
	if not container_id:
		raise click.ClickException(f"no matching devcontainer found for {ws}")

	host_port = int(env["DCMAN_SSH_PORT"])
	# Clear known_hosts only after rebuilds, when container host keys may rotate.
	warning = zed.bootstrap_ssh(container_id, host_port, clear_known_host=did_rebuild)
	if warning:
		click.echo(f"Warning: {warning}", err=True)

	container_workspace = remote_workspace_folder(ws)

	if open_zed:
		zed_uri = zed.open_editor(host_port, container_workspace)
		click.echo("Opening Zed project:")
		click.echo(f"  local:  {ws}")
		click.echo(f"  remote: {zed_uri}")
		click.echo(f"  name:   {ws.name}")

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
			workdir=container_workspace,
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
@click.pass_context
def cli(ctx: click.Context) -> None:
	# Host-only setup commands should not require container lifecycle tools.
	if ctx.invoked_subcommand != "agents":
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
@click.option("--no-rebuild", "no_rebuild", is_flag=True, help="skip automatic rebuild; changed config prints a notice")
@click.option("--lockfile", "lockfile", is_flag=True, help="allow devcontainer-lock.json creation/update")
@click.option("-d", "--debug", is_flag=True, help="show diagnostic devcontainer feature resolution")
def start(
	preset: str | None,
	workspace: str | None,
	idle_seconds: int,
	no_rebuild: bool,
	lockfile: bool,
	debug: bool,
) -> None:
	_run_managed_shell(workspace, idle_seconds, preset, no_rebuild, lockfile=lockfile, debug=debug)


@click.command(help="start or reuse the devcontainer, then open a shell (alias: start)")
@click.argument("preset", required=False, metavar="[PRESET]")
@click.option("-w", "--workspace", default=None, help="workspace folder (default: cwd)")
@click.option(
	"--idle-seconds",
	default=DEFAULT_IDLE_SECONDS,
	show_default=True,
	type=int,
	help="delay before auto-stopping after the last shell exits",
)
@click.option("--lockfile", "lockfile", is_flag=True, help="allow devcontainer-lock.json creation/update")
@click.option("-d", "--debug", is_flag=True, help="show diagnostic devcontainer feature resolution")
def shell(preset: str | None, workspace: str | None, idle_seconds: int, lockfile: bool, debug: bool) -> None:
	_run_managed_shell(workspace, idle_seconds, preset, no_rebuild=True, lockfile=lockfile, debug=debug)


@click.command(help="rebuild the devcontainer, reusing the layer cache unless --no-cache is passed")
@click.argument("workspace", required=False)
@click.option("--no-cache", "no_cache", is_flag=True, help="bypass BuildKit layer cache (full reinstall of all features)")
@click.option("--lockfile", "lockfile", is_flag=True, help="allow devcontainer-lock.json creation/update")
@click.option("-f", "--force", is_flag=True, help="force rebuild even if another managed shell session is still active")
@click.option("-d", "--debug", is_flag=True, help="show diagnostic devcontainer feature resolution")
def rebuild(workspace: str | None, no_cache: bool, lockfile: bool, force: bool, debug: bool) -> None:
	ws = _prepare_workspace(workspace)
	if active_session_count(ws) > 0 and not force:
		click.echo("Error: cannot rebuild while another managed shell session is still active. Pass --force to rebuild anyway.", err=True)
		return
	_container_up(ws, force_rebuild=True, no_cache=no_cache, lockfile=lockfile, debug=debug)
	container_id = wait_for_container(ws)
	if container_id:
		# Rebuild path always clears known-host entry to avoid key-mismatch warnings.
		warning = zed.bootstrap_ssh(container_id, alloc_ssh_port(ws), clear_known_host=True)
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


@click.command(name="prune")
@click.argument("target", default=".")
@click.option("-y", "--yes", is_flag=True, help="skip confirmation prompt")
def prune_cmd(target: str, yes: bool) -> None: 
	"""
	delete initialized devcontainer(s) for a workspace and clear dcman tracking.  
	
	TARGET can be a <path to workspace>, '.' (cwd), 'select' (interactive), or 'all'.  
	"""
	if target == "all":
		click.echo("current containers: ")
		containers = list_initialized_devcontainers()
		click.echo(render_devcontainer_table(containers))

		if not yes and not click.confirm(f"Delete {len(containers)} container(s)?", default=True):
			click.echo("Nothing changed.")
			return
		
		for path in [Path(cont["workspace"]) for cont in containers]:
			for cont in find_initialized_devcontainers(path):
				remove_container(cont["id"])
		return

	target_ws: Path
	if target == "select":
		# Interactive mode helps when many workspaces are present.
		entries = list_initialized_devcontainers()
		if not entries:
			click.echo("No initialized devcontainers found.")
			return
		click.echo(render_devcontainer_table(entries))
		choice = click.prompt("Select container number", type=click.IntRange(1, len(entries)))
		target_ws = Path(entries[choice - 1]["workspace"])
	else: 
		target_path = str(Path.cwd() if target == "." else target)
		target_ws = workspace_path(target_path)

	matches = find_initialized_devcontainers(target_ws)
	if not matches:
		# Even with no containers left, clearing tracking avoids stale local state.
		_clear_known_host_for_workspace(target_ws)
		clear_workspace_tracking(target_ws)
		click.echo(f"No initialized devcontainers found for {target_ws}. Cleared dcman tracking state.")
		return

	if not yes and not click.confirm(f"Delete {len(matches)} container(s) for {target_ws}?", default=True):
		click.echo("Nothing changed.")
		return

	for entry in matches:
		remove_container(entry["id"])
	_clear_known_host_for_workspace(target_ws)
	clear_workspace_tracking(target_ws)
	click.echo(f"Removed {len(matches)} container(s) for {target_ws}.")


@click.group(name="template", help="apply blessed devcontainer templates")
def template_cmd() -> None:
	pass


@click.command(name="list", help="list blessed devcontainer template aliases")
def template_list_cmd() -> None:
	if not DEVCONTAINER_TEMPLATES:
		click.echo("No blessed templates defined.")
		return

	for name, preset in DEVCONTAINER_TEMPLATES.items():
		fast_path = ""
		if preset.uid_fast_path is not None:
			fast_path = f"\tuid-fast-path={preset.uid_fast_path.uid}:{preset.uid_fast_path.gid}"
		click.echo(f"{name}\t{preset.ref}{fast_path}")


@click.command(name="apply", help="apply a blessed devcontainer template alias")
@click.argument("template", metavar="TEMPLATE")
def template_apply_cmd(template: str) -> None:
	preset = _resolve_template(template)
	click.echo(f"Applying template {template!r} ({preset.ref})")
	devcontainer_template_apply(preset.ref)


cast(Any, template_cmd).add_command(template_list_cmd)
cast(Any, template_cmd).add_command(template_apply_cmd)


@click.group(name="agents", help="manage global agent instructions")
def agents_cmd() -> None:
	pass


@click.command(name="link-host", help="symlink host agent instruction files to dcman's global AGENTS.md")
def agents_link_host_cmd() -> None:
	try:
		messages = agent_instructions.configure_host_links()
	except CmdError as exc:
		raise click.ClickException(str(exc)) from None
	for message in messages:
		click.echo(message)


cast(Any, agents_cmd).add_command(agents_link_host_cmd)


@click.command(name="zed", help="start the devcontainer, open it in Zed via SSH, and keep a shell")
@click.argument("preset", required=False, metavar="[PRESET]")
@click.option("-w", "--workspace", default=None, help="workspace folder (default: cwd)")
@click.option("--no-rebuild", "no_rebuild", is_flag=True, help="skip automatic rebuild; changed config prints a notice")
@click.option(
	"--idle-seconds",
	default=DEFAULT_IDLE_SECONDS,
	show_default=True,
	type=int,
	help="delay before auto-stopping after the last shell exits",
)
@click.option("--lockfile", "lockfile", is_flag=True, help="allow devcontainer-lock.json creation/update")
def zed_cmd(workspace: str | None, no_rebuild: bool, preset: str | None, idle_seconds: int, lockfile: bool) -> None:
	# Reuses exactly the same lifecycle path as `start`, adding only Zed launch.
	_run_managed_shell(workspace, idle_seconds, preset, no_rebuild, lockfile=lockfile, open_zed=True)


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


def _add_command(group: click.Group, command: click.Command) -> None:
	command.short_help = command.short_help or command.help
	group.add_command(command)


for command in (start, shell, rebuild, kill_cmd, list_cmd, prune_cmd, template_cmd, agents_cmd, zed_cmd, auth, idle_stop):
	_add_command(cast(click.Group, cli), command)


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
