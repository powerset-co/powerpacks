"""Load strict stage-local Jinja templates."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined


def template_environment(directory: Path, *, html: bool) -> Environment:
    """Return a strict environment; HTML stages escape every interpolated value.

    Each caller points `directory` at its own stage-local `templates/` dir (merge
    dossiers, synthesis dossiers, the review UI markup) — there is no shared
    templates directory. StrictUndefined turns a typo'd/missing template variable
    into a render-time error instead of a silently blank dossier field.
    """
    return Environment(
        loader=FileSystemLoader(directory),
        autoescape=html,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
