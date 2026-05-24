from __future__ import annotations

import subprocess

import pytest

from dcman import container


@pytest.mark.unit
def test_container_writable_size_reads_size_rw(monkeypatch: pytest.MonkeyPatch) -> None:
	calls: list[list[str]] = []

	def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
		calls.append(cmd)
		return subprocess.CompletedProcess(cmd, 0, stdout='[{"SizeRw": 1536, "SizeRootFs": 999999}]', stderr="")

	monkeypatch.setattr(container, "container_engine", lambda: "podman")
	monkeypatch.setattr(container, "run", fake_run)

	assert container.container_writable_size("container123") == 1536
	assert calls == [["podman", "container", "inspect", "--size", "container123"]]


@pytest.mark.unit
def test_container_writable_size_returns_none_on_unreadable_output(monkeypatch: pytest.MonkeyPatch) -> None:
	def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
		return subprocess.CompletedProcess(cmd, 0, stdout="not json", stderr="")

	monkeypatch.setattr(container, "container_engine", lambda: "podman")
	monkeypatch.setattr(container, "run", fake_run)

	assert container.container_writable_size("container123") is None


@pytest.mark.unit
def test_container_writable_size_returns_none_on_failed_inspect(monkeypatch: pytest.MonkeyPatch) -> None:
	def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
		return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no such container")

	monkeypatch.setattr(container, "container_engine", lambda: "podman")
	monkeypatch.setattr(container, "run", fake_run)

	assert container.container_writable_size("container123") is None
