from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path

import click
from click.testing import CliRunner, Result


def write_text(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def write_json(path: Path, payload: dict) -> None:
	write_text(path, json.dumps(payload, indent=2) + "\n")


def write_executable(path: Path, content: str) -> None:
	write_text(path, content)
	path.chmod(0o755)


def make_workspace(root: Path, files: dict[str, str]) -> Path:
	root.mkdir(parents=True, exist_ok=True)
	for rel, content in files.items():
		write_text(root / rel, content)
	return root


def load_json(path: Path) -> dict:
	return json.loads(path.read_text())


def invoke_in_click_context(
	callback: Callable[[], None],
	*,
	input: str | None = None,
	catch_exceptions: bool = False,
) -> Result:
	@click.command()
	def run() -> None:
		callback()

	return CliRunner().invoke(run, input=input, catch_exceptions=catch_exceptions)
