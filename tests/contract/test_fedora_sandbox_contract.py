from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.helpers import load_json


def _feature_name(ref: str) -> str:
	name = ref.rstrip("/").rsplit("/", 1)[-1]
	return name.split(":", 1)[0]


def _repo_root() -> Path:
	return Path(__file__).resolve().parents[2]


def _find_templates_root() -> Path | None:
	candidate = os.environ.get("DCMAN_TEMPLATES_ROOT")
	if candidate:
		path = Path(candidate)
		if path.exists():
			return path
	for path in (_repo_root() / "devcontainer-templates", _repo_root().parent / "devcontainer-templates"):
		if path.exists():
			return path
	return None


def _find_features_root() -> Path | None:
	candidate = os.environ.get("DCMAN_FEATURES_ROOT")
	if candidate:
		path = Path(candidate)
		if path.exists():
			return path
	for path in (_repo_root() / "devcontainer-features", _repo_root().parent / "devcontainer-features"):
		if path.exists():
			return path
	return None


def _assert_fedora_contract(data: dict) -> None:
	assert data["workspaceMount"] == "source=${localWorkspaceFolder},target=/home/vscode/workspaces/${localWorkspaceFolderBasename},type=bind,Z"
	assert data["workspaceFolder"] == "/home/vscode/workspaces/${localWorkspaceFolderBasename}"

	run_args = data.get("runArgs", [])
	assert "--name=dcman_${localWorkspaceFolderBasename}" in run_args
	assert "--publish=127.0.0.1:${localEnv:DCMAN_SSH_PORT}:2222" in run_args

	features = data.get("features", {})
	feature_ids = {_feature_name(ref) for ref in features}
	for required in {"pnpm", "ssh-zed", "codex-cli", "copilot-cli"}:
		assert required in feature_ids


@pytest.mark.contract
def test_repo_devcontainer_contract() -> None:
	config_path = _repo_root() / ".devcontainer.json"
	data = load_json(config_path)
	_assert_fedora_contract(data)


@pytest.mark.contract
def test_template_repo_contract_if_present() -> None:
	root = _find_templates_root()
	if root is None:
		pytest.skip("devcontainer-templates repo not found")

	config_path = root / "src" / "fedora-sandbox" / ".devcontainer.json"
	template_path = root / "src" / "fedora-sandbox" / "devcontainer-template.json"
	data = load_json(config_path)
	_assert_fedora_contract(data)

	template = load_json(template_path)
	assert template["id"] == "fedora-sandbox"
	assert template["name"]


@pytest.mark.contract
def test_feature_contracts_if_present() -> None:
	root = _find_features_root()
	if root is None:
		pytest.skip("devcontainer-features repo not found")

	def read_feature_text(feature: str) -> str:
		feature_dir = root / "src" / feature
		if not feature_dir.exists():
			raise AssertionError(f"missing feature directory: {feature_dir}")
		parts: list[str] = []
		for path in feature_dir.rglob("*"):
			if path.is_file() and path.suffix in {".json", ".sh", ".md"}:
				parts.append(path.read_text(errors="replace"))
		return "\n".join(parts)

	pnpm_text = read_feature_text("pnpm")
	assert "PNPM_HOME" in pnpm_text or "pnpm" in pnpm_text
	assert ".local" in pnpm_text or "PNPM_HOME" in pnpm_text
	assert "store" in pnpm_text.lower()

	ssh_text = read_feature_text("ssh-zed")
	assert "dropbear" in ssh_text
	assert "sftp-server" in ssh_text

	codex_text = read_feature_text("codex-cli")
	assert ".codex" in codex_text
	assert "volume" in codex_text

	copilot_text = read_feature_text("copilot-cli")
	assert ".copilot" in copilot_text
	assert "volume" in copilot_text
