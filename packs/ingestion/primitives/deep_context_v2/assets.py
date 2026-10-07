"""The prompt and model files that live next to a block's code, read one way everywhere.

A block's assets (a system prompt, a response schema, a frozen model, a prompt template) sit in its own
directory. Each is read once at import: `text` strips the file-ending newline, `json_file` parses the file,
`template` loads a Jinja template for the prompts that are built from data.

Created: 2026-10-07
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jinja2


def text(directory: Path, name: str) -> str:
    """The text file `name` in `directory`, minus its file-ending newline."""
    return (directory / name).read_text(encoding="utf-8").removesuffix("\n")


def json_file(directory: Path, name: str) -> Any:
    """The JSON file `name` in `directory`, parsed."""
    return json.loads((directory / name).read_text(encoding="utf-8"))


def template(directory: Path, name: str) -> jinja2.Template:
    """The Jinja template `name` in `directory`. Block tags take their line with them (trim and lstrip), no
    autoescaping: these are prompts, not HTML."""
    environment: jinja2.Environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(directory)), trim_blocks=True, lstrip_blocks=True,
        keep_trailing_newline=False, autoescape=False, undefined=jinja2.StrictUndefined,
    )
    return environment.get_template(name)
