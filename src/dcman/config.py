from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Centralized runtime/config constants so command logic stays focused on flow.
DESCRIPTION = "manage devcontainers with templates, start, rebuild, kill, zed remote, and delayed idle stop"

# Legacy/default container-side workspace path. Newer templates should set
# workspaceFolder dynamically (for example using ${localWorkspaceFolderBasename})
# and callers should resolve that value from the devcontainer config instead of
# importing this fallback directly.
DEFAULT_WORKSPACE_FOLDER = "/home/vscode/workspace"
REMOTE_USER = "vscode"

STATE_ROOT = Path.home() / ".cache" / "dcman"
# Previous location used before migration to dcman-specific cache namespace.
LEGACY_STATE_ROOT = Path.home() / ".cache" / "devcontainer-lifecycle"

# One-time host migration command:
# [ -d ~/.cache/devcontainer-lifecycle ] && mkdir -p ~/.cache/dcman && cp -a ~/.cache/devcontainer-lifecycle/. ~/.cache/dcman/ && rm -rf ~/.cache/devcontainer-lifecycle

DEFAULT_IDLE_SECONDS = 300

# Must match the container-side SSH port exposed in devcontainer runArgs.
SSH_CONTAINER_PORT = 2222
HOST_SSH_PUBKEY = Path.home() / ".ssh" / "id_ed25519.pub"


@dataclass(frozen=True)
class UidFastPath:
	# Images matching this prefix are known to already contain `remote_user` as
	# `uid:gid`, so dcman can skip the Dev Container CLI's UID-rewrite image when
	# the host user has the same IDs.
	image_prefix: str
	remote_user: str = REMOTE_USER
	uid: int = 1000
	gid: int = 1000


@dataclass(frozen=True)
class DevcontainerTemplatePreset:
	ref: str
	uid_fast_path: UidFastPath | None = None


# blessed template aliases for `dcman template apply <name>`.
DEFAULT_DEVCONTAINER_TEMPLATE = "fedora-sandbox"
DEVCONTAINER_TEMPLATES: dict[str, DevcontainerTemplatePreset] = {
	DEFAULT_DEVCONTAINER_TEMPLATE: DevcontainerTemplatePreset(
		ref="ghcr.io/KraXen72/devcontainer-templates/fedora-sandbox",
		uid_fast_path=UidFastPath(image_prefix="ghcr.io/kraxen72/fedora-toolchain-base"),
	),
}

# provider -> environment variable name injected into container exec sessions.
AUTH_PROVIDERS: dict[str, str] = {
	"copilot": "COPILOT_GITHUB_TOKEN",
}

# preset -> command to run automatically before handing over an interactive shell.
PRESETS: dict[str, str] = {
	"copilot": "copilot --yolo",
	"codex": "codex --yolo",
	"opencode": "OPENCODE_CONFIG=~/.config/opencode/opencode-yolo.json opencode",
}
