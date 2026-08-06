"""Executable entry for the SQLite-backed Deep Context review UI."""

from packs.ingestion.primitives.deep_context.review_web.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
