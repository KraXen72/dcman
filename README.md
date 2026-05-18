# dcman

`dcman` manages container-engine-backed devcontainers from one CLI.

It does a few things together:
1. Starts/reuses your devcontainer and rebuilds when config changes.
2. Bootstraps SSH access (injects your host public key + starts Dropbear).
3. Opens an interactive shell (or Zed over SSH) and auto-arms idle shutdown when you exit.
4. Optionally injects provider tokens from `secret-tool` into container env.
5. Optionally syncs one global `AGENTS.md` file into agent-specific config paths inside the container.

`dcman` is Linux-focused today. HTTP/.localhost port-forward proxying is not implemented yet.

`dcman` is built around and works best with my devcontainer template [fedora-sandbox](https://github.com/KraXen72/devcontainer-templates/tree/main/src/fedora-sandbox), but it should work with your template as well.

## system dependencies

You need:
- `podman` or `docker` (if both are installed, `dcman` prefers `podman` by default)
- Dev Container CLI (`devcontainer`)
- OpenSSH client and a host key at `~/.ssh/id_ed25519.pub` (modularization coming later)
- `secret-tool` (optional, only needed for `dcman auth`)
- `zed` (optional, only for `dcman zed`)

**Fedora:**
```bash
sudo dnf install podman docker nodejs openssh-clients libsecret
npm install -g @devcontainers/cli
```

**Debian/Ubuntu:**
```bash
sudo apt install podman docker.io nodejs npm openssh-client libsecret-tools
npm install -g @devcontainers/cli
```

Use `DCMAN_CONTAINER_ENGINE=docker` (or another engine binary name in `PATH`) to override engine selection.

Your workspace must contain either `.devcontainer.json` or `.devcontainer/devcontainer.json`.
To bootstrap one from a blessed template alias, run `dcman template apply fedora-sandbox`.

If you want SSH/Zed support, your devcontainer `runArgs` should publish `2222` from the container, e.g.:
```json
"runArgs": ["--publish=${localEnv:DCMAN_SSH_PORT}:2222"]
```
and use [my `ssh-zed` devcontainer feature](https://github.com/KraXen72/devcontainer-features/tree/main/src/ssh-zed) or an equivalent (needs `dropbear`).

For nicer Zed project names, prefer a project-specific container workspace path instead of mounting every repo as `/home/vscode/workspace`:
```json
"workspaceMount": "source=${localWorkspaceFolder},target=/home/vscode/workspaces/${localWorkspaceFolderBasename},type=bind,Z",
"workspaceFolder": "/home/vscode/workspaces/${localWorkspaceFolderBasename}"
```
`dcman` resolves `workspaceFolder` at runtime for shells and Zed, with `/home/vscode/workspace` kept only as a legacy fallback. Templates can also opt into readable container names, e.g. `--name=dcman_${localWorkspaceFolderBasename}`.  

## install

```bash
uv tool install git+https://github.com/KraXen72/dcman
dcman --help
```

Or run without installing globally:
```bash
uvx --from git+https://github.com/KraXen72/dcman dcman --help
```

## quick usage

```bash
# optional: apply the blessed fedora-sandbox devcontainer template
dcman template apply fedora-sandbox

# optional: store token for env injection
dcman auth copilot

# open shell and start Codex CLI when the codex-cli devcontainer feature is installed
dcman start codex

# open shell in managed devcontainer
dcman start

# open in Zed via ssh://... and keep a shell running
dcman zed

# rebuild, list, prune, stop
dcman rebuild
dcman list
dcman prune --workspace /absolute/path/to/workspace
dcman kill
```

`start`, `shell`, `rebuild`, and `zed` suppress Dev Container feature lockfile creation by default.
This is a deliberate tradeoff:

- an extra `devcontainer-lock.json` in each repo root is clutter for my workflow
- my devcontainer features are first-party, so skipping lockfiles is acceptable for my use case

Pass `--lockfile` to `start`, `shell`, `rebuild`, or `zed` if you want the Dev Container CLI to create or update `devcontainer-lock.json`.

Run `dcman --help` for all commands and options.

State is tracked under `~/.cache/dcman`.

## global agent instructions

Create a host-side file at:

```bash
mkdir -p ~/.config/dcman
$EDITOR ~/.config/dcman/AGENTS.md
```

On every `dcman start`, `dcman shell`, `dcman rebuild`, or `dcman zed`, dcman copies that file into the managed container as:

- `/home/vscode/.codex/AGENTS.md` for Codex CLI
- `/home/vscode/.copilot/copilot-instructions.md` for GitHub Copilot CLI
- `/home/vscode/.config/zed/AGENTS.md` for Zed's native agent

If `XDG_CONFIG_HOME` is set, dcman uses `$XDG_CONFIG_HOME/dcman/AGENTS.md` instead. Set `DCMAN_AGENTS_MD=/path/to/AGENTS.md` to use a different host file. dcman copies the file instead of symlinking it, and it does not write anything into project roots. Project-local files such as `AGENTS.md`, nested `AGENTS.md`, and `.github/copilot-instructions.md` are still loaded by the respective tools as project-specific guidance.

## local development

```bash
git clone https://github.com/KraXen72/dcman
cd dcman
uv sync
uv run dcman --help
```

To expose your local checkout as a global `dcman` command (while still using your latest repo code), install it as an editable uv tool:

```bash
cd /path/to/your/dcman
uv tool install --editable .
dcman --help
```

After code changes, just run `dcman ...` again; editable install points to your working tree.  
If you changed dependencies/entrypoints in `pyproject.toml`, run `uv sync` and reinstall:

```bash
uv tool install --editable --reinstall .
```

## notes
I made `dcman` primarily for myself. If you find it useful, great! However, please note that I reserve the right to not implement some features I won't use, to prevent `dcman`'s scope from balooning. Thank you for understanding.
