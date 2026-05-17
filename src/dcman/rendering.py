from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from io import StringIO
from typing import Literal

from rich import box
from rich.cells import cell_len
from rich.console import Console, RenderableType
from rich.syntax import Syntax
from rich.table import Table

Justify = Literal["default", "left", "center", "right", "full"]


def _capture_console() -> tuple[Console, StringIO]:
	buffer = StringIO()
	use_color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
	console = Console(
		file=buffer,
		force_terminal=use_color,
		color_system="auto" if use_color else None,
		width=4096,
	)
	return console, buffer


def render_to_string(renderable: RenderableType) -> str:
	console, buffer = _capture_console()
	console.print(renderable)
	return buffer.getvalue().rstrip("\n")


def _column_justify(header: str, cells: Sequence[str]) -> Justify:
	if cells and max(cell_len(cell) for cell in cells) < cell_len(header):
		return "right"
	return "left"


def render_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
	string_rows = [tuple(str(cell) for cell in row) for row in rows]
	table = Table(
		box=box.HORIZONTALS,
		show_header=True,
		show_edge=False,
		padding=(0, 1),
		collapse_padding=False,
	)

	for index, header in enumerate(headers):
		cells = [row[index] for row in string_rows]
		table.add_column(header, justify=_column_justify(header, cells), no_wrap=True)
	for row in string_rows:
		table.add_row(*row)

	rendered = render_to_string(table)
	lines = rendered.splitlines()
	separator = next((line for line in lines if set(line.strip()) == {"─"}), "")
	width = cell_len(separator) if separator else max((cell_len(line.rstrip()) for line in lines), default=0)
	return f"{rendered}\n{'─' * width}" if width else rendered


def render_diff(diff: str) -> str:
	return render_to_string(Syntax(diff, "diff", word_wrap=False, background_color="default"))
