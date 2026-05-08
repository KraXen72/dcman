from __future__ import annotations

# Shared domain errors so CLI can present consistent user-facing failures.


class CmdError(RuntimeError):
	pass


class SecretToolUnavailable(CmdError):
	pass
