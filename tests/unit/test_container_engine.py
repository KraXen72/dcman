from __future__ import annotations

import pytest

from dcman import container
from tests.helpers import write_executable


@pytest.mark.unit
def test_container_engine_prefers_podman(fake_bin_dir, monkeypatch: pytest.MonkeyPatch) -> None:
	write_executable(fake_bin_dir / "podman", "#!/usr/bin/env bash\nexit 0\n")
	write_executable(fake_bin_dir / "docker", "#!/usr/bin/env bash\nexit 0\n")

	container._client.cache_clear()
	assert container.container_engine() == "podman"

	monkeypatch.setenv("DCMAN_CONTAINER_ENGINE", "docker")
	container._client.cache_clear()
	assert container.container_engine() == "docker"
