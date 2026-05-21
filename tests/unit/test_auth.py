from __future__ import annotations

import pytest

from dcman import auth, config
from tests.helpers import write_executable


@pytest.mark.unit
def test_build_env_reads_token_from_fake_secret_tool(fake_bin_dir, monkeypatch: pytest.MonkeyPatch) -> None:
	script = """#!/usr/bin/env bash
set -euo pipefail
cmd="$1"
shift
if [[ "$cmd" == "lookup" ]]; then
  provider="${@: -1}"
  var="DCMAN_SECRET_${provider}"
  token="${!var-}"
  if [[ -n "$token" ]]; then
    printf '%s' "$token"
    exit 0
  fi
  exit 1
fi
if [[ "$cmd" == "store" ]]; then
  cat >/dev/null
  exit 0
fi
if [[ "$cmd" == "clear" ]]; then
  exit 0
fi
exit 0
"""
	write_executable(fake_bin_dir / "secret-tool", script)
	monkeypatch.setenv("DCMAN_SECRET_copilot", "token-xyz")

	env, warnings = auth.build_env(with_tokens=True)
	assert warnings == []
	assert env[config.AUTH_PROVIDERS["copilot"]] == "token-xyz"


@pytest.mark.unit
def test_build_env_warns_without_secret_tool(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setenv("PATH", "")
	env, warnings = auth.build_env(with_tokens=True)
	assert env
	assert any("secret-tool not found" in warning for warning in warnings)
