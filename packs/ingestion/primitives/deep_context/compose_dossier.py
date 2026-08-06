#!/usr/bin/env python3
"""Compose synthesized facts into person dossiers, index records, and a catalog.

The stable Node and CLI own the ``slugs`` index slice. Fact reduction and
byte-stable artifact rendering live in the concrete ``deep_context.dossier``
modules so other stages import policy from its defining home.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.common import (
    DOSSIER_DIR,
    DOSSIER_TEMPLATE,
    DOSSIERS_MANIFEST,
    FACTS_DIR,
    FACTS_TEMPLATE,
    INDEX_JSON,
    INDEX_MD,
    RAW_BUNDLE_TEMPLATE,
    RAW_DIR,
    emit,
    load_index,
    read_jsonl,
    slugify,
    write_index,
)
from packs.ingestion.primitives.deep_context.dossier.facts import headline, merge_facts
from packs.ingestion.primitives.deep_context.dossier.rendering import render_dossier, write_catalog
from packs.ingestion.primitives.pipeline.contract import Artifact, Node, StageManifest


class ComposeDossierManifest(StageManifest):
    source: str = "compose_dossier"
    dossiers_written: int = 0
    orphans_removed: int = 0
    dossier_dir: str = ""
    index_json: str = ""
    index_md: str = ""
    elapsed_ms: int = 0


class ComposeDossier(Node):
    """Render dossiers and replace the owned ``slugs`` index slice."""

    name = "deep_compose"
    inputs = (
        Artifact(path=FACTS_TEMPLATE, required=False),
        Artifact(path=RAW_BUNDLE_TEMPLATE, required=False),
    )
    outputs = (
        Artifact(path=DOSSIER_TEMPLATE, required=False),
        Artifact(path=str(INDEX_JSON), writes="upsert", owns_columns=("slugs",)),
    )
    payload = ComposeDossierManifest
    manifest = str(DOSSIERS_MANIFEST)

    def __init__(
        self,
        *,
        raw_dir: Path | None = None,
        facts_dir: Path | None = None,
        dossier_dir: Path | None = None,
        index_json: Path | None = None,
        index_md: Path | None = None,
        person: str = "",
    ) -> None:
        self.raw_dir = Path(raw_dir or RAW_DIR)
        self.facts_dir = Path(facts_dir or FACTS_DIR)
        self.dossier_dir = Path(dossier_dir or DOSSIER_DIR)
        self.index_json = Path(index_json or INDEX_JSON)
        self.index_md = Path(index_md or INDEX_MD)
        self.person = person

    def bindings(self) -> dict[str, str]:
        return {
            FACTS_TEMPLATE: str(self.facts_dir / "{person_id}.jsonl"),
            RAW_BUNDLE_TEMPLATE: str(self.raw_dir / "{person_id}.json"),
            DOSSIER_TEMPLATE: str(self.dossier_dir / "{slug}.md"),
            str(INDEX_JSON): str(self.index_json),
            self.manifest: str(self.dossier_dir / "manifest.json"),
        }

    def execute(self) -> ComposeDossierManifest:
        started = time.monotonic()
        self.dossier_dir.mkdir(parents=True, exist_ok=True)
        index = load_index(self.index_json)
        slugs: dict[str, Any] = dict(index.get("slugs") or {}) if self.person else {}
        index["slugs"] = slugs
        catalog: list[tuple[str, str, str]] = []
        written_slugs: set[str] = set()
        written = 0

        for facts_path in sorted(self.facts_dir.glob("*.jsonl")):
            if facts_path.name == "manifest.json":
                continue
            person_id = facts_path.stem
            if self.person and person_id != self.person:
                continue
            raw_path = self.raw_dir / f"{person_id}.json"
            if not raw_path.exists():
                continue
            meta = json.loads(raw_path.read_text(encoding="utf-8"))
            chunks = list(read_jsonl(facts_path))
            merged = merge_facts(chunks)
            if not merged:
                continue
            name = merged.get("canonical_name") or meta.get("full_name") or "person"
            slug = slugify(name, person_id)
            depth = chunks[-1] if chunks else {}
            (self.dossier_dir / f"{slug}.md").write_text(
                render_dossier(meta, merged, depth), encoding="utf-8",
            )
            written_slugs.add(slug)
            written += 1
            for stale in [
                prior_slug for prior_slug, info in slugs.items()
                if prior_slug != slug and (info or {}).get("person_id") == person_id
            ]:
                slugs.pop(stale)
            summary = headline(merged)
            slugs[slug] = {
                "person_id": person_id, "name": name, "path": f"dossiers/{slug}.md",
                "headline": summary, "full_name": str(meta.get("full_name") or ""),
                "emails": list(meta.get("emails") or []),
                "phones": list(meta.get("phones") or []),
            }
            catalog.append((name, summary, slug))

        orphans = 0
        if not self.person:
            for path in self.dossier_dir.glob("*.md"):
                if path.stem not in written_slugs:
                    path.unlink()
                    orphans += 1
        write_index(self.index_json, index)
        if self.person:
            catalog = [
                (info.get("name") or slug, info.get("headline") or "", slug)
                for slug, info in slugs.items()
            ]
        write_catalog(self.index_md, catalog)
        return ComposeDossierManifest(
            status="completed", dossiers_written=written,
            orphans_removed=orphans, dossier_dir=str(self.dossier_dir),
            index_json=str(self.index_json), index_md=str(self.index_md),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compose markdown dossiers + lookup index from synthesized facts.",
    )
    parser.add_argument("--raw-dir", default=str(RAW_DIR))
    parser.add_argument("--facts-dir", default=str(FACTS_DIR))
    parser.add_argument("--dossier-dir", default=str(DOSSIER_DIR))
    parser.add_argument("--index-json", default=str(INDEX_JSON))
    parser.add_argument("--index-md", default=str(INDEX_MD))
    parser.add_argument("--person", default="", help="Only this person id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = ComposeDossier(
        raw_dir=Path(args.raw_dir), facts_dir=Path(args.facts_dir),
        dossier_dir=Path(args.dossier_dir), index_json=Path(args.index_json),
        index_md=Path(args.index_md), person=args.person,
    ).run()
    emit(payload.to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
