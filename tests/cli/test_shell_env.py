from __future__ import annotations

import pytest

from dcman import cli, config


@pytest.mark.cli
def test_shell_env_forwards_terminal_and_auth_vars(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setenv("TERM", "xterm")
	monkeypatch.setenv("COLORTERM", "truecolor")
	monkeypatch.setenv("FORCE_COLOR", "1")
	monkeypatch.setenv("SHOULD_NOT_FORWARD", "nope")

	monkeypatch.setattr(cli, "_rich_color_system", lambda env: None)

	env = {
		config.AUTH_PROVIDERS["copilot"]: "token-123",
		"SHOULD_NOT_FORWARD": "nope",
	}
	container_env = cli._shell_env(env)

	assert container_env["TERM"] == "xterm"
	assert container_env["COLORTERM"] == "truecolor"
	assert container_env["FORCE_COLOR"] == "1"
	assert container_env[config.AUTH_PROVIDERS["copilot"]] == "token-123"
	assert "SHOULD_NOT_FORWARD" not in container_env
