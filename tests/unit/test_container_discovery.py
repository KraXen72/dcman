from __future__ import annotations

from types import SimpleNamespace

import pytest
from python_on_whales.exceptions import DockerException

from dcman import container


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
