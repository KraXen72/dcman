from __future__ import annotations

import os
from pathlib import Path

import pytest

from dcman import config, container as container_mod, devcontainer_cli, state
from dcman.integrations import zed


def pytest_addoption(parser: pytest.Parser) -> None:
	parser.addoption(
		"--run-engine-e2e",
		action="store_true",
		default=False,
		help="run tests marked e2e_engine (requires a real container engine)",
	)


def _e2e_requested(config: pytest.Config) -> bool:
	for arg in config.args:
		if Path(arg).name == "e2e":
			return True
	return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
	if config.getoption("--run-engine-e2e") or _e2e_requested(config):
		return
	skip_marker = pytest.mark.skip(reason="needs --run-engine-e2e or pytest e2e")
	for item in items:
		if item.get_closest_marker("e2e_engine"):
			item.add_marker(skip_marker)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	home = tmp_path / "home"
	home.mkdir(parents=True, exist_ok=True)
	monkeypatch.setenv("HOME", str(home))
	monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
	monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
	monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))

	state_root = home / ".cache" / "dcman"
	monkeypatch.setattr(config, "STATE_ROOT", state_root, raising=False)
	monkeypatch.setattr(config, "LEGACY_STATE_ROOT", home / ".cache" / "devcontainer-lifecycle", raising=False)
	monkeypatch.setattr(config, "HOST_SSH_PUBKEY", home / ".ssh" / "id_ed25519.pub", raising=False)
	monkeypatch.setattr(state, "STATE_ROOT", state_root, raising=False)
	monkeypatch.setattr(zed, "HOST_SSH_PUBKEY", config.HOST_SSH_PUBKEY, raising=False)

	state_root.mkdir(parents=True, exist_ok=True)

	container_mod._devcontainer_config.cache_clear()
	container_mod._client.cache_clear()
	devcontainer_cli.supports_up_no_lockfile.cache_clear()

	# Ensure PATH is always defined for tests that prepend fake binaries.
	monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
	yield


@pytest.fixture
def fake_bin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
	bin_dir = tmp_path / "fake-bin"
	bin_dir.mkdir(parents=True, exist_ok=True)
	monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
	return bin_dir
