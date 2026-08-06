"""[Phase 3, retarget] Re-attach the CORRECT LinkedIn to people whose link was detached.

The decisions table (review.csv) can carry `retarget` rows: the wrong link is
detached AND a `new_linkedin_url` is the correct person. people.csv is LinkedIn-only and
requires a RapidAPI profile, so re-attaching means ENRICHING the new link and producing a
valid people-schema row. This step does exactly that for every approved retarget:

  1. Enrich `new_linkedin_url` cache-first (profile_cache_v2; RapidAPI only on a miss — auto).
  2. Build a people row (valid rapidapi_response + work_experiences/education) and CARRY the
     original contact's emails/phones/interaction_counts so the merge keeps the person whole.
  3. Write all rows to overrides/retarget-people.csv.  At realization,
     persist-review-identities writes their approved contact mappings to directory.csv
     before fan-in merges the sources.

Only rows with action=retarget AND approved ∈ {auto, yes} are applied (a user `no`/pending
retarget is skipped). Enrichment is automatic (RapidAPI is cache-first + effectively free).

Changelog:
  2026-07-27 (declared contract): `ApplyRetargets` is a `pipeline/contract.py:Node`.
    It DECLARES the review decisions, merged people.csv, and profile cache it reads
    and `overrides/retarget-people.csv` (row model `PeopleRow`, the header it has
    always written) as its one output, instead of only opening them. `run(args)`
    became `execute()`; `manifest=""` because this stage writes no manifest file
    today and none was invented. Same flags, same payload keys, same enrichment
    gate (cache-first RapidAPI on a miss, exactly as before).
  2026-07-23 (audit dedup): now_iso import from common.jsonio instead of deep_context.common (deduped there); no behavior change.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.candidates import (
    candidate_carry,
    candidate_key_of,
    candidate_row,
)
from packs.ingestion.primitives.deep_context.common import (
    DEFAULT_PEOPLE_CSV,
    emit,
    ensure_no_review_session,
    LINKEDIN_OVERRIDES_CSV,
    load_env,
    PROFILE_CACHE_DIR,
    PROFILE_CACHE_TEMPLATE,
    RETARGET_PEOPLE_CSV,
    ROOT,
)
from packs.ingestion.primitives.deep_context.db import batons
from packs.ingestion.primitives.deep_context.db.models import (
    ApprovedState,
    IdentityMachineProjection,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.enrich.profile_transforms import (
    merge_provider_profile,
    normalize_rapidapi,
)
from packs.ingestion.primitives.pipeline.contract import Artifact, Node, PeopleRow, StageManifest
from packs.ingestion.primitives.enrich.rapidapi_client import rapidapi_profile
from packs.ingestion.schemas.people_schema import (
    PEOPLE_SCHEMA_COLUMNS,
    extract_public_identifier,
    normalize_linkedin_url,
)

# Contact identity carried from the original (detached) person onto the re-attached row,
# so the merge groups the re-enriched person with their real messages/contacts.
CARRY_COLUMNS = ["primary_email", "all_emails", "primary_phone", "all_phones",
                 "interaction_counts", "last_interaction", "source_channels"]
USER_APPROVED = {ApprovedState.YES.value, ApprovedState.NO.value}
APPLY_APPROVED = {ApprovedState.AUTO.value, ApprovedState.YES.value}
CANONICAL_DB = ROOT / "deep-context.sqlite"


def judge_accepted_candidate_retarget(row: dict[str, Any]) -> bool:
    """Candidate-origin accepted match; a human yes/no remains terminal."""
    return (
        str(row.get("action") or "").strip().lower() == "retarget"
        and str(row.get("approved") or "").strip().lower() not in USER_APPROVED
        and str(row.get("person_id") or "").strip().lower().startswith("candidate:")
        and str(row.get("llm_reject") or "").strip().lower() not in {"yes", "true", "1", "spam"}
    )


def _projection_with_approval(db_row: Any, approved: str) -> IdentityMachineProjection:
    """Preserve the machine projection and update only its realization marker."""
    return IdentityMachineProjection(
        row_key=db_row["row_key"], machine_action=db_row["machine_action"],
        machine_approved=approved, machine_confidence=db_row["machine_confidence"],
        machine_reason=db_row["machine_reason"], machine_judgment=db_row["machine_judgment"],
        machine_reject=db_row["machine_reject"],
        machine_reject_confidence=db_row["machine_reject_confidence"],
        machine_reject_reason=db_row["machine_reject_reason"],
        machine_proposed_url=db_row["machine_proposed_url"],
        machine_proposed_public_identifier=db_row["machine_proposed_public_identifier"],
        authoritative_detach=db_row["authoritative_detach"], paid_profile=db_row["paid_profile"],
        judgment_fingerprint=db_row["judgment_fingerprint"],
        judgment_artifact_path=db_row["judgment_artifact_path"],
        judgment_payload_json=db_row["judgment_payload_json"], source=db_row["source"],
        updated_at=now_iso(),
    )


def load_people_index(people_csv: Path) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """(by public_identifier, by id) for looking up the original contact's metadata."""
    by_pub: dict[str, dict[str, str]] = {}
    by_id: dict[str, dict[str, str]] = {}
    if not people_csv.exists():
        return by_pub, by_id
    with people_csv.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            pub = (row.get("public_identifier") or "").strip().lower()
            if pub:
                by_pub[pub] = row
            pid = (row.get("id") or "").strip()
            if pid:
                by_id[pid] = row
    return by_pub, by_id


def enrich_one(new_url: str, new_pub: str, cache_dir: Path) -> dict[str, Any]:
    """One `get_profile` call for one LinkedIn URL -> {raw, from_cache, error}.

    The bar here is "a profile exists" (normalized success), not the judge's
    decidable-content bar — an approved retarget to a thin-but-real profile
    still enriches."""
    result = rapidapi_profile(new_pub, new_url, cache_dir=cache_dir)
    normalized = result.get("normalized_profile") or {}
    if normalized.get("success") is not True:
        return {"raw": None, "from_cache": result.get("from_cache", False),
                "error": result.get("detail") or "enrichment failed / no profile"}
    return {"raw": result.get("data"), "from_cache": result.get("from_cache", False), "error": ""}


def build_retarget_row(new_url: str, new_pub: str, raw: dict[str, Any],
                       original: dict[str, str]) -> dict[str, str]:
    """Enriched people row for the correct LinkedIn, carrying the contact's identity."""
    rapid = normalize_rapidapi(raw, new_pub, new_url)
    row = merge_provider_profile({}, rapid, raw)  # valid rapidapi_response + profile columns
    for col in CARRY_COLUMNS:
        if original.get(col):
            row[col] = original[col]
    out = {col: "" for col in PEOPLE_SCHEMA_COLUMNS}
    for col in PEOPLE_SCHEMA_COLUMNS:
        if row.get(col) not in (None, ""):
            out[col] = row[col]
    out["public_identifier"] = new_pub
    out["linkedin_url"] = new_url
    return out


class ApplyRetargetsManifest(StageManifest):
    """The stage's typed payload — the raw dict's keys verbatim, including
    ``updated_at``: this stage writes no manifest file, so nothing downstream
    would stamp it (the Node manifest writer is what stamps it elsewhere)."""
    source: str = "apply_retargets"
    approved_retargets: int = 0
    enriched: int = 0
    cache_hits: int = 0
    rapidapi_misses: int = 0
    skipped: int = 0
    finalized_applied: int = 0
    stranded_count: int = 0
    stranded: list[dict[str, str]] = []
    retarget_people_csv: str = ""
    rows: int = 0
    details: list[dict[str, Any]] = []
    elapsed_ms: int = 0
    updated_at: str = ""


class ApplyRetargets(Node):
    """Enriches every approved retarget's NEW LinkedIn and writes the re-attach
    people rows. Cache-first: RapidAPI is called only on a profile-cache miss."""

    name = "deep_apply_retargets"
    # All optional: an absent review table or people.csv simply yields no
    # appliable retargets (the pre-review pipeline state, not an error), and an
    # absent profile-cache entry is the miss this stage hydrates.
    # NOT declared as outputs: the SQLite machine projection records realized
    # retargets, while a profile
    # cache miss hydrates the EXTERNAL RapidAPI cache in place
    # (`rapidapi_profile`), which no single node owns.
    inputs = (
        Artifact(path=str(LINKEDIN_OVERRIDES_CSV), required=False),
        Artifact(path=str(DEFAULT_PEOPLE_CSV), required=False),
        Artifact(path=PROFILE_CACHE_TEMPLATE, external=True, required=False),
    )
    # Always written, even when zero retargets apply (header-only), so the
    # declaration is `required` and the header is the full people schema.
    outputs = (
        Artifact(path=str(RETARGET_PEOPLE_CSV), row_model=PeopleRow, writes="full_rewrite"),
    )
    payload = ApplyRetargetsManifest
    # Declaration-only node: no manifest file today, and none invented — the
    # payload is emitted by the CLI and the durable output is the CSV.
    manifest = ""

    def __init__(
        self,
        *,
        db: Db,
        overrides_csv: Path | None = None,
        people_csv: Path | None = None,
        profile_cache_dir: Path | None = None,
        out_csv: Path | None = None,
    ) -> None:
        self.db = db
        self.overrides_csv = Path(overrides_csv or LINKEDIN_OVERRIDES_CSV)
        self.people_csv = Path(people_csv or DEFAULT_PEOPLE_CSV)
        self.profile_cache_dir = Path(profile_cache_dir or PROFILE_CACHE_DIR)
        self.out_csv = Path(out_csv or RETARGET_PEOPLE_CSV)

    def bindings(self) -> dict[str, str]:
        return {
            str(LINKEDIN_OVERRIDES_CSV): str(self.overrides_csv),
            str(DEFAULT_PEOPLE_CSV): str(self.people_csv),
            PROFILE_CACHE_TEMPLATE: str(self.profile_cache_dir / "{public_identifier}.json"),
            str(RETARGET_PEOPLE_CSV): str(self.out_csv),
        }

    def execute(self) -> ApplyRetargetsManifest:
        started = time.monotonic()
        # This CSV is an explicit downstream baton, not the runtime record.
        self.db.export_batons(self.overrides_csv)
        overrides = batons.load_override_rows(self.overrides_csv)
        by_pub, by_id = load_people_index(self.people_csv)

        # Marker lifecycle: retarget-people.csv is overwritten each run and the
        # realization persistence stage consumes it, so nothing used to close out the SOURCE row —
        # applied retargets kept reading as "pending" forever (and re-enriched on
        # later runs), while proposals whose old identity left the review model
        # became invisible limbo. Reconcile both here, before selecting work:
        #  - a row whose new pub already lives in people.csv is REALIZED: stamp
        #    approved=yes (recording what the merge already did) and skip it;
        #  - a still-pending proposal that resolves to NO current identity (old pub,
        #    person id, and candidate row all gone) and is not realized is STRANDED:
        #    reported so the agent can re-propose or drop it, never silently lost.
        all_markers = [r for r in overrides.values()
                       if (r.get("action") or "").strip().lower() == "retarget"]
        finalized = 0
        stranded: list[dict[str, str]] = []
        realized_pubs: set[str] = set()
        for r in all_markers:
            new_url = normalize_linkedin_url(r.get("new_linkedin_url") or "")
            new_pub = (r.get("new_public_identifier") or "").strip().lower() or \
                extract_public_identifier(new_url).lower()
            old_pub = (r.get("public_identifier") or "").strip().lower()
            approved = (r.get("approved") or "").strip().lower()
            if new_pub and new_pub in by_pub:
                realized_pubs.add(old_pub)
                if approved not in ("yes", "no"):
                    r["approved"] = "yes"
                    finalized += 1
                continue
            if approved:
                continue
            pid = (r.get("person_id") or "").strip()
            resolvable = bool(
                by_pub.get(old_pub) or by_id.get(pid)
                or candidate_row(candidate_key_of(pid) or candidate_key_of(old_pub)))
            if not resolvable:
                stranded.append({"old": old_pub, "new": new_pub})
        if finalized:
            realized_keys = {
                key for key, row in overrides.items()
                if (row.get("public_identifier") or "").strip().lower() in realized_pubs
            }
            current = self.db.query(
                "SELECT * FROM links WHERE row_key IN ("
                + ",".join("?" for _ in realized_keys) + ")",
                tuple(realized_keys),
            ) if realized_keys else []
            self.db.project_identity(tuple(
                _projection_with_approval(row, ApprovedState.YES.value) for row in current
            ))

        # Appliable: humanly/auto approved, plus judge-accepted candidate-origin
        # found profiles whose accepted machine verdict stands.
        # Real-network retargets still require the human/auto approval.
        retargets = [r for r in all_markers
                     if (r.get("public_identifier") or "").strip().lower() not in realized_pubs
                     and ((r.get("approved") or "").strip().lower() in APPLY_APPROVED
                          or judge_accepted_candidate_retarget(r))]

        if retargets:
            load_env()
        rows: list[dict[str, str]] = []
        enriched = cache_hits = misses = skipped = 0
        details: list[dict[str, Any]] = []
        for r in retargets:
            new_url = normalize_linkedin_url(r.get("new_linkedin_url") or "")
            new_pub = (r.get("new_public_identifier") or "").strip().lower() or extract_public_identifier(new_url).lower()
            old_pub = (r.get("public_identifier") or "").strip().lower()
            if not new_url or not new_pub:
                skipped += 1
                details.append({"old": old_pub, "status": "skipped", "reason": "no new_linkedin_url"})
                continue
            result = enrich_one(new_url, new_pub, self.profile_cache_dir)
            if result["error"]:
                skipped += 1
                details.append({"old": old_pub, "new": new_pub, "status": "skipped", "reason": result["error"]})
                continue
            enriched += 1
            cache_hits += bool(result["from_cache"])
            misses += not result["from_cache"]
            pid = (r.get("person_id") or "").strip()
            original = by_pub.get(old_pub) or by_id.get(pid) or {}
            if not original:
                # candidate:<key> parent -> contact identity lives in candidates.csv, not people.csv
                crow = candidate_row(candidate_key_of(pid) or candidate_key_of(old_pub))
                if crow:
                    original = candidate_carry(crow)
            rows.append(build_retarget_row(new_url, new_pub, result["raw"], original))
            details.append({"old": old_pub, "new": new_pub, "status": "enriched",
                            "from_cache": result["from_cache"]})

        out_path = self.out_csv
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=PEOPLE_SCHEMA_COLUMNS)
            w.writeheader()
            w.writerows(rows)

        return ApplyRetargetsManifest(
            status="completed",
            approved_retargets=len(retargets), enriched=enriched,
            cache_hits=cache_hits, rapidapi_misses=misses, skipped=skipped,
            finalized_applied=finalized,
            stranded_count=len(stranded), stranded=stranded[:25],
            retarget_people_csv=str(out_path), rows=len(rows),
            details=details[:50],
            elapsed_ms=int((time.monotonic() - started) * 1000), updated_at=now_iso(),
        )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Enrich + build re-attach rows for approved retargets.")
    p.add_argument("--overrides-csv", default=str(LINKEDIN_OVERRIDES_CSV))
    p.add_argument("--db", default=str(CANONICAL_DB))
    p.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    p.add_argument("--profile-cache-dir", default=str(PROFILE_CACHE_DIR))
    p.add_argument("--out-csv", default=str(RETARGET_PEOPLE_CSV))
    return p


def main(argv: list[str] | None = None) -> int:
    ensure_no_review_session("apply_retargets")
    args = build_parser().parse_args(argv)
    payload = ApplyRetargets(
        db=Db(Path(args.db)),
        overrides_csv=Path(args.overrides_csv),
        people_csv=Path(args.people_csv),
        profile_cache_dir=Path(args.profile_cache_dir),
        out_csv=Path(args.out_csv),
    ).run()
    emit(payload.to_payload())
    return 0


if __name__ == "__main__":
    sys.exit(main())
