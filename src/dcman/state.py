from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

from .config import STATE_ROOT

# Owns filesystem-backed runtime state (`state.json` + session marker files)
# used for per-workspace coordination (SSH port reuse, active shells, idle timer).


def workspace_path(raw: str | None) -> Path:
	# Normalize once (expand ~ + absolute path) so lookups are stable everywhere.
	return Path(raw or os.getcwd()).expanduser().resolve()


def workspace_key(workspace: Path) -> str:
	# Stable short hash keeps cache paths deterministic without leaking full paths
	# into directory names (which can be long/awkward).
	return sha256(str(workspace).encode("utf-8")).hexdigest()[:16]


def workspace_state_dir(workspace: Path) -> Path:
	# Every workspace gets an isolated state directory under ~/.cache.
	return STATE_ROOT / workspace_key(workspace)


def state_file(workspace: Path) -> Path:
	return workspace_state_dir(workspace) / "state.json"


def sessions_dir(workspace: Path) -> Path:
	return workspace_state_dir(workspace) / "sessions"


def ensure_state_dirs(workspace: Path) -> None:
	# Safe to call repeatedly; mkdir(..., exist_ok=True) is idempotent.
	sessions_dir(workspace).mkdir(parents=True, exist_ok=True)


def load_state(workspace: Path) -> dict[str, Any]:
	path = state_file(workspace)
	if not path.exists():
		# Include workspace path even for fresh state so downstream code can rely on it.
		return {"workspace": str(workspace)}
	try:
		data = json.loads(path.read_text())
	except Exception:
		# Treat corrupt JSON as recoverable; returning defaults lets dcman heal
		# state on next write instead of hard-failing core workflows.
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
	# Atomic rename avoids partially-written JSON if the process is interrupted.
	tmp.replace(path)


def pid_alive(pid: int | None) -> bool:
	if not pid or pid <= 0:
		return False
	try:
		# Signal 0 probes process existence without actually sending a signal.
		os.kill(pid, 0)
	except ProcessLookupError:
		return False
	except PermissionError:
		return True
	return True


def prune_stale_sessions(workspace: Path) -> int:
	ensure_state_dirs(workspace)
	removed = 0
	for entry in sessions_dir(workspace).glob("*.json"):
		try:
			payload = json.loads(entry.read_text())
		except Exception:
			# Broken marker files should not block lifecycle operations.
			entry.unlink(missing_ok=True)
			removed += 1
			continue
		pid = payload.get("manager_pid")
		# If pid is missing/invalid/dead, this session marker no longer represents
		# an active shell and should not block idle shutdown.
		if not isinstance(pid, int) or not pid_alive(pid):
			entry.unlink(missing_ok=True)
			removed += 1
	return removed


def active_session_files(workspace: Path) -> list[Path]:
	ensure_state_dirs(workspace)
	# Always prune first so callers get a truthful view of active sessions.
	prune_stale_sessions(workspace)
	return sorted(sessions_dir(workspace).glob("*.json"))


def active_session_count(workspace: Path) -> int:
	return len(active_session_files(workspace))


def register_session(workspace: Path, session_id: str) -> Path:
	ensure_state_dirs(workspace)
	payload = {
		"session_id": session_id,
		# We track the manager PID so stale sessions from crashed terminals can
		# be garbage-collected automatically.
		"manager_pid": os.getpid(),
		"created_at": int(time.time()),
	}
	path = sessions_dir(workspace) / f"{session_id}.json"
	path.write_text(json.dumps(payload, indent=2) + "\n")
	return path


def unregister_session(workspace: Path, session_id: str) -> None:
	(sessions_dir(workspace) / f"{session_id}.json").unlink(missing_ok=True)


def clear_all_sessions(workspace: Path) -> None:
	# Used by explicit lifecycle commands (kill/prune) to hard-reset workspace state.
	for entry in sessions_dir(workspace).glob("*.json"):
		entry.unlink(missing_ok=True)


def clear_timer(workspace: Path) -> None:
	state = load_state(workspace)
	if state.get("timer_token") or state.get("timer_pid"):
		# Clearing the token is enough to invalidate already-spawned timers.
		state["timer_token"] = None
		state["timer_pid"] = None
		state["timer_started_at"] = None
		save_state(workspace, state)


def schedule_idle_stop(workspace: Path, delay: int) -> None:
	ensure_state_dirs(workspace)
	token = secrets.token_hex(16)
	cmd = [
		sys.executable,
		"-m",
		# Re-enter the same CLI as a lightweight one-shot "timer worker".
		"dcman",
		"_idle-stop",
		"--workspace",
		str(workspace),
		"--delay",
		str(delay),
		"--token",
		token,
	]
	env = os.environ.copy()
	src_dir = str(Path(__file__).resolve().parents[1])
	# Keep `python dcman.py ...` mode working: background timer subprocess still
	# needs to import package modules from ./src when not globally installed.
	env["PYTHONPATH"] = f"{src_dir}:{env['PYTHONPATH']}" if env.get("PYTHONPATH") else src_dir
	proc = subprocess.Popen(
		cmd,
		stdin=subprocess.DEVNULL,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
		env=env,
		# Detached session prevents child timer from dying with the interactive shell.
		start_new_session=True,
	)
	state = load_state(workspace)
	# Token + pid make timer runs traceable and safely replaceable.
	state["timer_token"] = token
	state["timer_pid"] = proc.pid
	state["timer_started_at"] = int(time.time())
	state["idle_delay_seconds"] = delay
	save_state(workspace, state)


def clear_workspace_tracking(workspace: Path) -> None:
	ensure_state_dirs(workspace)
	clear_timer(workspace)
	clear_all_sessions(workspace)
	state = load_state(workspace)
	# Hash/snapshot reset forces next `start` to treat workspace as needing
	# fresh tracking instead of comparing against stale accepted config text.
	state["devcontainer_hash"] = None
	state["devcontainer_snapshot"] = None
	save_state(workspace, state)
