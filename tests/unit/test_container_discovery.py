from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from python_on_whales.exceptions import DockerException

from dcman import container
from dcman.errors import CmdError


def _docker_exception(stderr: str) -> DockerException:
	return DockerException(["podman", "container", "inspect", "container123"], 125, b"", stderr.encode())


def _devcontainer(container_id: str, workspace: str) -> SimpleNamespace:
	return SimpleNamespace(
		id=container_id,
		name=f"dcman_{container_id[:8]}",
		config=SimpleNamespace(labels={"devcontainer.local_folder": workspace}),
		mounts=[],
		state=SimpleNamespace(status="exited"),
	)


class _UninspectableContainer:
	@property
	def config(self) -> SimpleNamespace:
		raise _docker_exception('Error: getting container from store "container123": container not known')


@pytest.mark.unit
def test_list_initialized_devcontainers_skips_stale_list_entry(monkeypatch: pytest.MonkeyPatch) -> None:
	live = _devcontainer("live-container", "/workspace")
	monkeypatch.setattr(container, "_list_containers", lambda *, all_containers: [_UninspectableContainer(), live])

	assert container.list_initialized_devcontainers() == [
		{
			"id": "live-container",
			"short_id": "live-contain",
			"name": "dcman_live-con",
			"status": "exited",
			"workspace": "/workspace",
		}
	]


@pytest.mark.unit
def test_list_initialized_devcontainers_reraises_unexpected_inspect_error(monkeypatch: pytest.MonkeyPatch) -> None:
	class BrokenContainer:
		@property
		def config(self) -> SimpleNamespace:
			raise _docker_exception("Error: permission denied")

	monkeypatch.setattr(container, "_list_containers", lambda *, all_containers: [BrokenContainer()])

	with pytest.raises(DockerException, match="permission denied"):
		container.list_initialized_devcontainers()


class _ListedContainer:
	def __init__(self, container_id: str, reload_error: DockerException | None = None) -> None:
		self.id = container_id
		self.reload_error = reload_error

	def reload(self) -> None:
		if self.reload_error is not None:
			raise self.reload_error


class _ContainerClient:
	def __init__(self, listed: list[_ListedContainer]) -> None:
		self.listed = listed
		self.list_calls: list[dict[str, object]] = []
		self.removed: list[tuple[str, bool]] = []

	def list(self, **kwargs: object) -> list[_ListedContainer]:
		self.list_calls.append(kwargs)
		return self.listed

	def remove(self, container_id: str, *, force: bool = False) -> None:
		self.removed.append((container_id, force))


@pytest.mark.unit
def test_repair_stale_devcontainer_entries_removes_missing_list_entry(monkeypatch: pytest.MonkeyPatch) -> None:
	stale = _ListedContainer("stale-container", _docker_exception('Error: no such container "stale-container"'))
	container_client = _ContainerClient([stale])
	monkeypatch.setattr(container, "_client", lambda: SimpleNamespace(container=container_client))

	container._repair_stale_devcontainer_entries(Path("/workspace"))

	assert container_client.list_calls == [
		{"all": True, "filters": [("label", "devcontainer.local_folder=/workspace")]}
	]
	assert container_client.removed == [("stale-container", True)]


@pytest.mark.unit
def test_repair_stale_devcontainer_entries_uses_podman_cleanup_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
	stale = _ListedContainer("stale-container", _docker_exception('Error: no such container "stale-container"'))
	cleanup_calls: list[list[str]] = []

	class BrokenRemoveClient(_ContainerClient):
		def remove(self, container_id: str, *, force: bool = False) -> None:
			raise _docker_exception("Error: no such container")

	def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
		cleanup_calls.append(cmd)
		return SimpleNamespace(returncode=0)

	monkeypatch.setattr(container, "_client", lambda: SimpleNamespace(container=BrokenRemoveClient([stale])))
	monkeypatch.setattr(container, "container_engine", lambda: "podman")
	monkeypatch.setattr(container, "run", fake_run)

	container._repair_stale_devcontainer_entries(Path("/workspace"))

	assert cleanup_calls == [["podman", "container", "cleanup", "--rm", "stale-container"]]


@pytest.mark.unit
def test_repair_stale_devcontainer_entries_reraises_unexpected_inspect_error(monkeypatch: pytest.MonkeyPatch) -> None:
	broken = _ListedContainer("broken-container", _docker_exception("Error: permission denied"))
	monkeypatch.setattr(container, "_client", lambda: SimpleNamespace(container=_ContainerClient([broken])))

	with pytest.raises(CmdError, match="permission denied"):
		container._repair_stale_devcontainer_entries(Path("/workspace"))
