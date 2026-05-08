# dcman

`dcman` manages container-engine-backed devcontainers from one CLI.

It does a few things together:
1. Starts/reuses your devcontainer and rebuilds when config changes.
2. Bootstraps SSH access (injects your host public key + starts Dropbear).
3. Opens an interactive shell (or Zed over SSH) and auto-arms idle shutdown when you exit.
4. Optionally injects provider tokens from `secret-tool` into container env.

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

If you want SSH/Zed support, your devcontainer `runArgs` should publish `2222` from the container, e.g.:
```json
"runArgs": ["--publish=${localEnv:DCMAN_SSH_PORT}:2222"]
```
and use [my `ssh-zed` devcontainer feature](https://github.com/KraXen72/devcontainer-features/tree/main/src/ssh-zed) or an equivalent (needs `dropbear`).  

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
# optional: store token for env injection
dcman auth copilot

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

Run `dcman --help` for all commands and options.

State is tracked under `~/.cache/dcman`.

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
