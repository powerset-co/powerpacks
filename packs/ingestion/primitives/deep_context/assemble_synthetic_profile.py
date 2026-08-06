"""Project no-LinkedIn research results as one synthetic identity per parent."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.common import (
    DEFAULT_PEOPLE_CSV,
    ENRICH_MANIFEST,
    FACTS_DIR,
    INDEX_JSON,
    LINKEDIN_OVERRIDES_CSV,
    VERDICTS_JSONL,
)
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.research_reconcile.selection import (
    DR_OUT_DIR,
    QUEUE_CSV,
)
from packs.ingestion.primitives.deep_context.research_result import ResearchResult
from packs.ingestion.primitives.deep_context.db import views
from packs.ingestion.primitives.deep_context.db.models import ApprovedState
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot, identity_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrichment_receipt import EnrichmentReceipt
from packs.ingestion.schemas.people_schema import CONTACT_CARRY_COLUMNS, PEOPLE_SCHEMA_COLUMNS

ROOT = Path(__file__).resolve().parents[4]
CANONICAL_DB = ROOT / ".powerpacks" / "deep-context" / "deep-context.sqlite"
SYNTHETIC_PEOPLE_CSV = LINKEDIN_OVERRIDES_CSV.parent / "synthetic-people.csv"
DEFAULT_OUT = SYNTHETIC_PEOPLE_CSV
SYNTHETIC_PROVENANCE_COLUMNS = [
    "source_parent_slug", "source_person_ids", "source_candidate_public_identifier",
]
SYNTHETIC_COLUMNS = PEOPLE_SCHEMA_COLUMNS + SYNTHETIC_PROVENANCE_COLUMNS + [
    "approved", "synthetic_metadata",
]
DEFAULT_AUTO_COMPLETENESS = 0.6
USER_APPROVED = frozenset({ApprovedState.YES.value, ApprovedState.NO.value})


class Payload(dict):
    """Small result object retained for existing `.status`/`.to_payload()` callers."""

    def __getattr__(self, key: str) -> Any:
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def to_payload(self) -> dict[str, Any]:
        return dict(self)


@dataclass(frozen=True)
class ResearchContact:
    handle: str
    display_name: str = ""
    primary_email: str = ""
    phone_e164: str = ""
    source_channel: str = ""
    source_parent_slug: str = ""
    source_person_ids: str = ""
    source_candidate_public_identifier: str = ""

    @classmethod
    def from_row(cls, handle: str, row: dict[str, str]) -> ResearchContact:
        names = cls.__dataclass_fields__
        return cls(handle=handle, **{
            key: str(value or "") for key, value in row.items()
            if key != "handle" and key in names
        })


def synth_public_identifier(email: str, phone: str, handle: str) -> str:
    if email:
        value = hashlib.sha1(email.strip().lower().encode()).hexdigest()[:12]
        return f"synth-email-{value}"
    if phone:
        value = hashlib.sha1(phone.strip().encode()).hexdigest()[:12]
        return f"synth-phone-{value}"
    return f"synth-x-{handle.strip().lower()}"


def build_synthetic_row(
    profile: dict[str, Any], contact: ResearchContact,
    original: dict[str, str] | None, person_id: str,
    auto_completeness: float = DEFAULT_AUTO_COMPLETENESS,
    provenance: dict[str, str] | None = None,
) -> dict[str, str]:
    person, location = profile.get("person") or {}, profile.get("location") or {}
    metadata, social = profile.get("metadata") or {}, profile.get("social") or {}
    positions = [p for p in profile.get("positions") or []
                 if isinstance(p, dict) and (p.get("company_name") or p.get("title"))]
    education = [e for e in profile.get("education") or [] if isinstance(e, dict)]
    current = next((p for p in positions if p.get("is_current")), {})
    provenance = provenance or {}
    pub = synth_public_identifier(contact.primary_email, contact.phone_e164, contact.handle)
    completeness = float(metadata.get("estimated_completeness") or 0)
    row = {column: "" for column in SYNTHETIC_COLUMNS}
    row.update({
        "id": person_id or pub,
        "public_identifier": pub,
        "full_name": person.get("full_name") or contact.display_name,
        "first_name": person.get("first_name") or "",
        "last_name": person.get("last_name") or "",
        "headline": (profile.get("headline") or {}).get("text") or "",
        "summary": (profile.get("summary") or {}).get("text") or "",
        "city": location.get("city") or "",
        "state": location.get("state") or "",
        "country": location.get("country") or "",
        "location_raw": location.get("raw") or ", ".join(
            value for value in (location.get("city"), location.get("country")) if value
        ),
        "work_experiences": json.dumps(positions, ensure_ascii=False) if positions else "",
        "education": json.dumps(education, ensure_ascii=False) if education else "",
        "current_title": current.get("title") or "",
        "current_company": current.get("company_name") or "",
        "entity_urn": f"synthetic:{person_id or pub}",
        "enrichment_provider": "synthetic",
        "enriched_at": now_iso(),
        "twitter_handle": social.get("twitter_handle") or "",
        "primary_email": contact.primary_email,
        "primary_phone": contact.phone_e164,
        "approved": "auto" if completeness >= auto_completeness else "",
        **{key: provenance.get(key) or "" for key in SYNTHETIC_PROVENANCE_COLUMNS},
        "synthetic_metadata": json.dumps({
            "completeness": completeness,
            "name_confidence": person.get("confidence"),
            "gaps": metadata.get("gaps") or [],
            "research_date": metadata.get("research_date") or "",
            "research_method": metadata.get("research_method") or "",
            "source_channel": metadata.get("source_channel") or contact.source_channel,
        }, ensure_ascii=False),
    })
    for column in CONTACT_CARRY_COLUMNS:
        if original and original.get(column):
            row[column] = original[column]
    return row


def _completeness(profile: dict[str, Any]) -> float:
    return float((profile.get("metadata") or {}).get("estimated_completeness") or 0)


def merge_research_profiles(profiles: list[dict[str, Any]]) -> dict[str, Any]:
    """Union multiple already-paid results which now belong to one parent."""
    if len(profiles) < 2:
        return profiles[0] if profiles else {}
    ordered = sorted(profiles, key=_completeness, reverse=True)
    best = dict(ordered[0])

    def first(field: str) -> Any:
        return next((profile.get(field) for profile in ordered if profile.get(field)), {})

    def unique(field: str, keys: tuple[str, ...]) -> list[dict[str, Any]]:
        rows, seen = [], set()
        for profile in ordered:
            for row in profile.get(field) or []:
                if not isinstance(row, dict):
                    continue
                key = tuple(str(row.get(name) or "").strip().lower() for name in keys)
                if any(key) and key not in seen:
                    seen.add(key)
                    rows.append(row)
        return rows

    metadata = dict(best.get("metadata") or {})
    metadata["estimated_completeness"] = max(map(_completeness, ordered))
    metadata["gaps"] = list(dict.fromkeys(
        str(gap).strip() for profile in ordered
        for gap in (profile.get("metadata") or {}).get("gaps") or [] if str(gap).strip()
    ))
    best.update({
        "headline": first("headline"),
        "summary": first("summary"),
        "location": first("location"),
        "positions": unique("positions", ("company_name", "title", "start_date")),
        "education": unique("education", ("school_name", "school", "degree")),
        "metadata": metadata,
    })
    return best


def _queue(path: Path) -> dict[str, dict[str, str]]:
    if not path.is_file():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        return {row["handle"]: row for row in csv.DictReader(handle) if row.get("handle")}


def _json_list(value: str) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return list(dict.fromkeys(str(item).strip().lower() for item in parsed
                              if str(item).strip())) if isinstance(parsed, list) else []


def _write_csv(path: Path, rows: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SYNTHETIC_COLUMNS)
        writer.writeheader()
        writer.writerows({key: row.get(key, "") for key in SYNTHETIC_COLUMNS}
                         for _, row in sorted(rows.items()))


def _human_decision(rows: dict[str, dict[str, str]], pubs: set[str]) -> str:
    decisions = {str((rows.get(pub) or {}).get("approved") or "").lower() for pub in pubs}
    return "no" if "no" in decisions else "yes" if "yes" in decisions else ""


class AssembleSyntheticProfile:
    """SQLite-first synthetic projection; CSV is a one-way result export."""

    name = "deep_assemble_synthetic"

    def __init__(
        self, *, db: Db, research_dir: Path | None = None,
        queue_csv: Path | None = None, people_csv: Path | None = None,
        verdicts_jsonl: Path | None = None, out: Path | None = None,
        index_json: Path | None = None, facts_dir: Path | None = None,
        auto_completeness: float = DEFAULT_AUTO_COMPLETENESS,
        manifest: str | Path | None = None, prune: bool = True,
    ) -> None:
        self.db = db
        self.research_dir = Path(research_dir or DR_OUT_DIR)
        self.queue_csv = Path(queue_csv or QUEUE_CSV)
        self.out = Path(out or DEFAULT_OUT)
        self.auto_completeness = auto_completeness
        self.manifest_path = Path(manifest) if manifest else (
            ENRICH_MANIFEST if self.research_dir.resolve() == DR_OUT_DIR.resolve() else None
        )
        self.prune = prune
        del people_csv, verdicts_jsonl, index_json, facts_dir

    def run(self) -> Payload:
        return self.execute()

    def execute(self) -> Payload:
        started = time.monotonic()
        counts = {key: 0 for key in (
            "built", "auto_approved", "pending_review", "preserved_user_rows",
            "skipped_with_linkedin", "skipped_unusable", "skipped_worth_no",
            "pruned_stale_machine_rows", "collapsed_merged_parents",
        )}
        queue, queue_current = _queue(self.queue_csv), self.queue_csv.is_file()
        canonical, identity = canonical_snapshot(self.db), identity_snapshot(self.db)
        parents = {row.parent_id: row for row in canonical.parents}
        people = {row.person_id: row.parent_id for row in canonical.people}
        parent_people: dict[str, list[str]] = {}
        for person_id, parent_id in people.items():
            parent_people.setdefault(parent_id, []).append(person_id)
        by_slug = {
            (row.display_slug or row.public_identifier).lower(): row.parent_id
            for row in canonical.parents
        }
        identifiers: dict[str, dict[str, str]] = {}
        for item in canonical.identifiers:
            identifiers.setdefault(item.person_id, {})[item.kind] = (
                item.display_value or item.normalized_value
            )
        links = {row.row_key: row for row in identity.links}
        existing: dict[str, dict[str, str]] = {}
        parent_pubs: dict[str, set[str]] = {}
        for stored in identity.synthetic_profiles:
            link = links.get(stored.candidate_key)
            if not link:
                continue
            try:
                row = json.loads(stored.profile_json or "{}")
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            approved = link.decision_approved or link.machine_approved or row.get("approved") or ""
            if link.decision_action in {"detach", "exclude"} and link.decision_approved:
                approved = "no"
            row["approved"] = approved
            existing[stored.public_identifier] = {key: str(value or "") for key, value in row.items()}
            parent_pubs.setdefault(link.parent_id, set()).add(stored.public_identifier)
        if queue_current and self.prune:
            for pub, row in list(existing.items()):
                if row.get("source_parent_slug") and row.get("approved", "").lower() not in USER_APPROVED:
                    existing.pop(pub)
                    counts["pruned_stale_machine_rows"] += 1

        worth = {row["parent_id"]: row["effective"] for row in views.worth_review(self.db, "rows")}
        groups: dict[str, list[tuple[dict[str, Any], ResearchContact, list[str]]]] = {}
        dirs = sorted(self.research_dir.iterdir()) if self.research_dir.is_dir() else []
        for directory in dirs:
            if queue_current and directory.name not in queue:
                continue
            path = directory / "01_research_parallel.json"
            research = ResearchResult.load(path)
            if research is None:
                continue
            row = queue.get(directory.name, {})
            proposed = links.get(
                str(row.get("source_candidate_public_identifier") or "").strip().lower()
            )
            linkedin = research.linkedin_url
            rejected = str((proposed.machine_reject if proposed else "") or "").lower()
            if linkedin and rejected not in {"1", "true", "yes"}:
                counts["skipped_with_linkedin"] += 1
                continue
            if not research.usable:
                counts["skipped_unusable"] += 1
                continue
            profile = research.to_payload(without_linkedin=bool(linkedin))
            person_ids = _json_list(row.get("source_person_ids", ""))
            parent_id = next((people[pid] for pid in person_ids if pid in people), "")
            parent_id = parent_id or by_slug.get(
                str(row.get("source_parent_slug") or directory.name).lower(), ""
            )
            if not parent_id:
                counts["skipped_unusable"] += 1
                continue
            person_ids = person_ids or parent_people.get(parent_id, [])
            anchor = next((identifiers.get(pid, {}) for pid in person_ids if identifiers.get(pid)), {})
            contact = ResearchContact.from_row(directory.name, {
                **row,
                "display_name": row.get("display_name") or parents[parent_id].display_name or "",
                "primary_email": row.get("primary_email") or anchor.get("email") or "",
                "phone_e164": row.get("phone_e164") or anchor.get("phone") or "",
                "source_parent_slug": parents[parent_id].display_slug or directory.name,
                "source_person_ids": json.dumps(person_ids),
            })
            groups.setdefault(parent_id, []).append((profile, contact, person_ids))

        projections: list[tuple[str, str, list[str], dict[str, str]]] = []
        for parent_id, items in sorted(groups.items()):
            if len(items) > 1:
                counts["collapsed_merged_parents"] += 1
            if worth.get(parent_id) == "no" and all(pid.startswith("candidate:") for pid in items[0][2]):
                counts["skipped_worth_no"] += 1
                continue
            profile = merge_research_profiles([item[0] for item in items])
            primary = next((item[1] for item in items
                            if item[1].primary_email or item[1].phone_e164), items[0][1])
            person_ids = list(dict.fromkeys(pid for item in items for pid in item[2]))
            contact = replace(primary, handle=parents[parent_id].display_slug or primary.handle)
            row = build_synthetic_row(profile, contact, None, person_ids[0] if person_ids else "",
                                      self.auto_completeness, {
                "source_parent_slug": contact.handle,
                "source_person_ids": json.dumps(person_ids),
                "source_candidate_public_identifier": contact.source_candidate_public_identifier,
            })
            pub = row["public_identifier"].lower()
            collisions = parent_pubs.get(parent_id, set()) | {pub}
            decision = _human_decision(existing, collisions)
            previous = existing.get(pub)
            if previous and previous.get("approved", "").lower() in USER_APPROVED:
                row = previous
                counts["preserved_user_rows"] += 1
            else:
                if decision:
                    row["approved"] = decision
                counts["built"] += 1
                counts["auto_approved" if row["approved"] == "auto" else "pending_review"] += 1
            for old_pub in collisions - {pub}:
                existing.pop(old_pub, None)
            existing[pub] = row
            projections.append((pub, parent_id, person_ids, row))

        _write_csv(self.out, existing)
        result = Payload(status="completed", primitive="assemble_synthetic_profile",
                         **counts, total_rows=len(existing), out=str(self.out),
                         elapsed_ms=int((time.monotonic() - started) * 1000))
        if self.manifest_path:
            self._project(result, projections)
        return result

    def _project(self, result: Payload,
                 rows: list[tuple[str, str, list[str], dict[str, str]]]) -> None:
        path = self.manifest_path
        assert path is not None
        receipt_writer = EnrichmentReceipt(path, self.db)
        current = receipt_writer.read() or {}
        synthetic_dir = path.parent / "synthetic"
        synthetic_dir.mkdir(parents=True, exist_ok=True)
        artifacts = [item for item in current.get("artifacts") or []
                     if not str(item.get("artifact_key") or "").startswith("synthetic:")]
        for pub, parent_id, person_ids, row in rows:
            artifact_path = synthetic_dir / f"{hashlib.sha1(pub.encode()).hexdigest()}.json"
            data = json.dumps(row, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"
            artifact_path.write_bytes(data)
            artifacts.append({
                "artifact_key": f"synthetic:{pub}", "kind": "synthetic",
                "path": artifact_path.relative_to(path.parent).as_posix(),
                "sha256": hashlib.sha256(data).hexdigest(), "parent_id": parent_id,
                "candidate_key": pub, "public_identifier": pub, "person_ids": person_ids,
                "display_name": row.get("full_name") or "", "approved": row.get("approved") or "",
            })
        receipt_writer.write({
            **current,
            "stage": "enrich",
            "status": "research_complete",
            "phase": "profiles_pending",
            "assembly": result.to_payload(),
            "outputs": {
                **(current.get("outputs") or {}),
                "synthetic_people_csv": str(self.out),
            },
            "artifacts": artifacts,
        })


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research-dir", default=str(DR_OUT_DIR))
    parser.add_argument("--queue-csv", default=str(QUEUE_CSV))
    parser.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    parser.add_argument("--verdicts-jsonl", default=str(VERDICTS_JSONL))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--index-json", default=str(INDEX_JSON))
    parser.add_argument("--facts-dir", default=str(FACTS_DIR))
    parser.add_argument("--db", default=str(CANONICAL_DB))
    parser.add_argument("--auto-completeness", type=float, default=DEFAULT_AUTO_COMPLETENESS)
    parser.add_argument("--manifest")
    args = parser.parse_args(argv)
    payload = AssembleSyntheticProfile(
        db=Db(Path(args.db)), research_dir=Path(args.research_dir),
        queue_csv=Path(args.queue_csv), people_csv=Path(args.people_csv),
        verdicts_jsonl=Path(args.verdicts_jsonl), out=Path(args.out),
        index_json=Path(args.index_json), facts_dir=Path(args.facts_dir),
        auto_completeness=args.auto_completeness, manifest=args.manifest,
    ).run()
    print(json.dumps(payload.to_payload(), indent=2))


if __name__ == "__main__":
    main()
