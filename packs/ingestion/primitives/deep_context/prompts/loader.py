"""Load a named Deep Context prompt asset without interpreting its contents."""
from __future__ import annotations

from functools import cache
from pathlib import Path

_PROMPT_DIR = Path(__file__).parent


@cache
def load_prompt(name: str) -> str:
    """Return one trusted ``<name>.txt`` asset, minus its file-ending newline."""
    if not name.isidentifier():
        raise ValueError(f"invalid prompt name: {name!r}")
    return (_PROMPT_DIR / f"{name}.txt").read_text(encoding="utf-8").removesuffix("\n")
