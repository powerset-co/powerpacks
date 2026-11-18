"""Bring every saved person's Logbook up to date from the local stores (the scheduled refresh's step).

Flow: read `.powerpacks/logbook/manifest.json` -> its person entries that are still a family in
the v2 store (by archive slug, as the reader matches them) -> `build_logbook(append=True)`, which
appends only what each entry's saved watermark has not seen, for the person and their groups.

Nobody gets a logbook here who did not have one: the People page's "Build logbook" starts them.
No LLM, no network, no spend, nothing uploaded. An entry whose person is gone from the store (or
was renamed) is left as it is.

    python -m packs.ingestion.primitives.logbook.logbook_refresh --root ~/.powerpacks/app

Changelog:
  2026-11-18: created.
"""
from __future__ import annotations

import argparse
import json
from contextlib import closing
from pathlib import Path

from packs.ingestion.primitives.deep_context_v2.db.store import STORE_RELATIVE_PATH, open_store
from packs.ingestion.primitives.logbook.logbook_common import LOGBOOK_ROOT, MANIFEST_JSON
from packs.ingestion.primitives.logbook.logbook_export import LogbookBuild, build_logbook, default_paths, emit
from packs.ingestion.primitives.logbook.logbook_people import parent_slugs, people_for_parents


def refresh_logbooks(root: Path, *, stores: dict[str, Path] | None = None) -> LogbookBuild | None:
    """Append new messages to every saved person's logbook; None when there is none to update."""
    manifest = root / MANIFEST_JSON
    store = root / ".powerpacks" / STORE_RELATIVE_PATH
    if not manifest.exists() or not store.exists():
        return None
    entries = json.loads(manifest.read_text(encoding="utf-8")).get("entries", {})
    with closing(open_store(store)) as conn:
        slugs = parent_slugs(conn)
        parents = [slugs[slug] for slug, entry in entries.items() if entry.get("kind") == "person" and slug in slugs]
        people = people_for_parents(conn, parents)
    if not people:
        return None
    return build_logbook(people, paths=stores or default_paths(), root=root / LOGBOOK_ROOT, append=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    build = refresh_logbooks(parser.parse_args(argv).root.resolve())
    emit(build.to_payload() if build else {"entries": [], "messages": 0})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
