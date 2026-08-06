"""Score SQLite-projected dossier completeness and write display reports."""
from __future__ import annotations

import argparse
import statistics
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.common import (
    CANONICAL_DB,
    DOSSIER_DIR,
    emit,
)
from packs.ingestion.primitives.common.jsonio import now_iso, parse_json_object, write_json
from packs.ingestion.primitives.deep_context.db.models import ArtifactKind, CanonicalSnapshot
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.deep_context.synthesis.prompting import (
    DEFAULT_TARGET_CONFIDENCE,
)

DEFAULT_MIN_CONFIDENCE = 0.5
def _pct(n: int, total: int) -> float:
    return round(100 * n / total, 1) if total else 0.0


@dataclass(frozen=True)
class DossierRow:
    """One facts record joined to its projected source bundle."""

    parent_id: str
    name: str
    confidence: float
    has_rel: bool
    has_emp: bool
    has_title: bool
    has_school: bool
    has_loc: bool
    n_topics: int
    n_events: int
    n_shared: int
    batches_used: int
    messages_used: int
    messages_available: int
    capped: bool
    stop_reason: str
    error: bool

    @classmethod
    def from_record(cls, parent_id: str, rec: dict[str, Any], bundle: dict[str, Any]) -> DossierRow:
        fa = rec.get("facts") or {}
        n_bundled = len(bundle.get("messages") or [])
        return cls(
            parent_id=parent_id,
            name=fa.get("canonical_name") or bundle.get("full_name") or "?",
            confidence=float(fa.get("confidence") or 0.0),
            has_rel=bool(fa.get("relationship_to_owner")),
            has_emp=bool(fa.get("employers")),
            has_title=bool(fa.get("title")),
            has_school=bool(fa.get("school")),
            has_loc=bool(fa.get("location")),
            n_topics=len(fa.get("topics") or []),
            n_events=len(fa.get("notable_events") or []),
            n_shared=len(fa.get("shared_context") or []),
            batches_used=rec.get("batches_used", 1),
            messages_used=rec.get("messages_used", n_bundled),
            messages_available=rec.get("messages_available", n_bundled),
            # PINNED: capped compares the RAW record counts, not the
            # bundle-length fallbacks above — a record carrying neither key is
            # 0 > 0, i.e. not capped, even though it reports n/n messages.
            capped=rec.get("messages_available", 0) > rec.get("messages_used", 0),
            stop_reason=rec.get("stop_reason", ""),
            error=bool(rec.get("error")),
        )

def collect_rows(snapshot: CanonicalSnapshot) -> list[DossierRow]:
    """Parse every projected fact joined with its projected source bundle."""
    bundles = {
        row.parent_id: parse_json_object(row.payload_json)
        for row in snapshot.artifacts
        if row.kind == ArtifactKind.SOURCE_BUNDLE.value and row.person_id is None
    }
    records = {
        row.parent_id: parse_json_object(row.payload_json)
        for row in snapshot.artifacts
        if row.kind == ArtifactKind.FACTS.value and row.person_id is None
    }
    rows: list[DossierRow] = []
    for fact in snapshot.facts:
        if fact.person_id is not None:
            continue
        record = records.get(fact.parent_id)
        if record is None:
            continue
        record["facts"] = parse_json_object(fact.facts_json)
        rows.append(DossierRow.from_record(
            fact.parent_id, record, bundles.get(fact.parent_id, {}),
        ))
    return rows


def _brief(rows: list[DossierRow], k: int = 10) -> list[dict[str, Any]]:
    return [{"name": r.name, "parent_id": r.parent_id, "confidence": round(r.confidence, 2),
             "messages": f"{r.messages_used}/{r.messages_available}", "stop": r.stop_reason} for r in rows[:k]]


class ValidateDossiers:
    """Scores dossier completeness over canonical SQLite projections.

    Read-only on the pipeline's data: the only writes are validation.json and
    validation.md in the dossier dir, and the same dict is returned for stdout.
    """

    def __init__(
        self,
        *,
        dossier_dir: Path = DOSSIER_DIR,
        db: Db | None = None,
        db_path: Path = CANONICAL_DB,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        target_confidence: float = DEFAULT_TARGET_CONFIDENCE,
    ) -> None:
        self.dossier_dir = Path(dossier_dir)
        self.db = db or Db(Path(db_path))
        self.min_confidence = min_confidence
        self.target_confidence = target_confidence

    def run(self) -> dict[str, Any]:
        rows = collect_rows(canonical_snapshot(self.db))
        n = len(rows)
        if not n:
            return {"source": "validate_dossiers", "status": "empty", "people": 0, "updated_at": now_iso()}

        fields = {
            "relationship": sum(r.has_rel for r in rows),
            "employer": sum(r.has_emp for r in rows),
            "title": sum(r.has_title for r in rows),
            "school": sum(r.has_school for r in rows),
            "location": sum(r.has_loc for r in rows),
            "shared_context": sum(1 for r in rows if r.n_shared),
        }
        stop_reasons = dict(Counter(r.stop_reason for r in rows))

        low_conf = sorted([r for r in rows if r.confidence < self.min_confidence], key=lambda r: r.confidence)
        capped_under = sorted(
            [r for r in rows if r.capped and r.confidence < self.target_confidence],
            key=lambda r: r.messages_available, reverse=True,
        )
        empty_rel = [r for r in rows if not r.has_rel]
        errored = [r for r in rows if r.error]

        # Composite completeness: relationship + employer + topic-bearing + confident.
        score = round(100 * statistics.mean(
            0.35 * r.has_rel + 0.2 * r.has_emp + 0.2 * (r.n_topics > 0) + 0.25 * min(r.confidence / self.target_confidence, 1.0)
            for r in rows
        ), 1)

        manifest = {
            "source": "validate_dossiers",
            "status": "completed",
            "people": n,
            "completeness_score": score,
            "field_fill_pct": {k: _pct(v, n) for k, v in fields.items()},
            "topics_mean": round(statistics.mean(r.n_topics for r in rows), 1),
            "events_mean": round(statistics.mean(r.n_events for r in rows), 1),
            "confidence_mean": round(statistics.mean(r.confidence for r in rows), 2),
            "confidence_ge_target_pct": _pct(sum(1 for r in rows if r.confidence >= self.target_confidence), n),
            "depth": {
                "avg_batches": round(statistics.mean(r.batches_used for r in rows), 2),
                "total_messages_grokked": sum(r.messages_used for r in rows),
                "capped_people": sum(1 for r in rows if r.capped),
                "stop_reasons": stop_reasons,
            },
            "flags": {
                "low_confidence": {"count": len(low_conf), "examples": _brief(low_conf)},
                "capped_underconfident": {"count": len(capped_under), "examples": _brief(capped_under),
                                          "hint": "raise --deep-cap to grok more of these"},
                "empty_relationship": {"count": len(empty_rel), "examples": _brief(empty_rel)},
                "errors": {"count": len(errored), "examples": _brief(errored)},
            },
            "updated_at": now_iso(),
        }
        self.dossier_dir.mkdir(parents=True, exist_ok=True)
        write_json(self.dossier_dir / "validation.json", manifest)
        _write_md(self.dossier_dir / "validation.md", manifest)
        return manifest


def _write_md(path: Path, m: dict[str, Any]) -> None:
    lines = [
        f"# Dossier completeness ({m['people']} people)", "",
        f"_Generated {m['updated_at']}._", "",
        f"**Completeness score: {m['completeness_score']}/100**", "",
        "## Field fill", "",
        *(f"- {k}: {v}%" for k, v in m["field_fill_pct"].items()),
        "",
        f"- topics/profile: {m['topics_mean']}  ·  events/profile: {m['events_mean']}",
        f"- confidence mean: {m['confidence_mean']}  ·  ≥target: {m['confidence_ge_target_pct']}%",
        f"- avg batches: {m['depth']['avg_batches']}  ·  messages grokked: {m['depth']['total_messages_grokked']}  ·  capped people: {m['depth']['capped_people']}",
        f"- stop reasons: {m['depth']['stop_reasons']}", "",
        "## Flags", "",
    ]
    for key, f in m["flags"].items():
        lines.append(f"### {key}: {f['count']}" + (f" — _{f['hint']}_" if f.get("hint") else ""))
        for ex in f["examples"]:
            lines.append(f"- {ex['name']} (conf {ex['confidence']}, {ex['messages']} msgs, {ex['stop']})")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Validate deep-context dossier completeness (read-only).")
    p.add_argument("--dossier-dir", default=str(DOSSIER_DIR))
    p.add_argument("--db", default=str(CANONICAL_DB))
    p.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE)
    p.add_argument("--target-confidence", type=float, default=DEFAULT_TARGET_CONFIDENCE)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = open_existing_db(args.db)
    emit(ValidateDossiers(
        dossier_dir=Path(args.dossier_dir),
        db=db,
        min_confidence=args.min_confidence,
        target_confidence=args.target_confidence,
    ).run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
