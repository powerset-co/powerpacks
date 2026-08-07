"""Load strict stage-local Jinja templates."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined


def template_environment(directory: Path, *, html: bool) -> Environment:
    """Return a strict environment; HTML stages escape every interpolated value."""
    return Environment(
        loader=FileSystemLoader(directory),
        autoescape=html,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
