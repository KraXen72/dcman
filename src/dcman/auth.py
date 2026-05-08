from __future__ import annotations

import os
import shutil
import subprocess

from .config import AUTH_PROVIDERS
from .errors import CmdError, SecretToolUnavailable
from .process import run

# Integrates with Linux `secret-tool` to keep tokens out of shell history/files
# and inject them into container processes only when needed.


def require_secret_tool() -> None:
	# We intentionally hard-require it in token operations so failures are explicit.
	if shutil.which("secret-tool") is None:
		raise SecretToolUnavailable("secret-tool was not found in PATH.")


def _secret_attrs(provider: str) -> list[str]:
	# secret-tool identifies secrets by a flat list of attribute key-value pairs:
	# ["key1", "val1", "key2", "val2", ...]. We use two attributes — "app" and
	# "provider" — so entries are scoped to dcman and won't collide with other apps.
	return ["app", "dcman-devcontainer", "provider", provider]


def get_provider_token(provider: str) -> str | None:
	require_secret_tool()
	# `secret-tool lookup ...` returns non-zero when no entry matches.
	result = run(["secret-tool", "lookup", *_secret_attrs(provider)], capture=True, check=False)
	if result.returncode != 0:
		return None
	token = result.stdout.strip()
	return token or None


def store_provider_token(provider: str, token: str) -> None:
	require_secret_tool()
	label = f"dcman token: {provider}"
	proc = subprocess.run(
		["secret-tool", "store", f"--label={label}", *_secret_attrs(provider)],
		# secret-tool reads the secret value from stdin (newline-terminated).
		input=token + "\n",
		text=True,
	)
	if proc.returncode != 0:
		raise CmdError(f"failed to store {provider} token in secret-tool")


def clear_provider_token(provider: str) -> bool:
	require_secret_tool()
	# Return status allows caller to print "cleared" vs "nothing stored".
	result = run(["secret-tool", "clear", *_secret_attrs(provider)], capture=True, check=False)
	return result.returncode == 0


def build_env(with_tokens: bool) -> tuple[dict[str, str], list[str]]:
	env = os.environ.copy()
	warnings: list[str] = []
	if not with_tokens:
		# Some commands only need ambient env; skip secret lookup entirely.
		return env, warnings
	# Fail-soft here: shell access is still useful even if secret storage is missing.
	try:
		require_secret_tool()
	except SecretToolUnavailable:
		warnings.append("secret-tool not found; starting without any stored tokens.")
		return env, warnings
	for provider, env_var in AUTH_PROVIDERS.items():
		token = get_provider_token(provider)
		if token:
			# Value is only forwarded into spawned container commands, not persisted.
			env[env_var] = token
		else:
			warnings.append(f"no token found for provider {provider!r}; starting without it.")
	return env, warnings
