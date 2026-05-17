from __future__ import annotations

from pathlib import Path

# Centralized runtime/config constants so command logic stays focused on flow.
DESCRIPTION = "manage devcontainers with start, rebuild, kill, zed remote, and delayed idle stop"

WORKSPACE_DEST = "/home/vscode/workspace"
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
DEVCONTAINER_TEMPLATE_URL = "ghcr.io/KraXen72/devcontainer-templates/fedora-sandbox"

# provider -> environment variable name injected into container exec sessions.
AUTH_PROVIDERS: dict[str, str] = {
	"copilot": "COPILOT_GITHUB_TOKEN",
}

# preset -> command to run automatically before handing over an interactive shell.
PRESETS: dict[str, str] = {
	"copilot": "copilot --yolo",
	"codex": "codex --yolo",
}
